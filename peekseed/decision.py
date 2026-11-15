"""Decision-provider adapter: typed judgments for M3 auto-review (CP0.5).

A *decision link* (role ``decision``, protocol ``typesafe``) points at a
provider-independent adjudication endpoint that answers small, typed questions
(choice/noul) over a state instead of generating prose. Today that means the
TypeSafe System One API (``POST {base_url}/v1/systemone``); other providers add
their own protocol adapter later (CP0.5 setup-contract §3).

This module owns three things, and nothing else:

* the **envelope** -- request construction (model/state/questions/assets),
  response validation (typed answers, distributions, model identity, usage);
* the **synthetic probe** -- one fixed text request (and an opt-in image
  request) used by ``POST /api/links/{id}/test``. It validates the wire shape
  and records the resolved model; it is NOT a quality or calibration proof;
* the **transport** -- one HTTP POST. The API key travels only in the
  ``Authorization`` header; provider bodies are never echoed into errors.

Secrets never reach argv, logs, the link store, a dataset/job record or the
UI. GET/list/status/doctor never call the provider; only the explicit Test
action does (setup-contract §1 invariant 3).

The module is deliberately free of web-framework and storage imports so the
envelope can be reused from tests and the peelfeed-facing seam.
"""

import base64
import re
import math
import struct
import time
import zlib
from urllib.parse import urljoin

__all__ = [
    "DEFAULT_TIMEOUT",
    "DECISION_BAD_RESPONSE",
    "DECISION_RATE_LIMITED",
    "DECISION_TIMEOUT",
    "DECISION_UNAVAILABLE",
    "DecisionError",
    "ERROR_IMAGE_SIDE",
    "MODELS_PATH",
    "MOVING_ALIASES",
    "PROBE_VERSION",
    "SYSTEMONE_PATH",
    "build_request",
    "image_probe_fixture",
    "invoke",
    "probe",
    "text_probe_fixture",
    "validate_response",
]

#: Wire constants (setup-contract §3). ``base_url`` is the API root; requests
#: hang off these paths (trailing slash of the base is normalized by links).
SYSTEMONE_PATH = "/v1/systemone"
MODELS_PATH = "/v1/models"

#: System One model aliases that move between releases; links should pin a
#: versioned id (kept in sync with ``links.DECISION_MOVING_ALIASES``).
MOVING_ALIASES = ("jev-latest", "jev-preview")

#: Synthetic probe fixture version; bump when the probe request changes so a
#: passed/failed result is attributable to a frozen fixture.
PROBE_VERSION = 1

#: Probe timeout; decision calls are small typed judgments, and a hung
#: provider must not stall the settings page.
DEFAULT_TIMEOUT = 60.0

#: Frozen error codes (setup-contract §3); ``detail`` strings are sanitized.
DECISION_UNAVAILABLE = "decision_unavailable"
DECISION_TIMEOUT = "decision_timeout"
DECISION_BAD_RESPONSE = "decision_bad_response"
DECISION_RATE_LIMITED = "decision_rate_limited"

#: Provider image limits (TypeSafe docs, 2026-11-14; rejected, never resized).
IMAGE_SIDE_MIN = 280
IMAGE_SIDE_MAX = 1568
#: The synthetic image probe uses the minimum valid side length.
ERROR_IMAGE_SIDE = 280

#: Distribution sums are approximate on the provider side (OpenAPI: "values
#: sum to approximately 1"); anything further off is a bad response.
_SUM_TOLERANCE = 0.02

TEXT_PROBE_STATE = ("peekseed decision probe v1: the quick brown fox jumps "
                    "over the lazy dog.")
TEXT_PROBE_QUESTIONS = {
    "probe_mentions_dog": {
        "type": "noul",
        "instructions": "Does the `state` text mention a dog?",
    },
    "probe_animal": {
        "type": "choice",
        "instructions": "Which animal is named in the `state` text?",
        "criteria": {
            "fox": "The text names a fox.",
            "dog": "The text names a dog.",
            "cat": "The text names a cat.",
        },
    },
}


class DecisionError(Exception):
    """A typed decision-provider failure.

    ``code`` is one of the frozen codes above; ``detail`` is a sanitized,
    user-facing string that never contains the response body or the key.
    """

    def __init__(self, code, detail):
        self.code = str(code or DECISION_UNAVAILABLE)
        self.detail = str(detail or "")
        super().__init__("%s: %s" % (self.code, self.detail))


def text_probe_fixture():
    """The frozen synthetic text probe (setup-contract §3)."""
    return {"state": TEXT_PROBE_STATE, "questions": dict(TEXT_PROBE_QUESTIONS)}


