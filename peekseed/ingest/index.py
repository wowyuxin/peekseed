"""Dataset storage, generation-atomic vector indexes and job records.

A dataset directory owns the uploaded files, the canonical ``chunks.jsonl`` and
one index per embedding space::

    <dataset>/dataset.json
    <dataset>/original/...                    uploaded copies served by /pdf
    <dataset>/chunks.jsonl
    <dataset>/index/<space_key>/
        CURRENT                               one line: the live generation uuid
        generations/<uuid>/
            vectors.npy  meta.json  info.json  probe.npy  progress.json

The index is deliberately plain numpy: float32 ``[N, dim]`` in row order with the
chunk metadata, so loading needs no vector database. A build writes into a new
generation directory and flips ``CURRENT`` with a single atomic rename **last**:
a crash mid-build leaves the previous generation serving, and the incomplete
generation keeps a per-batch checkpoint so the next build resumes instead of
re-embedding the whole corpus. Loading validates the schema, row alignment,
unique ids and dimension; an invalid index is reported as needing a rebuild
(:class:`NeedsReindex`) rather than silently skipped.

Every index records the full identity of the embedding space it was built in
(link id, model, protocol, dimension, normalization, prefixes) plus a stored
**probe** vector. Re-embedding the fixed probe text at query time and comparing
cosine proves the live server is still the same embedding space; a mismatch is
an actionable error instead of a silent comparison of incompatible vectors.

The only network dependency is the configured embed link. Both supported link
protocols are implemented here -- OpenAI-compatible ``POST {base}/embeddings``
and a custom ``POST {base}/embed`` -- and the same functions (:func:`embed_texts`
and :func:`embed_query`) are reused by the API for search queries, so request
shapes live in exactly one place.

A link's API key is written only into request headers; it is never embedded in an
exception message or a returned dictionary.
"""

import hashlib
import json
import math
import os
import random
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from .. import contracts
from .chunk import chunk_pages
from .pdf import IngestError, extract_pdf, page_count

__all__ = [
    "IndexCancelled",
    "IndexState",
    "NeedsReindex",
    "ProbeMismatch",
    "active_job_for",
    "add_source",
    "build_index",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "index_state",
    "job_cancel_requested",
    "job_path",
    "list_jobs",
    "load_index",
    "mark_interrupted_jobs",
    "probe_cosine",
    "read_chunks",
    "read_job",
    "reindex",
    "request_cancel",
    "sanitize_filename",
    "verify_probe",
    "write_chunks",
    "write_job",
]

INFO_SCHEMA = contracts.INFO_SCHEMA
DEFAULT_BATCH_SIZE = 32
EMBED_TIMEOUT = 120.0
MAX_DATASET_ID = 64
MAX_FILENAME_LENGTH = 120

#: Retry policy for transient embed failures (429/5xx/timeouts/connect errors).
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 4.0

#: Layout names of one index space.
CURRENT_FILE = "CURRENT"
GENERATIONS_DIR = "generations"
PROGRESS_FILE = "progress.json"
PROBE_FILE = "probe.npy"

#: The probe gate: a live embedder must reproduce the stored probe this closely.
PROBE_MIN_COSINE = 0.999

#: Rough throughput used only for cost estimates in ``reindex --dry-run``.
ESTIMATE_SECONDS_PER_CHUNK = 0.06
ESTIMATE_SECONDS_PER_REMOTE_BATCH = 1.5

_META_FIELDS = (
    "id",
    "section",
    "chapter",
    "numbering",
    "parent",
    "path",
    "page_start",
    "page_end",
    "pdf_y",
    "doc",
    "text",
    "heading_only",
    "source",
)
# Provenance fields added by content-derived chunking; copied when present so an
# older chunks.jsonl keeps loading unchanged.
_META_PROVENANCE_FIELDS = (
    "source_sha256",
    "block_index",
    "chunk_index",
    "chunker_version",
    "ordinal",
    "block_kind",
)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


class NeedsReindex(IngestError):
    """The index on disk is missing, unreadable or internally inconsistent."""


class ProbeMismatch(IngestError):
    """The live embedder does not reproduce the index's stored probe vector."""


class IndexCancelled(IngestError):
    """A cancelled build; the previous generation is still serving."""


# --- small utilities -------------------------------------------------------


def _collapse_text(value):
    return " ".join(str(value or "").split())


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now():
    return contracts.utc_now()


def _size(path):
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


def _silent_unlink(path):
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise IngestError("Could not read '%s': invalid JSON (%s)." % (path.name, exc)) from None
    except OSError as exc:
        raise IngestError("Could not read '%s': %s." % (path.name, exc)) from None


