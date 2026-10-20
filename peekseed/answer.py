"""OpenAI-compatible chat client plus the two pure functions that pack an answer.

The split is deliberate: ``ask_excerpts`` and ``build_ask_messages`` are pure
functions, so the excerpt packing and the prompt contract are unit-tested as
plain strings; only :class:`ChatClient` touches the network. The client is the
single SSE reader for both the one-shot answer and the research loop — a second
parser would be a second place for framing, error frames and usage chunks to
drift.

``ChatClient.base_url`` is the API root, i.e. the URL that ``/chat/completions``
and ``/models`` hang off. For a provider whose OpenAI-compatible API lives under
``/v1``, that means including it (``http://127.0.0.1:8030/v1``). A base URL
ending in a slash is accepted.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Optional

import httpx

from . import prompts

# Streaming a long answer can legitimately take minutes; connecting should not.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
# Model listing is a probe: fail quickly so a dead endpoint does not stall a page.
PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=4.0, write=4.0, pool=2.0)

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096

# Characters a single excerpt may keep before it is cut on a word boundary.
EXCERPT_ELLIPSIS = " […]"


class AnswerError(Exception):
    """A failure worth showing the user, with the HTTP status it maps to.

    ``code`` is an optional additive in-band identifier the caller may branch on;
    a client that only reads ``detail`` keeps working. ``detail`` never contains
    the configured API key.
    """

    def __init__(self, status: int, detail: str, code: Optional[str] = None):
        super().__init__(detail)
        self.status = int(status)
        self.detail = str(detail)
        self.code = code


def _redact_secret(text: str, secret: str) -> str:
    """Remove a configured API key from an error string before it is surfaced.

    The key is normally only ever a request header, but an HTTP client that
    validates a malformed header quotes the offending value in its exception —
    so a key hand-edited into a config file could echo itself into a response.
    This is the last line of defense for that case.
    """
    try:
        if secret and text and secret in text:
            return text.replace(secret, "[api_key]")
    except TypeError:
        pass
    return text


def _error_text(err: Any) -> str:
    """Provider errors arrive as a string or as ``{"message": ..., "type": ...}``."""
    if isinstance(err, dict):
        return str(err.get("message") or err.get("detail") or err.get("type") or err)
    return str(err)


def _response_error_detail(response: httpx.Response, model: str = "") -> str:
    """The most useful thing a failed chat/completions response has to say.

    A 404 is by far the most common failure: the endpoint does not serve the id
    that was asked for. Say which id was asked for and that the endpoint's own
    model list is the authority.
    """
    try:
        data = response.json()
    except Exception:
        return "the endpoint answered HTTP %d" % response.status_code
    err = data.get("error", data) if isinstance(data, dict) else data
    detail = _error_text(err)
    if response.status_code == 404:
        asked = model or "(none selected)"
        detail += (" — the endpoint does not serve model '%s'. List the models the "
                   "endpoint advertises and pick one of those." % asked)
    return detail


class ChatClient:
    """One OpenAI-compatible chat-completions endpoint.

    The retrieval has already happened when this is used for a grounded answer,
    so the call is plain: messages in, streamed deltas out, no tool loop between
    the evidence and the answer. The same class is the transport for the research
    loop because it forwards tool-call deltas verbatim and never interprets them.
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: Optional[httpx.Timeout] = None):
        self.base = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

    @property
    def configured(self) -> bool:
        return bool(self.base)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def models(self) -> List[str]:
        """The model ids the endpoint advertises. Raises :class:`AnswerError`.

        This is the source of any model picker in a UI: whatever the provider
        says it serves is selectable, with no code change when it swaps models.
        """
        if not self.configured:
            raise AnswerError(503, "the answer endpoint is not configured")
        try:
            with httpx.Client(base_url=self.base, headers=self._headers(),
                              timeout=PROBE_TIMEOUT) as client:
                response = client.get("/models")
                if response.status_code >= 400:
                    raise AnswerError(
                        response.status_code,
                        _redact_secret(_response_error_detail(response, self.model),
                                       self.api_key))
                data = response.json()
        except AnswerError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreachable is a normal state
            raise AnswerError(503, _redact_secret(
                "cannot reach the answer endpoint: %s: %s"
                % (type(exc).__name__, exc), self.api_key))
        rows = data.get("data") if isinstance(data, dict) else None
        return [row.get("id") for row in (rows or [])
                if isinstance(row, dict) and row.get("id")]

    def stream(self, messages: List[Dict[str, Any]], *,
               tools: Optional[List[Dict[str, Any]]] = None,
               tool_choice: Any = None,
               temperature: float = DEFAULT_TEMPERATURE,
               max_tokens: int = DEFAULT_MAX_TOKENS,
               extra: Optional[Dict[str, Any]] = None) -> Iterator[tuple]:
        """Yield ``("text"|"reasoning"|"tool_call_delta"|"usage"|"finish", piece)``.

        The endpoint speaks SSE: one ``data:`` line per delta, terminated by
        ``data: [DONE]``. A reasoning model puts its chain of thought in
        ``reasoning_content`` beside ``content``; both are forwarded, kept apart,
        so a caller can show the answer and fold the thinking away. ``finish``
        carries the stop reason, which is how a truncated answer is told apart
        from an empty one.

        Passing ``tools`` turns this into the agentic client too: each
        ``delta.tool_calls`` entry is yielded verbatim for the caller to
        accumulate. Otherwise the request shape is the one-shot call exactly.

        ``extra`` is merged into the request body last, for provider-specific
        fields (for example a chat-template switch) without adding them to this
        signature.
        """
        if not self.configured:
            raise AnswerError(503, "the answer endpoint is not configured")

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Ask for token counts on the final chunk so a caller can report real
            # throughput instead of guessing from characters.
            "stream_options": {"include_usage": True},
        }
        if tools:
            # An empty array is rejected by some servers, so tools are only ever
            # sent when there is something to send; ``tool_choice="none"`` is how
            # a forced final answer turns them off while keeping them declared.
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if extra:
            body.update(extra)

        try:
            with httpx.Client(base_url=self.base, headers=self._headers(),
                              timeout=self.timeout) as client:
                with client.stream("POST", "/chat/completions", json=body) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise AnswerError(
                            response.status_code,
                            _redact_secret(
                                _response_error_detail(response, self.model),
                                self.api_key))
                    for line in response.iter_lines():
                        line = (line or "").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        error = chunk.get("error")
                        if isinstance(error, (dict, str)):
                            raise AnswerError(502, _redact_secret(
                                _error_text(error), self.api_key))
                        if chunk.get("usage"):
                            yield ("usage", chunk["usage"])
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            # ``reasoning_content`` is the documented field;
                            # ``reasoning`` is what some servers use instead.
                            for key in ("reasoning_content", "reasoning"):
                                if delta.get(key):
                                    yield ("reasoning", delta[key])
                                    break
                            if delta.get("content"):
                                yield ("text", delta["content"])
                            if delta.get("tool_calls"):
                                yield ("tool_call_delta", delta["tool_calls"])
                            if choice.get("finish_reason"):
                                yield ("finish", choice["finish_reason"])
        except AnswerError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface the reason, not a stack
            raise AnswerError(502, _redact_secret(
                "the answer stream failed: %s: %s" % (type(exc).__name__, exc),
                self.api_key))


