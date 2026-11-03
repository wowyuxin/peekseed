"""peelfeed: the extraction engine's config store and CLI adapter.

peekseed does not extract documents. It stores uploads and consumes the corpus
that **peelfeed** (a separate repo/venv) writes per source file. Since the
2026-11-02 decision the seam is **CLI only**: peekseed invokes peelfeed commands
as subprocesses, reads JSON from stdout and never speaks HTTP. There is no base
URL, no token and no multipart.

This module is the whole seam:

* ``config/peelfeed.json`` (0600) is seeded once from
  ``PEEKSEED_PEELFEED_{CLI,PYTHON,REPO,ENABLED,TIMEOUT,POLL,LLM_MODE,
  CHUNK_PROFILE}``. Environment values win over the file, so a deploy can point
  at a different engine without rewriting the config. The peel root is always
  ``$PEELFEED_SHARED_DIR/peels`` (see :mod:`peekseed.config`).
* :class:`PeelfeedClient` runs ``capabilities``/``status``/``ingest``/``review``/
  ``decide``/``rebuild``/``event``/``archive`` with ``--root`` and
  ``--json``, a timeout, and typed errors (:class:`PeelfeedUnavailable` when the
  command cannot start / ``capabilities`` fails, :class:`PeelfeedError` on a
  failed command).
* :func:`handshake` never raises: when peelfeed is unconfigured or its CLI is
  missing the result is ``available=False`` with a short reason, so uploads/file
  management keep working and the UI disables Extract with a banner (design
  §4.5/§4.6, CLI-only override).

The config store is a pure config module (no web framework) so it can be tested
and used from the CLI; the subprocess call is the only external dependency.
"""

import copy
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from . import config, contracts

__all__ = [
    "DEFAULT_CHUNK_PROFILE",
    "DEFAULT_LLM_MODE",
    "DEFAULT_POLL",
    "DEFAULT_TIMEOUT",
    "HANDSHAKE_TTL",
    "LLM_MODES",
    "PeelfeedClient",
    "PeelfeedConfigError",
    "PeelfeedError",
    "PeelfeedUnavailable",
    "SHORT_TIMEOUT",
    "STATE_MAP",
    "client",
    "config_path",
    "configured",
    "effective",
    "extraction_available",
    "extraction_from_summary",
    "extraction_state_for_peel",
    "handshake",
    "invalidate_handshake",
    "load",
    "mask",
    "reset_cache",
    "seeded",
    "status",
]

VERSION = 1
ENV_PREFIX = "PEEKSEED_PEELFEED_"
#: Long operations (ingest/rebuild) may run for the whole Docling conversion.
DEFAULT_TIMEOUT = 7200.0
#: Short operations (capabilities/status/review/decide/event/archive) get a
#: smaller cap so a hung CLI cannot stall a request.
SHORT_TIMEOUT = 120.0
DEFAULT_POLL = 2.0
DEFAULT_LLM_MODE = "off"
DEFAULT_CHUNK_PROFILE = "default"
LLM_MODES = ("off", "review", "aggressive")
HANDSHAKE_TTL = 60.0
#: Only these variables (plus every PEELFEED_*) reach the peelfeed subprocess;
#: peekseed's token and API keys never do.
_PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
             "TMPDIR")
#: Peel state machine -> peekseed's per-source ``extraction.state``.
STATE_MAP = {
    "queued": "queued",
    "created": "running",
    "profiled": "running",
    "converting": "running",
    "converted": "running",
    "normalizing": "running",
    "normalized": "running",
    "gating": "running",
    "rebuilding": "running",
    "ready": "extracted",
    "needs_review": "extracted",
    "failed": "failed",
    "archived": "stale",
    "deleted": "stale",
}


class PeelfeedConfigError(Exception):
    """The peelfeed config file or environment values cannot be used."""


class PeelfeedError(Exception):
    """peelfeed exited nonzero (a typed status + a short, redacted detail)."""

    def __init__(self, status, detail):
        self.status = int(status or 502)
        self.detail = str(detail or "")
        super().__init__(self.detail)


class PeelfeedUnavailable(PeelfeedError):
    """peelfeed cannot be invoked at all (missing CLI, bad interpreter)."""

    def __init__(self, detail):
        super().__init__(503, detail)


_CACHE = None
_CACHE_KEY = None
_SEEDED = False
_HANDSHAKE = None
_LOCK = threading.RLock()


