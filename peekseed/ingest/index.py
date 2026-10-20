"""Dataset storage and vector-index writing for peekseed.

A dataset directory owns the uploaded files, the canonical ``chunks.jsonl`` and
one compact index per embedding space::

    <dataset>/dataset.json
    <dataset>/original/...
    <dataset>/chunks.jsonl
    <dataset>/index/<space_key>/{vectors.npy, meta.json, info.json}

The index is deliberately plain numpy: vectors are float32 ``[N, dim]`` in
row-order with the chunk metadata, so loading needs no vector database and the
files are atomically replaceable while a process keeps serving.

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
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from .chunk import chunk_pages
from .pdf import IngestError, extract_pdf, page_count

__all__ = [
    "add_source",
    "build_index",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "read_chunks",
    "sanitize_filename",
    "write_chunks",
]

INFO_SCHEMA = 1
DEFAULT_BATCH_SIZE = 32
EMBED_TIMEOUT = 120.0
MAX_DATASET_ID = 64
MAX_FILENAME_LENGTH = 120

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
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


# --- small utilities -------------------------------------------------------


def _collapse_text(value):
    return " ".join(str(value or "").split())


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _save_vectors(path, vectors):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".vectors.", suffix=".npy"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            np.save(stream, np.asarray(vectors, dtype=np.float32))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
    except BaseException:
        _silent_unlink(temporary)
        raise


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
        "version": 1,
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


def dataset_indexes(dataset_dir):
    """Return the ``info.json`` dictionaries of every index in a dataset.

    Directories without a readable ``info.json`` are skipped: one damaged index
    must not hide the healthy ones from the datasets listing.
    """
    dataset_dir = Path(dataset_dir)
    index_root = dataset_dir / "index"
    if not index_root.is_dir():
        return []
    found = []
    for entry in sorted(index_root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            info = _read_json(entry / "info.json", None)
        except IngestError:
            continue
        if not isinstance(info, dict):
            continue
        info = dict(info)
        info.setdefault("space", entry.name)
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
    the resulting ``{name, path, bytes, pages}`` entry is appended to
    ``dataset.json`` ``sources`` and returned. Adding the same file twice is
    idempotent and returns the existing entry.
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
    if target.suffix.lower() == ".pdf":
        try:
            pages = page_count(target)
        except IngestError:
            pages = 0
    entry = {
        "name": name,
        "path": "original/" + name,
        "bytes": _size(target),
        "pages": pages,
    }

    sources = _source_entries(metadata.get("sources"))
    for index, existing in enumerate(sources):
        if str(existing.get("name") or "") == name:
            if not existing.get("bytes") or not existing.get("pages"):
                sources[index] = entry
                metadata["sources"] = sources
                _write_json_atomic(dataset_dir_path / "dataset.json", metadata, indent=2)
            if isinstance(dataset, dict):
                dataset["sources"] = _source_entries(metadata.get("sources"))
            return entry
    sources.append(entry)
    metadata["sources"] = sources
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
        "version": _int(dataset.get("version"), 1),
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

    The rule is owned by :mod:`peekseed.links` and imported from there when that
    module is available. The local fallback is kept in sync with it so ingestion
    still works headlessly: sanitized ``"<link_id>__<model>"``, or
    ``"<link_id>__default"`` when the link has no model string.
    """
    if not isinstance(link, dict):
        raise IngestError("An embedding link dictionary is required.")
    ready = _links_space_key()
    if ready is not None:
        try:
            key = ready(link)
        except Exception:
            key = None
        if isinstance(key, str) and key:
            return key
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


# --- index writing ---------------------------------------------------------


def _sources_summary(dataset_dir, dataset):
    """Provenance rows for ``info.json``: name, page count and byte size."""
    declared = {}
    raw = dataset.get("sources") if isinstance(dataset, dict) else None
    for entry in _source_entries(raw):
        name = str(entry.get("name") or "").strip()
        if name:
            declared[name] = entry
    files = _original_files(dataset_dir)
    if not files:
        return [
            {
                "name": name,
                "pages": _int(meta.get("pages"), 0),
                "bytes": _int(meta.get("bytes"), 0),
            }
            for name, meta in declared.items()
        ]
    # Declared order first (upload order), then any file the metadata missed.
    by_name = {path.name: path for path in files}
    ordered = []
    for name, meta in declared.items():
        path = by_name.pop(name, None)
        if path is not None:
            ordered.append((name, path, meta))
    for name in sorted(by_name):
        ordered.append((name, by_name[name], {}))
    summaries = []
    for name, path, meta in ordered:
        pages = _int(meta.get("pages"), 0)
        if pages <= 0 and path.suffix.lower() == ".pdf":
            try:
                pages = page_count(path)
            except IngestError:
                pages = 0
        summaries.append({"name": name, "pages": pages, "bytes": _size(path)})
    return summaries


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
            id_start=next_id,
        )
        chunks.extend(produced)
        next_id += len(produced)
    notify(0.22, "Chunked %d source file(s)" % total)
    return chunks


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
    return {field: record[field] for field in _META_FIELDS}