# --- excerpt packing -------------------------------------------------------

def excerpt_label(excerpt: Dict[str, Any]) -> str:
    """Human label for one excerpt: its section (with numbering) and page range.

    Built from the retrieved fields rather than assumed present, because a caller
    may hand over excerpts produced elsewhere. The section title usually already
    begins with its own numbering, so it is not prefixed twice.
    """
    label = (excerpt.get("label") or "").strip()
    if label:
        return label
    numbering = (excerpt.get("numbering") or "").strip()
    section = (excerpt.get("section") or "").strip()
    if numbering and section.startswith(numbering):
        name = section
    else:
        name = " ".join(part for part in (numbering, section) if part)
    if not name:
        name = (excerpt.get("chapter") or "").strip()
    parts = [name] if name else []

    page = _as_int(excerpt.get("page_start"))
    if page is not None and page >= 1:
        shown = "p.%d" % page
        end = _as_int(excerpt.get("page_end"))
        if end is not None and end != page:
            shown += "–%d" % end
        parts.append(shown)
    return " · ".join(parts)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(text: str, max_chars: int) -> str:
    """Cut to ``max_chars`` on a word boundary when there is one near the cap.

    The cap exists so a few large chunks cannot crowd out the rest of the
    evidence; cutting mid-token would leave a half identifier as the last visible
    word, so the boundary is preferred when it is not far back.
    """
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.8:
        cut = cut[:space]
    return cut.rstrip() + EXCERPT_ELLIPSIS