# --- config store ----------------------------------------------------------


def config_path(root=None):
    """The peelfeed config file for ``root`` (default: the current data root)."""
    if root is not None:
        return Path(root) / "config" / "peelfeed.json"
    return config.paths().config_dir / "peelfeed.json"


def seeded():
    """Whether THIS process created ``config/peelfeed.json`` from env."""
    return _SEEDED


def reset_cache():
    """Drop the file/handshake caches (tests point PEEKSEED_DATA elsewhere)."""
    global _CACHE, _CACHE_KEY, _SEEDED, _HANDSHAKE
    with _LOCK:
        _CACHE = None
        _CACHE_KEY = None
        _SEEDED = False
        _HANDSHAKE = None


def invalidate_handshake():
    """Force the next :func:`handshake` to run the CLI again."""
    global _HANDSHAKE
    with _LOCK:
        _HANDSHAKE = None


def _signature(path):
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _env(name):
    return (os.environ.get(ENV_PREFIX + name) or "").strip()


def _env_raw():
    """The raw config a first run seeds from environment values."""
    return {
        "version": VERSION,
        "cli": _env("CLI"),
        "python": _env("PYTHON"),
        "repo": _env("REPO"),
        "enabled": _env("ENABLED"),
        "timeout": _env("TIMEOUT"),
        "poll": _env("POLL"),
        "llm_mode": _env("LLM_MODE"),
        "chunk_profile": _env("CHUNK_PROFILE"),
    }


def _env_overrides():
    """Only the env values that are actually set (they win over the file)."""
    raw = _env_raw()
    return {key: value for key, value in raw.items()
            if key != "version" and str(value or "").strip() != ""}


def _float(raw, name, default):
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise PeelfeedConfigError("%s must be a number of seconds." % name) from None
    if value <= 0:
        raise PeelfeedConfigError("%s must be positive." % name)
    return value


