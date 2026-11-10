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
import mimetypes
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

from . import (__version__, answer as answer_mod, config, contracts, ingest,
               links, localmodels, peelfeed, prompts, security, workspace)
from .engine import IndexStore

LOG = logging.getLogger("peekseed.app")

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"

# Upload limits live in peekseed.security (per-file, file count and total size).
MAX_UPLOAD_BYTES = security.MAX_UPLOAD_BYTES
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
    """Drop cached stores (tests point PEEKSEED_HOME at a temp dir).

    Job records deliberately survive this: they live on disk and are the API's
    source of truth; only the in-process caches are dropped.
    """
    global _STORE, _HISTORY
    with _STORE_LOCK:
        _STORE = None
    with _HISTORY_LOCK:
        _HISTORY = None
    with _ACTIVE_EXTRACTIONS_LOCK:
        _ACTIVE_EXTRACTIONS.clear()
    config.reset_cache()
    links.reset_cache()
    localmodels.reset_cache()
    peelfeed.reset_cache()


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
    key = links.api_key(link)
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _join(base, path):
    return (base or "").rstrip("/") + path


def _link_url_problem(payload):
    """The SSRF rejection for a submitted ``base_url``, or None.

    Model links are user-supplied URLs the server will call, so they get the
    same guard as any outbound request: cloud metadata endpoints, link-local
    addresses and non-http(s) schemes are refused. Loopback, RFC1918 and tailnet
    addresses stay allowed (local-first model servers). A missing/invalid URL is
    left to :func:`peekseed.links.create`/:func:`peekseed.links.update`, which
    already report it precisely.
    """
    if not isinstance(payload, dict) or "base_url" not in payload:
        return None
    url = payload.get("base_url")
    if not isinstance(url, str) or not url.strip():
        return None
    return security.validate_url(url)


def _embed_chunk_sizes():
    """Chunk sizes of every embed link that has them (peelfeed builds one set per size)."""
    try:
        sizes = [link["chunk"] for link in links.links("embed") if link.get("chunk")]
    except links.LinkConfigError:
        return None
    return sizes or None


def _space_indexed(space):
    """Dataset ids that have an index directory for one embedding space."""
    root = config.paths().datasets_dir
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return []
    return [name for name in names if DATASET_ID_RE.match(name)
            and (root / name / "index" / space).is_dir()]


def _existing_link(link_id):
    try:
        link = links.get(link_id)
    except links.LinkConfigError as exc:
        _link_config_500(exc)
    if link is None:
        raise HTTPException(404, "no link with id '%s'" % link_id)
    return link


def _guard_chunk_change(link_id, payload):
    """409 when an edit changes the chunk sizes of an embed link that has indexes.

    Those indexes were embedded from the old chunk set; they must be deleted
    (``DELETE /api/links/<id>/indexes``) before the sizes may change.
    """
    if not isinstance(payload, dict) or "chunk" not in payload:
        return
    try:
        existing = links.get(link_id)
    except links.LinkConfigError:
        return
    if existing is None or existing.get("role") != "embed":
        return
    proposed, errors = links.validate(payload, existing=existing)
    if errors or proposed.get("chunk") == existing.get("chunk"):
        return
    indexed = _space_indexed(links.embed_space_key(existing))
    if indexed:
        raise HTTPException(
            409, "chunk_sizes_locked: link '%s' has indexes built with chunk set %s "
                 "(datasets: %s); delete them first (DELETE /api/links/%s/indexes)."
                 % (link_id, links.chunk_set_name(existing.get("chunk")) or "none",
                    ", ".join(indexed), link_id))


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


def _result_row(rank, dataset, index, meta, score):
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
        # CP3 wire §2.7/§2.9: the dataset-level corrections-conflict banner data
        # (empty when no source is in the ``corrections_conflict`` state).
        "corrections_conflict": _dataset_conflicts(
            config.paths().datasets_dir / dataset_id),
    }


def _dataset_conflicts(directory):
    """Sources in ``corrections_conflict`` with their conflicting-correction data.

    The detail comes from peelfeed's ``status`` payload (persisted on the source
    extraction block); it lists the conflicting corrections and their source
    batches so the dataset/review banner can link back to them.
    """
    try:
        raw = contracts.read_json(Path(directory) / "dataset.json")
    except contracts.ContractError:
        return []
    if not isinstance(raw, dict):
        return []
    rows = []
    for entry in raw.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        if contracts.extraction_state(entry) != "corrections_conflict":
            continue
        block = entry.get("extraction") if isinstance(
            entry.get("extraction"), dict) else {}
        rows.append({
            "sha256": str(entry.get("sha256") or ""),
            "name": str(entry.get("name") or ""),
            "state": "corrections_conflict",
            "corrections_conflict": block.get("corrections_conflict"),
            "error": block.get("error"),
        })
    return rows


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


def _workspace_report():
    """The workspace this server actually serves, with absolute paths.

    Resolution is taken from :func:`peekseed.config.paths` (what the server
    opened), never re-derived from the process cwd, so the API cannot describe a
    different workspace than the one whose dataset directories just answered.
    """
    paths = config.ensure()
    return workspace.status_for(paths)


def _indexed_shas(dataset_dir):
    """Source sha256 values present in any built index of this dataset."""
    found = set()
    try:
        indexes = ingest.dataset_indexes(dataset_dir)
    except Exception:  # noqa: BLE001 - a broken index must not hide sources
        return found
    for info in indexes:
        for source in info.get("sources") or []:
            if isinstance(source, dict) and source.get("sha256"):
                found.add(str(source["sha256"]).lower())
    return found


def _source_rows(dataset_dir):
    """Per-source provenance rows from ``dataset.json`` plus file presence.

    ``GET /api/datasets/{id}/sources`` is the honest counterpart of the upload
    response: it says which files are stored, their extraction state (peelfeed
    peel identity) and whether the stored copy is still on disk.
    """
    indexed = _indexed_shas(dataset_dir)
    try:
        raw = contracts.read_json(Path(dataset_dir) / "dataset.json")
    except contracts.ContractError as exc:
        raise HTTPException(500, _http_detail(exc))
    if not isinstance(raw, dict):
        raise HTTPException(500, "dataset.json is not readable")
    rows = []
    for entry in raw.get("sources") or []:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        stored = ingest.stored_source_path(Path(dataset_dir), entry)
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict):
            extraction = contracts.extraction_default()
        rows.append({
            "name": name,
            "path": str(stored) if stored is not None else "",
            "sha256": str(entry.get("sha256") or ""),
            "bytes": entry.get("bytes"),
            "pages": entry.get("pages"),
            "extractor": str(entry.get("extractor") or ""),
            "status": str(entry.get("status") or ""),
            "extraction": dict(extraction),
            "indexed": str(entry.get("sha256") or "").lower() in indexed,
            "present": stored is not None,
        })
    return rows


def _release_added(entries):
    """Release shared sources added to a dataset that is being rolled back."""
    for entry in entries or []:
        sha = str((entry or {}).get("sha256") or "")
        if sha:
            try:
                ingest.release_shared_source(sha)
            except Exception:  # noqa: BLE001 - rollback must not mask the error
                pass


#: Extraction states that are authoritative. A late poller (or a stale status
#: read) must never downgrade one of these back to queued/running, and a
#: request-path refresh may persist a terminal block when peelfeed already
#: reports it (restart recovery) -- never the reverse.
TERMINAL_EXTRACTION_STATES = ("extracted", "failed", "stale",
                              "corrections_conflict")

#: Serialises ``dataset.json`` extraction writes in this process: the worker's
#: authoritative final block and the status pollers can race on one source.
_EXTRACTION_WRITE_LOCK = threading.RLock()

#: In-process registry of active extraction workers, keyed by
#: ``(dataset dir, sha256)`` (CP5.1 H9 / wire §10.9). An entry is registered
#: *before* the worker thread starts and removed only after
#: :func:`_finish_extraction` wrote the authoritative terminal block. While it
#: exists, :func:`_refresh_peel_states` skips that source entirely: a re-ingest
#: of an already-ready peel must not have the previous peel's terminal state
#: persisted as ``extracted`` while the new ingest is still running (the
#: 2026-11-10 deploy smoke: index pinned the old build, then failed with
#: ``NeedsReindex`` after the ingest published).
_ACTIVE_EXTRACTIONS = {}
_ACTIVE_EXTRACTIONS_LOCK = threading.RLock()


def _extraction_key(directory, sha):
    """The registry key: ``(resolved dataset dir, lowercase sha256)``."""
    return (str(Path(directory).resolve()), str(sha or "").lower())


def _register_extraction(directory, sha):
    """Mark a source's extraction worker active (before the thread starts)."""
    with _ACTIVE_EXTRACTIONS_LOCK:
        _ACTIVE_EXTRACTIONS[_extraction_key(directory, sha)] = True


def _clear_extraction(directory, sha):
    """Clear the active marker (only after the authoritative terminal write)."""
    with _ACTIVE_EXTRACTIONS_LOCK:
        _ACTIVE_EXTRACTIONS.pop(_extraction_key(directory, sha), None)


def _extraction_active(directory, sha):
    """Whether a worker is currently running for this source."""
    with _ACTIVE_EXTRACTIONS_LOCK:
        return _extraction_key(directory, sha) in _ACTIVE_EXTRACTIONS


def _persist_extraction(directory, sha, block, *, terminal=False):
    """Merge one extraction block into ``dataset.json`` (best-effort).

    ``terminal=True`` is the authoritative write after the CLI command/worker
    finished. Non-terminal poll updates never overwrite an already terminal
    block: with ``PEEKSEED_PEELFEED_POLL=0.05`` a poller can otherwise persist a
    stale ``running`` block *after* the worker wrote ``extracted``, leaving the
    source stuck and the UI polling forever (the CP2 flake hardened in CP3).
    Returns True when the block was written.
    """
    state = str((block or {}).get("state") or "")
    with _EXTRACTION_WRITE_LOCK:
        if not terminal and state not in TERMINAL_EXTRACTION_STATES:
            try:
                entry = ingest.find_source(Path(directory), sha)
            except ingest.IngestError:
                entry = None
            current = contracts.extraction_state(entry) if entry else "not_extracted"
            if current in TERMINAL_EXTRACTION_STATES:
                return False
        try:
            ingest.update_source(directory, sha, status=block.get("state"),
                                 extraction=block, pages=block.get("pages"))
        except ingest.IngestError:
            return False
    return True


def _reconcile_datasets(paths):
    """Best-effort shared-dataset refresh; a read must never fail on a peel."""
    try:
        return ingest.reconcile_all_datasets(paths)
    except Exception as exc:  # noqa: BLE001 - reconcile is a best-effort side path
        LOG.warning("could not reconcile dataset sources: %s", _http_detail(exc))
        return {}


def _reconcile_after_peel_change(peel_id):
    """Refresh every dataset referencing ``peel_id`` after publish/discard/rollback."""
    try:
        return ingest.reconcile_peel(peel_id)
    except Exception as exc:  # noqa: BLE001 - a successful publish stays successful
        LOG.warning("could not reconcile datasets after peel %s changed: %s",
                    peel_id, _http_detail(exc))
        return {}


async def _store_uploads(name, note, files):
    """Validate, stream and store uploaded PDFs, returning the new dataset dict.

    Order matters for safety:

    * the filename is sanitised *before* any path is built (a zip-slip name like
      ``../../evil.pdf`` becomes ``evil.pdf`` and can never escape);
    * the extension is checked before the body is read;
    * each file is streamed to the uploads staging area in 64 KiB chunks with the
      per-file and running-total limits enforced as bytes arrive, and its first
      chunk must carry the ``%PDF-`` magic;
    * the dataset directory is created only after *every* file passed validation,
      so a rejected upload leaves no half-created dataset behind.

    Staging is removed in all cases. ``UploadError`` maps to its own status and
    ``IngestError`` to a short 400; no path or secret is echoed.
    """
    if not (name or "").strip():
        raise HTTPException(400, "a dataset name is required")
    if not files:
        raise HTTPException(400, "at least one PDF is required")
    if len(files) > security.MAX_UPLOAD_FILES:
        raise HTTPException(413, "a dataset accepts at most %d files"
                            % security.MAX_UPLOAD_FILES)
    staging = config.ensure().uploads_dir / ("upload-" + uuid.uuid4().hex)
    dataset = None
    added = []
    try:
        staging.mkdir(parents=True, exist_ok=True)
        security.harden_dir(staging)
        prepared = []
        total = 0
        for number, upload in enumerate(files):
            raw_name = getattr(upload, "filename", "") or ""
            filename = ingest.sanitize_filename(raw_name)
            if not filename.lower().endswith(".pdf"):
                raise HTTPException(400, "%r is not a PDF" % filename)
            part_dir = staging / ("%03d" % number)
            part_dir.mkdir(parents=True, exist_ok=True)
            security.harden_dir(part_dir)
            temp = part_dir / filename
            try:
                written = await security.stream_upload(
                    upload, temp, total_used=total, expect_prefix=b"%PDF-")
            except security.UploadInvalid:
                raise HTTPException(400, "%r is not a valid PDF" % filename)
            except security.UploadLimitExceeded as exc:
                raise HTTPException(exc.status, exc.detail)
            total += written
            prepared.append(temp)
        dataset = ingest.create_dataset(config.paths(), name, note=note)
        security.harden_dataset_dir(dataset.get("dir"))
        added = []
        for temp in prepared:
            added.append(ingest.add_source(dataset, temp))
        return dataset
    except HTTPException:
        if dataset is not None and dataset.get("dir"):
            shutil.rmtree(dataset["dir"], ignore_errors=True)
        # Release only after the dataset is gone: its dataset.json still lists
        # the sha, so releasing first would keep the shared file.
        _release_added(added)
        raise
    except Exception as exc:  # noqa: BLE001 - ingestion errors are user-facing
        if dataset is not None and dataset.get("dir"):
            shutil.rmtree(dataset["dir"], ignore_errors=True)
        _release_added(added)
        raise HTTPException(400, _http_detail(exc))
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)