def _embedding_text(chunk):
    text = str(chunk.get("doc") or chunk.get("text") or "").strip()
    if text:
        return text
    fallback = str(chunk.get("section") or chunk.get("id") or "").strip()
    return fallback or "document"


def build_index(
    dataset_dir,
    link,
    *,
    target_chars=1200,
    max_chars=2000,
    progress=None,
    batch_size=DEFAULT_BATCH_SIZE
):
    """Build (or rebuild) one dataset's index for one embedding link.

    ``chunks.jsonl`` is the canonical input: when it exists it is used as-is;
    otherwise every PDF under ``original/`` is extracted and chunked first and the
    result is written back. The embed link is then called in batches and the
    vectors plus row-aligned metadata are written atomically to
    ``index/<space_key>/``. Rebuilding one space never touches other spaces.

    ``progress(frac, message)`` is called with a 0..1 fraction at each stage. The
    returned dictionary is the ``info.json`` content.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise IngestError("Dataset directory not found: '%s'." % dataset_dir.name)
    dataset = _read_dataset_json(dataset_dir)
    dataset_id = str(dataset.get("id") or dataset_dir.name)
    target = _link_target(link)
    space = embed_space_key(link)
    notify = _progress_function(progress)

    notify(0.0, "Preparing dataset")
    chunks = read_chunks(dataset_dir)
    if chunks:
        notify(0.08, "Loaded %d existing chunk(s)" % len(chunks))
        sources = _sources_summary(dataset_dir, dataset)
    else:
        chunks = _chunks_from_pdfs(
            dataset_dir, dataset_id, target_chars, max_chars, notify
        )
        if not chunks:
            raise IngestError(
                "No extractable text was found in the dataset's PDF files."
            )
        write_chunks(dataset_dir, chunks)
        # Declared upload order (not directory order) is the provenance order.
        sources = _sources_summary(dataset_dir, dataset)
        notify(0.25, "Wrote %d chunk(s)" % len(chunks))
    if not chunks:
        raise IngestError("No chunks to index for dataset '%s'." % dataset_id)

    total = len(chunks)
    texts = [_embedding_text(chunk) for chunk in chunks]
    try:
        batch = int(batch_size)
    except (TypeError, ValueError):
        raise IngestError("batch_size must be an integer.") from None
    if batch <= 0:
        raise IngestError("batch_size must be positive.")

    notify(0.3, "Embedding %d chunk(s)" % total)
    parts = []
    dim = 0
    for start in range(0, total, batch):
        subset = texts[start : start + batch]
        vectors = embed_texts(link, subset, batch_size=len(subset), timeout=None)
        array = np.asarray(vectors, dtype=np.float32)
        if (
            array.ndim != 2
            or array.shape[0] != len(subset)
            or array.shape[1] <= 0
        ):
            raise IngestError(
                "The embedding link returned an unusable vector array for %d chunk(s)."
                % len(subset)
            )
        if dim == 0:
            dim = int(array.shape[1])
        elif array.shape[1] != dim:
            raise IngestError(
                "Embedding dimension changed from %d to %d between batches; check "
                "the embedding link's model." % (dim, array.shape[1])
            )
        parts.append(array)
        done = min(total, start + len(subset))
        notify(
            0.3 + 0.6 * (float(done) / total),
            "Embedded %d/%d chunk(s)" % (done, total),
        )

    matrix = np.vstack(parts) if parts else np.zeros((0, dim), dtype=np.float32)
    if matrix.shape[0] != total:
        raise IngestError(
            "The embedding link returned %d vector(s) for %d chunk(s)."
            % (matrix.shape[0], total)
        )
    info = {
        "schema": INFO_SCHEMA,
        "dataset": dataset_id,
        "name": str(dataset.get("name") or dataset_id),
        "space": space,
        "link_id": str(link.get("id") or ""),
        "model": target[2],
        "protocol": target[0],
        "dim": int(dim),
        "n": int(total),
        "created_at": _now(),
        "chunker": {
            "target_chars": _int(target_chars, 1200),
            "max_chars": _int(max_chars, 2000),
            "overlap": 0,
        },
        "sources": sources,
    }
    space_dir = dataset_dir / "index" / space
    space_dir.mkdir(parents=True, exist_ok=True)
    notify(0.93, "Writing index files")
    _save_vectors(space_dir / "vectors.npy", matrix)
    _write_json_atomic(space_dir / "meta.json", [_meta_record(chunk) for chunk in chunks])
    _write_json_atomic(space_dir / "info.json", info, indent=2)
    notify(1.0, "Index ready: %d chunk(s), dimension %d" % (total, dim))
    return info