def _absolute(raw, name):
    """Validate one optional absolute path (CLI/interpreter/repo)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise PeelfeedConfigError("%s must be an absolute path." % name)
    return str(path)


def _derive_repo(path):
    """The peelfeed repo root implied by a venv interpreter/console script.

    ``<repo>/.venv/bin/python`` and ``<repo>/.venv/bin/peelfeed`` both resolve to
    ``parents[2]``; a path that does not look like a repo checkout yields "".
    """
    if not path:
        return ""
    candidate = Path(path).expanduser()
    try:
        root = candidate.parents[2]
    except IndexError:
        return ""
    if (root / "peelfeed").is_dir():
        return str(root)
    return ""


def _normalize(raw):
    """Validate one peelfeed config document (file or env seed)."""
    if not isinstance(raw, dict):
        raise PeelfeedConfigError("the peelfeed config must be a JSON object.")
    version = raw.get("version", VERSION)
    if version != VERSION:
        raise PeelfeedConfigError(
            "the peelfeed config has unsupported version %r (this build reads "
            "version %d)." % (version, VERSION))
    cli = _absolute(raw.get("cli"), "PEEKSEED_PEELFEED_CLI")
    python = _absolute(raw.get("python"), "PEEKSEED_PEELFEED_PYTHON")
    repo = _absolute(raw.get("repo"), "PEEKSEED_PEELFEED_REPO")
    if not repo:
        repo = _derive_repo(python) or _derive_repo(cli)
    root = str(config.paths().peels_dir)
    enabled_raw = raw.get("enabled")
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        text = str(enabled_raw or "").strip().lower()
        if text:
            enabled = text not in ("0", "false", "no", "off")
        else:
            # Auto: a configured CLI/python path means extraction is available.
            enabled = bool(cli or python)
    llm_mode = str(raw.get("llm_mode") or DEFAULT_LLM_MODE).strip().lower()
    if llm_mode not in LLM_MODES:
        raise PeelfeedConfigError(
            "PEEKSEED_PEELFEED_LLM_MODE must be one of %s."
            % ", ".join(LLM_MODES))
    chunk_profile = str(raw.get("chunk_profile") or DEFAULT_CHUNK_PROFILE).strip()
    if not chunk_profile:
        raise PeelfeedConfigError(
            "PEEKSEED_PEELFEED_CHUNK_PROFILE must not be empty.")
    return {
        "version": VERSION,
        "cli": cli,
        "python": python,
        "repo": repo,
        "root": root,
        "enabled": bool(enabled),
        "timeout": _float(raw.get("timeout"), "PEEKSEED_PEELFEED_TIMEOUT",
                          DEFAULT_TIMEOUT),
        "poll": _float(raw.get("poll"), "PEEKSEED_PEELFEED_POLL", DEFAULT_POLL),
        "llm_mode": llm_mode,
        "chunk_profile": chunk_profile,
    }


def load(force=False, *, root=None, seed=True):
    """The effective peelfeed config: the file with env values layered on top.

    A missing file is seeded from the environment and written once with mode
    0600; with ``seed=False`` (read-only callers such as ``doctor``/``status``)
    nothing is written and the env-derived config is returned. ``root`` selects
    another data root than the current one. A corrupt file raises
    :class:`PeelfeedConfigError`; callers report it as "extraction unavailable"
    rather than crashing the service. Environment values win over the file, so
    a deploy can override the CLI path or root without rewriting it.
    """
    global _CACHE, _CACHE_KEY, _SEEDED
    path = config_path(root)
    overrides = _env_overrides()
    with _LOCK:
        try:
            file_key = _signature(path)
        except OSError:
            file_key = None
        key = (str(path), file_key, tuple(sorted(overrides.items())))
        if _CACHE is not None and not force and key == _CACHE_KEY:
            return copy.deepcopy(_CACHE)
        if not path.is_file():
            cfg = _normalize(_env_raw())
            if not seed:
                return cfg
            config.ensure()
            config.write_json_atomic(path, cfg, mode=0o600)
            _SEEDED = True
        else:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (OSError, ValueError) as exc:
                raise PeelfeedConfigError(
                    "cannot read peelfeed config %s: %s" % (path.name, exc)) from None
            if not isinstance(raw, dict):
                raise PeelfeedConfigError(
                    "the peelfeed config must be a JSON object.")
            merged = dict(raw)
            merged.update(overrides)
            cfg = _normalize(merged)
            _SEEDED = False
        _CACHE = cfg
        _CACHE_KEY = key
        return copy.deepcopy(cfg)


def effective():
    """A copy of the effective config."""
    return load()


def mask(cfg=None, *, root=None):
    """The config as the API/UI may see it (no secrets exist any more)."""
    cfg = load(root=root) if cfg is None else cfg
    path = config_path(root)
    return {
        "cli": cfg["cli"],
        "python": cfg["python"],
        "repo": cfg["repo"],
        "root": cfg["root"],
        "enabled": bool(cfg["enabled"]),
        "configured": bool(cfg["enabled"] and _command(cfg)),
        "timeout": cfg["timeout"],
        "poll": cfg["poll"],
        "llm_mode": cfg["llm_mode"],
        "chunk_profile": cfg["chunk_profile"],
        "path": str(path),
        "present": path.is_file(),
        "seeded": seeded(),
    }


def configured():
    """True when a CLI/interpreter is configured and extraction is enabled."""
    try:
        cfg = load()
    except PeelfeedConfigError:
        return False
    return bool(cfg["enabled"] and _command(cfg))


def _command(cfg):
    """The argv prefix for the configured peelfeed executable, or None.

    ``PEEKSEED_PEELFEED_CLI`` (the console script) is preferred; otherwise the
    venv interpreter runs ``-m peelfeed.cli`` with ``PYTHONPATH`` pointing at the
    repo root (the package is not installed in the engine venv).
    """
    if cfg.get("cli"):
        return (cfg["cli"],)
    if cfg.get("python"):
        return (cfg["python"], "-m", "peelfeed.cli")
    return None


def client(cfg=None, *, timeout=None):
    """A :class:`PeelfeedClient` for the effective config."""
    cfg = load() if cfg is None else cfg
    return PeelfeedClient(
        cfg, timeout=timeout if timeout is not None else cfg["timeout"])


# --- handshake -------------------------------------------------------------


def _handshake_key(cfg):
    return (_command(cfg), cfg["root"], cfg["enabled"], cfg["timeout"])


def handshake(force=False, timeout=None, *, cfg=None):
    """Run ``peelfeed capabilities``, cached for 60 seconds.

    Never raises: an unconfigured/unreachable engine returns
    ``{"available": False, "error": …}`` so uploads keep working and the UI can
    disable Extract with an honest banner. ``cfg`` skips :func:`load`.
    """
    global _HANDSHAKE
    try:
        cfg = load() if cfg is None else cfg
    except PeelfeedConfigError as exc:
        return {
            "available": False,
            "configured": False,
            "enabled": False,
            "error": str(exc),
            "checked_at": contracts.utc_now(),
        }
    command = _command(cfg)
    if command is None:
        return {
            "available": False,
            "configured": False,
            "enabled": bool(cfg["enabled"]),
            "cli": "",
            "python": "",
            "root": cfg["root"],
            "error": "peelfeed is not configured (set PEEKSEED_PEELFEED_CLI or "
                     "PEEKSEED_PEELFEED_PYTHON)",
            "checked_at": contracts.utc_now(),
        }
    if not cfg["enabled"]:
        return {
            "available": False,
            "configured": False,
            "enabled": False,
            "cli": cfg["cli"],
            "python": cfg["python"],
            "root": cfg["root"],
            "error": "peelfeed is disabled (PEEKSEED_PEELFEED_ENABLED=0)",
            "checked_at": contracts.utc_now(),
        }
    key = _handshake_key(cfg)
    now = time.monotonic()
    with _LOCK:
        if (not force and _HANDSHAKE is not None
                and _HANDSHAKE["key"] == key
                and (now - _HANDSHAKE["at"]) < HANDSHAKE_TTL):
            return copy.deepcopy(_HANDSHAKE["result"])
    result = {
        "available": False,
        "configured": True,
        "enabled": True,
        "cli": cfg["cli"],
        "python": cfg["python"],
        "repo": cfg["repo"],
        "root": cfg["root"],
        "error": None,
        "checked_at": contracts.utc_now(),
    }
    try:
        caps = client(cfg, timeout=timeout or min(SHORT_TIMEOUT, cfg["timeout"])
                      ).capabilities()
        result.update({
            "available": True,
            "engine": str(caps.get("engine") or ""),
            "version": str(caps.get("version") or ""),
            "cli_protocol": caps.get("cli_protocol"),
            "corpus_schema": caps.get("corpus_schema"),
            "chunk_contract": caps.get("chunk_contract"),
            "chunker_version": caps.get("chunker_version"),
            "peel_event_schema": caps.get("peel_event_schema"),
            "formats": list(caps.get("formats") or []),
            "profiles": list(caps.get("profiles") or []),
            "chunk_profiles": list(caps.get("chunk_profiles") or []),
            "llm_modes": list(caps.get("llm_modes") or []),
            "docling": caps.get("docling") or {},
            "peel_root": str(caps.get("peel_root") or ""),
        })
    except PeelfeedError as exc:
        result["error"] = exc.detail
    except Exception as exc:  # noqa: BLE001 - unavailable is a supported state
        result["error"] = "peelfeed CLI failed (%s)" % type(exc).__name__
    with _LOCK:
        _HANDSHAKE = {"key": key, "at": now, "result": copy.deepcopy(result)}
    return copy.deepcopy(result)


def extraction_available():
    """``enabled && handshake ok`` -- the gate for the Extract action."""
    try:
        cfg = load()
    except PeelfeedConfigError:
        return False
    return bool(cfg["enabled"] and _command(cfg) and handshake().get("available"))


def status(probe=False, timeout=None, *, root=None, seed=True):
    """The UI/CLI status: config + handshake/capabilities.

    ``probe=True`` forces a fresh ``capabilities`` call (read-only commands such
    as ``peekseed status``/``doctor``); a request path uses the 60 s cache.
    ``root``/``seed`` are passed to :func:`load` (read-only callers use
    ``seed=False`` so inspecting a workspace never writes into it).
    """
    try:
        cfg = load(root=root, seed=seed)
    except PeelfeedConfigError as exc:
        return {
            "config": {"configured": False, "path": str(config_path(root)),
                       "seeded": False, "error": str(exc)},
            "handshake": {"available": False, "configured": False,
                          "error": str(exc), "checked_at": contracts.utc_now()},
        }
    if probe:
        result = handshake(force=True,
                           timeout=timeout or min(10.0, cfg["timeout"]), cfg=cfg)
    else:
        result = handshake(cfg=cfg)
    return {"config": mask(cfg, root=root), "handshake": result}


# --- peel state mapping ----------------------------------------------------


def extraction_state_for_peel(state):
    """peekseed's per-source ``extraction.state`` for a peel state."""
    return STATE_MAP.get(str(state or "").lower(), "running")