def _write_json_atomic(path, data, *, indent=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if indent:
        payload = json.dumps(data, ensure_ascii=False, indent=indent)
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    _write_text_atomic(path, payload + "\n")


def _write_text_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except BaseException:
        _silent_unlink(temporary)
        raise


def _write_lines_atomic(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except BaseException:
        _silent_unlink(temporary)
        raise


def _save_npy_atomic(path, array):
    """Write one ``.npy`` file atomically (temp file + fsync + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            np.save(stream, np.asarray(array, dtype=np.float32))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except BaseException:
        _silent_unlink(temporary)
        raise


def _save_vectors(path, vectors):
    _save_npy_atomic(path, vectors)


def _datasets_dir(paths):
    """Resolve the datasets directory from a config ``Paths`` object or a path."""
    if paths is None:
        raise IngestError("A data-root paths object is required.")
    value = getattr(paths, "datasets_dir", None)
    if value is None and isinstance(paths, dict):
        value = paths.get("datasets_dir")
    if value is None:
        value = paths  # callers may pass the datasets directory directly
    if not isinstance(value, (str, os.PathLike)):
        raise IngestError(
            "A data-root paths object or the datasets directory is required."
        )
    return Path(value)


def _progress_function(progress):
    """Wrap the optional progress callback so a broken reporter cannot abort work."""
    if progress is None:
        return lambda fraction, message: None

    def report(fraction, message):
        try:
            progress(max(0.0, min(1.0, float(fraction))), str(message))
        except Exception:
            # Progress is observability only; indexing must finish regardless.
            pass

    return report


def _read_dataset_json(dataset_dir):
    dataset_dir = Path(dataset_dir)
    path = dataset_dir / "dataset.json"
    data = _read_json(path, None)
    if not isinstance(data, dict):
        raise IngestError(
            "dataset.json is missing or invalid in dataset '%s'." % dataset_dir.name
        )
    return data


def _dataset_slug(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = text.strip("-._")
    return text[:MAX_DATASET_ID].rstrip("-._") or "dataset"


def _unique_dataset_id(datasets_dir, name):
    base = _dataset_slug(name)
    candidate = base
    counter = 2
    while (datasets_dir / candidate).exists():
        suffix = "-%d" % counter
        stem = base[: MAX_DATASET_ID - len(suffix)].rstrip("-._") or "dataset"
        candidate = stem + suffix
        counter += 1
    return candidate


# --- filenames and datasets ------------------------------------------------


def sanitize_filename(name):
    """Reduce an uploaded filename to a safe basename for ``original/``.

    Directory components are dropped (never joined onto a path), control and
    Windows-reserved characters are replaced, leading dots are removed so hidden
    or traversal-like names cannot be created, and the result is bounded in
    length while keeping the extension.
    """
    raw = str(name or "").replace("\\", "/")
    base = raw.rsplit("/", 1)[-1]
    base = _INVALID_FILENAME_CHARS.sub("_", base)
    base = _collapse_text(base).lstrip(".").strip()
    if not base:
        return "document"
    stem, dot, extension = base.rpartition(".")
    if dot and stem and 0 < len(extension) <= 12:
        stem = stem[:100].rstrip(" .") or "document"
        return "%s.%s" % (stem, extension)
    return base[:MAX_FILENAME_LENGTH].rstrip(" .") or "document"


def create_dataset(paths, name, note=""):
    """Create one dataset directory and return its metadata dictionary.

    The caller copies uploaded files into the returned ``dir`` ``/original``
    directory (using :func:`sanitize_filename` for the basenames) and records
    their names in ``dataset.json`` ``sources``. The ``dir`` key is a convenience
    for the caller and is not written to ``dataset.json``.
    """
    display = _collapse_text(name)
    if not display:
        raise IngestError("A dataset name is required.")
    display = display[:120].rstrip()
    datasets_dir = _datasets_dir(paths)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    dataset_id = _unique_dataset_id(datasets_dir, display)
    dataset_dir = datasets_dir / dataset_id
    (dataset_dir / "original").mkdir(parents=True, exist_ok=True)
    dataset = {
        "schema": contracts.DATASET_SCHEMA,
        "id": dataset_id,
        "name": display,
        "created_at": _now(),
        "sources": [],
        "notes": str(note or ""),
    }
    _write_json_atomic(dataset_dir / "dataset.json", dataset, indent=2)
    result = dict(dataset)
    result["dir"] = str(dataset_dir)
    return result


def _index_info_paths(dataset_dir):
    """``[(space_name, info_path)]`` for every index in a dataset.

    Supports both the historical flat ``index/<space>/info.json`` layout and the
    generation layout ``index/<space>/CURRENT`` -> ``generations/<uuid>/info.json``.
    ``CURRENT`` wins when present.
    """
    dataset_dir = Path(dataset_dir)
    index_root = dataset_dir / "index"
    if not index_root.is_dir():
        return []
    found = []
    for entry in sorted(index_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        current = entry / CURRENT_FILE
        candidate = None
        if current.is_file():
            try:
                generation = current.read_text(encoding="utf-8").strip().splitlines()[0]
            except (OSError, IndexError):
                generation = ""
            if generation:
                candidate = entry / GENERATIONS_DIR / generation.strip() / "info.json"
            if candidate is None or not candidate.is_file():
                continue
        else:
            candidate = entry / "info.json"
        if candidate.is_file():
            found.append((entry.name, candidate))
    return found


def dataset_indexes(dataset_dir):
    """Return the ``info.json`` dictionaries of every index in a dataset.

    Directories without a readable ``info.json`` are skipped: one damaged index
    must not hide the healthy ones from the datasets listing.
    """
    dataset_dir = Path(dataset_dir)
    found = []
    for space, info_path in _index_info_paths(dataset_dir):
        try:
            raw = _read_json(info_path, None)
        except IngestError:
            continue
        if not isinstance(raw, dict):
            continue
        try:
            info = contracts.ensure_current(raw, "info.json")
        except contracts.ContractError:
            # A schema-1 index still belongs in the listing; the UI shows it as
            # needing an index in the active space rather than hiding the dataset.
            info = dict(raw)
        info.setdefault("space", space)
        info.setdefault("dataset", dataset_dir.name)
        found.append(info)
    found.sort(key=lambda item: str(item.get("space") or ""))
    return found


def dataset_dir(paths, dataset_id=None):
    """Resolve one dataset directory.

    Two forms are supported:

    * ``dataset_dir(paths, dataset_id)`` -- ``paths`` is a config ``Paths``
      object (or the datasets directory itself) and ``dataset_id`` names the
      dataset; this is the explicit form used by the API.
    * ``dataset_dir(dataset_id)`` -- the data root is resolved lazily from
      :mod:`peekseed.config`; an absolute path or a ``create_dataset()`` result
      (which carries ``dir``) is returned as-is.

    ``config`` is imported lazily (never at module import time) so ingestion
    stays usable in a headless test that only hands over a temporary directory.
    """
    if dataset_id is None:
        if isinstance(paths, dict):
            directory = paths.get("dir")
            if directory:
                return Path(directory)
            paths = paths.get("id")
        value = str(paths or "").strip()
        if not value:
            raise IngestError("A dataset id or dataset directory is required.")
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        return _default_datasets_dir() / value
    value = str(dataset_id or "").strip()
    if not value:
        raise IngestError("A dataset id is required.")
    return _datasets_dir(paths) / value


def _default_datasets_dir():
    try:
        from .. import config

        return config.paths().datasets_dir
    except Exception as exc:
        raise IngestError(
            "Could not resolve the data root (%s); pass the datasets directory "
            "explicitly." % type(exc).__name__
        ) from None


def _source_entries(raw):
    """Normalise ``dataset.json``'s ``sources`` to a list of dictionaries."""
    entries = []
    for item in raw or []:
        if isinstance(item, dict):
            entries.append(dict(item))
        else:
            name = str(item or "").strip()
            if name:
                entries.append({"name": name})
    return entries


def _source_names(dataset):
    raw = dataset.get("sources") if isinstance(dataset, dict) else None
    return [
        str(entry.get("name") or "").strip()
        for entry in _source_entries(raw)
        if str(entry.get("name") or "").strip()
    ]


def _file_digest(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _same_file_content(left, right):
    """True when two paths hold byte-identical content (used to avoid re-copies)."""
    try:
        if _size(left) != _size(right):
            return False
        return _file_digest(left) == _file_digest(right)
    except OSError:
        return False


def _split_name(name):
    """Split ``name`` into ``(stem, ".ext")`` while keeping extension-less names."""
    stem, dot, suffix = str(name).rpartition(".")
    if dot and stem and 0 < len(suffix) <= 12:
        return stem, "." + suffix
    return str(name), ""


def _dataset_root(dataset):
    if isinstance(dataset, dict):
        raw = dataset.get("dir")
        if raw:
            return Path(raw)
        raise IngestError(
            "The dataset metadata has no directory; pass create_dataset()'s result."
        )
    if isinstance(dataset, (str, os.PathLike)):
        return Path(dataset)
    raise IngestError("A dataset dictionary or dataset directory is required.")


def add_source(dataset, source_path):
    """Copy one source file into a dataset's ``original/`` directory.

    ``dataset`` is the metadata dictionary returned by :func:`create_dataset`
    (it must carry the dataset directory under ``dir``); a dataset directory path
    is also accepted. The filename is sanitised, an existing different file with
    the same name is never overwritten (a numbered suffix is used instead), and
    the resulting ``{name, path, sha256, bytes, pages, extractor, status}`` entry
    is appended to ``dataset.json`` ``sources`` and returned. Adding the same file
    twice is idempotent and returns the existing entry.
    """
    dataset_dir_path = _dataset_root(dataset)
    source = Path(source_path)
    if not source.is_file():
        raise IngestError("Source file not found: '%s'." % source.name)
    metadata = _read_dataset_json(dataset_dir_path)
    original = dataset_dir_path / "original"
    original.mkdir(parents=True, exist_ok=True)

    base_name = sanitize_filename(source.name)
    name = base_name
    target = original / name
    counter = 2
    while target.exists() and not _same_file_content(target, source):
        stem, suffix = _split_name(base_name)
        name = "%s-%d%s" % (stem, counter, suffix)
        target = original / name
        counter += 1
    if not target.exists():
        try:
            shutil.copyfile(str(source), str(target))
        except OSError as exc:
            raise IngestError(
                "Could not store '%s' in the dataset: %s." % (base_name, exc)
            ) from None

    pages = 0
    extractor = ""
    if target.suffix.lower() == ".pdf":
        try:
            pages = page_count(target)
        except IngestError:
            pages = 0
    entry = {
        "name": name,
        "path": "original/" + name,
        "sha256": _file_digest(target),
        "bytes": _size(target),
        "pages": pages,
        "extractor": extractor,
        "status": "new",
    }

    sources = _source_entries(metadata.get("sources"))
    for index, existing in enumerate(sources):
        if str(existing.get("name") or "") == name:
            if not existing.get("bytes") or not existing.get("pages") or not existing.get("sha256"):
                sources[index] = {**dict(existing), **entry}
                metadata["sources"] = sources
                _write_json_atomic(dataset_dir_path / "dataset.json", metadata, indent=2)
            if isinstance(dataset, dict):
                dataset["sources"] = _source_entries(metadata.get("sources"))
            return entry
    sources.append(entry)
    metadata["schema"] = contracts.DATASET_SCHEMA
    metadata["sources"] = sources
    metadata.setdefault("updated_at", _now())
    _write_json_atomic(dataset_dir_path / "dataset.json", metadata, indent=2)
    if isinstance(dataset, dict):
        dataset["sources"] = [dict(item) for item in sources]
    return entry


def _original_files(dataset_dir):
    original = Path(dataset_dir) / "original"
    if not original.is_dir():
        return []
    return sorted(path for path in original.iterdir() if path.is_file())


def _count_chunks(dataset_dir):
    path = Path(dataset_dir) / "chunks.jsonl"
    if not path.is_file():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def dataset_info(dataset_dir):
    """Read one dataset's metadata, enriched with chunk and index state.

    ``sources`` is a list of source filenames; when ``dataset.json`` does not
    declare any yet (for example after a CLI upload) the files actually present
    under ``original/`` are reported instead.
    """
    dataset_dir = Path(dataset_dir)
    dataset = _read_dataset_json(dataset_dir)
    sources = _source_names(dataset)
    if not sources:
        sources = [path.name for path in _original_files(dataset_dir)]
    return {
        "version": contracts.DATASET_SCHEMA,
        "id": str(dataset.get("id") or dataset_dir.name),
        "name": str(dataset.get("name") or dataset.get("id") or dataset_dir.name),
        "created_at": str(dataset.get("created_at") or ""),
        "notes": str(dataset.get("notes") or ""),
        "sources": sources,
        "chunks": _count_chunks(dataset_dir),
        "indexes": dataset_indexes(dataset_dir),
        "dir": str(dataset_dir),
    }


# --- chunks ----------------------------------------------------------------


def read_chunks(dataset_dir):
    """Read ``chunks.jsonl`` as a list of records (empty when the file is absent)."""
    path = Path(dataset_dir) / "chunks.jsonl"
    if not path.is_file():
        return []
    chunks = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise IngestError(
                    "chunks.jsonl line %d is not valid JSON: %s" % (number, exc)
                ) from None
            if not isinstance(record, dict):
                raise IngestError("chunks.jsonl line %d is not a JSON object." % number)
            chunks.append(record)
    return chunks


def write_chunks(dataset_dir, chunks):
    """Atomically write the canonical chunk records as JSON Lines."""
    records = list(chunks or [])
    for record in records:
        if not isinstance(record, dict):
            raise IngestError("Each chunk record must be a JSON object.")
    lines = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        for record in records
    )
    _write_lines_atomic(Path(dataset_dir) / "chunks.jsonl", lines)


def _meta_record(chunk):
    page_start = _int(chunk.get("page_start"), 0)
    record = {
        "id": str(chunk.get("id") or ""),
        "section": str(chunk.get("section") or ""),
        "chapter": str(chunk.get("chapter") or ""),
        "numbering": str(chunk.get("numbering") or ""),
        "parent": str(chunk.get("parent") or ""),
        "path": str(chunk.get("path") or ""),
        "page_start": page_start,
        "page_end": _int(chunk.get("page_end"), page_start),
        "pdf_y": _int(chunk.get("pdf_y"), 0),
        "doc": str(chunk.get("doc") or ""),
        "text": str(chunk.get("text") or chunk.get("doc") or ""),
        "heading_only": bool(chunk.get("heading_only")),
        "source": str(chunk.get("source") or ""),
    }
    for field in _META_PROVENANCE_FIELDS:
        if chunk.get(field) is not None:
            record[field] = chunk[field]
    return {field: record[field] for field in _META_FIELDS if field in record} | {
        field: record[field] for field in _META_PROVENANCE_FIELDS if field in record
    }


def _embedding_text(chunk):
    text = str(chunk.get("doc") or chunk.get("text") or "").strip()
    if text:
        return text
    fallback = str(chunk.get("section") or chunk.get("id") or "").strip()
    return fallback or "document"


# --- embedding link --------------------------------------------------------


def _space_slug(value):
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return text.strip("-._")[:48] or "default"


def _links_space_key():
    try:
        from ..links import embed_space_key as ready
    except Exception:
        return None
    return ready


def embed_space_key(link):
    """The ``index/<space_key>`` directory name for one embedding link.

    The rule is owned by :mod:`peekseed.contracts.space_key` (and mirrored by
    :mod:`peekseed.links`); the local fallback keeps ingestion usable headlessly.
    """
    if not isinstance(link, dict):
        raise IngestError("An embedding link dictionary is required.")
    try:
        return contracts.space_key(link)
    except contracts.ContractError:
        return "%s__%s" % (_space_slug(link.get("id")), _space_slug(link.get("model")))


def _link_target(link):
    """Validate a link and return ``(protocol, base_url, model, api_key)``."""
    if not isinstance(link, dict):
        raise IngestError("An embedding link is required to build or query an index.")
    base_url = str(link.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise IngestError("The embedding link has no base URL configured.")
    protocol = str(link.get("protocol") or "openai").strip().lower()
    if protocol not in ("openai", "custom"):
        raise IngestError(
            "Unsupported embedding protocol '%s'; expected 'openai' or 'custom'."
            % protocol
        )
    return (
        protocol,
        base_url,
        str(link.get("model") or ""),
        str(link.get("api_key") or ""),
    )


def _timeout_value(timeout, link):
    if timeout is not None:
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            raise IngestError("The embedding timeout must be a number of seconds.") from None
        if value <= 0:
            raise IngestError("The embedding timeout must be positive.")
        return value
    if isinstance(link, dict):
        configured = link.get("timeout")
        if configured is not None:
            try:
                value = float(configured)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
    return EMBED_TIMEOUT


def _response_detail(response):
    try:
        text = response.text or ""
    except Exception:
        return ""
    return _collapse_text(text)[:300]


def _post_embeddings(target, texts, timeout):
    protocol, base_url, model, api_key = target
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        with httpx.Client(timeout=timeout) as client:
            if protocol == "custom":
                response = client.post(
                    base_url + "/embed",
                    json={"inputs": [{"text": text} for text in texts]},
                    headers=headers,
                )
            else:
                body = {"input": list(texts)}
                if model:
                    body["model"] = model
                response = client.post(
                    base_url + "/embeddings", json=body, headers=headers
                )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = _response_detail(exc.response)
        if api_key and detail:
            # A chatty server may echo request headers; the key must never leak.
            detail = detail.replace(api_key, "***")
        suffix = (": " + detail) if detail else ""
        raise IngestError(
            "The embedding link returned HTTP %d%s."
            % (exc.response.status_code, suffix)
        ) from None
    except httpx.HTTPError as exc:
        raise IngestError(
            "Could not reach the embedding link (%s)." % type(exc).__name__
        ) from None
    except ValueError as exc:
        raise IngestError("The embedding link returned invalid JSON: %s" % exc) from None
    except Exception as exc:
        raise IngestError(
            "The embedding request failed (%s)." % type(exc).__name__
        ) from None
    return _parse_embeddings(protocol, data, len(texts))


def _parse_embeddings(protocol, data, expected):
    if not isinstance(data, dict):
        raise IngestError("The embedding link returned an unexpected JSON value.")
    if protocol == "custom":
        rows = data.get("embeddings")
        label = "embeddings"
    else:
        rows = data.get("data")
        label = "data[].embedding"
        if isinstance(rows, list):
            rows = [row.get("embedding") if isinstance(row, dict) else row for row in rows]
    if not isinstance(rows, list):
        raise IngestError("The embedding link response has no '%s' array." % label)
    if len(rows) != expected:
        raise IngestError(
            "The embedding link returned %d vector(s) for %d input(s); expected one "
            "vector per input." % (len(rows), expected)
        )
    vectors = []
    dim = 0
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or not row:
            raise IngestError(
                "The embedding link returned an empty vector at position %d." % index
            )
        try:
            vector = [float(value) for value in row]
        except (TypeError, ValueError):
            raise IngestError(
                "The embedding link returned a non-numeric vector at position %d."
                % index
            ) from None
        if not all(math.isfinite(value) for value in vector):
            raise IngestError(
                "The embedding link returned a non-finite value at position %d."
                % index
            )
        if dim == 0:
            dim = len(vector)
        elif len(vector) != dim:
            raise IngestError(
                "The embedding link returned vectors with mixed dimensions "
                "(%d and %d)." % (dim, len(vector))
            )
        vectors.append(vector)
    return vectors


def embed_texts(link, texts, *, batch_size=DEFAULT_BATCH_SIZE, timeout=None):
    """Embed texts through a link, returning one numeric vector per input.

    Both supported protocols are handled: OpenAI-compatible
    ``POST {base_url}/embeddings`` with ``{"model", "input": [...]}`` and custom
    ``POST {base_url}/embed`` with ``{"inputs": [{"text": ...}, ...]}``. Requests
    are split into ``batch_size`` inputs per call; the response length and every
    vector dimension are validated so a misconfigured server fails loudly instead
    of writing a misaligned index.

    Raises :class:`IngestError` with a user-facing message on any failure; the
    message never contains the link's API key.
    """
    target = _link_target(link)
    try:
        batch = int(batch_size)
    except (TypeError, ValueError):
        raise IngestError("batch_size must be an integer.") from None
    if batch <= 0:
        raise IngestError("batch_size must be positive.")
    timeout_value = _timeout_value(timeout, link)
    payload = [str(text) if str(text).strip() else " " for text in (texts or [])]
    if not payload:
        return []
    results = []
    expected_dim = 0
    for start in range(0, len(payload), batch):
        subset = payload[start : start + batch]
        batch_vectors = _post_embeddings(target, subset, timeout_value)
        for vector in batch_vectors:
            if expected_dim == 0:
                expected_dim = len(vector)
            elif len(vector) != expected_dim:
                raise IngestError(
                    "The embedding link returned vectors with mixed dimensions "
                    "(%d and %d)." % (expected_dim, len(vector))
                )
        results.extend(batch_vectors)
    return results


def embed_query(link, text, *, timeout=None):
    """Embed one search query and return its vector as a list of floats."""
    vectors = embed_texts(link, [text], batch_size=1, timeout=timeout)
    if not vectors:
        raise IngestError("The embedding link returned no vector for the query.")
    return vectors[0]


def _retryable(exc):
    """True for transient embed failures worth retrying (429/5xx/timeout/connect)."""
    message = str(exc)
    match = _HTTP_STATUS.search(message)
    if match:
        status = int(match.group(1))
        return status == 429 or status >= 500
    return "Could not reach" in message or "timed out" in message.lower()


def _embed_with_retry(link, texts, *, batch_size, timeout=None, cancel=None):
    """Embed one batch, retrying transient failures with backoff and jitter."""
    attempt = 0
    while True:
        try:
            return embed_texts(link, texts, batch_size=batch_size, timeout=timeout)
        except IngestError as exc:
            attempt += 1
            if cancel is not None and cancel():
                raise IndexCancelled(
                    "the index build was cancelled; the previous index is still "
                    "serving and this build can be resumed"
                ) from None
            if not _retryable(exc) or attempt >= RETRY_ATTEMPTS:
                raise
            delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            delay += random.uniform(0.0, delay * 0.25)
            time.sleep(delay)


# --- index layout ----------------------------------------------------------


def _space_dir(dataset_dir, space):
    return Path(dataset_dir) / "index" / str(space)


def _read_current(space_dir):
    """The live generation name written in ``CURRENT`` (or None if absent)."""
    current = Path(space_dir) / CURRENT_FILE
    if not current.is_file():
        return None
    try:
        text = current.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text.splitlines()[0].strip() if text else None


def _resolve_generation_dir(space_dir):
    """The directory holding the live generation of an index space.

    ``CURRENT`` names it; a legacy schema-1 space keeps its files directly in the
    space directory, so it is used when no ``CURRENT`` file exists. A dangling
    ``CURRENT`` returns ``None`` (the index needs a rebuild).
    """
    space_dir = Path(space_dir)
    generation = _read_current(space_dir)
    if generation is None:
        return space_dir if (space_dir / "info.json").is_file() else None
    candidate = space_dir / GENERATIONS_DIR / generation
    return candidate if (candidate / "info.json").is_file() else None


def _generations_dir(space_dir):
    return Path(space_dir) / GENERATIONS_DIR


def _generation_dirs(space_dir):
    root = _generations_dir(space_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _write_current(space_dir, generation):
    """Flip an index space to a generation. Always the last write of a build."""
    _write_text_atomic(Path(space_dir) / CURRENT_FILE, str(generation) + "\n")


def _load_info(info_path):
    raw = contracts.read_json(info_path)
    if not isinstance(raw, dict):
        raise NeedsReindex("index metadata is missing or not a JSON object")
    try:
        return contracts.ensure_current(raw, "info.json")
    except contracts.SchemaTooNew as exc:
        raise NeedsReindex(str(exc)) from None
    except contracts.ContractError as exc:
        raise NeedsReindex("index metadata is invalid: %s" % exc) from None


def load_index(dataset_dir, space):
    """Load one index space, validating it before use.

    Returns ``{"info", "vectors", "meta", "dir", "space"}``. Raises
    :class:`NeedsReindex` with a user-facing reason when the index is missing,
    points at a missing generation, has unreadable files, or fails validation
    (schema, row alignment, unique ids, dimension). A broken index must be
    reported as needing a rebuild, never silently skipped or half-served.
    """
    dataset_dir = Path(dataset_dir)
    space_dir = _space_dir(dataset_dir, space)
    if not space_dir.is_dir():
        raise NeedsReindex(
            "there is no index for space '%s' in dataset '%s'; build it first"
            % (space, dataset_dir.name)
        )
    generation_dir = _resolve_generation_dir(space_dir)
    if generation_dir is None:
        raise NeedsReindex(
            "index space '%s' in dataset '%s' has no usable generation (CURRENT "
            "is missing or dangling); rebuild the index" % (space, dataset_dir.name)
        )
    info = _load_info(generation_dir / "info.json")
    try:
        vectors = np.load(str(generation_dir / "vectors.npy"))
    except Exception as exc:  # corrupt/truncated npy is a rebuild, not a crash
        raise NeedsReindex(
            "the index vectors of space '%s' are unreadable (%s); rebuild the index"
            % (space, type(exc).__name__)
        ) from None
    meta = contracts.read_json(generation_dir / "meta.json")
    if not isinstance(meta, list):
        raise NeedsReindex("the index metadata rows of space '%s' are not a list" % space)
    if not isinstance(vectors, np.ndarray) or vectors.ndim != 2:
        raise NeedsReindex("the index vectors of space '%s' are not a 2-d array" % space)
    rows, width = int(vectors.shape[0]), int(vectors.shape[1])
    if rows != len(meta):
        raise NeedsReindex(
            "the index of space '%s' has %d vector(s) for %d metadata row(s)"
            % (space, rows, len(meta))
        )
    if rows != int(info.get("n") or 0):
        raise NeedsReindex(
            "the index of space '%s' records %s chunk(s) but holds %d"
            % (space, info.get("n"), rows)
        )
    if width != int(info.get("dim") or 0):
        raise NeedsReindex(
            "the index of space '%s' records dimension %s but holds %d"
            % (space, info.get("dim"), width)
        )
    ids = {}
    for position, row in enumerate(meta):
        if not isinstance(row, dict):
            raise NeedsReindex("index metadata row %d is not an object" % position)
        identifier = str(row.get("id") or "")
        if not identifier:
            raise NeedsReindex("index metadata row %d has no chunk id" % position)
        if identifier in ids:
            raise NeedsReindex(
                "duplicate chunk id '%s' in the index of space '%s'" % (identifier, space)
            )
        ids[identifier] = position
    return {
        "info": info,
        "vectors": vectors,
        "meta": meta,
        "dir": str(generation_dir),
        "space": str(space),
    }


def _dataset_sources_for_digest(dataset_dir):
    """Current source rows (path+sha256) used to judge whether an index is stale."""
    dataset_dir = Path(dataset_dir)
    dataset = _read_dataset_json(dataset_dir)
    rows = []
    for entry in _source_entries(dataset.get("sources")):
        sha = str(entry.get("sha256") or "").lower()
        if not sha:
            continue
        rows.append({
            "path": str(entry.get("path") or entry.get("name") or ""),
            "sha256": sha,
        })
    return rows


def index_state(dataset_dir, space, *, chunker=None, extractor=None):
    """One space's state: ``indexed``, ``stale``, ``needs_reindex`` or ``not_indexed``.

    ``stale`` means the index is readable but was built from different inputs
    (source set, chunker version or extractor version changed). ``needs_reindex``
    means it cannot be served at all. Both carry short human reasons.
    """
    dataset_dir = Path(dataset_dir)
    space_dir = _space_dir(dataset_dir, space)
    if not space_dir.is_dir():
        return {"status": "not_indexed", "reasons": ["no index for this space"],
                "generation": "", "space": str(space)}
    try:
        loaded = load_index(dataset_dir, space)
    except NeedsReindex as exc:
        return {"status": "needs_reindex", "reasons": [str(exc)],
                "generation": "", "space": str(space)}
    info = loaded["info"]
    reasons = contracts.stale_reasons(
        info, _dataset_sources_for_digest(dataset_dir),
        chunker=chunker, extractor=extractor,
    )
    if not contracts.has_probe(info):
        reasons.append("index has no stored embedding probe")
    status = "stale" if reasons else "indexed"
    return {
        "status": status,
        "reasons": reasons,
        "generation": str(info.get("generation") or ""),
        "space": str(space),
        "n": int(info.get("n") or 0),
        "dim": int(info.get("dim") or 0),
    }


# --- probe gate -----------------------------------------------------------


def _probe_text(link):
    prefix = str(link.get("doc_prefix") or "") if isinstance(link, dict) else ""
    return prefix + contracts.PROBE_TEXT


def _normalise_vector(vector):
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    if norm > 0:
        array = array / norm
    return array


def _save_probe(path, vector):
    _save_npy_atomic(path, _normalise_vector(vector))


def _probe_path(generation_dir):
    return Path(generation_dir) / PROBE_FILE


def _load_probe(probe_file, space):
    """The stored, normalised probe vector, or an actionable NeedsReindex."""
    if not probe_file.is_file():
        raise NeedsReindex(
            "the index of space '%s' has no stored probe vector; rebuild the index "
            "(or run `peekseed migrate`) so a space mismatch can be detected" % space
        )
    try:
        stored = np.load(str(probe_file))
    except Exception as exc:
        raise NeedsReindex(
            "the stored probe vector of space '%s' is unreadable (%s)"
            % (space, type(exc).__name__)
        ) from None
    return _normalise_vector(stored)


def _live_probe(link):
    try:
        return _normalise_vector(embed_query(link, _probe_text(link), timeout=None))
    except IngestError as exc:
        raise ProbeMismatch(
            "could not embed the probe text through the active link: %s" % exc
        ) from None


def _probe_cosine(stored, live):
    if stored.shape != live.shape:
        raise ProbeMismatch(
            "the live embedder returned dimension %d but the index was built at "
            "dimension %d; the embedding space changed. Reindex the dataset into "
            "a new space, or restore the embedder that built it."
            % (live.shape[0], stored.shape[0])
        )
    return float(np.dot(stored, live))


def probe_cosine_for_generation(link, generation_dir, space):
    """Cosine between a loaded generation's stored probe and the live embedder.

    Same check as :func:`probe_cosine`, but it reads ``probe.npy`` directly from
    the generation directory a caller already loaded. That keeps the query-time
    gate cheap: a search must not reload ``vectors.npy`` just to verify the
    embedding space.
    """
    stored = _load_probe(_probe_path(generation_dir), space)
    return _probe_cosine(stored, _live_probe(link))


def probe_cosine(link, dataset_dir, space):
    """Cosine between the stored probe vector and the live embedder's probe output.

    Embeds the fixed probe text through ``link``, normalises both vectors and
    returns their dot product (1.0 = identical space). Raises
    :class:`NeedsReindex` when the space has no stored probe and
    :class:`ProbeMismatch` when the live call fails to produce a vector.
    """
    loaded = load_index(dataset_dir, space)
    return probe_cosine_for_generation(link, loaded["dir"], space)


def _check_cosine(cosine):
    if cosine < PROBE_MIN_COSINE:
        raise ProbeMismatch(
            "the embedder does not match the index's embedding space (probe "
            "cosine %.3f < %.3f). The model, weights or server changed: use the "
            "link that built this index, or reindex into a new space."
            % (cosine, PROBE_MIN_COSINE)
        )
    return cosine


def verify_probe_for_generation(link, generation_dir, space):
    """Refuse to search a loaded generation whose live embedder no longer matches it."""
    return _check_cosine(probe_cosine_for_generation(link, generation_dir, space))


def verify_probe(link, dataset_dir, space):
    """Refuse to search a space whose live embedder no longer matches it."""
    return _check_cosine(probe_cosine(link, dataset_dir, space))


# --- jobs ------------------------------------------------------------------


def _jobs_dir(dataset_dir):
    """The persisted jobs directory for a dataset (``.peekseed/jobs``)."""
    try:
        from .. import config

        return config.paths().jobs_dir
    except Exception:
        return Path(dataset_dir).parent.parent / "jobs"


def _persist_job(jobs_dir, job):
    """Write a job record atomically; returns False when the directory is unwritable.

    A ``cancel_requested`` flag set through the API while the builder is between
    batches must survive the builder's next progress write: the flag is a
    one-way latch, so it is re-read from disk and preserved here.
    """
    try:
        data = contracts.validate_job(job)
        path = Path(jobs_dir) / (job["id"] + ".json")
        try:
            existing = contracts.read_json(path)
        except contracts.ContractError:
            existing = None
        if isinstance(existing, dict) and existing.get("cancel_requested"):
            data["cancel_requested"] = True
        contracts.write_json_atomic(path, data)
        return True
    except (OSError, contracts.ContractError):
        return False


# --- job store (the API's source of truth is the record on disk) -----------

JOB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def job_path(jobs_dir, job_id):
    """The record path for a validated job id (rejects path traversal)."""
    identifier = str(job_id or "").strip()
    if not JOB_ID_RE.match(identifier):
        raise IngestError("invalid job id")
    return Path(jobs_dir) / (identifier + ".json")


def read_job(jobs_dir, job_id):
    """One validated job record, or None when it does not exist."""
    path = job_path(jobs_dir, job_id)
    if not path.is_file():
        return None
    try:
        raw = contracts.read_json(path)
    except contracts.ContractError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return contracts.validate_job(raw)
    except contracts.ContractError:
        return None


def write_job(jobs_dir, job):
    """Persist one validated job record and return the normalised copy."""
    data = contracts.validate_job(job)
    path = job_path(jobs_dir, data["id"])
    contracts.write_json_atomic(path, data)
    return data


def list_jobs(jobs_dir, *, state=None, collection=None, limit=200):
    """Every readable job record, newest first.

    Corrupt or unreadable records are skipped rather than failing the list: one
    bad file must not hide the jobs a user can still act on.
    """
    root = Path(jobs_dir)
    rows = []
    try:
        names = sorted(root.glob("*.json"))
    except OSError:
        return rows
    states = None
    if state:
        states = {part.strip() for part in str(state).split(",") if part.strip()}
    for path in names:
        try:
            raw = contracts.read_json(path)
            if not isinstance(raw, dict):
                continue
            job = contracts.validate_job(raw)
        except (OSError, contracts.ContractError):
            continue
        if states and job["state"] not in states:
            continue
        if collection and job.get("collection") != collection:
            continue
        rows.append(job)
    rows.sort(key=lambda job: (job.get("updated_at") or "",
                               job.get("created_at") or "",
                               job["id"]), reverse=True)
    try:
        return rows[:max(1, int(limit))]
    except (TypeError, ValueError):
        return rows[:200]


def request_cancel(jobs_dir, job_id):
    """Latch ``cancel_requested`` on a running job.

    The builder checks the flag between batches, so this returns immediately and
    the job's state becomes ``cancelled`` when the current batch is committed.
    Terminal and interrupted jobs cannot be cancelled (resume is the action for
    the latter), which is an :class:`IngestError` the API maps to 409.
    """
    job = read_job(jobs_dir, job_id)
    if job is None:
        raise IngestError("no such job")
    if job["state"] in ("succeeded", "failed", "cancelled"):
        raise IngestError("job '%s' is already %s" % (job["id"], job["state"]))
    if job["state"] == "interrupted":
        raise IngestError("job '%s' was interrupted; resume it instead"
                          % job["id"])
    job["cancel_requested"] = True
    job["updated_at"] = _now()
    return write_job(jobs_dir, job)


def job_cancel_requested(jobs_dir, job_id):
    """Whether a cancel was requested for this job (never raises)."""
    try:
        job = read_job(jobs_dir, job_id)
    except IngestError:
        return False
    return bool(job and job.get("cancel_requested"))


def active_job_for(jobs_dir, collection):
    """The running job record for a collection, or None."""
    for job in list_jobs(jobs_dir, state="running", collection=collection, limit=20):
        return job
    return None


def _job_touch(job, jobs_dir, *, state=None, phase=None, done=None, total=None,
               fraction=None, error=None):
    """Update a job record and (optionally) its in-memory API counterpart."""
    if state is not None:
        job["state"] = state
    if error is not None:
        job["error"] = str(error)[:500]
    if phase is not None:
        job["progress"]["phase"] = str(phase)
    if done is not None:
        job["progress"]["done"] = int(done)
    if total is not None:
        job["progress"]["total"] = int(total)
    if fraction is not None:
        job["progress"]["fraction"] = float(fraction)
    job["updated_at"] = _now()
    if state == "running" and not job.get("started_at"):
        job["started_at"] = job["updated_at"]
    if state in ("succeeded", "failed", "cancelled"):
        job["finished_at"] = job["updated_at"]
    if jobs_dir is not None:
        _persist_job(jobs_dir, job)


def mark_interrupted_jobs(jobs_dir):
    """Mark job records that were left ``running`` by a crash as ``interrupted``.

    A process death leaves the record ``running``; treating that as ``failed``
    would hide that the work can be resumed. Returns the ids that were updated.
    """
    jobs_dir = Path(jobs_dir)
    if not jobs_dir.is_dir():
        return []
    interrupted = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            raw = contracts.read_json(path)
            if not isinstance(raw, dict):
                continue
            job = contracts.validate_job(raw)
        except contracts.ContractError:
            continue
        if job.get("state") != "running":
            continue
        job["state"] = "interrupted"
        job["updated_at"] = _now()
        job["finished_at"] = job["updated_at"]
        try:
            contracts.write_json_atomic(path, job)
        except OSError:
            continue
        interrupted.append(job["id"])
    return interrupted


# --- source summaries -----------------------------------------------------


def _source_file(dataset_dir, entry):
    """The on-disk file for one source entry, if it can be found."""
    dataset_dir = Path(dataset_dir)
    name = str(entry.get("name") or "")
    raw_path = str(entry.get("path") or "")
    candidates = []
    if raw_path:
        candidates.append(dataset_dir / raw_path)
    if name:
        candidates.append(dataset_dir / "original" / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _sources_summary(dataset_dir, dataset):
    """Provenance rows for ``info.json``: name, path, sha256, pages and bytes.

    Declared upload order wins over directory order. A hash is computed from the
    file when ``dataset.json`` predates content hashing; when the file is gone the
    entry is marked ``missing`` with no hash rather than inventing one.
    """
    dataset_dir = Path(dataset_dir)
    declared = []
    for entry in _source_entries((dataset or {}).get("sources")):
        name = str(entry.get("name") or "").strip()
        if name:
            declared.append((name, entry))
    paths = {path.name: path for path in _original_files(dataset_dir)}
    rows = []
    for name, entry in declared:
        sha = str(entry.get("sha256") or "").lower()
        path = str(entry.get("path") or ("original/" + name))
        file_path = _source_file(dataset_dir, entry) if not sha else None
        if not sha and file_path is not None:
            sha = _file_digest(file_path)
        pages = _int(entry.get("pages"), 0)
        if pages <= 0 and file_path is not None and file_path.suffix.lower() == ".pdf":
            try:
                pages = page_count(file_path)
            except IngestError:
                pages = 0
        byte_size = _int(entry.get("bytes"), 0) or (
            _size(file_path) if file_path is not None else 0
        )
        row = {
            "name": name,
            "path": path,
            "sha256": sha,
            "bytes": byte_size,
            "pages": pages,
        }
        if not sha:
            row["status"] = "missing"
        rows.append(row)
        paths.pop(name, None)
    # A file the metadata missed (for example a hand-copied dataset).
    for name in sorted(paths):
        path = paths[name]
        rows.append({
            "name": name,
            "path": "original/" + name,
            "sha256": _file_digest(path),
            "bytes": _size(path),
            "pages": page_count(path) if path.suffix.lower() == ".pdf" else 0,
        })
    return rows


def _chunks_from_pdfs(dataset_dir, dataset_id, target_chars, max_chars, notify):
    """Extract and chunk every PDF under ``original/`` into one chunk sequence."""
    original = Path(dataset_dir) / "original"
    files = (
        sorted(
            path
            for path in original.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
        if original.is_dir()
        else []
    )
    if not files:
        raise IngestError(
            "No chunks.jsonl and no PDF files under original/; nothing to index."
        )
    chunks = []
    next_id = 1
    total = len(files)
    for index, path in enumerate(files):
        notify(
            0.02 + 0.18 * (float(index) / max(1, total)),
            "Extracting %s" % path.name,
        )
        pages = extract_pdf(path)
        notify(
            0.02 + 0.18 * ((float(index) + 0.5) / max(1, total)),
            "Chunking %s" % path.name,
        )
        produced = chunk_pages(
            pages,
            target_chars=target_chars,
            max_chars=max_chars,
            dataset_id=dataset_id,
            source=path.name,
            source_sha256=_file_digest(path),
            id_start=next_id,
        )
        chunks.extend(produced)
        next_id += len(produced)
    notify(0.22, "Chunked %d source file(s)" % total)
    return chunks


def _extractor_identity(dataset):
    """The extractor that produced a dataset's chunks, from its provenance labels."""
    label = str((dataset or {}).get("extractor") or "")
    if "@" in label and label != "mixed":
        name, _, version = label.partition("@")
        return {"name": name, "version": version}
    labels = set()
    for entry in _source_entries((dataset or {}).get("sources")):
        value = str(entry.get("extractor") or "")
        if value:
            labels.add(value)
    labels.discard("mixed")
    if len(labels) == 1:
        value = labels.pop()
        if "@" in value:
            name, _, version = value.partition("@")
            return {"name": name, "version": version}
    return {"name": "", "version": ""}


def _documents_digest(sources):
    try:
        return contracts.documents_digest(sources)
    except contracts.ContractError:
        return ""


# --- resumable generations -------------------------------------------------


def _find_resumable_generation(space_dir, *, space, digest, chunker, n):
    """An interrupted generation that can continue from its checkpoint.

    Compatibility is exact: same space, same documents digest, same chunker
    version, same chunk count, a readable vector memmap of that row count and a
    stored probe whose dimension matches the memmap width. Anything else starts a
    fresh generation, because resuming into a changed corpus would write vectors
    that do not belong to their rows.
    """
    for generation_dir in reversed(_generation_dirs(space_dir)):
        progress = _read_json(generation_dir / PROGRESS_FILE, None)
        vectors_path = generation_dir / "vectors.npy"
        probe_file = generation_dir / PROBE_FILE
        if not isinstance(progress, dict):
            continue
        if not vectors_path.is_file() or not probe_file.is_file():
            continue
        if str(progress.get("space") or "") != str(space):
            continue
        if str(progress.get("documents_digest") or "") != str(digest):
            continue
        recorded_chunker = progress.get("chunker") or {}
        if (recorded_chunker.get("name"), recorded_chunker.get("version")) != (
            chunker.get("name"), chunker.get("version")
        ):
            continue
        if int(progress.get("n") or -1) != n:
            continue
        row = int(progress.get("row") or 0)
        if row <= 0 or row >= n:
            continue
        try:
            shape = np.load(str(vectors_path), mmap_mode="r").shape
            probe_dim = int(np.load(str(probe_file)).shape[0])
        except Exception:
            continue
        if len(shape) != 2 or int(shape[0]) != n or int(shape[1]) != probe_dim:
            continue
        return generation_dir, row
    return None, 0


def _open_vectors(generation_dir, n, dim):
    """Open (or preallocate) the generation's row-aligned float32 memmap."""
    path = Path(generation_dir) / "vectors.npy"
    if path.is_file():
        return np.lib.format.open_memmap(
            str(path), mode="r+", dtype=np.float32, shape=(n, dim)
        )
    return np.lib.format.open_memmap(
        str(path), mode="w+", dtype=np.float32, shape=(n, dim)
    )


def _write_progress(generation_dir, *, space, digest, chunker, n, dim, row):
    _write_json_atomic(
        Path(generation_dir) / PROGRESS_FILE,
        {
            "space": str(space),
            "documents_digest": str(digest),
            "chunker": dict(chunker),
            "n": int(n),
            "dim": int(dim),
            "row": int(row),
            "updated_at": _now(),
        },
    )


# --- build -----------------------------------------------------------------


def _cancelled(cancel, job):
    if cancel is not None:
        try:
            if cancel():
                return True
        except Exception:
            return False
    if isinstance(job, dict) and job.get("cancel_requested"):
        return True
    return False


def _build_info(dataset, dataset_id, space, generation, dim, n, link, sources,
                chunker, extractor, digest):
    target = _link_target(link)
    embedding = contracts.space_identity(link, dim=dim)
    embedding["probe"] = {
        "text_sha256": contracts.probe_sha256(),
        "file": PROBE_FILE,
    }
    info = {
        "schema": contracts.INFO_SCHEMA,
        "dataset": dataset_id,
        "name": str((dataset or {}).get("name") or dataset_id),
        "space": str(space),
        "generation": str(generation),
        "n": int(n),
        "dim": int(dim),
        "link_id": str(link.get("id") or ""),
        "model": target[2],
        "protocol": target[0],
        "created_at": _now(),
        "embedding": embedding,
        "chunker": dict(chunker),
        "extractor": dict(extractor),
        "documents_digest": str(digest),
        "sources": sources,
    }
    return contracts.validate_info(info)


def build_index(
    dataset_dir,
    link,
    *,
    target_chars=1200,
    max_chars=2000,
    progress=None,
    batch_size=DEFAULT_BATCH_SIZE,
    job=None,
    cancel=None,
    lock=None,
):
    """Build (or rebuild) one dataset's index for one embedding link.

    ``chunks.jsonl`` is the canonical input: when it exists it is used as-is;
    otherwise every PDF under ``original/`` is extracted and chunked first and the
    result is written back. Then:

    * the fixed probe text is embedded and stored (the space-mismatch gate);
    * every chunk is embedded in batches into a **preallocated** memmap, with a
      per-batch checkpoint (no ``vstack`` of the whole corpus in memory);
    * transient failures (429/5xx/timeouts) are retried with backoff and jitter;
    * the new generation's ``info.json`` is written, and ``CURRENT`` is flipped
      **last**, so a crash or cancel leaves the previous generation serving;
    * the job record (``.peekseed/jobs/<id>.json``) tracks the lifecycle.

    ``progress(frac, message)`` is called with a 0..1 fraction at each stage.
    ``cancel`` is a zero-argument callable checked between batches. Returns the
    ``info.json`` content.

    ``job`` is either the persisted job record the caller wants this build to
    own (the API creates the record and passes it, so the job id the client polls
    is the record on disk) or a legacy in-memory dict kept for compatibility.
    ``lock`` is an already-held :class:`peekseed.ingest.lock.CollectionLock`;
    when omitted the builder takes the collection's writer lock itself, so CLI and
    scan callers cannot race the server either.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise IngestError("Dataset directory not found: '%s'." % dataset_dir.name)
    dataset = _read_dataset_json(dataset_dir)
    dataset_id = str(dataset.get("id") or dataset_dir.name)
    target = _link_target(link)
    space = embed_space_key(link)
    notify = _progress_function(progress)

    try:
        batch = int(batch_size)
    except (TypeError, ValueError):
        raise IngestError("batch_size must be an integer.") from None
    if batch <= 0:
        raise IngestError("batch_size must be positive.")

    jobs_dir = _jobs_dir(dataset_dir)
    legacy_job = None
    if (isinstance(job, dict) and job.get("schema") == contracts.JOB_SCHEMA
            and job.get("kind")):
        try:
            job_record = contracts.validate_job(job)
        except contracts.ContractError as exc:
            raise IngestError("invalid job record: %s" % exc) from None
    else:
        if isinstance(job, dict):
            legacy_job = job
        job_record = contracts.new_job(
            "index",
            collection=dataset_dir.name,
            job_id=(legacy_job or {}).get("id"),
        )
    if not job_record.get("collection"):
        job_record["collection"] = dataset_dir.name

    # The API acquires the lock before returning 202 and passes it here; CLI and
    # scan callers let the builder take it, so no two writers overlap either way.
    own_lock = None
    if lock is None:
        from .lock import CollectionLock

        own_lock = CollectionLock(jobs_dir, dataset_dir.name)
        own_lock.acquire(job_id=job_record["id"])

    _persist_job(jobs_dir, job_record)
    _job_touch(job_record, jobs_dir, state="running", phase="preparing",
               done=0, total=0, fraction=0.0, error=None)
    if legacy_job is not None:
        legacy_job["status"] = "running"
        legacy_job["message"] = "preparing"

    try:
        notify(0.0, "Preparing dataset")
        chunks = read_chunks(dataset_dir)
        if chunks:
            notify(0.05, "Loaded %d existing chunk(s)" % len(chunks))
        else:
            chunks = _chunks_from_pdfs(
                dataset_dir, dataset_id, target_chars, max_chars, notify
            )
            if not chunks:
                raise IngestError(
                    "No extractable text was found in the dataset's PDF files."
                )
            write_chunks(dataset_dir, chunks)
            notify(0.15, "Wrote %d chunk(s)" % len(chunks))
        total = len(chunks)
        if not total:
            raise IngestError("No chunks to index for dataset '%s'." % dataset_id)

        sources = _sources_summary(dataset_dir, dataset)
        digest = _documents_digest(sources)
        chunker = contracts.chunker_identity(target_chars, max_chars)
        extractor = _extractor_identity(dataset)
        texts = [_embedding_text(chunk) for chunk in chunks]

        space_dir = _space_dir(dataset_dir, space)
        space_dir.mkdir(parents=True, exist_ok=True)
        generation_dirs = _generation_dirs(space_dir)

        # The probe fixes the dimension before the memmap is preallocated, and
        # resumes use the already-stored probe so no embed call is wasted.
        resumable, start_row = (None, 0)
        if generation_dirs:
            resumable, start_row = _find_resumable_generation(
                space_dir, space=space, digest=digest, chunker=chunker, n=total,
            )
        generation = uuid.uuid4().hex[:16]
        generation_dir = space_dir / GENERATIONS_DIR / generation
        resumed = False
        if resumable is not None:
            dim = int(np.load(str(resumable / PROBE_FILE)).shape[0])
            generation_dir = resumable
            generation = resumable.name
            resumed = True

        if not resumed:
            notify(0.2, "Embedding the probe vector")
            probe_vector = _normalise_vector(
                embed_query(link, _probe_text(link), timeout=None)
            )
            dim = int(probe_vector.shape[0])
            generation_dir.mkdir(parents=True, exist_ok=True)
            _save_probe(generation_dir / PROBE_FILE, probe_vector)
            _write_progress(generation_dir, space=space, digest=digest,
                            chunker=chunker, n=total, dim=dim, row=0)
        else:
            notify(0.2, "Resuming generation %s at row %d/%d"
                   % (generation, start_row, total))

        notify(0.25, "Writing %d metadata row(s)" % total)
        _write_json_atomic(
            generation_dir / "meta.json",
            [_meta_record(chunk) for chunk in chunks],
        )
        vectors = _open_vectors(generation_dir, total, dim)

        _job_touch(job_record, jobs_dir, phase="embedding", total=total,
                   done=start_row, fraction=0.3 + 0.6 * (start_row / max(1, total)))
        if legacy_job is not None:
            legacy_job["progress"] = max(0.0, min(1.0, 0.3 + 0.6 * (start_row / max(1, total))))
            legacy_job["message"] = "embedding"

        notify(0.3 + 0.6 * (start_row / max(1, total)),
               "Embedding %d/%d chunk(s)" % (start_row, total))
        row = start_row
        while row < total:
            if _cancelled(cancel, legacy_job if legacy_job is not None else job_record):
                _job_touch(job_record, jobs_dir, state="cancelled", phase="cancelled")
                raise IndexCancelled(
                    "the index build was cancelled; the previous index is still "
                    "serving and this build can be resumed"
                )
            subset = texts[row : row + batch]
            vectors_batch = _embed_with_retry(
                link, subset, batch_size=len(subset), cancel=cancel
            )
            array = np.asarray(vectors_batch, dtype=np.float32)
            if array.ndim != 2 or array.shape[0] != len(subset) or array.shape[1] != dim:
                raise IngestError(
                    "The embedding link returned an unusable vector array for %d "
                    "chunk(s) (expected dimension %d)." % (len(subset), dim)
                )
            norms = np.linalg.norm(array, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vectors[row : row + len(subset)] = array / norms
            vectors.flush()
            row += len(subset)
            _write_progress(generation_dir, space=space, digest=digest,
                            chunker=chunker, n=total, dim=dim, row=row)
            fraction = 0.3 + 0.6 * (row / max(1, total))
            notify(fraction, "Embedded %d/%d chunk(s)" % (row, total))
            _job_touch(job_record, jobs_dir, done=row, fraction=fraction)
            if legacy_job is not None:
                legacy_job["progress"] = max(0.0, min(1.0, fraction))
                legacy_job["message"] = "embedded %d/%d" % (row, total)

        notify(0.93, "Writing index files")
        info = _build_info(dataset, dataset_id, space, generation, dim, total,
                           link, sources, chunker, extractor, digest)
        _write_json_atomic(generation_dir / "info.json", info, indent=2)
        # The atomic pointer flip is the commit point: before this line the
        # previous generation is live, after it the new one is.
        _write_current(space_dir, generation)
        _silent_unlink(generation_dir / PROGRESS_FILE)
        if generation_dir != space_dir and (space_dir / "vectors.npy").exists():
            # A legacy in-place index is superseded; leave it alone on disk until
            # `reindex --gc`, but CURRENT now selects the generation.
            pass
        _job_touch(job_record, jobs_dir, state="succeeded", phase="done",
                   done=total, fraction=1.0)
        if legacy_job is not None:
            legacy_job["progress"] = 1.0
            legacy_job["message"] = "index ready: %d chunks, dimension %d" % (total, dim)
        notify(1.0, "Index ready: %d chunk(s), dimension %d" % (total, dim))
        if own_lock is not None:
            own_lock.release()
        return info
    except IndexCancelled:
        if own_lock is not None:
            own_lock.release()
        raise
    except BaseException as exc:
        _job_touch(job_record, jobs_dir, state="failed", error=str(exc))
        if legacy_job is not None:
            legacy_job["error"] = str(exc)[:500]
        if own_lock is not None:
            own_lock.release()
        raise


# --- reindex / gc ----------------------------------------------------------


def reindex(dataset_dir, link, *, dry_run=False, gc=False, progress=None):
    """Rebuild a dataset's index in the link's embedding space, with a scope estimate.

    ``dry_run`` prints (returns) exactly what would happen and writes nothing. A
    real run rebuilds the target space generation-atomically and then, with
    ``gc``, removes every non-``CURRENT`` generation of that space.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise IngestError("Dataset directory not found: '%s'." % dataset_dir.name)
    chunks = read_chunks(dataset_dir)
    total = len(chunks)
    target_space = embed_space_key(link)
    space_dir = _space_dir(dataset_dir, target_space)
    generations = _generation_dirs(space_dir)
    current = _read_current(space_dir)
    batch = DEFAULT_BATCH_SIZE
    embed_calls = (math.ceil(total / batch) if total else 0) + 1  # + the probe
    remote = not str(link.get("base_url") or "").startswith(("http://127.0.0.1",
                                                             "http://localhost",
                                                             "http://[::1]"))
    if remote:
        estimate_seconds = embed_calls * ESTIMATE_SECONDS_PER_REMOTE_BATCH
    else:
        estimate_seconds = total * ESTIMATE_SECONDS_PER_CHUNK
    gc_candidates = [path.name for path in generations
                     if current is None or path.name != current]
    report = {
        "dataset": dataset_dir.name,
        "chunks": total,
        "target_space": target_space,
        "current_space": target_space if current else "",
        "generations": len(generations),
        "gc_candidates": gc_candidates,
        "embed_calls": embed_calls,
        "estimate_seconds": round(estimate_seconds, 1),
        "estimate": "%d embed call(s), about %s" % (
            embed_calls,
            ("%d min" % max(1, int(estimate_seconds / 60))) if estimate_seconds >= 60
            else ("%d s" % max(1, int(estimate_seconds)))),
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return report

    info = build_index(dataset_dir, link, progress=progress)
    report["space"] = info.get("space")
    report["n"] = info.get("n")
    if gc:
        removed = []
        for generation_dir in _generation_dirs(space_dir):
            if generation_dir.name == str(info.get("generation") or ""):
                continue
            shutil.rmtree(str(generation_dir), ignore_errors=True)
            removed.append(generation_dir.name)
        report["gc_removed"] = removed
    return report