# --------------------------------------------------------------------- images
def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png_from_rows(width, height, rows):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel) for pixel in row)
                   for row in rows)
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
            + _png_chunk(b"IDAT", zlib.compress(raw, 9))
            + _png_chunk(b"IEND", b""))


#: 5x7 block glyphs for the probe image (dependency-free, like the engine's
#: VLM probe). Only the letters the fixture needs are defined.
_PROBE_GLYPHS = {
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
}

IMAGE_PROBE_REFERENCE = "a1b2c3-probe"
IMAGE_PROBE_QUESTION = {
    "probe_reads_text": {
        "type": "noul",
        "instructions": ("Does the image referenced by `probe_image` contain "
                         "large block letters?"),
    },
}


def probe_png_bytes(side=ERROR_IMAGE_SIDE):
    """The synthetic image-probe PNG: white, ``PS`` block letters, in-bounds.

    Side length defaults to the provider minimum (280 px) so the image is
    valid without resizing and stays tiny; the probe only checks the wire
    shape, not OCR quality.
    """
    side = int(side)
    if side < IMAGE_SIDE_MIN:
        side = IMAGE_SIDE_MIN
    scale = max(4, side // 40)
    gap = scale
    margin = (side - (sum(len(g[0]) for g in _PROBE_GLYPHS.values()) * scale
                      + gap)) // 2
    white, black = (255, 255, 255), (0, 0, 0)
    rows = [[white] * side for _ in range(side)]
    x = max(2, margin)
    for letter in ("P", "S"):
        glyph = _PROBE_GLYPHS[letter]
        for r, bits in enumerate(glyph):
            for c, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        py = max(2, margin) + r * scale + dy
                        px = x + c * scale + dx
                        if 0 <= py < side and 0 <= px < side:
                            rows[py][px] = black
        x += len(glyph[0]) * scale + gap
    return _png_from_rows(side, side, rows)


def image_probe_fixture():
    """The frozen synthetic image probe (opt-in; danger-zone header set)."""
    data = base64.b64encode(probe_png_bytes()).decode("ascii")
    return {
        "state": {"probe_image": IMAGE_PROBE_REFERENCE},
        "questions": dict(IMAGE_PROBE_QUESTION),
        "assets": {IMAGE_PROBE_REFERENCE: {"type": "image", "format": "png",
                                           "data_base64": data}},
    }


# ------------------------------------------------------------------- envelope
def build_request(model, fixture):
    """One System One request body from a fixture (model pinned by the link)."""
    if not str(model or "").strip():
        raise DecisionError(DECISION_UNAVAILABLE, "the link has no model set")
    body = {"model": str(model).strip(),
            "state": fixture.get("state"),
            "questions": fixture.get("questions")}
    if fixture.get("assets"):
        body["assets"] = fixture["assets"]
    return body


def _finite01(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0)


def _validate_choice_answer(question, answer):
    criteria = question.get("criteria")
    options = set(criteria) if isinstance(criteria, dict) else set()
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in options:
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "choice answer selected an unknown option")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != options:
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "choice probabilities do not match the options")
    if not all(_finite01(v) for v in probabilities.values()):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "choice probabilities must be numbers in [0, 1]")
    total = sum(float(v) for v in probabilities.values())
    if abs(total - 1.0) > _SUM_TOLERANCE:
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "choice probabilities do not sum to 1")
    if not _finite01(answer.get("confidence")):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "choice answer has no confidence in [0, 1]")


#: CP3 addendum B13 (CP5): exact answer key sets and image-echo rejection.
_ANSWER_KEYS = {
    "noul": frozenset(("type", "noul")),
    "choice": frozenset(("type", "choice", "confidence", "probabilities")),
}
_IMAGE_ECHO_RE = re.compile(
    r"(?i)(data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,|;base64,|data:image/)")
_MAX_TEXT_FIELD = 8192


def _reject_image_echo(value, where):
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_FIELD or _IMAGE_ECHO_RE.search(value):
            raise DecisionError(
                DECISION_BAD_RESPONSE,
                "%s contains image data or an oversized string" % where)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_image_echo(key, where)
            _reject_image_echo(item, where)
        return
    if isinstance(value, list):
        for item in value:
            _reject_image_echo(item, where)


def _validate_noul_answer(answer):
    if not _finite01(answer.get("noul")):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "noul answer must be a probability in [0, 1]")