def extraction_from_summary(summary, *, previous=None):
    """Build/merge a dataset source ``extraction`` block from a peel summary.

    ``peelfeed status`` returns the state, conversion id, counts and validation;
    that is enough for the UI chip and the index gate. The engine fingerprint is
    filled by :func:`peekseed.ingest.index.build_index` from ``build_summary.json``
    (the status payload does not carry it).
    """
    summary = summary if isinstance(summary, dict) else {}
    previous = dict(previous or {})
    state = extraction_state_for_peel(summary.get("state"))
    validation = summary.get("validation") if isinstance(
        summary.get("validation"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    source = summary.get("source") if isinstance(summary.get("source"), dict) else {}
    errors = validation.get("errors")
    error_count = len(errors) if isinstance(errors, list) else int(errors or 0)
    review_items = summary.get("review_items")
    if review_items is None:
        review_items = validation.get("review_items") or 0
    block = dict(previous)
    block.update({
        "state": state,
        "peel_id": str(summary.get("peel_id") or previous.get("peel_id") or ""),
        "conversion_id": str(summary.get("conversion_id")
                             or previous.get("conversion_id") or ""),
        "profile": str(summary.get("profile") or previous.get("profile") or ""),
        "engine": str(previous.get("engine") or ""),
        "validation": {
            "errors": int(error_count),
            "review_items": int(review_items or 0),
        },
        "error": str(summary.get("error") or "") or None,
    })
    if source.get("pages"):
        block["pages"] = int(source["pages"])
    elif counts.get("pages"):
        block["pages"] = int(counts["pages"])
    now = contracts.utc_now()
    if state in ("queued", "running") and not block.get("started_at"):
        block["started_at"] = now
    if state in ("extracted", "failed", "stale"):
        block["finished_at"] = block.get("finished_at") or now
    return block


# --- CLI adapter -----------------------------------------------------------


def _parse_json(text):
    """Parse the JSON a ``--json`` command printed.

    The CLI prints one JSON document on stdout. Be tolerant of a stray warning
    line: try the whole output, then the last line that decodes.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty output")
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line[:1] not in "{[":
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    raise ValueError("no JSON document in the output")


def _tail(text, limit=300):
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1].strip()[:limit]


class PeelfeedClient(object):
    """A thin, typed adapter over the peelfeed command line interface."""

    def __init__(self, cfg, timeout=DEFAULT_TIMEOUT):
        self.cfg = dict(cfg)
        self.timeout = float(timeout)

    # --- process plumbing ---
    def _argv(self, args, *, root=True, json_flag=True):
        command = _command(self.cfg)
        if command is None:
            raise PeelfeedUnavailable(
                "peelfeed is not configured (set PEEKSEED_PEELFEED_CLI or "
                "PEEKSEED_PEELFEED_PYTHON)")
        argv = list(command) + [str(item) for item in args]
        if root:
            argv.extend(["--root", self.cfg["root"]])
        if json_flag:
            argv.append("--json")
        return argv

    def _environment(self):
        env = {key: value for key, value in os.environ.items()
               if key in _PASS_ENV or key.startswith("PEELFEED_")}
        env["PEELFEED_SHARED_DIR"] = str(config.paths().shared_dir)
        if self.cfg.get("python") and not self.cfg.get("cli"):
            repo = self.cfg.get("repo") or ""
            if repo:
                env["PYTHONPATH"] = repo
        return env

    def _run(self, args, *, timeout=None, root=True, json_flag=True):
        argv = self._argv(args, root=root, json_flag=json_flag)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired:
            raise PeelfeedError(
                504, "peelfeed timed out after %ss" % (timeout or self.timeout)
            ) from None
        except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
            raise PeelfeedUnavailable(
                "cannot start the peelfeed CLI (%s)" % type(exc).__name__) from None
        except OSError as exc:
            raise PeelfeedUnavailable(
                "cannot start the peelfeed CLI (%s)" % type(exc).__name__) from None
        if completed.returncode != 0:
            raise PeelfeedError(
                self._error_status(completed.returncode),
                self._error_detail(completed),
            )
        try:
            return _parse_json(completed.stdout)
        except ValueError:
            raise PeelfeedError(
                502, "peelfeed printed no parseable JSON: %s"
                % (_tail(completed.stdout) or "empty output")) from None

    @staticmethod
    def _error_status(code):
        if int(code) == 3:  # peelfeed's NotFoundError
            return 404
        return 502

    def _error_detail(self, completed):
        """A short, readable reason for a failed command (stderr + payload)."""
        detail = _tail(completed.stderr)
        try:
            payload = _parse_json(completed.stdout)
        except ValueError:
            payload = None
        reason = ""
        if isinstance(payload, dict):
            peel = payload.get("peel")
            if isinstance(peel, dict):
                reason = str(peel.get("error") or "")
            validation = (peel.get("validation")
                          if isinstance(peel, dict) else None)
            if not reason and isinstance(validation, dict):
                errors = validation.get("errors")
                count = len(errors) if isinstance(errors, list) else int(
                    errors or 0)
                if count:
                    reason = "%d validation error(s)" % count
            gates = payload.get("gates")
            if not reason and isinstance(gates, dict) and gates.get("errors"):
                reason = "; ".join(str(item) for item in gates["errors"])[:300]
            if not reason:
                reason = str(payload.get("error") or "")
        if detail and reason and reason not in detail:
            detail = "%s (%s)" % (detail, reason)
        elif not detail:
            detail = reason or "peelfeed exited with status %d" % completed.returncode
        return detail[:300]

    # --- capabilities / status ---
    def capabilities(self, timeout=None):
        """``peelfeed capabilities`` (it prints JSON unconditionally, no --root)."""
        return self._run(["capabilities"], timeout=timeout, root=False,
                         json_flag=False)

    def status(self, peel_id=None, timeout=None):
        """One peel summary (``peel_id``) or the list of summaries."""
        args = ["status"] + ([str(peel_id)] if peel_id else [])
        return self._run(args, timeout=timeout or min(SHORT_TIMEOUT, self.timeout))

    def status_all(self, timeout=None):
        """Every peel summary under the configured root."""
        return self._run(["status"], timeout=timeout or min(SHORT_TIMEOUT,
                                                            self.timeout))

    # --- long operations (run in a worker thread by the API) ---
    def ingest(self, source_path, profile=None, llm_mode=None,
               chunk_profile=None, force=False, timeout=None, chunk_versions=None):
        args = ["ingest", str(source_path)]
        if profile:
            args.extend(["--profile", str(profile)])
        if llm_mode:
            args.extend(["--llm", str(llm_mode)])
        if chunk_profile:
            args.extend(["--chunk-profile", str(chunk_profile)])
        if chunk_versions:
            args.extend(["--chunk-versions", json.dumps(list(chunk_versions))])
        if force:
            # The CLI has --rebuild (rebuild the corpus even when it looks
            # fresh) rather than a literal --force; this is the force semantics.
            args.append("--rebuild")
        return self._run(args, timeout=timeout or self.timeout)

    def rebuild(self, peel_id, reasons=None, timeout=None, chunk_versions=None):
        """``chunk_versions`` (a list of chunk sizes) replaces the peel's chunk-set table."""
        args = ["rebuild", str(peel_id)]
        for reason in reasons or []:
            args.extend(["--reason", str(reason)])
        if chunk_versions:
            args.extend(["--chunk-versions", json.dumps(list(chunk_versions))])
        return self._run(args, timeout=timeout or self.timeout)

    # --- short operations ---
    def review(self, peel_id):
        return self._run(["review", str(peel_id)],
                         timeout=min(SHORT_TIMEOUT, self.timeout))

    def decide(self, peel_id, item, decision, note=None, evidence=None,
               model=None, confidence=None):
        args = ["decide", str(peel_id), "--item", str(item),
                "--decision", str(decision)]
        if note is not None:
            args.extend(["--note", str(note)])
        if evidence is not None:
            args.extend(["--evidence", json.dumps(evidence)])
        if model is not None:
            args.extend(["--model", str(model)])
        if confidence is not None:
            args.extend(["--confidence", str(confidence)])
        return self._run(args, timeout=min(SHORT_TIMEOUT, self.timeout))

    def event(self, peel_id, event, data=None):
        args = ["event", str(peel_id), "--event", str(event)]
        if data:
            args.extend(["--data", json.dumps(data)])
        return self._run(args, timeout=min(SHORT_TIMEOUT, self.timeout))

    def archive(self, peel_id, reason=None):
        args = ["archive", str(peel_id)]
        if reason:
            args.extend(["--reason", str(reason)])
        return self._run(args, timeout=min(SHORT_TIMEOUT, self.timeout))
