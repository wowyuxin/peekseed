"""peekseed's HTTP layer: one local FastAPI app over the data root.

This module wires the independent pieces together and owns nothing that could
live elsewhere: retrieval is :mod:`peekseed.engine`, model calls are
:mod:`peekseed.answer`, ingestion is :mod:`peekseed.ingest`, and links/history
are their own stores. That keeps every one of those testable without a server.

Two design rules run through every endpoint:

* **Off is a supported state.** No embed link, no rerank link, no answer link,
  no index, no history -- each produces a clear message, never a crash and never
  a silent fallback to a model the user did not configure.
* **A failure in a side path must not fail the main path.** History saves,
  recorder updates and status probes are best-effort; a search that worked still
  returns its results when the history disk is full.
"""

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__, answer as answer_mod, config, ingest, links, prompts
from .engine import IndexStore

LOG = logging.getLogger("peekseed.app")

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_QUERY_CHARS = 4000
DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RERANK_PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=60.0, pool=5.0)
PROBE_TIMEOUT = httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=2.0)


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def _env_int(name, default):
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name, default):
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name, default=False):
    raw = _env(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _agent_enabled():
    """Research mode is opt-in, read at request time so a restart is not needed."""
    return _env_bool("PEEKSEED_AGENT", False)
ASK_MAX_CHUNKS = max(1, _env_int("PEEKSEED_ASK_MAX_CHUNKS", 8))
ASK_MAX_CHARS = max(500, _env_int("PEEKSEED_ASK_MAX_CHARS", 6000))
ASK_MAX_TOKENS = max(64, _env_int("PEEKSEED_ASK_MAX_TOKENS", 4096))
ASK_TEMPERATURE = _env_float("PEEKSEED_ASK_TEMPERATURE", 0.2)
RERANK_POOL = max(0, _env_int("PEEKSEED_RERANK_POOL", 20))
RERANK_BATCH = max(1, _env_int("PEEKSEED_RERANK_BATCH", 8))
AGENT_MAX_STEPS = max(1, _env_int("PEEKSEED_AGENT_MAX_STEPS", 6))


# ---------------------------------------------------------------------------
# process state
# ---------------------------------------------------------------------------

_STORE = None
_STORE_LOCK = threading.RLock()
_HISTORY = None
_HISTORY_LOCK = threading.RLock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.RLock()
_UI_CACHE = {"bytes": None, "etag": None}


def store() -> IndexStore:
    """The index store, loaded on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = IndexStore(config.ensure().datasets_dir)
            _STORE.load()
        return _STORE


def reload_store() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = IndexStore(config.ensure().datasets_dir)
        _STORE.load()


def history():
    """The history store, or None when it cannot be created.

    History is a convenience, not evidence: a read-only home directory must not
    stop a search from working.
    """
    global _HISTORY
    with _HISTORY_LOCK:
        if _HISTORY is None:
            try:
                _HISTORY = history_store()
            except Exception as exc:  # noqa: BLE001 - never fatal
                LOG.warning("history is unavailable: %s: %s", type(exc).__name__, exc)
                return None
        return _HISTORY


def history_store():
    from .history import HistoryStore
    return HistoryStore(config.ensure().history_dir)


def reset_state():
    """Drop cached stores/jobs (tests point PEEKSEED_DATA at a temp dir)."""
    global _STORE, _HISTORY
    with _STORE_LOCK:
        _STORE = None
    with _HISTORY_LOCK:
        _HISTORY = None
    with _JOBS_LOCK:
        _JOBS.clear()
    config.reset_cache()
    links.reset_cache()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _http_detail(exc: Exception) -> str:
    """A user-facing string for an error, with no secret text.

    Ingest's IngestError and answer's AnswerError both already redact the key;
    anything else is reported by type so a provider body is not echoed blindly.
    """
    detail = getattr(exc, "detail", None)
    if detail:
        return str(detail)
    return "%s: %s" % (type(exc).__name__, exc)


def _link_config_500(exc):
    raise HTTPException(500, "model links config is not readable: %s" % exc)


def _auth_headers(link):
    headers = {"Content-Type": "application/json"}
    key = (link or {}).get("api_key") or ""
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _join(base, path):
    return (base or "").rstrip("/") + path


def _clip(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _quote_segment(value):
    return quote(str(value or ""), safe="")


def _pdf_url(dataset, meta):
    """The page link for one chunk, or "" when it has no source/page.

    The original PDF is served from the API and opened by the browser's native
    viewer; a chunk without a page (a text-only format, for example) gets no
    link rather than a dead one.
    """
    source = (meta or {}).get("source") or ""
    try:
        page = int((meta or {}).get("page_start") or 0)
    except (TypeError, ValueError):
        page = 0
    if not source or page < 1:
        return ""
    return "/pdf/%s/%s#page=%d" % (_quote_segment(dataset),
                                   _quote_segment(source), page)


def _result_label(meta):
    section = (meta or {}).get("section") or ""
    numbering = (meta or {}).get("numbering") or ""
    head = " ".join(p for p in (numbering, section) if p) or (meta or {}).get("id") or ""
    try:
        start = int((meta or {}).get("page_start") or 0)
        end = int((meta or {}).get("page_end") or 0)
    except (TypeError, ValueError):
        start = end = 0
    if start and end and end != start:
        head += " · p.%d–%d" % (start, end)
    elif start:
        head += " · p.%d" % start
    return head


def _result_row(rank, dataset, index, position, score):
    meta = index.meta[position]
    return {
        "rank": rank,
        "score": round(float(score), 4),
        "id": meta.get("id") or "",
        "dataset": dataset,
        "space": index.space,
        "label": _result_label(meta),
        "section": meta.get("section") or "",
        "chapter": meta.get("chapter") or "",
        "numbering": meta.get("numbering") or "",
        "page_start": meta.get("page_start"),
        "page_end": meta.get("page_end"),
        "source": meta.get("source") or "",
        "pdf_url": _pdf_url(dataset, meta),
        "snippet": _clip(meta.get("text") or meta.get("doc") or "", 320),
    }


# ---------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------

def _dataset_dir(dataset_id):
    dataset_id = (dataset_id or "").strip()
    if not DATASET_ID_RE.match(dataset_id):
        raise HTTPException(400, "invalid dataset id")
    directory = config.paths().datasets_dir / dataset_id
    if not directory.is_dir():
        raise HTTPException(404, "no dataset with id '%s'" % dataset_id)
    return directory


def _dataset_public(dataset_id):
    try:
        info = ingest.dataset_info(config.paths().datasets_dir / dataset_id)
    except ingest.IngestError as exc:
        raise HTTPException(404, str(exc))
    return {
        "id": info["id"],
        "name": info["name"],
        "created_at": info.get("created_at") or "",
        "notes": info.get("notes") or "",
        "sources": [s if isinstance(s, str) else s.get("name")
                    for s in info.get("sources") or []],
        "chunks": info.get("chunks") or 0,
        "indexes": [_index_public(entry) for entry in info.get("indexes") or []],
    }


def _index_public(info):
    return {
        "space": info.get("space") or "",
        "model": info.get("model") or "",
        "protocol": info.get("protocol") or "",
        "dim": info.get("dim") or 0,
        "n": info.get("n") or 0,
        "created_at": info.get("created_at") or "",
    }


def _all_datasets():
    root = config.ensure().datasets_dir
    rows = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return rows
    for name in names:
        if name.startswith(".") or not (root / name).is_dir():
            continue
        try:
            rows.append(_dataset_public(name))
        except HTTPException:
            continue
    return rows


# ---------------------------------------------------------------------------
# model links: probing
# ---------------------------------------------------------------------------

def _probe_link(link):
    role = link.get("role")
    base = link.get("base_url") or ""
    try:
        if role == "answer":
            client = answer_mod.ChatClient(base, link.get("api_key") or "",
                                           link.get("model") or "")
            models = client.models()
            return {"ok": True, "models": models,
                    "detail": "%d model(s)" % len(models) if models else "reachable"}
        if role == "embed":
            timeout = httpx.Timeout(connect=3.0, read=15.0, write=15.0, pool=2.0)
            with httpx.Client(timeout=timeout, headers=_auth_headers(link)) as client:
                if link.get("protocol") == "custom":
                    response = client.post(_join(base, "/embed"),
                                           json={"inputs": [{"text": "ping"}]})
                else:
                    response = client.post(_join(base, "/embeddings"),
                                           json={"model": link.get("model") or "",
                                                 "input": ["ping"]})
                if response.status_code >= 400:
                    return {"ok": False,
                            "detail": "HTTP %d: %s" % (response.status_code,
                                                       _clip(response.text, 200))}
                data = response.json()
                if link.get("protocol") == "custom":
                    vectors = data.get("embeddings") or []
                else:
                    vectors = [row.get("embedding") for row in data.get("data") or []
                               if isinstance(row, dict)]
                dim = len(vectors[0]) if vectors and vectors[0] else 0
                return {"ok": bool(vectors), "dim": dim,
                        "detail": "embedding of dimension %d" % dim}
        if role == "rerank":
            timeout = httpx.Timeout(connect=3.0, read=20.0, write=20.0, pool=2.0)
            with httpx.Client(timeout=timeout, headers=_auth_headers(link)) as client:
                response = client.post(_join(base, "/rerank"), json={
                    "query": {"text": "ping"},
                    "documents": [{"text": "a"}, {"text": "b"}]})
                if response.status_code >= 400:
                    return {"ok": False,
                            "detail": "HTTP %d: %s" % (response.status_code,
                                                       _clip(response.text, 200))}
                scores = response.json().get("scores")
                ok = isinstance(scores, list) and len(scores) == 2
                return {"ok": ok, "detail": "2 scores" if ok else "unexpected response"}
        return {"ok": False, "detail": "unknown link role"}
    except Exception as exc:  # noqa: BLE001 - unreachable is a normal state
        return {"ok": False, "detail": "%s: %s" % (type(exc).__name__,
                                                   _clip(str(exc), 200))}


def _links_status():
    rows = {}
    all_links = links.links()
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {link["id"]: pool.submit(_probe_link, link)
                      for link in all_links}
        for link_id, future in future_map.items():
            try:
                rows[link_id] = future.result()
            except Exception as exc:  # noqa: BLE001
                rows[link_id] = {"ok": False, "detail": str(exc)}
    return rows


# ---------------------------------------------------------------------------
# embedding / reranking as HTTP adapters
# ---------------------------------------------------------------------------

def _embed_link():
    try:
        link = links.active("embed")
    except links.LinkConfigError as exc:
        _link_config_500(exc)
    if not link:
        raise HTTPException(
            409, "no embedding link is enabled - add one in Settings and click Use")
    return link


def _embed_query(link, text):
    try:
        vectors = ingest.embed_query(link, text)
    except Exception as exc:  # noqa: BLE001 - provider detail is user-facing
        raise HTTPException(502, "the embedding link failed: %s" % _http_detail(exc))
    if not vectors:
        raise HTTPException(502, "the embedding link returned no vector")
    return np.asarray(vectors, dtype=np.float32).ravel()


def _rerank_callable():
    """The enabled rerank link as a callable, or None when reranking is off."""
    try:
        link = links.active("rerank")
    except links.LinkConfigError:
        return None
    if not link:
        return None

    def rerank(query, documents):
        body = {"query": {"text": query},
                "documents": [{"text": doc} for doc in documents]}
        try:
            with httpx.Client(timeout=RERANK_PROBE_TIMEOUT,
                              headers=_auth_headers(link)) as client:
                response = client.post(_join(link["base_url"], "/rerank"), json=body)
                if response.status_code >= 400:
                    LOG.warning("rerank link returned HTTP %d", response.status_code)
                    return None
                scores = response.json().get("scores")
                if not isinstance(scores, list) or len(scores) != len(documents):
                    LOG.warning("rerank link returned an unusable score list")
                    return None
                return [float(value) for value in scores]
        except Exception as exc:  # noqa: BLE001 - rerank is optional
            LOG.warning("rerank link failed (%s); using fusion order",
                        type(exc).__name__)
            return None

    return rerank


def _resolve_index(dataset, link, space=None):
    index_store = store()
    candidate_space = space or links.embed_space_key(link)
    index = index_store.get(dataset, candidate_space)
    if index is None:
        index = index_store.find(dataset, link.get("id"), link.get("model"))
    if index is None:
        available = [entry.space for entry in index_store.for_dataset(dataset)]
        raise HTTPException(
            409,
            "dataset '%s' has no index for the enabled embedding model '%s' (%s). "
            "Build its index first. Indexed spaces: %s"
            % (dataset, link.get("label") or link.get("id"),
               link.get("model") or "default", ", ".join(available) or "none"))
    return index


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class SearchReq(BaseModel):
    dataset: str
    query: str = ""
    topk: int = 10
    space: Optional[str] = None
    rerank: bool = True


def _run_search(req, on_stage=None):
    dataset = (req.dataset or "").strip()
    _dataset_dir(dataset)
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(400, "empty query")
    if len(query) > MAX_QUERY_CHARS:
        raise HTTPException(400, "query is longer than %d characters" % MAX_QUERY_CHARS)
    link = _embed_link()

    def stage(name, status, sec=None):
        if on_stage:
            on_stage({"stage": name, "status": status, "sec": sec})

    t_request = time.perf_counter()
    stage("embed", "start")
    t_embed = time.perf_counter()
    qvec = _embed_query(link, query)
    embed_sec = time.perf_counter() - t_embed
    stage("embed", "done", embed_sec)

    index = _resolve_index(dataset, link, req.space)
    topk = max(1, min(int(req.topk or 10), 50))
    try:
        order, reranked, scores, timings = index.search(
            qvec, query, topk,
            rerank_pool=RERANK_POOL, rerank_batch=RERANK_BATCH,
            on_stage=on_stage, rerank=bool(req.rerank),
            rerank_fn=_rerank_callable())
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    results = [_result_row(rank, dataset, index, position, score)
               for rank, (position, score) in enumerate(
                   zip(order[:topk], scores[:topk]), start=1)]
    timings = dict(timings or {})
    timings["embed_sec"] = round(embed_sec, 4)
    timings["total_sec"] = round(time.perf_counter() - t_request, 4)
    result = {
        "dataset": dataset,
        "query": query,
        "space": index.space,
        "model": link.get("model") or "",
        "link": link.get("id") or "",
        "timings": timings,
        "reranked": bool(reranked),
        "results": results,
    }
    result["history_id"] = _record_search(result)
    return result


def _record_search(result):
    try:
        hist = history()
        if hist is None:
            return None
        return hist.record_search(result)
    except Exception as exc:  # noqa: BLE001 - history must not fail a search
        LOG.warning("history save failed: %s: %s", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# answer / research shared setup
# ---------------------------------------------------------------------------

class AskReq(BaseModel):
    dataset: str = ""
    query: str = ""
    question: Optional[str] = None
    space: Optional[str] = None
    link: Optional[str] = None
    model: Optional[str] = None
    results: Optional[List[dict]] = None
    max_chunks: Optional[int] = None
    history_id: Optional[str] = None
    refs: Optional[List[int]] = None
    thread_context: Optional[bool] = None
    image_chunks: Optional[List[str]] = None


class AgentReq(AskReq):
    auto: bool = False
    max_steps: Optional[int] = None


def _answer_choice(req):
    try:
        if req.link:
            link = links.get(req.link)
        else:
            link = links.default_answer_link()
    except links.LinkConfigError as exc:
        _link_config_500(exc)
    if link is None:
        raise answer_mod.AnswerError(
            503, "no answer link is configured - add one in Settings")
    if link.get("role") != "answer" or not link.get("enabled"):
        raise answer_mod.AnswerError(
            409, "link '%s' is not an enabled answer link" % (req.link or link.get("id")))
    model = (req.model or link.get("model") or "").strip()
    client = answer_mod.ChatClient(link.get("base_url") or "",
                                   link.get("api_key") or "", model)
    if not model:
        models = [m for m in client.models() if m]
        if not models:
            raise answer_mod.AnswerError(
                409, "the answer link advertises no models; set one in Settings")
        model = models[0]
        client = answer_mod.ChatClient(link.get("base_url") or "",
                                       link.get("api_key") or "", model)
    return link, client


def _seed_results(req):
    """The result rows an answer/research request works over.

    The UI sends the rows it is showing, so the answer reads exactly the evidence
    on screen. An API client that sends only a query gets a fresh search instead.
    """
    if req.results:
        return list(req.results)
    if not (req.dataset and req.query):
        return []
    result = _run_search(SearchReq(dataset=req.dataset, query=req.query,
                                   space=req.space, rerank=True))
    return result.get("results") or []


def _seed_excerpts(req, results):
    max_chunks = req.max_chunks or ASK_MAX_CHUNKS
    max_chunks = max(1, min(int(max_chunks), 24))
    try:
        excerpts = answer_mod.ask_excerpts(
            store(), req.dataset, results, max_chunks=max_chunks,
            max_chars=ASK_MAX_CHARS)
    except Exception as exc:  # noqa: BLE001 - excerpt packing must not 500
        LOG.warning("excerpt packing failed: %s: %s", type(exc).__name__, exc)
        raise answer_mod.AnswerError(500, "could not build excerpts from the results")
    if not excerpts:
        raise answer_mod.AnswerError(
            400, "there are no search results to answer from")
    return excerpts


def _seed_summary(excerpts):
    return [{key: excerpt.get(key) for key in
             ("n", "id", "label", "section", "numbering", "chapter",
              "page_start", "page_end", "pdf_url", "source")
             if excerpt.get(key) is not None}
            for excerpt in excerpts]


def _context_event(req, mode, excerpts, auto=False, extra=None):
    types = {excerpt.get("source") for excerpt in excerpts}
    event = {
        "type": "context",
        "dataset": req.dataset,
        "mode": mode,
        "auto": bool(auto),
        "chars": sum(len(excerpt.get("text") or "") for excerpt in excerpts),
        "dropped": 0,
        "full_chunks": sum(1 for excerpt in excerpts
                           if excerpt.get("source") == "chunk"),
        "tables": 0,
        "images": {"enabled": False, "sent": [], "requested": []},
        "snippet_only": sum(1 for excerpt in excerpts
                            if excerpt.get("source") == "snippet"),
        "excerpts": _seed_summary(excerpts),
    }
    if extra:
        event.update(extra)
    return event


def _begin_run(req, mode, question, model, seed, thread=None):
    """Start (or adopt) a history run; returns a recorder or None."""
    hist = history()
    if hist is None:
        return None
    try:
        return hist.begin_run(
            req.history_id, mode=mode, question=question, model=model,
            dataset=req.dataset, seed=seed,
            thread=thread or {"context_runs": [], "turn": 1},
            adopted_from=None)
    except Exception as exc:  # noqa: BLE001 - history never fails a dialog
        LOG.warning("history begin_run failed: %s: %s", type(exc).__name__, exc)
        return None


def _record_event(recorder, event):
    if recorder is None:
        return
    try:
        recorder.observe(event)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("history observe failed: %s: %s", type(exc).__name__, exc)


def _finish_run(recorder, status="done", error=None):
    if recorder is None:
        return
    try:
        recorder.finish(status=status, error=error)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("history finish failed: %s: %s", type(exc).__name__, exc)


def _run_ids(recorder):
    if recorder is None:
        return {}
    out = {"run_id": getattr(recorder, "run_id", None)}
    if getattr(recorder, "persisted", False):
        out["history_id"] = getattr(recorder, "entry_id", None)
    return out


def _ndjson_stream(worker):
    """Run ``worker(put)`` on a thread and stream its events as NDJSON.

    A callback cannot yield, so every streaming endpoint uses this one shape: the
    worker pushes dicts onto a queue (or None when finished) and the response
    generator drains it. It also gives the stream a single place to report an
    unexpected failure instead of leaving the browser waiting forever.
    """
    events = queue.Queue()
    out = {}

    def put(item):
        # An error event already streamed by the worker must not be repeated by
        # the generator at the end; the queue is the single delivery path.
        if isinstance(item, dict) and item.get("type") == "error":
            out["error_emitted"] = True
        events.put(item)

    def run():
        try:
            worker(put, out)
        except Exception as exc:  # noqa: BLE001 - always terminate the stream
            LOG.exception("stream worker failed")
            out["error"] = {"type": "error", "status": 500,
                            "detail": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            events.put(None)

    threading.Thread(target=run, name="peekseed-stream", daemon=True).start()

    def generate():
        try:
            while True:
                item = events.get()
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"
            if out.get("error") and not out.get("error_emitted"):
                yield json.dumps(out["error"], ensure_ascii=False) + "\n"
        finally:
            pass

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# agent helpers
# ---------------------------------------------------------------------------

def _agent_semantic(dataset, query, k):
    """The agent's ``search`` tool: a fast retrieval without reranking.

    None means "no embedder available"; the tool then answers from its own
    keyword ranking. A retrieval failure mid-loop must not end the session, so
    every error maps to None.
    """
    try:
        link = links.active("embed")
        if not link:
            return None
        index = store().get(dataset, links.embed_space_key(link)) or \
            store().find(dataset, link.get("id"), link.get("model"))
        if index is None:
            return None
        qvec = _embed_query(link, query)
        order, _reranked, scores, _timings = index.search(
            qvec, query, max(1, min(int(k or 5), 10)),
            rerank_pool=0, rerank_batch=RERANK_BATCH, rerank=False)
        return [(index.meta[position], float(score))
                for position, score in zip(order, scores)]
    except Exception as exc:  # noqa: BLE001 - degrade to keyword ranking
        LOG.warning("agent semantic search failed: %s: %s",
                    type(exc).__name__, exc)
        return None


def _agent_link(row):
    """``(pdf_url, viewer_url)`` for one ledger row."""
    return _pdf_url(row.get("dataset") or "", row), ""


def _public_ledger(rows):
    """Normalise ledger rows for the UI (id/chunk aliases, resolved PDF URL)."""
    out = []
    for row in rows or []:
        item = dict(row)
        item.setdefault("id", item.get("chunk") or "")
        item.setdefault("chunk_file", item.get("chunk") or item.get("id") or "")
        item.setdefault("dataset", item.get("dataset") or "")
        item.setdefault("pdf_url", _pdf_url(item.get("dataset"), item))
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app():
    @asynccontextmanager
    async def lifespan(_app):
        # Create the data root and load whatever is already there. A corrupt
        # link config is reported by the link endpoints; it must not stop the
        # whole service from starting, because "no model configured" is a valid
        # state and the user needs the UI to fix it.
        config.ensure()
        try:
            links.load()
        except links.LinkConfigError as exc:
            LOG.warning("model links config is not readable: %s", exc)
        reload_store()
        yield

    app = FastAPI(title="peekseed", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def cache_policy(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path == "/":
            # The page is cacheable but must be revalidated: a changed UI has to
            # reach the browser, while a back/forward navigation stays instant.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # --- UI ---------------------------------------------------------------
    def _ui():
        if _UI_CACHE["bytes"] is None:
            path = WEB_DIR / "index.html"
            if not path.is_file():
                raise HTTPException(503, "the web UI is not installed")
            data = path.read_bytes()
            _UI_CACHE["bytes"] = data
            _UI_CACHE["etag"] = '"%s"' % hashlib.sha256(data).hexdigest()[:16]
        return _UI_CACHE["bytes"], _UI_CACHE["etag"]

    @app.get("/")
    def index(request: Request):
        data, etag = _ui()
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag,
                                                      "Cache-Control": "no-cache"})
        return Response(data, media_type="text/html",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})

    @app.get("/health")
    def health():
        return {"ok": True, "datasets": len(_all_datasets())}

    # --- datasets ---------------------------------------------------------
    @app.get("/api/datasets")
    def datasets_list():
        return {"datasets": _all_datasets()}

    @app.post("/api/datasets", status_code=201)
    async def datasets_create(name: str = Form(...), note: str = Form(""),
                              files: List[UploadFile] = File(...)):
        if not (name or "").strip():
            raise HTTPException(400, "a dataset name is required")
        if not files:
            raise HTTPException(400, "at least one PDF is required")
        dataset = None
        staging = config.ensure().uploads_dir / ("upload-" + uuid.uuid4().hex)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            dataset = ingest.create_dataset(config.paths(), name, note=note)
            for number, upload in enumerate(files):
                filename = os.path.basename(upload.filename or "")
                if not filename.lower().endswith(".pdf"):
                    raise HTTPException(400, "%r is not a PDF" % filename)
                data = await upload.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "%r is larger than %d MB"
                                        % (filename, MAX_UPLOAD_BYTES // (1024 * 1024)))
                if not data.startswith(b"%PDF-"):
                    raise HTTPException(400, "%r is not a valid PDF" % filename)
                temp_dir = staging / ("%03d" % number)
                temp_dir.mkdir(parents=True, exist_ok=True)
                temp = temp_dir / filename
                temp.write_bytes(data)
                ingest.add_source(dataset, temp)
            return {"dataset": _dataset_public(dataset["id"])}
        except HTTPException:
            if dataset is not None and dataset.get("dir"):
                shutil.rmtree(dataset["dir"], ignore_errors=True)
            raise
        except Exception as exc:  # noqa: BLE001
            if dataset is not None and dataset.get("dir"):
                shutil.rmtree(dataset["dir"], ignore_errors=True)
            raise HTTPException(400, _http_detail(exc))
        finally:
            shutil.rmtree(str(staging), ignore_errors=True)

    @app.delete("/api/datasets/{dataset_id}")
    def datasets_delete(dataset_id: str):
        directory = _dataset_dir(dataset_id)
        shutil.rmtree(str(directory), ignore_errors=True)
        reload_store()
        return {"ok": True, "deleted": dataset_id}

    # --- index jobs -------------------------------------------------------
    @app.post("/api/datasets/{dataset_id}/index", status_code=202)
    def datasets_index(dataset_id: str, payload: Optional[dict] = None):
        directory = _dataset_dir(dataset_id)
        link_id = (payload or {}).get("link")
        try:
            link = links.get(link_id) if link_id else links.active("embed")
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if link is None:
            raise HTTPException(409, "no embedding link is enabled")
        if link.get("role") != "embed" or not link.get("enabled"):
            raise HTTPException(409, "link '%s' is not an enabled embed link"
                                % (link_id or ""))
        job = {
            "id": "job-" + uuid.uuid4().hex[:12],
            "dataset": dataset_id,
            "space": links.embed_space_key(link),
            "status": "running",
            "progress": 0.0,
            "message": "queued",
            "error": None,
            "created_at": time.time(),
        }
        with _JOBS_LOCK:
            _JOBS[job["id"]] = job

        def work():
            def progress(frac, message):
                job["progress"] = max(0.0, min(1.0, float(frac)))
                job["message"] = str(message)

            try:
                info = ingest.build_index(directory, link, progress=progress)
                job["progress"] = 1.0
                job["message"] = "index ready: %d chunks, dimension %d" % (
                    info.get("n", 0), info.get("dim", 0))
                job["status"] = "done"
                reload_store()
            except Exception as exc:  # noqa: BLE001 - the job reports, never crashes
                job["status"] = "error"
                job["error"] = _http_detail(exc)
                job["message"] = "index failed"
            finally:
                job["finished_at"] = time.time()

        threading.Thread(target=work, name="peekseed-index", daemon=True).start()
        return {"job": job["id"]}

    @app.get("/api/jobs/{job_id}")
    def jobs_get(job_id: str):
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                raise HTTPException(404, "no such job")
            return dict(job)

    @app.get("/pdf/{dataset_id}/{filename}")
    def dataset_pdf(dataset_id: str, filename: str):
        directory = _dataset_dir(dataset_id)
        name = os.path.basename(filename)
        if name != filename or not name:
            raise HTTPException(400, "invalid file name")
        path = directory / "original" / name
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise HTTPException(404, "no such file")
        original = (directory / "original").resolve()
        if original not in resolved.parents:
            raise HTTPException(400, "invalid file name")
        return FileResponse(str(resolved), media_type="application/pdf",
                            filename=name)

    # --- model links ------------------------------------------------------
    @app.get("/api/links")
    def links_list():
        try:
            all_links = links.links()
            return {"links": [links.mask(link) for link in all_links],
                    "path": str(links.config_path()), "seeded": links.seeded()}
        except links.LinkConfigError as exc:
            _link_config_500(exc)

    @app.post("/api/links", status_code=201)
    def links_create(payload: Optional[dict] = None):
        try:
            link = links.create(payload or {})
        except links.LinkValidationError as exc:
            raise HTTPException(400, str(exc))
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        return {"link": links.mask(link)}

    @app.put("/api/links/{link_id}")
    def links_update(link_id: str, payload: Optional[dict] = None):
        try:
            link = links.update(link_id, payload or {})
        except KeyError:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        except links.LinkValidationError as exc:
            raise HTTPException(400, str(exc))
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        return {"link": links.mask(link)}

    @app.delete("/api/links/{link_id}")
    def links_delete(link_id: str):
        try:
            deleted = links.delete(link_id)
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if not deleted:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        return {"deleted": link_id}

    @app.post("/api/links/{link_id}/use")
    def links_use(link_id: str, payload: Optional[dict] = None):
        enabled = (payload or {}).get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(400, "enabled must be true or false")
        try:
            link = links.set_enabled(link_id, enabled)
        except KeyError:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        return {"link": links.mask(link)}

    @app.post("/api/links/{link_id}/test")
    def links_test(link_id: str):
        try:
            link = links.get(link_id)
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if link is None:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        return _probe_link(link)

    @app.get("/api/links/status")
    def links_status():
        try:
            links.links()
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        return {"links": _links_status(), "checked_at": time.time()}

    # --- search -----------------------------------------------------------
    @app.post("/api/search")
    def search(req: SearchReq):
        return _run_search(req)

    @app.post("/api/search/stream")
    def search_stream(req: SearchReq):
        def worker(put, out):
            try:
                result = _run_search(req, on_stage=put)
                out["result"] = result
                # The result is an event like any other; the stage events were
                # already pushed by on_stage.
                put({"result": result})
            except HTTPException as exc:
                out["error"] = {"error": {"status": exc.status_code,
                                          "detail": exc.detail}}
            except Exception as exc:  # noqa: BLE001
                out["error"] = {"error": {"status": 500,
                                          "detail": _http_detail(exc)}}

        return _ndjson_stream(worker)

    # --- ask / agent ------------------------------------------------------
    @app.get("/api/ask-status")
    def ask_status():
        try:
            answer_links = links.answer_links()
            default = links.default_answer_link()
        except links.LinkConfigError:
            answer_links, default = [], None
        models = []
        if default is not None:
            probe = _probe_link(default)
            models = probe.get("models") or []
        return {
            "configured": default is not None,
            "base": (default or {}).get("base_url") or "",
            "model": (default or {}).get("model") or "",
            "models": models,
            "links": [links.mask(link) for link in answer_links],
            "default_provider": (default or {}).get("id"),
            "max_chunks": ASK_MAX_CHUNKS,
            "max_chunk_chars": ASK_MAX_CHARS,
            "images": {"enabled": False},
            "agent": {
                "enabled": _agent_enabled(),
                "max_steps": AGENT_MAX_STEPS,
                "detail": "" if _agent_enabled() else
                          "research mode is off (set PEEKSEED_AGENT=1 and restart)",
            },
        }

    @app.post("/api/ask/stream")
    def ask_stream(req: AskReq):
        def worker(put, out):
            recorder = None
            try:
                question = (req.question or req.query or "").strip()
                if not question:
                    raise answer_mod.AnswerError(400, "empty question")
                _dataset_dir(req.dataset) if req.dataset else None
                results = _seed_results(req)
                excerpts = _seed_excerpts(req, results)
                _link, client = _answer_choice(req)
                seed = {"refs": req.refs or [], "auto": False,
                        "excerpts": _seed_summary(excerpts)}
                recorder = _begin_run(req, "ask", question, client.model, seed)
                context = _context_event(req, "ask", excerpts, extra=_run_ids(recorder))
                put(context)
                _record_event(recorder, context)
                messages = answer_mod.build_ask_messages(question, excerpts)
                t0 = time.perf_counter()
                first_sec = None
                parts = []
                usage = None
                finish = None
                for kind, piece in client.stream(
                        messages, temperature=ASK_TEMPERATURE,
                        max_tokens=ASK_MAX_TOKENS):
                    if kind == "usage":
                        usage = piece
                        continue
                    if kind == "finish":
                        finish = piece
                        continue
                    if first_sec is None:
                        first_sec = time.perf_counter() - t0
                    event = {"type": kind, "text": piece}
                    put(event)
                    _record_event(recorder, event)
                    if kind == "text":
                        parts.append(piece)
                done = {
                    "type": "done",
                    "mode": "ask",
                    "model": client.model,
                    "answer": "".join(parts),
                    "chars": sum(len(part) for part in parts),
                    "usage": usage,
                    "usage_total": usage,
                    "finish_reason": finish,
                    "truncated": finish == "length",
                    "first_token_sec": round(first_sec, 4) if first_sec is not None else None,
                    "total_sec": round(time.perf_counter() - t0, 4),
                }
                done.update(_run_ids(recorder))
                put(done)
                _record_event(recorder, done)
                out["done"] = done
                _finish_run(recorder, "done")
            except answer_mod.AnswerError as exc:
                error = {"type": "error", "status": exc.status, "detail": exc.detail}
                error.update(_run_ids(recorder))
                put(error)
                _record_event(recorder, error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": exc.status, "detail": exc.detail})
            except HTTPException as exc:
                error = {"type": "error", "status": exc.status_code,
                         "detail": exc.detail}
                error.update(_run_ids(recorder))
                put(error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": exc.status_code, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                LOG.exception("ask failed")
                error = {"type": "error", "status": 500, "detail": _http_detail(exc)}
                error.update(_run_ids(recorder))
                put(error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": 500, "detail": _http_detail(exc)})

        return _ndjson_stream(worker)

    @app.post("/api/agent/stream")
    def agent_stream(req: AgentReq):
        if not _agent_enabled():
            raise HTTPException(404, "research mode is off (set PEEKSEED_AGENT=1 "
                                     "and restart the service)")

        def worker(put, out):
            recorder = None
            try:
                from .agent import (AgentBudget, AgentSession, Ledger,
                                    build_agent_messages, build_auto_messages)
                from .tools import DatasetTools

                question = (req.question or req.query or "").strip()
                if not question:
                    raise answer_mod.AnswerError(400, "empty question")
                _link, client = _answer_choice(req)
                tools = DatasetTools(config.paths().datasets_dir,
                                     semantic=_agent_semantic)
                excerpts = []
                if req.auto:
                    datasets = []
                    for row in _all_datasets():
                        indexed = bool(row.get("indexes"))
                        datasets.append((row["id"], "%s (%d chunks%s)" % (
                            row["name"], row.get("chunks") or 0,
                            "" if indexed else ", no index yet")))
                    messages = build_auto_messages(
                        question, req.dataset,
                        datasets, followup=bool(req.history_id))
                else:
                    results = _seed_results(req)
                    excerpts = _seed_excerpts(req, results)
                    messages = build_agent_messages(question, excerpts)
                seed = {"refs": req.refs or [], "auto": bool(req.auto),
                        "excerpts": _seed_summary(excerpts)}
                recorder = _begin_run(req, "auto" if req.auto else "research",
                                      question, client.model, seed)
                if recorder is not None:
                    tools = tools.with_search_log(
                        lambda log: recorder.add_search(log))
                budget = AgentBudget.from_env()
                if req.max_steps:
                    budget.max_steps = max(1, min(int(req.max_steps), 12))
                ledger = Ledger(
                    [{**excerpt, "chunk_file": excerpt.get("id")}
                     for excerpt in excerpts],
                    link_for=_agent_link)
                session = AgentSession(
                    chat=lambda msgs, schemas, choice: client.stream(
                        msgs, tools=schemas, tool_choice=choice,
                        temperature=ASK_TEMPERATURE, max_tokens=ASK_MAX_TOKENS),
                    tools=tools, budget=budget, dataset=req.dataset,
                    ledger=ledger, model=client.model)
                context = _context_event(
                    req, "auto" if req.auto else "research", excerpts,
                    auto=bool(req.auto), extra=_run_ids(recorder))
                put(context)
                _record_event(recorder, context)
                t0 = time.perf_counter()
                first_sec = None
                answer_parts = []
                for event in session.run(messages):
                    if event.get("type") == "text":
                        if first_sec is None:
                            first_sec = time.perf_counter() - t0
                        answer_parts.append(event.get("text") or "")
                    elif event.get("type") == "text_retract":
                        answer_parts = []
                    if event.get("type") == "done":
                        event.update({
                            "model": client.model,
                            "dataset": req.dataset,
                            "mode": "auto" if req.auto else "research",
                            "answer": "".join(answer_parts),
                            "steps": session.steps,
                            "tool_calls": session.tool_calls,
                            "excerpts": len(excerpts),
                            "ledger": _public_ledger(ledger.rows),
                            "images_sent": 0,
                            "first_token_sec": (round(first_sec, 4)
                                                if first_sec is not None else None),
                            "total_sec": round(time.perf_counter() - t0, 4),
                        })
                        event.update(_run_ids(recorder))
                    put(event)
                    _record_event(recorder, event)
                    if event.get("type") == "done":
                        out["done"] = event
                _finish_run(recorder, "done")
            except answer_mod.AnswerError as exc:
                error = {"type": "error", "status": exc.status, "detail": exc.detail}
                error.update(_run_ids(recorder))
                put(error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": exc.status, "detail": exc.detail})
            except HTTPException as exc:
                error = {"type": "error", "status": exc.status_code,
                         "detail": exc.detail}
                error.update(_run_ids(recorder))
                put(error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": exc.status_code, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                LOG.exception("agent failed")
                error = {"type": "error", "status": 500, "detail": _http_detail(exc)}
                error.update(_run_ids(recorder))
                put(error)
                out["error"] = error
                _finish_run(recorder, "error",
                            {"status": 500, "detail": _http_detail(exc)})

        return _ndjson_stream(worker)

    # --- history ----------------------------------------------------------
    @app.get("/api/history")
    def history_list():
        hist = history()
        if hist is None:
            return {"entries": [], "max_entries": 0}
        return {"entries": hist.list_meta(), "max_entries": hist.max_entries}

    @app.get("/api/history/{history_id}")
    def history_get(history_id: str, view: int = 1):
        hist = history()
        entry = hist.get(history_id, view=bool(view)) if hist else None
        if entry is None:
            raise HTTPException(404, "no such history entry")
        return entry

    @app.post("/api/history/clear")
    def history_clear():
        hist = history()
        if hist is None:
            return {"ok": True, "cleared": 0, "kept_running": 0, "entries": []}
        result = hist.clear()
        result["entries"] = hist.list_meta()
        return result

    # --- settings ---------------------------------------------------------
    @app.get("/api/settings")
    def settings_get():
        data = config.load_settings()
        data["version"] = __version__
        return data

    @app.put("/api/settings")
    def settings_put(payload: Optional[dict] = None):
        try:
            data = config.save_settings(payload or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        data["version"] = __version__
        return data

    return app


app = create_app()