# ---------------------------------------------------------------------------
# model links: probing
# ---------------------------------------------------------------------------

def _probe_link(link):
    role = link.get("role")
    base = link.get("base_url") or ""
    try:
        if role == "answer":
            client = answer_mod.ChatClient(base, links.api_key(link),
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
                    # The provider's body is never echoed: it can carry keys or
                    # account details, and the status is enough to act on.
                    return {"ok": False,
                            "detail": "the embed endpoint returned HTTP %d"
                            % response.status_code}
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
                            "detail": "the rerank endpoint returned HTTP %d"
                            % response.status_code}
                scores = response.json().get("scores")
                ok = isinstance(scores, list) and len(scores) == 2
                return {"ok": ok, "detail": "2 scores" if ok else "unexpected response"}
        return {"ok": False, "detail": "unknown link role"}
    except Exception as exc:  # noqa: BLE001 - unreachable is a normal state
        # Type only: a provider/HTTP client message could contain a response
        # body, an API key or a machine path, and this is a UI status line.
        return {"ok": False, "detail": "the link is not reachable (%s)"
                % type(exc).__name__}


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


def _probe_check(link, index):
    """Fail closed when the live embedder no longer matches this index space.

    The probe vector was stored when the index was built. Re-embedding the same
    fixed probe text and comparing (cosine >= 0.999) is what catches a server
    that still reports the same model name but returns different vectors --
    comparing the query vector against such an index would silently rank noise.
    """
    try:
        if index.generation_dir:
            cosine = ingest.verify_probe_for_generation(
                link, index.generation_dir, index.space)
        else:  # an index constructed directly has no recorded generation dir
            cosine = ingest.verify_probe(
                link, config.paths().datasets_dir / index.dataset, index.space)
    except ingest.ProbeMismatch as exc:
        raise HTTPException(409, str(exc)) from None
    except ingest.NeedsReindex as exc:
        raise HTTPException(
            409,
            "%s (rebuild the index before searching so the embedder can be "
            "verified)" % exc) from None
    return cosine


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
    _probe_check(link, index)
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
    # Resolve (and probe-verify) the space before embedding the query: a query
    # must not be embedded through an embedder that no longer matches the index.
    t_probe = time.perf_counter()
    index = _resolve_index(dataset, link, req.space)
    probe_sec = time.perf_counter() - t_probe

    stage("embed", "start")
    t_embed = time.perf_counter()
    qvec = _embed_query(link, query)
    embed_sec = time.perf_counter() - t_embed
    stage("embed", "done", embed_sec)

    topk = max(1, min(int(req.topk or 10), 50))
    try:
        order, reranked, scores, timings = index.search(
            qvec, query, topk,
            rerank_pool=RERANK_POOL, rerank_batch=RERANK_BATCH,
            on_stage=on_stage, rerank=bool(req.rerank),
            rerank_fn=_rerank_callable())
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    results = [_result_row(rank, dataset, index, record, score)
               for rank, (record, score) in enumerate(
                   store().reading_hits(dataset, index, order, scores, topk),
                   start=1)]
    timings = dict(timings or {})
    timings["probe_sec"] = round(probe_sec, 4)
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
                                   links.api_key(link), model)
    if not model:
        models = [m for m in client.models() if m]
        if not models:
            raise answer_mod.AnswerError(
                409, "the answer link advertises no models; set one in Settings")
        model = models[0]
        client = answer_mod.ChatClient(link.get("base_url") or "",
                                       links.api_key(link), model)
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