def _chunk_records(store: Any, dataset: str) -> Dict[str, Dict[str, Any]]:
    """Chunk metadata for a dataset, keyed by chunk id, from every loaded space.

    All embedding spaces of one dataset describe the same chunks, so the first
    copy wins; the lookup is only used to recover a chunk's full text.
    """
    records: Dict[str, Dict[str, Any]] = {}
    try:
        indexes = store.for_dataset(dataset) if store is not None else []
    except Exception:  # noqa: BLE001 - a lookup must not break the answer
        indexes = []
    for index in indexes or []:
        for meta in getattr(index, "meta", None) or []:
            if not isinstance(meta, dict):
                continue
            chunk_id = meta.get("id")
            if chunk_id and chunk_id not in records:
                records[chunk_id] = meta
    return records


def ask_excerpts(store: Any, dataset: str, results: List[Dict[str, Any]],
                 max_chunks: int = 8, max_chars: int = 6000) -> List[Dict[str, Any]]:
    """Turn a search result set into numbered excerpts for the answer prompt.

    ``results`` is the ranked list a search returned. ``store`` is the
    :class:`~peekseed.engine.IndexStore` used to recover the WHOLE chunk instead
    of the short snippet a result carries: grounding an answer in a truncated
    snippet invites the model to finish the sentence itself. When the chunk
    cannot be resolved the snippet is kept, so the feature still works with no
    index loaded.

    Each returned excerpt carries ``n``, ``id``, ``dataset``, ``section``,
    ``page_start``, ``page_end``, ``text`` and ``pdf_url``; ``label``,
    ``numbering``, ``chapter`` and ``source`` are additive conveniences.
    ``max_chunks`` caps how many results are sent and ``max_chars`` caps one
    excerpt's text.
    """
    records = _chunk_records(store, dataset)
    excerpts: List[Dict[str, Any]] = []
    for result in results or []:
        if max_chunks and len(excerpts) >= max_chunks:
            break
        chunk_id = result.get("id") or result.get("chunk_file")
        meta = records.get(chunk_id) if chunk_id else None
        # The reader view (`text`) is preferred over `doc` because `doc` starts
        # with the breadcrumb line, which is already the excerpt label.
        body = ""
        if meta:
            body = (meta.get("text") or meta.get("doc") or "").strip()
        source = "chunk" if body else "snippet"
        if not body:
            body = (result.get("snippet") or "").strip()
        if not body:
            continue
        body = _clip(body, max_chars)

        merged = dict(result)
        if meta:
            for key in ("section", "numbering", "chapter", "page_start",
                        "page_end", "id", "dataset"):
                if merged.get(key) in (None, "") and meta.get(key) not in (None, ""):
                    merged[key] = meta.get(key)
        excerpts.append({
            "n": len(excerpts) + 1,
            "id": chunk_id,
            "dataset": dataset,
            "section": merged.get("section") or "",
            "page_start": merged.get("page_start"),
            "page_end": merged.get("page_end"),
            "text": body,
            "pdf_url": result.get("pdf_url") or "",
            # Additive: a ready-made label keeps the prompt and the reference
            # list on the page showing the same wording.
            "label": excerpt_label(merged),
            "numbering": merged.get("numbering") or "",
            "chapter": merged.get("chapter") or "",
            "source": source,
        })
    return excerpts


def build_ask_messages(question: str, excerpts: List[Dict[str, Any]],
                       system_prompt: Optional[str] = None) -> List[Dict[str, Any]]:
    """The chat messages for a grounded answer: one system turn, one user turn.

    Evidence goes in the USER turn, not the system turn: that is where a chat
    model is trained to read supplied documents, and it keeps the system prompt a
    stable, cacheable prefix across every question.

    ``system_prompt`` overrides the default generic instruction; the closing
    instruction is always appended to the question so the citation contract is
    the last thing the model reads.
    """
    blocks = []
    for position, excerpt in enumerate(excerpts or [], 1):
        number = excerpt.get("n") or position
        label = excerpt_label(excerpt)
        head = ("[%s] %s" % (number, label)).rstrip()
        blocks.append(head + "\n" + (excerpt.get("text") or ""))
    evidence = "\n\n".join(blocks) if blocks else "(no excerpts were retrieved)"
    body = (
        "EXCERPTS\n\n%s\n\n"
        "---\n\nQUESTION\n\n%s\n\n%s"
        % (evidence, question or "", prompts.ASK_ANSWER_PLAIN)
    )
    return [
        {"role": "system", "content": system_prompt or prompts.ASK_SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]