def validate_response(payload, questions):
    """Validate one System One response against its request (contract §3).

    Returns ``{"resolved_model", "usage"}``. Every violation raises
    :class:`DecisionError` with code ``decision_bad_response``; illegal output
    is never normalized into looking legal.
    """
    if not isinstance(payload, dict):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the response is not a JSON object")
    resolved = payload.get("model")
    if not isinstance(resolved, str) or not resolved.strip():
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the response has no model id")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the response has no answers object")
    expected = set(questions or {})
    if set(answers) != expected:
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the answers do not match the questions asked")
    for qid, question in (questions or {}).items():
        answer = answers.get(qid)
        if not isinstance(answer, dict):
            raise DecisionError(DECISION_BAD_RESPONSE,
                                "answer %r is not an object" % qid)
        qtype = (question or {}).get("type")
        if answer.get("type") != qtype:
            raise DecisionError(DECISION_BAD_RESPONSE,
                                "answer %r has the wrong type" % qid)
        allowed = _ANSWER_KEYS.get(qtype)
        if allowed is not None and set(answer) != set(allowed):
            raise DecisionError(
                DECISION_BAD_RESPONSE,
                "answer %r has unexpected keys (expected %s)"
                % (qid, ", ".join(sorted(allowed))))
        _reject_image_echo(answer, "answer %r" % qid)
        if qtype == "choice":
            _validate_choice_answer(question, answer)
        elif qtype == "noul":
            _validate_noul_answer(answer)
        # score/box exist in the API but the CP0.5 probe never asks them.
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the response has no usage object")
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 0):
            raise DecisionError(DECISION_BAD_RESPONSE,
                                "usage.%s must be a non-negative integer"
                                % key)
    return {"resolved_model": resolved.strip(),
            "usage": {"input_tokens": int(usage["input_tokens"]),
                      "output_tokens": int(usage["output_tokens"])}}


# ------------------------------------------------------------------- transport
def invoke(base_url, api_key, model, fixture, timeout=None):
    """One synthetic probe call; returns the normalized result or raises.

    ``api_key`` may be "" (a local unauthenticated endpoint is allowed). The
    provider's response body is never put into an error string.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise DecisionError(DECISION_UNAVAILABLE, "the link has no base URL")
    body = build_request(model, fixture)
    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    if body.get("assets"):
        # Experimental images are a per-request opt-in (danger-zone header,
        # setup-contract §3); the fixture already validated the size bounds.
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    url = urljoin(base + "/", SYSTEMONE_PATH.lstrip("/"))
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships in the workspace venv
        raise DecisionError(DECISION_UNAVAILABLE,
                            "httpx is not installed") from None
    started = time.monotonic()
    try:
        response = httpx.post(url, json=body, headers=headers,
                              timeout=float(timeout or DEFAULT_TIMEOUT))
    except httpx.TimeoutException:
        raise DecisionError(DECISION_TIMEOUT,
                            "the decision endpoint timed out") from None
    except Exception as exc:  # noqa: BLE001 - transport detail is not shown
        raise DecisionError(DECISION_UNAVAILABLE,
                            "the decision endpoint is not reachable (%s)"
                            % type(exc).__name__) from None
    latency_ms = int((time.monotonic() - started) * 1000)
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 429:
        raise DecisionError(DECISION_RATE_LIMITED,
                            "the decision endpoint returned HTTP 429")
    if status >= 400:
        # Never echo the body: it can carry account or provider details.
        raise DecisionError(DECISION_UNAVAILABLE,
                            "the decision endpoint returned HTTP %d" % status)
    try:
        payload = response.json()
    except ValueError:
        raise DecisionError(DECISION_BAD_RESPONSE,
                            "the decision endpoint returned non-JSON") from None
    validated = validate_response(payload, body["questions"])
    return {"ok": True,
            "resolved_model": validated["resolved_model"],
            "model_match": validated["resolved_model"] == str(model).strip(),
            "usage": validated["usage"],
            "latency_ms": latency_ms,
            "images": bool(body.get("assets"))}


def probe(link, *, images=False, timeout=None, api_key=""):
    """The synthetic link test used by ``POST /api/links/{id}/test``.

    ``link`` is the stored decision link dict; ``api_key`` is resolved by the
    caller from ``api_key_env`` (never from this module). Returns the
    :func:`invoke` result plus probe provenance; raises :class:`DecisionError`.
    """
    fixture = image_probe_fixture() if images else text_probe_fixture()
    result = invoke(link.get("base_url"), api_key, link.get("model"), fixture,
                    timeout=timeout)
    result["probe_version"] = PROBE_VERSION
    return result