def _agent_probe_gate(req):
    """409 before a research run if the embedder no longer matches its spaces.

    The agent's search tool degrades to keyword ranking on any failure (a
    mid-loop retrieval error must not end the session), so a probe mismatch
    discovered lazily there would be silent. Verify up front instead, with one
    probe embedding per dataset the active embed link has an index for.
    """
    try:
        link = links.active("embed")
    except links.LinkConfigError as exc:
        _link_config_500(exc)
    if not link:
        return
    index_store = store()
    wanted_space = links.embed_space_key(link)
    model = link.get("model") or ""
    targets = []
    if req.dataset:
        index = (index_store.get(req.dataset, wanted_space) or
                 index_store.find(req.dataset, link.get("id"), model))
        if index is not None:
            targets.append(index)
    else:
        for index in index_store.all().values():
            info = index.info or {}
            if (index.space == wanted_space or
                    (info.get("link_id") == link.get("id") and
                     (info.get("model") or "") == model)):
                targets.append(index)
    for index in targets:
        _probe_check(link, index)


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
        top = max(1, min(int(k or 5), 10))
        order, _reranked, scores, _timings = index.search(
            qvec, query, top,
            rerank_pool=0, rerank_batch=RERANK_BATCH, rerank=False)
        return store().reading_hits(dataset, index, order, scores, top)
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
        security.harden_data_root(config.paths())
        # A process death leaves "running" job records behind; marking them
        # "interrupted" at startup is what makes the Resume button honest. The
        # records themselves are the API's source of truth (not memory).
        try:
            interrupted = ingest.mark_interrupted_jobs(config.paths().jobs_dir)
            if interrupted:
                LOG.warning("marked %d interrupted job(s): %s",
                            len(interrupted), ", ".join(interrupted))
        except OSError as exc:
            LOG.warning("could not reconcile job records: %s", exc)
        try:
            links.load()
        except links.LinkConfigError as exc:
            LOG.warning("model links config is not readable: %s", exc)
        reload_store()
        # CP3 wire §2.7: reconcile every dataset's peel identity at startup, so a
        # publish that happened while the service was down is reflected before
        # the first read (only a changed conversion_id makes an index stale).
        _reconcile_datasets(config.paths())
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

    # --- workspace / collections -----------------------------------------
    @app.get("/api/workspace")
    def workspace_get():
        """Absolute data paths and per-dataset state."""
        return _workspace_report()

    @app.get("/api/collections")
    def collections_get():
        report = _workspace_report()
        rows = []
        for name in sorted(report["collections"]):
            row = dict(report["collections"][name])
            row["id"] = name
            rows.append(row)
        return {"collections": rows,
                "counts": report.get("counts")}

    # --- datasets ---------------------------------------------------------
    @app.get("/api/datasets")
    def datasets_list():
        # Cheap reconcile on reads (wire §2.7): external CLI publishes are picked
        # up without a hard failure; broken peels are left untouched.
        _reconcile_datasets(config.paths())
        return {"datasets": _all_datasets()}

    # --- peelfeed (the extraction engine) ---------------------------------
    def _peelfeed_handshake():
        try:
            return peelfeed.handshake()
        except peelfeed.PeelfeedConfigError as exc:
            return {"available": False, "configured": False, "error": str(exc)}

    def _require_peelfeed():
        """503 with a readable reason when extraction is unavailable."""
        hs = _peelfeed_handshake()
        if hs.get("available"):
            return hs
        raise HTTPException(
            503,
            "extraction is unavailable: %s. Configure PEEKSEED_PEELFEED_CLI (the "
            "peelfeed console script) or PEEKSEED_PEELFEED_PYTHON (its venv "
            "interpreter) and restart peekseed; uploads and file management keep "
            "working." % (hs.get("error") or "peelfeed is not configured"),
        )

    def _require_review_schema():
        """Handshake plus the structured review schema gate (contract §2/§4).

        Review state lives in peelfeed and is only meaningful when its
        ``capabilities`` advertise ``review_schema: 2``. A mismatch is a clear
        409 (never a silent fallback to the removed flat review payload).
        """
        hs = _require_peelfeed()
        schema = hs.get("review_schema")
        if schema != peelfeed.REVIEW_SCHEMA:
            raise HTTPException(
                409,
                "peelfeed review schema is %r but this build requires %d; "
                "upgrade peelfeed to a CP1-capable version"
                % (schema, peelfeed.REVIEW_SCHEMA),
            )
        return hs

    def _call_peelfeed(what, call):
        """Run one peelfeed call and map its typed error to HTTP (contract §4).

        Allowed passthrough statuses: 400 (validation/ocr_config_invalid), 404
        (not found), 409 (conflict/stale/review_schema/not_ready), 423 (busy)
        and 503 (ocr_unavailable); every other engine error is a peelfeed
        failure (502).
        """
        try:
            return call()
        except peelfeed.PeelfeedUnavailable as exc:
            raise HTTPException(503, "extraction is unavailable: %s" % exc.detail)
        except peelfeed.PeelfeedError as exc:
            status = exc.status if exc.status in (400, 404, 409, 423, 503) else 502
            raise HTTPException(status, "peelfeed %s failed: %s"
                                % (what, exc.detail))

    def _review_plan_request(body):
        """Validate ``POST …/review/plan`` and return ``(action, cli_request)``.

        Shape per the CP2 wire contract §2.6/§4.1:
        ``{base_plan_version, action: select|confirm|lock|unlock|defer|accept|
        clear, entry_id?, entry?, item_ids?, reason_code?, reason?}``.
        ``select`` wraps the selection in ``entry``; ``defer`` carries
        ``item_id``/``reason_code``; ``accept`` is all-or-nothing over a
        non-empty, unique ``item_ids`` list and accepts the CP5.1 additive
        ``sources:{item_id:"rule"|"vlm"}`` map (strict body, wire §10.5);
        ``clear`` names one ``entry_id``.
        """
        if not isinstance(body, dict):
            raise HTTPException(400, "a review plan request must be a JSON object")
        action = str(body.get("action") or "").strip().lower()
        if action not in peelfeed.PLAN_ACTIONS or action in ("show", "patch",
                                                            "propagate"):
            # CP5: the targeted plan patch has its own frozen route
            # (POST …/review/plan/patches); CP6: the cluster-level propagation
            # has its own route (POST …/review/plan/propagate). Neither is an
            # action here.
            raise HTTPException(
                400, "action must be one of select, confirm, lock, unlock, "
                     "defer, accept, clear")
        base = body.get("base_plan_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            raise HTTPException(
                400, "base_plan_version is required and must be an integer >= 0")
        if action == "accept":
            # CP5.1 H5 (wire §10.5): the accept body is strict. Optional
            # ``sources`` maps requested item ids to the adoption source; any
            # other field, a non-object ``sources``, an unknown item key or a
            # value other than "rule"/"vlm" is a 400.
            unknown = sorted(set(body) - {"action", "base_plan_version",
                                          "item_ids", "sources"})
            if unknown:
                raise HTTPException(
                    400, "unknown plan accept field(s): %s"
                         % ", ".join(unknown))
            item_ids = body.get("item_ids")
            if not isinstance(item_ids, list) or not item_ids:
                raise HTTPException(
                    400, "accept needs a non-empty item_ids list")
            cleaned = []
            seen = set()
            for raw in item_ids:
                if not isinstance(raw, str):
                    raise HTTPException(
                        400, "item_ids must contain only strings")
                item_id = raw.strip()
                if not item_id:
                    raise HTTPException(
                        400, "item_ids must not contain empty ids")
                if item_id in seen:
                    raise HTTPException(
                        400, "item_ids must be unique (duplicate %s)" % item_id)
                seen.add(item_id)
                cleaned.append(item_id)
            request = {"base_plan_version": base, "item_ids": cleaned}
            sources = body.get("sources")
            if sources is not None:
                if not isinstance(sources, dict):
                    raise HTTPException(
                        400, "sources must be an object mapping requested "
                             "item ids to 'rule' or 'vlm'")
                cleaned_sources = {}
                for raw_key, raw_value in sources.items():
                    item_id = str(raw_key)
                    if item_id not in seen:
                        raise HTTPException(
                            400, "sources keys must be requested item ids "
                                 "(unknown: %s)" % item_id)
                    if raw_value not in ("rule", "vlm"):
                        raise HTTPException(
                            400, "sources values must be 'rule' or 'vlm' "
                                 "(item %s)" % item_id)
                    cleaned_sources[item_id] = raw_value
                if cleaned_sources:
                    request["sources"] = cleaned_sources
            return action, request
        if action == "clear":
            entry_id = str(body.get("entry_id") or "").strip()
            if not entry_id:
                raise HTTPException(400, "clear needs entry_id")
            return action, {"base_plan_version": base, "entry_id": entry_id}
        if action == "select":
            entry = body.get("entry")
            if not isinstance(entry, dict):
                raise HTTPException(
                    400, "select needs an entry object "
                         "{item_id, strategy{id,version}, params?, notes?}")
            item_id = str(entry.get("item_id") or "").strip()
            if not item_id:
                raise HTTPException(400, "entry.item_id is required")
            strategy = entry.get("strategy")
            if not isinstance(strategy, dict):
                raise HTTPException(400, "entry.strategy must be an object")
            strategy_id = str(strategy.get("id") or "").strip()
            if not strategy_id:
                raise HTTPException(400, "entry.strategy.id is required")
            version = strategy.get("version")
            if isinstance(version, bool) or not isinstance(version, int):
                raise HTTPException(
                    400, "entry.strategy.version must be an integer")
            params = entry.get("params")
            if params is not None and not isinstance(params, dict):
                raise HTTPException(400, "entry.params must be an object")
            request = {
                "base_plan_version": base,
                "item_id": item_id,
                "strategy": {"id": strategy_id, "version": version},
            }
            if params is not None:
                request["params"] = params
            notes = entry.get("notes")
            if notes is not None:
                request["notes"] = str(notes)
            return action, request
        if action in ("confirm", "lock", "unlock"):
            entry_id = str(body.get("entry_id") or "").strip()
            if not entry_id:
                raise HTTPException(400, "%s needs entry_id" % action)
            return action, {"base_plan_version": base, "entry_id": entry_id}
        item_id = str(body.get("item_id") or "").strip()
        if not item_id:
            raise HTTPException(400, "defer needs item_id")
        reason_code = str(body.get("reason_code") or "").strip().lower()
        if reason_code not in peelfeed.DEFER_REASONS:
            raise HTTPException(
                400, "reason_code must be one of %s"
                     % ", ".join(peelfeed.DEFER_REASONS))
        request = {"base_plan_version": base, "item_id": item_id,
                   "reason_code": reason_code}
        reason = body.get("reason")
        if reason is not None:
            request["reason"] = str(reason)
        return action, request

    # --- CP5 VLM privacy confirmation (wire §1.2/§6.1/§6.2) ----------------
    # Every report/discuss provider call is preceded by an explicit user
    # confirmation. peekseed validates the frozen shape field-by-field, then
    # re-resolves the default answer link and compares provider/link_id/model
    # before any CLI call or job is created. The resolved scope sets are
    # re-checked by peelfeed (wire §1.2/§6.2); peekseed passes them through.
    _CONFIRMATION_KEYS = {"confirmed", "provider", "link_id", "model", "scope"}
    _CONFIRMATION_SCOPE_KEYS = {
        "item_ids", "cluster_ids", "include_crop", "include_original_page",
        "include_adjacent_pages", "include_native", "include_ocr",
    }
    _CONFIRMATION_FLAGS = ("include_crop", "include_original_page",
                           "include_adjacent_pages", "include_native",
                           "include_ocr")

    def _privacy_confirmation_required(message):
        raise HTTPException(400, "privacy_confirmation_required: %s" % message)

    def _review_id_list(value, field):
        """A sorted-unique list of non-empty strings (confirmation scope sets)."""
        if not isinstance(value, list):
            raise HTTPException(400, "%s must be a list of ids" % field)
        cleaned = []
        seen = set()
        for raw in value:
            if not isinstance(raw, str):
                raise HTTPException(400, "%s must contain only strings" % field)
            item = raw.strip()
            if not item:
                raise HTTPException(400, "%s must not contain empty ids" % field)
            if item in seen:
                raise HTTPException(400, "%s must be unique (duplicate %s)"
                                    % (field, item))
            seen.add(item)
            cleaned.append(item)
        if cleaned != sorted(cleaned):
            raise HTTPException(400, "%s must be sorted unique strings" % field)
        return cleaned

    def _review_confirmation(value, *, what):
        """Validate the frozen privacy confirmation object (wire §1.2).

        Every field is required, unknown fields anywhere are a 400 and
        ``confirmed`` must be exactly ``true``. Returns the normalised object
        that is sent to peelfeed as the job payload (never a token).
        """
        if not isinstance(value, dict):
            raise HTTPException(400, "%s needs a confirmation object" % what)
        unknown = sorted(set(value) - _CONFIRMATION_KEYS)
        if unknown:
            raise HTTPException(
                400, "unknown confirmation field(s): %s" % ", ".join(unknown))
        missing = sorted(_CONFIRMATION_KEYS - set(value))
        if missing:
            raise HTTPException(
                400, "confirmation is missing field(s): %s" % ", ".join(missing))
        if value.get("confirmed") is not True:
            _privacy_confirmation_required(
                "confirmation.confirmed must be true; nothing is sent without "
                "an explicit confirmation")
        provider = value.get("provider")
        if provider not in ("answer_link", "env"):
            raise HTTPException(
                400, "confirmation.provider must be 'answer_link' or 'env'")
        link_id = value.get("link_id")
        if not isinstance(link_id, str) or not link_id.strip():
            raise HTTPException(
                400, "confirmation.link_id must be a non-empty string")
        model = value.get("model")
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(
                400, "confirmation.model must be a non-empty string")
        scope = value.get("scope")
        if not isinstance(scope, dict):
            raise HTTPException(400, "confirmation.scope must be an object")
        unknown_scope = sorted(set(scope) - _CONFIRMATION_SCOPE_KEYS)
        if unknown_scope:
            raise HTTPException(
                400, "unknown confirmation.scope field(s): %s"
                     % ", ".join(unknown_scope))
        missing_scope = sorted(_CONFIRMATION_SCOPE_KEYS - set(scope))
        if missing_scope:
            raise HTTPException(
                400, "confirmation.scope is missing field(s): %s"
                     % ", ".join(missing_scope))
        flags = {}
        for flag in _CONFIRMATION_FLAGS:
            if scope.get(flag) is not True and scope.get(flag) is not False:
                raise HTTPException(
                    400, "confirmation.scope.%s must be a boolean" % flag)
            flags[flag] = bool(scope[flag])
        return {
            "confirmed": True,
            "provider": provider,
            "link_id": link_id.strip(),
            "model": model.strip(),
            "scope": {
                "item_ids": _review_id_list(scope.get("item_ids"),
                                            "confirmation.scope.item_ids"),
                "cluster_ids": _review_id_list(scope.get("cluster_ids"),
                                               "confirmation.scope.cluster_ids"),
                **flags,
            },
        }

    def _review_vlm_provider(confirmation):
        """Re-resolve the VLM provider and compare the confirmation.

        Resolution order (design §3.2, Phase 4): peekseed's own
        ``PEELFEED_LLM_BASE/MODEL/TOKEN`` env wins; otherwise the default answer
        link (first enabled). A missing/unusable provider is a 503
        ``vlm_unavailable`` (no job, no CLI call); a provider/link/model
        mismatch is a 400 ``privacy_confirmation_required`` before any data
        could be sent (wire §1.2/§1.3). Returns the public provider descriptor
        (never the token).
        """
        provider = peelfeed.vlm_provider()
        if not provider.get("available"):
            raise HTTPException(
                503, "VLM is unavailable: error [vlm_unavailable] %s"
                     % (provider.get("reason") or "the provider is not configured"))
        if confirmation["provider"] != provider["provider"]:
            _privacy_confirmation_required(
                "the confirmed provider (%s) does not match the resolved "
                "provider (%s); re-confirm the provider shown in the dialog"
                % (confirmation["provider"], provider["provider"]))
        if confirmation["link_id"] != str(provider.get("link_id") or ""):
            _privacy_confirmation_required(
                "the confirmed link_id does not match the resolved provider; "
                "re-confirm the provider/link/model")
        if confirmation["model"] != str(provider.get("model") or ""):
            _privacy_confirmation_required(
                "the confirmed model does not match the resolved provider; "
                "re-confirm the provider/link/model")
        return provider

    def _review_vlm_capabilities(payload):
        """Overlay peekseed-resolved VLM fields on a review payload (CP5 §2.3).

        peelfeed's ``capabilities`` command is a read-only child that never gets
        the provider env (only report/discuss children do), so its
        ``capabilities.vlm`` cannot know about peekseed's env-first provider or
        the default answer link. peekseed therefore reports the truth here,
        preserving any peelfeed-provided probe/budget fields (``vlm_vision``,
        ``vlm_budget_usd``, ``vlm_spent_usd``, ``vlm_calls``,
        ``vlm_vision_checked_at``) and the CP5.1 additive
        ``vlm_priced``/``vlm_max_calls_per_peel`` (wire §10.7): those come from
        the read-only child's env, which now receives the forwarded
        ``PEELFEED_LLM_*`` tuning variables, so peekseed must not overwrite
        them. Additive ``vlm_provider`` names the resolved provider
        (``answer_link``/``env``) for the confirmation dialog.
        """
        if not isinstance(payload, dict):
            return payload
        caps = payload.get("capabilities")
        overlay = dict(caps) if isinstance(caps, dict) else {}
        provider = peelfeed.vlm_provider()
        available = bool(provider.get("available"))
        overlay["vlm"] = available
        overlay["vlm_provider"] = provider.get("provider")
        overlay["vlm_model"] = (str(provider.get("model") or "")
                                if available else None)
        overlay["vlm_link_id"] = (str(provider.get("link_id") or "")
                                  if available else None)
        overlay["vlm_detail"] = None if available else (
            "VLM 不可用：%s" % (provider.get("reason") or "未配置"))
        # The vision probe belongs to peelfeed; keep its value (null = unknown)
        # instead of guessing.
        overlay.setdefault("vlm_vision", None)
        payload["capabilities"] = overlay
        return payload

    def _review_report_filters(body):
        """The optional ``item_ids``/``cluster_ids`` report filters."""
        filters = {}
        for field in ("item_ids", "cluster_ids"):
            value = body.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                raise HTTPException(400, "%s must be a list of ids" % field)
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned:
                filters[field] = cleaned
        return filters

    def _review_report_request(body):
        """Validate ``POST …/review/reports`` (CP5 wire §6.1).

        Rules body is unchanged: ``{item_ids?, cluster_ids?}`` (no ``mode``/
        ``confirmation``). VLM body is ``{mode:"vlm", confirmation:{…},
        item_ids?, cluster_ids?}``; ``mode`` other than ``"vlm"`` is a 400 and
        the confirmation is validated field-by-field here and re-resolved
        against the default answer link by the route before any CLI call.
        Returns ``{mode, confirmation, item_ids, cluster_ids}``.
        """
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "a review report request must be a JSON object")
        unknown = sorted(set(body) - {"mode", "confirmation", "item_ids",
                                      "cluster_ids"})
        if unknown:
            raise HTTPException(400, "unknown review report field(s): %s"
                                % ", ".join(unknown))
        filters = _review_report_filters(body)
        has_mode = "mode" in body
        has_confirmation = "confirmation" in body
        if not has_mode and not has_confirmation:
            return {"mode": "rules", "confirmation": None,
                    "item_ids": filters.get("item_ids"),
                    "cluster_ids": filters.get("cluster_ids")}
        if not has_mode:
            raise HTTPException(
                400, "confirmation is only accepted with mode:\"vlm\"")
        mode = str(body.get("mode") or "").strip().lower()
        if mode != "vlm":
            raise HTTPException(400, "mode must be \"vlm\" (or omitted for a "
                                     "rules-only report)")
        if not has_confirmation:
            raise HTTPException(
                400, "a VLM report needs a confirmation object")
        confirmation = _review_confirmation(
            body.get("confirmation"), what="a VLM report")
        return {"mode": "vlm", "confirmation": confirmation,
                "item_ids": filters.get("item_ids"),
                "cluster_ids": filters.get("cluster_ids")}

    _DISCUSSION_KEYS = {"conversation_id", "scope", "message",
                        "base_plan_version", "confirmation"}
    _DISCUSSION_SCOPE_KEYS = {"item_id", "cluster_id", "peel"}

    def _review_discussion_scope(value):
        """Validate the frozen discussion scope (wire §4.1/§6.2).

        Exactly one of ``item_id``, ``cluster_id`` or ``peel:true``; the scope
        defaults to the current item/cluster in the UI, and full-peel discussion
        is allowed but never executes a batch (peelfeed returns a preview only).
        """
        if not isinstance(value, dict):
            raise HTTPException(400, "a review discussion needs a scope object")
        unknown = sorted(set(value) - _DISCUSSION_SCOPE_KEYS)
        if unknown:
            raise HTTPException(400, "unknown discussion scope field(s): %s"
                                % ", ".join(unknown))
        selectors = 0
        cleaned = {}
        item_id = value.get("item_id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id.strip():
                raise HTTPException(
                    400, "scope.item_id must be a non-empty string")
            cleaned["item_id"] = item_id.strip()
            selectors += 1
        cluster_id = value.get("cluster_id")
        if cluster_id is not None:
            if not isinstance(cluster_id, str) or not cluster_id.strip():
                raise HTTPException(
                    400, "scope.cluster_id must be a non-empty string")
            cleaned["cluster_id"] = cluster_id.strip()
            selectors += 1
        peel = value.get("peel")
        if peel is not None:
            if peel is not True:
                raise HTTPException(400, "scope.peel must be true when present")
            cleaned["peel"] = True
            selectors += 1
        if selectors != 1:
            raise HTTPException(
                400, "a discussion scope needs exactly one of item_id, "
                     "cluster_id or peel:true")
        return cleaned

    def _review_discussion_request(body):
        """Validate ``POST …/review/discussions`` (CP5 wire §6.2).

        Body is exactly ``{conversation_id?, scope:{item_id?|cluster_id?|
        peel:true}, message, base_plan_version, confirmation:{…}}``; unknown
        fields are a 400. ``message`` is non-empty (≤ 4000 chars),
        ``base_plan_version`` is an int ≥ 0, the confirmation is validated and
        compared against the default answer link before the CLI runs. Returns
        the frozen peelfeed stdin request.
        """
        if not isinstance(body, dict):
            raise HTTPException(
                400, "a review discussion request must be a JSON object")
        unknown = sorted(set(body) - _DISCUSSION_KEYS)
        if unknown:
            raise HTTPException(400, "unknown review discussion field(s): %s"
                                % ", ".join(unknown))
        missing = sorted({"scope", "message", "base_plan_version",
                          "confirmation"} - set(body))
        if missing:
            raise HTTPException(400, "a review discussion is missing field(s): %s"
                                % ", ".join(missing))
        scope = _review_discussion_scope(body.get("scope"))
        message = body.get("message")
        if not isinstance(message, str):
            raise HTTPException(400, "message must be a non-empty string")
        message = message.strip()
        if not message:
            raise HTTPException(400, "message must be a non-empty string")
        if len(message) > 4000:
            raise HTTPException(400, "message is longer than 4000 characters")
        base = body.get("base_plan_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            raise HTTPException(
                400, "base_plan_version is required and must be an integer >= 0")
        conversation_id = body.get("conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise HTTPException(
                    400, "conversation_id must be a non-empty string")
            conversation_id = conversation_id.strip()
        confirmation = _review_confirmation(
            body.get("confirmation"), what="a review discussion")
        return {
            "conversation_id": conversation_id,
            "scope": scope,
            "message": message,
            "base_plan_version": base,
            "confirmation": confirmation,
        }

    _PLAN_PATCH_KEYS = {"base_plan_version", "patch", "source",
                        "conversation_id", "turn"}

    def _review_plan_patch_request(body):
        """Validate ``POST …/review/plan/patches`` (CP5 wire §6.3 / CP5.1 §10.5).

        Body is ``{base_plan_version, patch, source:"vlm|user",
        conversation_id?, turn?}``; unknown fields are a 400. ``turn`` (the
        assistant turn number, int ≥ 1) is required with ``source:"vlm"`` --
        peelfeed compares the submitted patch with that archived turn and
        records the actual source/origin (identical → ``vlm``, edited →
        ``user``). peekseed checks the envelope only -- the patch content
        (scope/members/exclusions/changes) is validated all-or-nothing by
        peelfeed (wire §5), which returns ``validation`` 400 / ``conflict`` 409
        / ``not_found`` 404. The route is synchronous and never confirms or
        applies anything.
        """
        if not isinstance(body, dict):
            raise HTTPException(
                400, "a review plan patch request must be a JSON object")
        unknown = sorted(set(body) - _PLAN_PATCH_KEYS)
        if unknown:
            raise HTTPException(400, "unknown review plan patch field(s): %s"
                                % ", ".join(unknown))
        missing = sorted({"base_plan_version", "patch", "source"} - set(body))
        if missing:
            raise HTTPException(
                400, "a review plan patch is missing field(s): %s"
                     % ", ".join(missing))
        base = body.get("base_plan_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            raise HTTPException(
                400, "base_plan_version is required and must be an integer >= 0")
        patch = body.get("patch")
        if not isinstance(patch, dict):
            raise HTTPException(400, "patch must be a JSON object")
        source = str(body.get("source") or "").strip().lower()
        if source not in ("vlm", "user"):
            raise HTTPException(400, "source must be 'vlm' or 'user'")
        conversation_id = body.get("conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise HTTPException(
                    400, "conversation_id must be a non-empty string")
            conversation_id = conversation_id.strip()
        turn = body.get("turn")
        if turn is not None:
            if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
                raise HTTPException(
                    400, "turn must be an integer >= 1 (the assistant turn "
                         "number)")
        if source == "vlm":
            # CP5.1 H5 (wire §10.5): a VLM-sourced patch must name the archived
            # conversation turn it was produced by.
            if conversation_id is None:
                raise HTTPException(
                    400, "source 'vlm' requires conversation_id")
            if turn is None:
                raise HTTPException(400, "source 'vlm' requires turn")
        request = {
            "base_plan_version": base,
            "patch": patch,
            "source": source,
        }
        if conversation_id is not None:
            request["conversation_id"] = conversation_id
        if turn is not None:
            request["turn"] = turn
        return request

    _PROPAGATE_KEYS = {"base_plan_version", "scope", "strategy", "params",
                       "source_item_id", "exclusions", "overrides", "dry_run",
                       "operator"}
    _PROPAGATE_SCOPE_KEYS = {"kind", "item_id", "cluster_id", "peel"}

    def _review_propagate_request(body):
        """Validate ``POST …/review/plan/propagate`` (CP6 wire §1.1/§2).

        Body is exactly the frozen ``plan propagate`` request:
        ``{base_plan_version, scope:{kind:"item|cluster|peel", item_id?,
        cluster_id?, peel?}, strategy:{id,version}, params, source_item_id?,
        exclusions[], overrides[], dry_run, operator?}``. Unknown fields
        (request, scope or strategy) are a 400; ``base_plan_version`` is an
        int >= 0; the scope has exactly one selector matching ``kind``;
        ``strategy.id`` is a non-empty string and ``strategy.version`` an
        int >= 1; ``params`` is an object (its content is validated by peelfeed
        per strategy); ``source_item_id`` is an optional non-empty string;
        ``exclusions`` is a list of non-empty item-id strings; ``overrides``
        (CP6.1 wire §7.2/§7.5) is an optional list of non-empty item-id
        strings, deduplicated in order; ``dry_run`` is a bool (default false);
        ``operator`` is an optional non-empty string (peelfeed defaults it to
        ``"user"``). peekseed only checks the envelope -- member resolution,
        per-member statuses (including ``decided``), applicability and
        validators are all-or-nothing in peelfeed, as are the ``overrides``
        member-set/subset/``exclusions``-overlap checks (CP6 wire §1.2/§1.3,
        CP6.1 §7.2). The request is proxied synchronously with no model call
        and no LLM env.
        """
        if not isinstance(body, dict):
            raise HTTPException(
                400, "a review plan propagate request must be a JSON object")
        unknown = sorted(set(body) - _PROPAGATE_KEYS)
        if unknown:
            raise HTTPException(
                400, "unknown review plan propagate field(s): %s"
                     % ", ".join(unknown))
        missing = sorted({"base_plan_version", "scope", "strategy"} - set(body))
        if missing:
            raise HTTPException(
                400, "a review plan propagate request is missing field(s): %s"
                     % ", ".join(missing))
        base = body.get("base_plan_version")
        if isinstance(base, bool) or not isinstance(base, int) or base < 0:
            raise HTTPException(
                400, "base_plan_version is required and must be an integer >= 0")
        scope = body.get("scope")
        if not isinstance(scope, dict):
            raise HTTPException(400, "scope must be a JSON object")
        unknown_scope = sorted(set(scope) - _PROPAGATE_SCOPE_KEYS)
        if unknown_scope:
            raise HTTPException(
                400, "unknown scope field(s): %s" % ", ".join(unknown_scope))
        kind = str(scope.get("kind") or "").strip().lower()
        if kind not in ("item", "cluster", "peel"):
            raise HTTPException(
                400, "scope.kind must be 'item', 'cluster' or 'peel'")
        cleaned_scope = {"kind": kind, "item_id": None, "cluster_id": None,
                         "peel": None}
        if kind == "item":
            item_id = str(scope.get("item_id") or "").strip()
            if not item_id:
                raise HTTPException(400, "scope.item_id is required for kind "
                                         "'item'")
            if scope.get("cluster_id") is not None:
                raise HTTPException(
                    400, "scope must have exactly one selector "
                         "(item_id and cluster_id are both set)")
            if scope.get("peel") is not None:
                raise HTTPException(
                    400, "scope must have exactly one selector "
                         "(item_id and peel are both set)")
            cleaned_scope["item_id"] = item_id
        elif kind == "cluster":
            cluster_id = str(scope.get("cluster_id") or "").strip()
            if not cluster_id:
                raise HTTPException(400, "scope.cluster_id is required for "
                                         "kind 'cluster'")
            if scope.get("item_id") is not None:
                raise HTTPException(
                    400, "scope must have exactly one selector "
                         "(cluster_id and item_id are both set)")
            if scope.get("peel") is not None:
                raise HTTPException(
                    400, "scope must have exactly one selector "
                         "(cluster_id and peel are both set)")
            cleaned_scope["cluster_id"] = cluster_id
        else:
            if scope.get("peel") is not True:
                raise HTTPException(
                    400, "scope.peel must be true for kind 'peel'")
            if scope.get("item_id") is not None or scope.get(
                    "cluster_id") is not None:
                raise HTTPException(
                    400, "scope must have exactly one selector "
                         "(peel and an item/cluster id are both set)")
            cleaned_scope["peel"] = True
        strategy = body.get("strategy")
        if not isinstance(strategy, dict):
            raise HTTPException(400, "strategy must be a JSON object")
        unknown_strategy = sorted(set(strategy) - {"id", "version"})
        if unknown_strategy:
            raise HTTPException(
                400, "unknown strategy field(s): %s"
                     % ", ".join(unknown_strategy))
        strategy_id = str(strategy.get("id") or "").strip()
        if not strategy_id:
            raise HTTPException(400, "strategy.id is required")
        version = strategy.get("version")
        if isinstance(version, bool) or not isinstance(version, int) \
                or version < 1:
            raise HTTPException(
                400, "strategy.version must be an integer >= 1")
        params = body.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise HTTPException(400, "params must be a JSON object")
        source_item_id = body.get("source_item_id")
        if source_item_id is not None:
            if not isinstance(source_item_id, str) \
                    or not source_item_id.strip():
                raise HTTPException(
                    400, "source_item_id must be a non-empty string")
            source_item_id = source_item_id.strip()
        exclusions = body.get("exclusions", [])
        if exclusions is None:
            exclusions = []
        if not isinstance(exclusions, list):
            raise HTTPException(
                400, "exclusions must be a list of item ids")
        cleaned_exclusions = []
        for raw in exclusions:
            if not isinstance(raw, str):
                raise HTTPException(
                    400, "exclusions must contain only strings")
            item_id = raw.strip()
            if not item_id:
                raise HTTPException(
                    400, "exclusions must not contain empty ids")
            if item_id not in cleaned_exclusions:
                cleaned_exclusions.append(item_id)
        # CP6.1 (wire §7.2/§7.5): optional ``overrides`` = the ``decided``
        # members the user explicitly wants to replace. Envelope/type only:
        # membership, subset and ``exclusions`` overlap are checked
        # all-or-nothing by peelfeed, so peekseed must not invent divergent
        # semantics here.
        overrides = body.get("overrides", [])
        if overrides is None:
            overrides = []
        if not isinstance(overrides, list):
            raise HTTPException(
                400, "overrides must be a list of item ids")
        cleaned_overrides = []
        for raw in overrides:
            if not isinstance(raw, str):
                raise HTTPException(
                    400, "overrides must contain only strings")
            item_id = raw.strip()
            if not item_id:
                raise HTTPException(
                    400, "overrides must not contain empty ids")
            if item_id not in cleaned_overrides:
                cleaned_overrides.append(item_id)
        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise HTTPException(400, "dry_run must be a boolean")
        operator = body.get("operator")
        if operator is not None:
            if not isinstance(operator, str) or not operator.strip():
                raise HTTPException(
                    400, "operator must be a non-empty string")
            operator = operator.strip()
        request = {
            "base_plan_version": base,
            "scope": cleaned_scope,
            "strategy": {"id": strategy_id, "version": version},
            "params": params,
            "exclusions": cleaned_exclusions,
            "dry_run": dry_run,
        }
        if "overrides" in body:
            # Pass the cleaned optional field through only when the caller sent
            # it; peelfeed treats an absent field as ``[]`` (CP6.1 §7.2).
            request["overrides"] = cleaned_overrides
        if source_item_id is not None:
            request["source_item_id"] = source_item_id
        if operator is not None:
            request["operator"] = operator
        return request

    def _review_ocr_check_request(body):
        """Validate ``POST …/review/ocr/check`` (contract §4 / wire §2.8).

        The body is optional and may be empty or ``{}`` only: M2.1 OCR config
        is env-only (``PEELFEED_OCR_*``), so the HTTP API must never accept a
        caller-supplied ``provider`` (CP4.1 G1: peelfeed would execute its
        ``command`` as a subprocess). Any field at all is a ``400``. Returns
        ``None`` (the adapter always sends ``{"provider": {}}``).
        """
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                400, "an OCR check request must be a JSON object")
        if body:
            raise HTTPException(400, "unknown OCR check field(s): %s"
                                % ", ".join(sorted(body)))
        return None

    def _review_ocr_refresh_request(body):
        """Validate ``POST …/review/ocr/refresh`` (contract §4 / wire §2.8).

        Body is exactly ``{item_ids:[…]}``; ``item_ids`` must be a non-empty,
        unique list of non-empty strings (the frozen job payload). No other
        field is accepted, in particular ``provider`` (CP4.1 G1: the HTTP API
        never forwards a provider override; it exists only in peelfeed's local
        CLI). Returns the cleaned ``item_ids`` list.
        """
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                400, "an OCR refresh request must be a JSON object")
        unknown = sorted(set(body) - {"item_ids"})
        if unknown:
            raise HTTPException(400, "unknown OCR refresh field(s): %s"
                                % ", ".join(unknown))
        item_ids = body.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise HTTPException(
                400, "an OCR refresh needs a non-empty item_ids list")
        cleaned = []
        seen = set()
        for raw in item_ids:
            if not isinstance(raw, str):
                raise HTTPException(400, "item_ids must contain only strings")
            item_id = raw.strip()
            if not item_id:
                raise HTTPException(400, "item_ids must not contain empty ids")
            if item_id in seen:
                raise HTTPException(
                    400, "item_ids must be unique (duplicate %s)" % item_id)
            seen.add(item_id)
            cleaned.append(item_id)
        return cleaned

    def _review_batches_request(body):
        """Validate ``POST …/review/batches`` (wire §2.7): only ``entry_ids``.

        Tier/scope/operations are planner inputs and must never be passed by the
        API/UI (contract §4); they are rejected explicitly rather than ignored.
        ``entry_ids`` must be a non-empty, unique list of non-empty strings.
        """
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(400, "a review batch request must be a JSON object")
        for forbidden in ("tier", "scope", "operations"):
            if forbidden in body:
                raise HTTPException(
                    400, "a review batch accepts only entry_ids; %s is chosen by "
                         "the planner and cannot be passed" % forbidden)
        unknown = sorted(set(body) - {"entry_ids"})
        if unknown:
            raise HTTPException(400, "unknown review batch field(s): %s"
                                % ", ".join(unknown))
        entry_ids = body.get("entry_ids")
        if not isinstance(entry_ids, list) or not entry_ids:
            raise HTTPException(400, "a review batch needs a non-empty entry_ids list")
        cleaned = []
        seen = set()
        for raw in entry_ids:
            if not isinstance(raw, str):
                raise HTTPException(400, "entry_ids must contain only strings")
            entry_id = raw.strip()
            if not entry_id:
                raise HTTPException(400, "entry_ids must not contain empty ids")
            if entry_id in seen:
                raise HTTPException(400, "entry_ids must be unique (duplicate %s)"
                                    % entry_id)
            seen.add(entry_id)
            cleaned.append(entry_id)
        return cleaned

    def _review_publish_body(body):
        """Validate the empty publish/discard/rollback request (wire §2.7)."""
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise HTTPException(400, "the request must be a JSON object")
        if body:
            raise HTTPException(
                400, "publish/discard/rollback take an empty JSON object; the "
                     "batch id is in the URL")
        return {}

    def _record_review_job(directory, sha, job_id, *, job_type, batch_id=None,
                           conversation_id=None):
        """Persist a peelfeed job descriptor in ``extraction.review_jobs``.

        Best-effort: the job already exists in the peel; losing the descriptor
        only costs restart re-attachment, so a successful 202 stays successful.
        """
        try:
            ingest.record_review_job(directory, sha, job_id, job_type=job_type,
                                     batch_id=batch_id,
                                     conversation_id=conversation_id)
        except (ingest.IngestError, contracts.ContractError) as exc:
            LOG.warning("could not persist review job %s for source %s: %s",
                        job_id, str(sha)[:8], _http_detail(exc))

    def _source_by_sha(directory, sha):
        sha = str(sha or "").strip().lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise HTTPException(400, "invalid source sha256")
        entry = ingest.find_source(directory, sha)
        if entry is None:
            raise HTTPException(404, "no source with sha256 '%s'" % sha[:12])
        return entry

    def _peel_id_of(entry):
        block = entry.get("extraction") if isinstance(
            entry.get("extraction"), dict) else {}
        peel_id = str(block.get("peel_id") or "")
        if not peel_id:
            raise HTTPException(
                409, "source '%s' has no peelfeed peel; run Extract first"
                % (entry.get("name") or ""))
        return peel_id

    def _match_peel(rows, sha):
        """Pick the peel for one source sha from ``peelfeed status`` list rows.

        A full ``status <id>`` summary carries ``source{sha256}``, but the list
        form only carries the source **name**. peelfeed peel ids are
        content-addressed (``<slug>-<sha256[:8]>``), so the ``-<sha8>`` suffix is
        the reliable lookup key for the list form.
        """
        sha = str(sha or "").lower()
        if not sha:
            return None
        if isinstance(rows, dict):
            rows = rows.get("peels") or []
        suffix = "-" + sha[:8]
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            source = row.get("source")
            if isinstance(source, dict):
                if str(source.get("sha256") or "").lower() == sha:
                    return row
                continue
            if str(row.get("peel_id") or "").endswith(suffix):
                return row
        return None

    def _peel_by_sha(sha, engine=None):
        """Best-effort peel summary lookup by source sha256 (the id may be new).

        ``peelfeed status`` (no id) lists every peel; the extract route uses this
        before the peel id is known and the poller uses it while ``ingest`` is
        still running.
        """
        sha = str(sha or "").lower()
        if not sha:
            return None
        try:
            rows = (engine or peelfeed.client()).status_all()
        except Exception:  # noqa: BLE001 - polling is best-effort
            return None
        return _match_peel(rows, sha)

    def _poll_peel(directory, sha, block, engine, stop, peel_id=None):
        """Update a source's extraction state from ``peelfeed status`` until done.

        Runs in its own thread next to the long CLI command (ingest/rebuild), so
        the chip moves queued → running while the engine works. It stops when
        ``stop`` is set, before the worker writes the authoritative final block.
        """
        try:
            interval = float(peelfeed.effective().get("poll") or 2.0)
        except peelfeed.PeelfeedConfigError:
            interval = 2.0
        interval = max(0.05, interval)
        while not stop.wait(interval):
            try:
                summary = (engine.status(peel_id) if peel_id
                           else _peel_by_sha(sha, engine))
            except peelfeed.PeelfeedError:
                continue
            if stop.is_set():
                # The CLI command finished while this poll was in flight; the
                # worker owns the final block, so do not persist a late "running".
                break
            if not isinstance(summary, dict):
                continue
            updated = peelfeed.extraction_from_summary(summary, previous=block)
            if updated.get("state") in TERMINAL_EXTRACTION_STATES:
                # The worker writes the authoritative terminal block from the CLI
                # result; this poller only reports progress. `_persist_extraction`
                # also refuses to downgrade a terminal block if the timing races.
                continue
            _persist_extraction(directory, sha, updated)

    def _finish_extraction(directory, sha, block, outcome):
        """Persist the final ``extracted``/``failed`` block after a CLI command."""
        kind, error, result = outcome
        updated = dict(block)
        if kind == "ok" and isinstance(result, dict):
            summary = result.get("peel")
            if not isinstance(summary, dict):
                summary = {}
            updated = peelfeed.extraction_from_summary(summary, previous=block)
            updated["peel_id"] = str(summary.get("peel_id")
                                     or updated.get("peel_id") or "")
            errors = int((updated.get("validation") or {}).get("errors") or 0)
            state = str(summary.get("state") or "")
            if state in ("failed", "archived", "deleted"):
                kind = "failed"
                error = str(summary.get("error") or "") or (
                    "peelfeed reported state '%s'" % state)
            elif state == "corrections_conflict":
                # A system rebuild blocked by a historical correction conflict:
                # the current corpus is untouched; keep the conflict state and
                # the conflicting-corrections detail (wire §2.7).
                updated.update({
                    "state": "corrections_conflict",
                    "error": str(summary.get("error") or "")
                    or "peelfeed reported a corrections conflict",
                })
                updated["finished_at"] = contracts.utc_now()
                _persist_extraction(directory, sha, updated, terminal=True)
                return
            elif errors:
                kind = "failed"
                error = "validation reported %d error(s)" % errors
        if kind == "ok":
            updated.update({"state": "extracted", "error": None})
        else:
            updated.update({"state": "failed",
                            "error": (error or "unknown error")[:300]})
        updated["finished_at"] = contracts.utc_now()
        # The worker's terminal write is authoritative (terminal=True) and wins
        # over any late poller observation (flake hardened in CP3 W0/W4).
        _persist_extraction(directory, sha, updated, terminal=True)

    def _run_peel_command(directory, sha, block, peel_id, command):
        """Run one long CLI command (ingest/rebuild) with a status poller.

        The command runs synchronously in the worker; the poller updates the
        source state every ``PEEKSEED_PEELFEED_POLL`` seconds. The poller is
        stopped before the final block is written, so its last observation can
        never overwrite the authoritative result.

        CP5.1 H9 (wire §10.9): the caller registers this worker in the
        active-extraction registry before the thread starts; the entry is
        removed only here, **after** :func:`_finish_extraction` persisted the
        authoritative terminal block. Until then ``_refresh_peel_states`` skips
        the source (the previous peel's ``ready`` state must not mark a
        re-extracting source ``extracted`` early).
        """
        engine = peelfeed.client()
        stop = threading.Event()
        poller = threading.Thread(
            target=_poll_peel,
            args=(directory, sha, block, engine, stop, peel_id),
            name="peel-poll-%s" % sha[:8], daemon=True)
        poller.start()
        try:
            result = command(engine)
            outcome = ("ok", None, result)
        except peelfeed.PeelfeedUnavailable as exc:
            outcome = ("failed", "peelfeed unreachable: %s" % exc.detail, None)
        except peelfeed.PeelfeedError as exc:
            outcome = ("failed", "peelfeed failed: %s" % exc.detail, None)
        except Exception as exc:  # noqa: BLE001 - a worker must not die silently
            outcome = ("failed", "peelfeed failed (%s)" % type(exc).__name__,
                       None)
        finally:
            stop.set()
            poller.join(timeout=30.0)
        try:
            _finish_extraction(directory, sha, block, outcome)
        finally:
            # H9: the source may be refreshed from peelfeed again only once the
            # authoritative terminal block is on disk.
            _clear_extraction(directory, sha)

    def _refresh_peel_states(directory):
        """Poll peelfeed for non-terminal sources and persist the new state.

        Called by ``GET /api/datasets/{id}/sources`` (the UI poll) and before an
        index build. Cheap when peelfeed is unavailable: the 60s handshake cache
        answers without a CLI call. CP3: a source already terminal in peelfeed is
        persisted (restart recovery), so a source can never stay stuck at
        ``running`` after a worker crash or a lost final write; ``_persist_extraction``
        still refuses to downgrade a terminal block with a stale non-terminal one.

        CP5.1 H9 (wire §10.9): a source with an active extraction worker is
        **skipped entirely**. A re-ingest of an already-ready peel still shows
        the *previous* peel as ``ready`` until the new ingest publishes; that
        terminal state must not be persisted while the worker runs (otherwise
        ``GET …/sources`` reports ``extracted`` and an index build pins the old
        build). Restart recovery is unchanged: with no worker active an existing
        terminal peelfeed state is still persisted.
        """
        if not peelfeed.configured():
            return
        if not _peelfeed_handshake().get("available"):
            return
        try:
            engine = peelfeed.client()
        except Exception:  # noqa: BLE001 - config errors are reported elsewhere
            return
        try:
            raw = contracts.read_json(Path(directory) / "dataset.json")
        except contracts.ContractError:
            return
        if not isinstance(raw, dict):
            return
        pending = []
        for entry in raw.get("sources") or []:
            if not isinstance(entry, dict):
                continue
            block = entry.get("extraction") if isinstance(
                entry.get("extraction"), dict) else {}
            if str(block.get("state") or "") in ("queued", "running",
                                                 "corrections_conflict"):
                pending.append((entry, block))
        if not pending:
            return
        all_peels = None
        for entry, block in pending:
            peel_id = str(block.get("peel_id") or "")
            sha = str(entry.get("sha256") or "").lower()
            if not sha:
                continue
            if _extraction_active(directory, sha):
                # H9: never read/persist peelfeed state for a source whose
                # extraction worker is still running; the worker owns the
                # terminal block.
                continue
            try:
                if peel_id:
                    summary = engine.status(peel_id)
                else:
                    if all_peels is None:
                        all_peels = engine.status_all()
                    summary = _match_peel(all_peels, sha)
            except peelfeed.PeelfeedError:
                continue
            if not isinstance(summary, dict):
                continue
            updated = peelfeed.extraction_from_summary(summary, previous=block)
            terminal = updated.get("state") in TERMINAL_EXTRACTION_STATES
            _persist_extraction(directory, sha, updated, terminal=terminal)

    @app.get("/api/datasets/{dataset_id}/sources")
    def dataset_sources(dataset_id: str):
        directory = _dataset_dir(dataset_id)
        # CP3 wire §2.7: cheap reconcile before the UI poll, so a publish by
        # peelfeed (or another dataset) is visible on the next read.
        try:
            ingest.reconcile_peels(directory)
        except Exception as exc:  # noqa: BLE001 - a broken peel must not fail reads
            LOG.warning("could not reconcile dataset %s: %s", dataset_id,
                        _http_detail(exc))
        _refresh_peel_states(directory)
        return {"dataset": dataset_id, "sources": _source_rows(directory)}

    def _queue_counts():
        """Every stored source across datasets, counted by extraction state."""
        states = {}
        total = 0
        root = config.ensure().datasets_dir
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            if name.startswith(".") or not (Path(root) / name).is_dir():
                continue
            try:
                raw = contracts.read_json(Path(root) / name / "dataset.json")
            except contracts.ContractError:
                continue
            if not isinstance(raw, dict):
                continue
            for entry in raw.get("sources") or []:
                if not isinstance(entry, dict):
                    continue
                state = contracts.extraction_state(entry)
                states[state] = states.get(state, 0) + 1
                total += 1
        return {"count": total, "states": states}

    @app.get("/api/peelfeed/status")
    def peelfeed_status():
        """Config (CLI/python/root), reachability/capabilities and queue counts."""
        payload = peelfeed.status(probe=False)
        payload["queue"] = _queue_counts()
        return payload

    @app.post("/api/datasets/{dataset_id}/sources", status_code=201)
    async def dataset_sources_upload(dataset_id: str,
                                     files: List[UploadFile] = File(...)):
        """Upload 1..N files into an existing dataset (storage only).

        Same validation/limits/zip-slip guard as dataset creation; extraction is
        a separate, user-triggered peelfeed job (design §4.5).
        """
        directory = _dataset_dir(dataset_id)
        if not files:
            raise HTTPException(400, "at least one PDF is required")
        if len(files) > security.MAX_UPLOAD_FILES:
            raise HTTPException(413, "a dataset accepts at most %d files"
                                % security.MAX_UPLOAD_FILES)
        staging = config.ensure().uploads_dir / ("upload-" + uuid.uuid4().hex)
        added = []
        try:
            staging.mkdir(parents=True, exist_ok=True)
            security.harden_dir(staging)
            prepared = []
            total = 0
            for number, upload in enumerate(files):
                raw_name = getattr(upload, "filename", "") or ""
                filename = ingest.sanitize_filename(raw_name)
                if not filename.lower().endswith(".pdf"):
                    raise HTTPException(400, "%r is not a PDF" % filename)
                part_dir = staging / ("%03d" % number)
                part_dir.mkdir(parents=True, exist_ok=True)
                security.harden_dir(part_dir)
                temp = part_dir / filename
                try:
                    written = await security.stream_upload(
                        upload, temp, total_used=total, expect_prefix=b"%PDF-")
                except security.UploadInvalid:
                    raise HTTPException(400, "%r is not a valid PDF" % filename)
                except security.UploadLimitExceeded as exc:
                    raise HTTPException(exc.status, exc.detail)
                total += written
                prepared.append(temp)
            for temp in prepared:
                added.append(ingest.add_source(directory, temp))
            return {"dataset": _dataset_public(dataset_id),
                    "sources": _source_rows(directory)}
        except HTTPException:
            for entry in added:
                try:
                    ingest.remove_source(directory, entry.get("sha256") or "")
                except ingest.IngestError:
                    pass
            raise
        except Exception as exc:  # noqa: BLE001 - ingestion errors are user-facing
            for entry in added:
                try:
                    ingest.remove_source(directory, entry.get("sha256") or "")
                except ingest.IngestError:
                    pass
            raise HTTPException(400, _http_detail(exc))
        finally:
            shutil.rmtree(str(staging), ignore_errors=True)

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/extract")
    def source_extract(dataset_id: str, sha: str,
                       payload: Optional[dict] = None):
        """Ask the peelfeed CLI to build a peel for one stored source.

        503 when peelfeed is unconfigured/unavailable (upload and file
        management are unaffected); otherwise a background thread runs
        ``peelfeed ingest`` and polls ``peelfeed status`` while it works, so the
        source chip moves ``queued → running → extracted|failed``. The UI polls
        ``GET …/sources`` until a terminal state.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        stored = ingest.stored_source_path(directory, entry)
        if stored is None or not stored.is_file():
            raise HTTPException(409, "the stored file for '%s' is missing"
                                % (entry.get("name") or ""))
        hs = _peelfeed_handshake()
        if not hs.get("available"):
            failed = dict(entry.get("extraction")
                          or contracts.extraction_default())
            failed.update({
                "state": "failed",
                "error": "peelfeed unavailable: %s"
                         % (hs.get("error") or "not configured"),
            })
            ingest.update_source(directory, entry["sha256"], status="failed",
                                 extraction=failed)
            raise HTTPException(
                503,
                "extraction is unavailable: %s. Uploads and file management "
                "keep working; configure PEEKSEED_PEELFEED_CLI (or "
                "PEEKSEED_PEELFEED_PYTHON) and retry."
                % (hs.get("error") or "peelfeed is not configured"),
            )
        body = payload or {}
        cfg = peelfeed.effective()
        request = {
            "profile": body.get("profile") or None,
            "llm_mode": body.get("llm_mode") or cfg["llm_mode"],
            "chunk_profile": body.get("chunk_profile") or cfg["chunk_profile"],
            "chunk_versions": _embed_chunk_sizes(),
            "force": bool(body.get("force")),
        }
        source_sha = str(entry.get("sha256") or "").lower()
        block = dict(entry.get("extraction") or contracts.extraction_default())
        existing = None if request["force"] else _peel_by_sha(source_sha)
        peel_id = str((existing or {}).get("peel_id")
                      or block.get("peel_id") or "")
        reused = bool(
            existing
            and str(existing.get("state") or "") in ("ready", "needs_review")
            and not request["force"]
        )
        block.update({
            "state": "queued",
            "peel_id": peel_id,
            "profile": str(request["profile"] or block.get("profile") or ""),
            "started_at": contracts.utc_now(),
            "finished_at": None,
            "error": None,
        })
        # CP5.1 H9 (wire §10.9): register the worker *before* the queued write
        # and the thread start so a concurrent GET …/sources / index build can
        # never persist the previous peel's terminal state while this ingest is
        # running. The worker clears the entry only after its authoritative
        # terminal write.
        _register_extraction(directory, source_sha)
        try:
            ingest.update_source(directory, source_sha, status="queued",
                                 extraction=block)
            threading.Thread(
                target=_run_peel_command,
                args=(directory, source_sha, block, peel_id,
                      lambda engine: engine.ingest(
                          str(stored.resolve()),
                          profile=request["profile"],
                          llm_mode=request["llm_mode"],
                          chunk_profile=request["chunk_profile"],
                          chunk_versions=request["chunk_versions"],
                          force=request["force"])),
                name="peel-extract-%s" % source_sha[:8], daemon=True).start()
        except Exception:
            # The queued write or the thread start failed: clear the marker so
            # the source can be refreshed/recovered from peelfeed again.
            _clear_extraction(directory, source_sha)
            raise
        return {
            "source": {"sha256": entry["sha256"],
                       "name": entry.get("name") or "",
                       "extraction": block},
            "peel": {"peel_id": peel_id,
                     "state": "queued",
                     "reused": reused},
            "capabilities": {
                "version": hs.get("version"),
                "profiles": hs.get("profiles"),
                "chunk_profiles": hs.get("chunk_profiles"),
                "llm_modes": hs.get("llm_modes"),
            },
        }

    @app.get("/api/datasets/{dataset_id}/sources/{sha}/review")
    def source_review(dataset_id: str, sha: str,
                      group: Optional[str] = None,
                      status: Optional[str] = None):
        """Proxy ``peelfeed review`` — the structured schema-2 review view.

        ``group=cluster|code`` groups item ids (default: cluster) and
        ``status=`` filters items/states; both are passed through unchanged.
        Reading the list never calls an LLM. Requires ``review_schema == 2``
        (409 otherwise) because the old flat queue no longer exists.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        group_value = str(group or "").strip().lower() or None
        if group_value is not None and group_value not in peelfeed.REVIEW_GROUPS:
            raise HTTPException(400, "group must be 'cluster' or 'code'")
        status_value = str(status or "").strip() or None
        payload = _call_peelfeed(
            "review",
            lambda: peelfeed.client().review(
                peel_id, group=group_value, status=status_value))
        # CP5: the review payload carries the VLM capability block the UI needs
        # to enable/disable report/discuss actions; peelfeed's read-only
        # capabilities child never sees the provider env, so overlay the
        # provider peekseed resolved (env-first, else default answer link).
        return _review_vlm_capabilities(payload)

    @app.get("/api/datasets/{dataset_id}/sources/{sha}/review/items/{item_id}")
    def source_review_item(dataset_id: str, sha: str, item_id: str):
        """Proxy ``peelfeed review --item``: one item with evidence/strategies."""
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        identifier = str(item_id or "").strip()
        if not identifier:
            raise HTTPException(400, "invalid review item id")
        payload = _call_peelfeed(
            "review item",
            lambda: peelfeed.client().review(peel_id, item=identifier))
        # CP5: an item view opened directly also needs the VLM capability
        # overlay (additive ``capabilities``) for the bar/confirmation dialog.
        return _review_vlm_capabilities(payload)

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/plan")
    def source_review_plan(dataset_id: str, sha: str,
                           payload: Optional[dict] = None):
        """Proxy one ``peelfeed plan`` action.

        Actions: select/confirm/lock/unlock/defer/accept/clear. Returns the
        latest complete ReviewPlan (with the changed ``entry`` and any R0
        ``resolution``) plus the CP2 additive ``accepted``/``cleared``/
        ``refresh`` objects. Errors map per contract §4: 409 for a stale
        ``base_plan_version``/conflict/review schema, 400 for validation
        (``accept`` lists all per-item reasons, all-or-nothing), 423 when
        peelfeed's review lock is busy.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        action, request = _review_plan_request(payload or {})
        request["action"] = action
        return _call_peelfeed(
            "review plan",
            lambda: peelfeed.client().plan(peel_id, request))

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/reports",
              status_code=202)
    def source_review_reports(dataset_id: str, sha: str,
                              payload: Optional[dict] = None):
        """Create a persistent peelfeed ``report`` job (CP3/CP5 wire §2.7/§6.1).

        Rules body is unchanged: ``{item_ids?, cluster_ids?}``. VLM body is
        ``{mode:"vlm", confirmation:{…}, item_ids?, cluster_ids?}``: the
        confirmation is validated field-by-field, the default answer link is
        re-resolved and provider/link_id/model are compared **before** any CLI
        call (mismatch -> 400 ``privacy_confirmation_required``); an
        unconfigured link/key is 503 ``vlm_unavailable`` and creates no job.
        Returns 202 ``{job_id, status: "queued"}``; the job writes
        ``report.json`` and is polled through ``GET …/review/jobs/{id}``. The
        descriptor is stored in ``extraction.review_jobs`` so a restart
        re-attaches by id (never reruns).
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        request = _review_report_request(payload or {})
        confirmation = request["confirmation"]
        if confirmation is not None:
            _review_vlm_provider(confirmation)
        result = _call_peelfeed(
            "review report",
            lambda: peelfeed.client().review_report(
                peel_id, item_ids=request.get("item_ids"),
                cluster_ids=request.get("cluster_ids"),
                mode=request["mode"], confirmation=confirmation))
        job_id = str((result or {}).get("job_id") or "")
        if not job_id:
            raise HTTPException(502, "peelfeed review-report returned no job_id")
        _record_review_job(directory, str(entry.get("sha256") or ""), job_id,
                           job_type="report")
        return {"job_id": job_id,
                "status": str(result.get("status") or "queued")}

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/discussions",
              status_code=202)
    def source_review_discussions(dataset_id: str, sha: str,
                                  payload: Optional[dict] = None):
        """Create one persistent peelfeed ``discuss`` job (CP5 wire §6.2).

        Body is ``{conversation_id?, scope:{item_id?|cluster_id?|peel:true},
        message, base_plan_version, confirmation:{…}}``; exactly one scope
        selector, ``message`` non-empty ≤ 4000 chars, ``base_plan_version`` int
        ≥ 0, and the privacy confirmation validated/compared exactly like the
        VLM report route (400 ``privacy_confirmation_required``, 503
        ``vlm_unavailable``, no job/CLI call on rejection). Returns 202
        ``{job_id, conversation_id, status:"queued"}`` and persists the
        additive ``conversation_id`` in ``extraction.review_jobs`` for restart
        re-attachment (never auto-rerun). The job returns explanations and a
        draft patch only; it never applies a plan change.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        request = _review_discussion_request(payload or {})
        _review_vlm_provider(request["confirmation"])
        result = _call_peelfeed(
            "review discussion",
            lambda: peelfeed.client().discuss(peel_id, request))
        job_id = str((result or {}).get("job_id") or "")
        conversation_id = str((result or {}).get("conversation_id") or "")
        if not job_id or not conversation_id:
            raise HTTPException(
                502, "peelfeed discuss returned no job_id/conversation_id")
        _record_review_job(directory, str(entry.get("sha256") or ""), job_id,
                           job_type="discuss",
                           conversation_id=conversation_id)
        return {"job_id": job_id, "conversation_id": conversation_id,
                "status": str(result.get("status") or "queued")}

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/plan/patches")
    def source_review_plan_patches(dataset_id: str, sha: str,
                                   payload: Optional[dict] = None):
        """Proxy the synchronous targeted plan patch (CP5 wire §5/§6.3).

        Body is ``{base_plan_version, patch, source:"vlm|user",
        conversation_id?}``. Returns 200 with the new plan plus the targeted
        ``diff`` (added/changed/removed/unchanged). Errors map per contract §4:
        409 ``conflict``/``stale`` for a base mismatch or locked/batch-bound
        entries, 400 ``validation`` for invalid strategy/params/evidence,
        404 for an unknown ``conversation_id``. This route never confirms or
        applies anything: adopting a patch only edits the draft plan.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        request = _review_plan_patch_request(payload or {})
        return _call_peelfeed(
            "plan patch",
            lambda: peelfeed.client().plan_patch(peel_id, request))

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/plan/propagate")
    def source_review_plan_propagate(dataset_id: str, sha: str,
                                     payload: Optional[dict] = None):
        """Proxy the synchronous CP6 cluster-level propagation (wire §2).

        Body is exactly the frozen ``plan propagate`` request (CP6 wire §1.1;
        CP6.1 wire §7.2/§7.5 adds the optional ``overrides``):
        ``{base_plan_version, scope:{kind:"item|cluster|peel", …},
        strategy:{id,version}, params, source_item_id?, exclusions[],
        overrides[], dry_run, operator?}``; the body is validated strictly
        here and proxied to ``peelfeed plan propagate`` synchronously with
        **no model call and no LLM env**. ``overrides`` lists ``decided``
        members the user explicitly wants to replace; peekseed validates the
        envelope/type only (peelfeed enforces membership, subset and
        ``exclusions`` overlap all-or-nothing). ``dry_run:true`` returns the
        member preview and writes nothing; ``dry_run:false`` writes the draft
        plan entries (with a ``kind:"propagation"`` origin) and the propagation
        audit record, never confirming/applying/building/publishing. Members
        with a pre-existing decision are ``decided`` and kept by default (no
        entry write, no resolution revert, no error); overridden ``decided``
        members are written as ``ready`` and their active R0 resolution is
        reverted in the same review lock (CP6.1 §7.1/§7.2). The response
        (``decided``/``existing``/``kept``/``overridden``/``counts.decided``/
        ``overrides``) is passed through unchanged. Status mapping follows
        contract §4: 200 preview/apply; 409 ``conflict`` (base mismatch or
        locked/batch-bound members, ``error.details.conflicts``); 400
        ``validation`` (invalid scope/strategy/params/overrides or non-excluded
        failing members, ``error.details.reasons``); 404 not found. No job
        descriptor is created.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        request = _review_propagate_request(payload or {})
        return _call_peelfeed(
            "plan propagate",
            lambda: peelfeed.client().plan_propagate(peel_id, request))

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/ocr/check")
    def source_review_ocr_check(dataset_id: str, sha: str,
                                payload: Optional[dict] = None):
        """Probe the OCR provider through ``peelfeed ocr-check`` (wire §2.8).

        Body is optional and must be empty or ``{}``: the HTTP API never accepts
        a ``provider`` object (CP4.1 G1), so any field is a ``400``. The
        adapter always sends ``{"provider": {}}`` = the deploy env
        ``PEELFEED_OCR_*`` config. 200 with the check payload
        (``available/engine/provider/probe/checked_at/error``); 503
        ``ocr_unavailable``; 400 ``ocr_config_invalid``/validation; other OCR
        codes 502. The short probe never writes the corpus and creates no job.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        _review_ocr_check_request(payload)
        return _call_peelfeed(
            "ocr check",
            lambda: peelfeed.client().ocr_check(peel_id))

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/ocr/refresh",
              status_code=202)
    def source_review_ocr_refresh(dataset_id: str, sha: str,
                                  payload: Optional[dict] = None):
        """Create one persistent ``type=ocr`` evidence-refresh job (wire §2.8).

        Body is exactly ``{item_ids:[…]}``; item_ids must be a non-empty,
        unique list of non-empty strings. No other field is accepted, in
        particular ``provider`` (CP4.1 G1; the HTTP API never forwards a
        provider override). Returns 202 ``{job_id, status:"queued",
        result_ref:null}`` and stores
        ``extraction.review_jobs[job_id] = {"type":"ocr","created_at":…,
        "batch_id":null}`` for restart re-attachment (never auto-rerun). The job
        takes a reader lease only and never blocks builds/publishing; 409
        ``not_ready`` when there is no current build.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        item_ids = _review_ocr_refresh_request(payload)
        result = _call_peelfeed(
            "ocr refresh",
            lambda: peelfeed.client().ocr_refresh(peel_id, item_ids))
        job_id = str((result or {}).get("job_id") or "")
        if not job_id:
            raise HTTPException(502, "peelfeed ocr-refresh returned no job_id")
        _record_review_job(directory, str(entry.get("sha256") or ""), job_id,
                           job_type="ocr")
        return {"job_id": job_id,
                "status": str(result.get("status") or "queued"),
                "result_ref": result.get("result_ref")}

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/batches",
              status_code=202)
    def source_review_batches(dataset_id: str, sha: str,
                              payload: Optional[dict] = None):
        """Prepare an ApplyBatch and a ``build`` job (wire §2.7).

        Body is exactly ``{entry_ids: [...]}`` (confirmed operation entries);
        tier/scope/operations are planner inputs and are rejected. Returns 202
        ``{job_id, batch_id, status: "queued"}``; the descriptor is persisted in
        ``extraction.review_jobs`` with ``type="build"`` and the batch id.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        entry_ids = _review_batches_request(payload or {})
        result = _call_peelfeed(
            "review apply",
            lambda: peelfeed.client().apply(peel_id, entry_ids))
        job_id = str((result or {}).get("job_id") or "")
        batch_id = str((result or {}).get("batch_id") or "")
        if not job_id or not batch_id:
            raise HTTPException(
                502, "peelfeed apply returned no job_id/batch_id")
        _record_review_job(directory, str(entry.get("sha256") or ""), job_id,
                           job_type="build", batch_id=batch_id)
        return {"job_id": job_id, "batch_id": batch_id,
                "status": str(result.get("status") or "queued")}

    @app.get("/api/datasets/{dataset_id}/sources/{sha}/review/batches/{batch_id}")
    def source_review_batch(dataset_id: str, sha: str, batch_id: str):
        """Proxy ``batch show``: the persisted ApplyBatch record (wire §2.7)."""
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        identifier = str(batch_id or "").strip()
        if not identifier:
            raise HTTPException(400, "invalid batch id")
        return _call_peelfeed(
            "batch show",
            lambda: peelfeed.client().batch_show(peel_id, identifier))

    @app.post(
        "/api/datasets/{dataset_id}/sources/{sha}/review/batches/{batch_id}/publish")
    def source_review_batch_publish(dataset_id: str, sha: str, batch_id: str,
                                    payload: Optional[dict] = None):
        """Publish a ready batch synchronously (wire §2.7).

        200 ``{batch, current}``; 409 stale/conflict/corrections_conflict, 423
        busy, 400 validation. After success **every** dataset referencing the
        peel is reconciled; only a changed conversion_id makes an index stale.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        _review_publish_body(payload)
        identifier = str(batch_id or "").strip()
        if not identifier:
            raise HTTPException(400, "invalid batch id")
        result = _call_peelfeed(
            "review publish",
            lambda: peelfeed.client().publish(peel_id, identifier))
        _reconcile_after_peel_change(peel_id)
        return result

    @app.post(
        "/api/datasets/{dataset_id}/sources/{sha}/review/batches/{batch_id}/discard")
    def source_review_batch_discard(dataset_id: str, sha: str, batch_id: str,
                                    payload: Optional[dict] = None):
        """Discard an unpublished batch; current/ledger unchanged (wire §2.7)."""
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        _review_publish_body(payload)
        identifier = str(batch_id or "").strip()
        if not identifier:
            raise HTTPException(400, "invalid batch id")
        result = _call_peelfeed(
            "review discard",
            lambda: peelfeed.client().discard(peel_id, identifier))
        _reconcile_after_peel_change(peel_id)
        return result

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/review/rollback",
              status_code=202)
    def source_review_rollback(dataset_id: str, sha: str,
                               payload: Optional[dict] = None):
        """Create the revert batch + ``rollback`` job (wire §2.7).

        202 ``{job_id, batch_id, status: "queued"}``; the job auto-publishes the
        revert when it finishes, so the datasets are reconciled again on reads.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        _review_publish_body(payload)
        result = _call_peelfeed(
            "review rollback",
            lambda: peelfeed.client().rollback(peel_id))
        job_id = str((result or {}).get("job_id") or "")
        batch_id = str((result or {}).get("batch_id") or "")
        if not job_id or not batch_id:
            raise HTTPException(
                502, "peelfeed rollback returned no job_id/batch_id")
        _record_review_job(directory, str(entry.get("sha256") or ""), job_id,
                           job_type="rollback", batch_id=batch_id)
        _reconcile_after_peel_change(peel_id)
        return {"job_id": job_id, "batch_id": batch_id,
                "status": str(result.get("status") or "queued")}

    @app.get("/api/datasets/{dataset_id}/sources/{sha}/review/jobs/{job_id}")
    def source_review_job(dataset_id: str, sha: str, job_id: str):
        """Proxy ``job show``: the real job status/progress/result (wire §2.7).

        peekseed only polls and re-attaches from ``extraction.review_jobs``; it
        never reruns a job automatically. A terminal descriptor stays for UI
        queries, and the record's derived ``interrupted`` state is passed through.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_review_schema()
        identifier = str(job_id or "").strip()
        if not identifier:
            raise HTTPException(400, "invalid job id")
        return _call_peelfeed(
            "job show",
            lambda: peelfeed.client().job_show(peel_id, identifier))

    @app.post("/api/datasets/{dataset_id}/sources/{sha}/events")
    def source_events(dataset_id: str, sha: str,
                      payload: Optional[dict] = None):
        """Append one ``embed``/``feedback`` event to the peel's ``.peel`` log."""
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_peelfeed()
        body = payload or {}
        event = str(body.get("event") or "").strip().lower()
        if event not in ("embed", "feedback"):
            raise HTTPException(400, "event must be 'embed' or 'feedback'")
        data = body.get("data")
        if not isinstance(data, dict):
            data = {key: value for key, value in body.items() if key != "event"}
        try:
            return peelfeed.client().event(peel_id, event, data)
        except peelfeed.PeelfeedUnavailable as exc:
            raise HTTPException(503, "extraction is unavailable: %s" % exc.detail)
        except peelfeed.PeelfeedError as exc:
            raise HTTPException(
                exc.status if exc.status in (400, 404, 409) else 502,
                "peelfeed event failed: %s" % exc.detail)

    @app.get("/api/datasets/{dataset_id}/sources/{sha}/assets/{path:path}")
    def source_asset(dataset_id: str, sha: str, path: str):
        """Serve one peel/corpus asset from the filesystem (no HTTP proxy).

        The path is tried peel-relative first (``<root>/<peel>/<path>``) and then
        corpus-relative (``<root>/<peel>/extract/corpus/<path>``). Every
        candidate is realpath-checked against the configured peel root, so a
        symlink or ``..`` cannot escape it; traversal is a 403.
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        peel_id = _peel_id_of(entry)
        _require_peelfeed()
        try:
            root = Path(peelfeed.effective()["root"]).expanduser().resolve()
        except peelfeed.PeelfeedConfigError as exc:
            raise HTTPException(503, "the peelfeed config is unreadable: %s" % exc)
        identifier = str(peel_id or "").strip()
        if (not identifier or identifier in (".", "..")
                or "/" in identifier or "\\" in identifier
                or "\x00" in identifier):
            raise HTTPException(400, "invalid peel id")
        peel_dir = (root / identifier).resolve()
        try:
            peel_dir.relative_to(root)
        except ValueError:
            raise HTTPException(403, "the peel resolves outside the peel root")
        if not peel_dir.is_dir():
            raise HTTPException(404, "peel '%s' is not on disk" % identifier)
        relative = str(path or "").lstrip("/")
        if not relative:
            raise HTTPException(404, "no asset path")
        escaped = False
        for candidate in (peel_dir / relative,
                          peel_dir / "extract" / "corpus" / relative):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                escaped = True
                continue
            if resolved.is_file():
                media_type = (mimetypes.guess_type(str(resolved))[0]
                              or "application/octet-stream")
                return FileResponse(
                    str(resolved),
                    media_type=media_type,
                    headers={"Cache-Control": "private, max-age=60"},
                )
        if escaped:
            raise HTTPException(403, "the asset resolves outside the peel root")
        raise HTTPException(404, "no asset '%s' in peel '%s'"
                            % (relative, identifier))

    @app.delete("/api/datasets/{dataset_id}/sources/{sha}")
    def source_delete(dataset_id: str, sha: str, peel: str = "keep"):
        """Remove a source; ``?peel=delete`` also archives its peelfeed peel.

        The default keeps the peel for audit/rollback. The index/chunks are left
        in place until the next build, which reports the source-set change as
        staleness (never silently serving removed files as if they were current).
        """
        directory = _dataset_dir(dataset_id)
        entry = _source_by_sha(directory, sha)
        removed = ingest.remove_source(directory, entry["sha256"])
        peel_result = {"action": "kept", "peel_id": ""}
        block = entry.get("extraction") if isinstance(
            entry.get("extraction"), dict) else {}
        peel_id = str(block.get("peel_id") or "")
        if str(peel or "keep").lower() == "delete":
            if not peel_id:
                peel_result = {"action": "none", "peel_id": ""}
            elif not peelfeed.configured():
                peel_result = {"action": "not_deleted",
                               "peel_id": peel_id,
                               "reason": "peelfeed is not configured"}
            else:
                try:
                    result = peelfeed.client().archive(
                        peel_id, reason="source deleted from peekseed")
                    peel_result = {"action": "archived", "peel_id": peel_id,
                                   "peel": result}
                except peelfeed.PeelfeedUnavailable as exc:
                    peel_result = {"action": "not_deleted",
                                   "peel_id": peel_id,
                                   "reason": exc.detail[:200]}
                except peelfeed.PeelfeedError as exc:
                    peel_result = {"action": "not_deleted",
                                   "peel_id": peel_id,
                                   "reason": exc.detail[:200]}
        elif peel_id:
            peel_result = {"action": "kept", "peel_id": peel_id}
        reload_store()
        return {"ok": True, "deleted": entry["sha256"],
                "name": entry.get("name") or "", "peel": peel_result,
                "removed": removed is not None}

    @app.post("/api/datasets", status_code=201)
    async def datasets_create(name: str = Form(...), note: str = Form(""),
                              files: List[UploadFile] = File(...)):
        dataset = await _store_uploads(name, note, files)
        return {"dataset": _dataset_public(dataset["id"])}

    @app.delete("/api/datasets/{dataset_id}")
    def datasets_delete(dataset_id: str):
        directory = _dataset_dir(dataset_id)
        shas = []
        try:
            raw = contracts.read_json(Path(directory) / "dataset.json")
        except contracts.ContractError:
            raw = None
        if isinstance(raw, dict):
            for entry in raw.get("sources") or []:
                if isinstance(entry, dict) and entry.get("sha256"):
                    shas.append(str(entry["sha256"]).lower())
        shutil.rmtree(str(directory), ignore_errors=True)
        reload_store()
        for sha in shas:
            ingest.release_shared_source(sha)
        return {"ok": True, "deleted": dataset_id}

    # --- index jobs -------------------------------------------------------
    def _index_link(link_id):
        try:
            link = links.get(link_id) if link_id else links.active("embed")
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if link is None:
            raise HTTPException(409, "no embedding link is enabled")
        if link.get("role") != "embed" or not link.get("enabled"):
            raise HTTPException(409, "link '%s' is not an enabled embed link"
                                % (link_id or ""))
        return link

    def _start_index_job(directory, dataset_id, link, record, lock):
        """Run one index build on a worker thread; the record is the truth.

        The lock is already held by the route, so a second request fails fast
        with 409 instead of queueing behind a thread the user cannot see. The
        worker only releases it; every state change goes through the record.
        """
        jobs_dir = config.ensure().jobs_dir

        def work():
            try:
                ingest.build_index(
                    directory, link, job=record, lock=lock,
                    cancel=lambda: ingest.job_cancel_requested(
                        jobs_dir, record["id"]))
            except ingest.IndexCancelled as exc:
                # The builder already wrote state="cancelled" and kept the
                # previous generation serving.
                LOG.info("index job %s cancelled: %s", record["id"], exc)
            except Exception as exc:  # noqa: BLE001 - the record reports, never crashes
                LOG.warning("index job %s failed: %s", record["id"],
                            _http_detail(exc))
            finally:
                lock.release()
                reload_store()

        threading.Thread(target=work, name="peekseed-index", daemon=True).start()

    def _extracted_sources(directory):
        """``(extracted, skipped)`` source rows from ``dataset.json``."""
        try:
            raw = contracts.read_json(Path(directory) / "dataset.json")
        except contracts.ContractError as exc:
            raise HTTPException(500, _http_detail(exc))
        if not isinstance(raw, dict):
            raise HTTPException(500, "dataset.json is not readable")
        extracted = []
        skipped = []
        for entry in raw.get("sources") or []:
            if isinstance(entry, str):
                entry = {"name": entry}
            if not isinstance(entry, dict):
                continue
            state = contracts.extraction_state(entry)
            block = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
            row = {"name": str(entry.get("name") or ""), "state": state}
            if state == "extracted" and str(block.get("peel_id") or ""):
                extracted.append(row)
            else:
                skipped.append(row)
        return extracted, skipped

    @app.post("/api/datasets/{dataset_id}/index", status_code=202)
    def datasets_index(dataset_id: str, payload: Optional[dict] = None):
        directory = _dataset_dir(dataset_id)
        extracted, skipped = _extracted_sources(directory)
        if not extracted:
            names = ", ".join(
                "%s (%s)" % (row["name"], row["state"]) for row in skipped
            ) or "none"
            raise HTTPException(
                409,
                "no_extracted_sources: no source in dataset '%s' has a peelfeed "
                "corpus; upload files, configure peelfeed and Extract them first. "
                "Sources: %s" % (dataset_id, names),
            )
        link = _index_link((payload or {}).get("link"))
        try:
            ingest.link_chunk_sizes(link)
        except ingest.NoChunkSizes as exc:
            raise HTTPException(409, str(exc))
        jobs_dir = config.ensure().jobs_dir
        lock = ingest.CollectionLock(jobs_dir, dataset_id)
        try:
            lock.acquire()
        except ingest.CollectionBusy as exc:
            raise HTTPException(409, str(exc))
        try:
            record = ingest.write_job(
                jobs_dir, contracts.new_job("index", collection=dataset_id))
        except Exception:
            lock.release()
            raise
        _start_index_job(directory, dataset_id, link, record, lock)
        return {"job": record["id"], "state": record["state"]}

    @app.get("/api/jobs")
    def jobs_list(state: str = "", collection: str = "", limit: int = 200):
        """Every job record on disk, newest first (state=, collection= filters)."""
        try:
            limit = min(max(1, int(limit)), 500)
        except (TypeError, ValueError):
            limit = 200
        return {"jobs": ingest.list_jobs(config.ensure().jobs_dir,
                                         state=state or None,
                                         collection=collection or None,
                                         limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def jobs_get(job_id: str):
        jobs_dir = config.ensure().jobs_dir
        try:
            job = ingest.read_job(jobs_dir, job_id)
        except ingest.IngestError as exc:
            raise HTTPException(400, str(exc))
        if job is None:
            raise HTTPException(404, "no such job")
        return job

    @app.delete("/api/jobs/{job_id}")
    def jobs_cancel(job_id: str):
        """Request cancellation; the builder stops between batches."""
        jobs_dir = config.ensure().jobs_dir
        try:
            job = ingest.read_job(jobs_dir, job_id)
        except ingest.IngestError as exc:
            raise HTTPException(400, str(exc))
        if job is None:
            raise HTTPException(404, "no such job")
        try:
            cancelled = ingest.request_cancel(jobs_dir, job_id)
        except ingest.IngestError as exc:
            raise HTTPException(409, str(exc))
        return {"job": cancelled, "cancelling": True}

    @app.post("/api/jobs/{job_id}/resume", status_code=202)
    def jobs_resume(job_id: str):
        """Resume an interrupted/cancelled/failed index job from its checkpoint.

        The build resumes from the generation's committed row range, so a
        restarted service does not re-embed the corpus. The job id is reused so
        the history of the work stays one record.
        """
        jobs_dir = config.ensure().jobs_dir
        try:
            job = ingest.read_job(jobs_dir, job_id)
        except ingest.IngestError as exc:
            raise HTTPException(400, str(exc))
        if job is None:
            raise HTTPException(404, "no such job")
        if job["kind"] != "index":
            raise HTTPException(409, "only index jobs can be resumed")
        if job["state"] == "running":
            raise HTTPException(409, "job '%s' is already running" % job["id"])
        if job["state"] == "succeeded":
            raise HTTPException(409, "job '%s' already succeeded" % job["id"])
        collection = job.get("collection") or ""
        if not collection:
            raise HTTPException(409, "job '%s' has no collection to resume" % job["id"])
        directory = _dataset_dir(collection)
        link = _index_link(None)
        lock = ingest.CollectionLock(jobs_dir, collection)
        try:
            lock.acquire(job_id=job["id"])
        except ingest.CollectionBusy as exc:
            raise HTTPException(409, str(exc))
        try:
            job.update({
                "state": "queued",
                "cancel_requested": False,
                "error": None,
                "finished_at": None,
                "updated_at": contracts.utc_now(),
            })
            record = ingest.write_job(jobs_dir, job)
        except Exception:
            lock.release()
            raise
        _start_index_job(directory, collection, link, record, lock)
        return {"job": record["id"], "resumed": True, "state": record["state"]}

    @app.get("/pdf/{dataset_id}/{filename}")
    def dataset_pdf(dataset_id: str, filename: str):
        directory = _dataset_dir(dataset_id)
        name = os.path.basename(filename)
        if name != filename or not name or name in (".", ".."):
            raise HTTPException(400, "invalid file name")
        if "/" in name or "\\" in name or "\x00" in name:
            raise HTTPException(400, "invalid file name")
        try:
            raw = contracts.read_json(Path(directory) / "dataset.json")
        except contracts.ContractError as exc:
            raise HTTPException(500, _http_detail(exc))
        if not isinstance(raw, dict):
            raise HTTPException(500, "dataset.json is not readable")
        entry = None
        for item in raw.get("sources") or []:
            if isinstance(item, str):
                item = {"name": item}
            if isinstance(item, dict) and str(item.get("name") or "") == name:
                entry = item
                break
        if entry is None:
            raise HTTPException(404, "no such file")
        stored = ingest.stored_source_path(Path(directory), entry)
        if stored is None or not stored.is_file():
            raise HTTPException(404, "no such file")
        # Inline so a browser opens its PDF viewer directly; #page=N then jumps
        # to the chunk's page.
        return FileResponse(str(stored), media_type="application/pdf",
                            content_disposition_type="inline", filename=name)

    # --- model links ------------------------------------------------------
    @app.get("/api/links")
    def links_list():
        try:
            all_links = links.links()
            return {"links": [links.mask(link) for link in all_links],
                    "path": str(links.config_path())}
        except links.LinkConfigError as exc:
            _link_config_500(exc)

    @app.post("/api/links", status_code=201)
    def links_create(payload: Optional[dict] = None):
        problem = _link_url_problem(payload)
        if problem:
            raise HTTPException(400, problem)
        try:
            link = links.create(payload or {})
        except links.LinkValidationError as exc:
            raise HTTPException(400, str(exc))
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        return {"link": links.mask(link)}

    @app.put("/api/links/{link_id}")
    def links_update(link_id: str, payload: Optional[dict] = None):
        problem = _link_url_problem(payload)
        if problem:
            raise HTTPException(400, problem)
        _guard_chunk_change(link_id, payload)
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

    @app.get("/api/links/{link_id}/indexes")
    def links_indexes(link_id: str):
        """The datasets that have an index built with this embed link."""
        link = _existing_link(link_id)
        return {"link": link_id, "space": links.embed_space_key(link),
                "datasets": _space_indexed(links.embed_space_key(link))}

    @app.delete("/api/links/{link_id}/indexes")
    def links_indexes_delete(link_id: str):
        """Delete every index built with this embed link (e.g. to change its chunk sizes)."""
        link = _existing_link(link_id)
        space = links.embed_space_key(link)
        jobs_dir = config.ensure().jobs_dir
        removed = []
        for dataset_id in _space_indexed(space):
            lock = ingest.CollectionLock(jobs_dir, dataset_id)
            try:
                lock.acquire()
            except ingest.CollectionBusy as exc:
                raise HTTPException(409, str(exc))
            try:
                shutil.rmtree(str(config.paths().datasets_dir / dataset_id / "index"
                                  / space), ignore_errors=True)
                removed.append(dataset_id)
            finally:
                lock.release()
        reload_store()
        return {"link": link_id, "space": space, "removed": removed}

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

    # --- local model control (optional, machine-local) -------------------
    def _local_models_500(exc):
        raise HTTPException(500, "the local model config is not readable: %s"
                                 % exc)

    @app.get("/api/control-status")
    def control_status():
        """One poll for a local control panel: stages, models, links, answer.

        This is a READ: it probes each configured stage link's health URL and
        reports whether the link is locally controllable, so a panel can render
        Start/Stop honestly. A missing local_models.json is a normal "no local
        models on this machine" state. A config that exists but cannot be
        trusted is reported ON THE STAGES (start_stop false, detail explains)
        instead of failing the whole poll: the panel must stay usable.
        """
        try:
            raw_links = links.links()
            default_answer = links.default_answer_link()
        except links.LinkConfigError as exc:
            _link_config_500(exc)

        local_error = ""
        try:
            localmodels.load()
        except localmodels.LocalModelsError as exc:
            local_error = str(exc)
        try:
            local_models = localmodels.models()
        except localmodels.LocalModelsError:
            local_models = []

        stages = {}
        for role in links.STAGE_ROLES:
            link = links.active(role)
            if link is None:
                # A stage link that exists but is not enabled is still worth
                # controlling: the panel may start the process before the user
                # enables the link. Prefer the first locally controllable one.
                if not local_error:
                    try:
                        link = next(
                            (candidate for candidate in links.links(role)
                             if localmodels.local_stage(candidate)), None)
                    except localmodels.LocalModelsError as exc:
                        local_error = str(exc)
            if link is None:
                stages[role] = {
                    "running": False, "model": "", "label": "",
                    "detail": local_error or "no %s link configured" % role,
                    "link_id": "", "base_url": "", "enabled": False,
                    "capabilities": {"start_stop": False, "models": []},
                }
                continue
            if local_error:
                stages[role] = {
                    "running": False, "model": "",
                    "label": link.get("label") or "",
                    "detail": local_error,
                    "link_id": link["id"],
                    "base_url": link.get("base_url") or "",
                    "enabled": bool(link.get("enabled")),
                    "capabilities": {"start_stop": False, "models": []},
                }
                continue
            try:
                entry = localmodels.status(link)
                caps = localmodels.capabilities(link)
            except localmodels.LocalModelsError as exc:
                entry = {"running": False, "model": "", "label": "",
                         "detail": str(exc)}
                caps = {"start_stop": False, "models": []}
            entry["link_id"] = link["id"]
            entry["base_url"] = link.get("base_url") or ""
            entry["enabled"] = bool(link.get("enabled"))
            entry["capabilities"] = caps
            stages[role] = entry

        return {
            "service": {"ok": True},
            "models": local_models,
            "stages": stages,
            "links": [links.mask(item) for item in raw_links],
            "answer": {"configured": default_answer is not None,
                       "default_provider": (default_answer or {}).get("id")},
        }

    @app.post("/api/links/{link_id}/start")
    def links_start(link_id: str, payload: Optional[dict] = None):
        """Start one local link's process (the embed stage takes a model)."""
        try:
            link = links.get(link_id)
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if link is None:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        model = (payload or {}).get("model")
        if model is not None and not isinstance(model, str):
            raise HTTPException(400, "model must be a string")
        try:
            out = localmodels.start(link, model=model)
        except localmodels.NotLocalError as exc:
            raise HTTPException(400, str(exc))
        except localmodels.StartError as exc:
            raise HTTPException(502, str(exc))
        except localmodels.LocalModelsError as exc:
            _local_models_500(exc)
        out["link_id"] = link_id
        return out

    @app.post("/api/links/{link_id}/stop")
    def links_stop(link_id: str, payload: Optional[dict] = None):
        """Stop one local link's process."""
        try:
            link = links.get(link_id)
        except links.LinkConfigError as exc:
            _link_config_500(exc)
        if link is None:
            raise HTTPException(404, "no link with id '%s'" % link_id)
        try:
            return localmodels.stop(link)
        except localmodels.NotLocalError as exc:
            raise HTTPException(400, str(exc))
        except localmodels.StartError as exc:
            raise HTTPException(502, str(exc))
        except localmodels.LocalModelsError as exc:
            _local_models_500(exc)

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
                error = {"type": "error", "status": exc.status_code,
                         "detail": exc.detail}
                put(error)
                out["error"] = error
            except Exception as exc:  # noqa: BLE001
                error = {"type": "error", "status": 500,
                         "detail": _http_detail(exc)}
                put(error)
                out["error"] = error

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
                _agent_probe_gate(req)
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

    security.install(app)
    return app


app = create_app()
