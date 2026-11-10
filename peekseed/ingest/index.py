"""Dataset storage, generation-atomic vector indexes and job records.

Uploads are **storage only**: a file is stored once in the shared pool
(``$PEEKSEED_SHARED_DIR/sources/<sha256>/<name>.pdf``) with its sha256/bytes
recorded and ``status="uploaded"``; peekseed never parses a document.
Extraction is owned by the peelfeed engine (a separate process/venv), which
writes one peel directory per source under ``$PEEKSEED_SHARED_DIR/peels``::

    <shared>/peels/<slug>-<sha256[:8]>/
        source/<original.pdf>     hardlink to the shared sources/ copy
        extract/corpus -> conversions/<build_id>    current build (CP3 symlink)
        extract/conversions/<build_id>/…            manifest/chunks/assets/…
        extract/reader-locks/<build_id>.lock        reader SH lease / cleanup EX
        .peel

:func:`build_index` reads every source whose ``extraction.state`` is
``"extracted"`` from its peel's **current build directory** (gated on the
realpath allowlist, the source sha256, the recorded ``conversion_id`` and
``validation.errors == 0``), leases it with ``flock(LOCK_SH)`` so peelfeed cannot
delete it mid-build, and rechecks the current conversion/effective hash/build id
before flipping ``CURRENT`` (CP3 wire-contract §2.8). It maps the corpus chunks
onto peekseed's chunk contract, embeds them through the configured link and
writes the same generation-atomic numpy index as before. There is deliberately
**no in-process extraction fallback**.

A dataset directory owns its metadata, the canonical ``chunks.jsonl`` and one
index per embedding space; the uploaded bytes live once in the shared pool::

    <shared>/sources/<sha256>/<name>.pdf      the one stored copy, served by /pdf
    <home>/datasets/<dataset>/dataset.json
    <home>/datasets/<dataset>/chunks.jsonl
    <home>/datasets/<dataset>/index/<space_key>/
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

import errno
import fcntl
import hashlib
import json
import math
import os
import random
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from .. import contracts
from .errors import IngestError

__all__ = [
    "IndexCancelled",
    "NeedsReindex",
    "NoChunkSizes",
    "NoExtractedSources",
    "ProbeMismatch",
    "PROBE_MIN_COSINE",
    "ReaderLease",
    "ReaderLeaseBusy",
    "active_job_for",
    "add_source",
    "build_index",
    "chunk_set_name",
    "create_dataset",
    "dataset_dir",
    "dataset_indexes",
    "dataset_info",
    "embed_query",
    "embed_space_key",
    "embed_texts",
    "find_source",
    "index_state",
    "job_cancel_requested",
    "job_path",
    "link_chunk_sizes",
    "list_jobs",
    "load_index",
    "mark_interrupted_jobs",
    "peel_root",
    "probe_cosine",
    "read_chunks",
    "read_review_jobs",
    "reconcile_all_datasets",
    "reconcile_peel",
    "reconcile_peels",
    "record_review_job",
    "release_shared_source",
    "remove_source",
    "read_job",
    "reindex",
    "request_cancel",
    "resolve_current_build",
    "sanitize_filename",
    "store_shared_source",
    "stored_source_path",
    "update_source",
    "verify_probe",
    "write_chunks",
    "write_job",
]

INFO_SCHEMA = contracts.INFO_SCHEMA
DEFAULT_BATCH_SIZE = 32
EMBED_TIMEOUT = 120.0
MAX_DATASET_ID = 64
MAX_FILENAME_LENGTH = 120

#: Corpus files peekseed consumes from one peel (design §2.1/§4.3).
PEEL_CORPUS_FILES = ("manifest.json", "chunks.jsonl", "assets.jsonl",
                     "validation.json", "review_queue.json")
#: Newer corpus schema versions are refused rather than guessed at.
SUPPORTED_CORPUS_SCHEMA = 2
#: The chunker version recorded on chunks read from a peel corpus.
PEEL_CHUNKER_VERSION = contracts.PEEL_CHUNKER_VERSION
#: A PDF must start with this magic; storage-only uploads still validate it.
PDF_MAGIC = b"%PDF-"
#: Serialises the shared-source store/release paths (two requests may store or
#: delete the same content at once).
_SOURCES_LOCK = threading.RLock()

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
# Optional fields added by the peelfeed corpus contract (design §4.2); copied
# when present so an index can serve crops/pages/canonical ids without re-reading
# the peel at query time.
_META_PEEL_FIELDS = (
    "kind",
    "image_paths",
    "source_pages",
    "char_count",
    "parts",
    "part",
    "prev",
    "next",
    "canonical_ids",
    "engine",
    "conversion_id",
    "peel_id",
    # Chunk-set rows: the set they belong to and the canonical (reading-layer)
    # chunk ids in ``chunks.jsonl`` that a search hit maps back to.
    "chunk_set",
    "reading_ids",
)
#: The embed link's chunk sizes; they select the peelfeed chunk set to index.
CHUNK_KEYS = ("target_chars", "max_chars", "image_cap")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


class NeedsReindex(IngestError):
    """The index on disk is missing, unreadable or internally inconsistent."""


class NoExtractedSources(IngestError):
    """No source in the dataset has a usable peelfeed corpus.

    ``code`` lets the HTTP layer map this to 409 without string matching;
    ``skipped`` names every source that was skipped and why, so the response can
    list them instead of silently ignoring them.
    """

    code = "no_extracted_sources"

    def __init__(self, message, skipped=None):
        self.skipped = list(skipped or [])
        super().__init__(message)


class ProbeMismatch(IngestError):
    """The live embedder does not reproduce the index's stored probe vector."""


class NoChunkSizes(IngestError):
    """The embed link names no chunk sizes, so there is no chunk set to index."""

    code = "no_chunk_sizes"


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

    Uploaded files are stored once in the shared sources pool by
    :func:`add_source`; a dataset keeps only ``dataset.json``, ``chunks.jsonl``
    and its indexes. The ``dir`` key is a convenience for the caller and is not
    written to ``dataset.json``.
    """
    display = _collapse_text(name)
    if not display:
        raise IngestError("A dataset name is required.")
    display = display[:120].rstrip()
    datasets_dir = _datasets_dir(paths)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    dataset_id = _unique_dataset_id(datasets_dir, display)
    dataset_dir = datasets_dir / dataset_id
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


def store_shared_source(source, digest):
    """Store ``source`` once at ``<shared>/sources/<digest>/<name>``; reuse an existing copy."""
    from .. import config

    folder = config.paths().sources_dir / digest
    if folder.is_dir():
        for existing in sorted(folder.iterdir()):
            if existing.is_file() and not existing.name.endswith(".part"):
                return existing
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / sanitize_filename(Path(source).name)
    partial = target.with_name(target.name + ".part")
    try:
        shutil.copyfile(str(source), str(partial))
        os.chmod(str(partial), 0o600)
        if _file_digest(partial) != digest:
            raise IngestError("Stored copy of '%s' does not match its hash." % target.name)
        os.replace(str(partial), str(target))
    finally:
        if partial.exists():
            partial.unlink()
    return target


def release_shared_source(sha):
    """Delete ``<shared>/sources/<sha>/`` when no dataset.json references it any more."""
    from .. import config

    sha = str(sha or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False
    with _SOURCES_LOCK:
        p = config.paths()
        if p.datasets_dir.is_dir():
            for dataset_dir in p.datasets_dir.iterdir():
                if not (dataset_dir / "dataset.json").is_file():
                    continue
                try:
                    metadata = _read_dataset_json(dataset_dir)
                except IngestError:
                    return False   # unreadable dataset: keep the file (safe side)
                for entry in _source_entries(metadata.get("sources")):
                    if str(entry.get("sha256") or "").lower() == sha:
                        return False
        folder = p.sources_dir / sha
        if not folder.is_dir():
            return False
        for child in folder.iterdir():
            child.unlink()
        folder.rmdir()
        return True


def add_source(dataset, source_path):
    """Store one uploaded file once in the shared sources pool.

    Upload is deliberately **storage only**: the file's magic/extension is
    checked, the bytes are stored once at ``<shared>/sources/<sha256>/<name>``
    (0600) and its sha256/bytes are recorded with ``status="uploaded"`` and
    ``extraction.state="not_extracted"``. No page count, extraction or chunking
    happens here -- extraction is a separate, user-triggered peelfeed job
    (design §4.5).

    ``dataset`` is the metadata dictionary returned by :func:`create_dataset`
    (it must carry the dataset directory under ``dir``); a dataset directory
    path is also accepted. Adding the same content twice is idempotent and
    returns the existing entry (content, not path, is identity); a second
    dataset gets its own entry pointing at the same one stored file.
    """
    dataset_dir_path = _dataset_root(dataset)
    source = Path(source_path)
    if not source.is_file():
        raise IngestError("Source file not found: '%s'." % source.name)
    if source.suffix.lower() != ".pdf":
        raise IngestError(
            "Unsupported file type '%s': uploads are PDFs; extraction formats "
            "are decided by the peelfeed profile." % (source.suffix or "unknown")
        )
    try:
        with open(str(source), "rb") as handle:
            if handle.read(len(PDF_MAGIC)) != PDF_MAGIC:
                raise IngestError("'%s' is not a PDF (missing %%PDF- header)."
                                  % source.name)
    except OSError as exc:
        raise IngestError("Could not read '%s': %s." % (source.name, exc)) from None

    digest = _file_digest(source)
    with _SOURCES_LOCK:
        metadata = _read_dataset_json(dataset_dir_path)
        sources = _source_entries(metadata.get("sources"))
        for existing in sources:
            if (str(existing.get("sha256") or "").lower() == digest
                    and _source_file(dataset_dir_path, existing) is not None):
                # Re-uploading the same content is idempotent: one stored copy,
                # one entry (content, not path, is identity).
                if isinstance(dataset, dict):
                    dataset["sources"] = _source_entries(sources)
                return dict(existing)
        stored = store_shared_source(source, digest)
        base_name = sanitize_filename(source.name)
        used = {str(entry.get("name") or "") for entry in sources}
        name = base_name
        counter = 2
        while name in used:
            stem, suffix = _split_name(base_name)
            name = "%s-%d%s" % (stem, counter, suffix)
            counter += 1
        entry = {
            "name": name,
            "path": "sources/%s/%s" % (digest, stored.name),
            "sha256": digest,
            "bytes": _size(stored),
            "pages": 0,
            "extractor": "",
            "status": "uploaded",
            "extraction": contracts.extraction_default(),
        }
        sources.append(entry)
        metadata["schema"] = contracts.DATASET_SCHEMA
        metadata["sources"] = sources
        metadata.setdefault("updated_at", _now())
        _write_json_atomic(dataset_dir_path / "dataset.json", metadata, indent=2)
        if isinstance(dataset, dict):
            dataset["sources"] = [dict(item) for item in sources]
        return entry


def _entries_by_sha(dataset_dir):
    """``{sha256: entry}`` for every declared source with a content hash."""
    metadata = _read_dataset_json(dataset_dir)
    found = {}
    for entry in _source_entries(metadata.get("sources")):
        sha = str(entry.get("sha256") or "").lower()
        if sha:
            found.setdefault(sha, entry)
    return found


def find_source(dataset_dir, sha):
    """One source entry by content hash (a copy), or None."""
    sha = str(sha or "").lower()
    if not sha:
        return None
    entry = _entries_by_sha(dataset_dir).get(sha)
    return dict(entry) if entry is not None else None


def stored_source_path(dataset_dir, entry):
    """The stored file for a source entry (``original/`` copy), or None."""
    return _source_file(dataset_dir, entry)


def update_source(dataset_dir, sha, *, status=None, extraction=None,
                  pages=None, extractor=None):
    """Merge fields into one ``dataset.json`` source and persist it atomically.

    ``extraction`` replaces the whole per-source block; callers build it from the
    peelfeed summary so a stale state cannot survive a refresh.
    """
    dataset_dir = Path(dataset_dir)
    metadata = _read_dataset_json(dataset_dir)
    sources = _source_entries(metadata.get("sources"))
    sha = str(sha or "").lower()
    for position, entry in enumerate(sources):
        if str(entry.get("sha256") or "").lower() != sha:
            continue
        updated = dict(entry)
        if status is not None:
            updated["status"] = str(status)
        if extractor is not None:
            updated["extractor"] = str(extractor)
        if pages is not None:
            updated["pages"] = int(pages)
        if extraction is not None:
            updated["extraction"] = dict(extraction)
        sources[position] = updated
        metadata["schema"] = contracts.DATASET_SCHEMA
        metadata["sources"] = sources
        metadata["updated_at"] = _now()
        validated = contracts.validate_dataset(metadata)
        _write_json_atomic(dataset_dir / "dataset.json", validated, indent=2)
        return dict(updated)
    return None


def remove_source(dataset_dir, sha, *, delete_file=True):
    """Remove one source entry (and optionally release its shared copy).

    The stored PDF lives once in the shared pool, so the file is deleted only
    when no other dataset still references that sha256. The peel itself is
    owned by peelfeed; ``DELETE /api/datasets/{id}/sources/{sha}?peel=keep|delete``
    decides separately whether to archive it. Removing the source leaves the
    index/chunks stale until the next build, which the normal staleness gate
    reports.
    """
    dataset_dir = Path(dataset_dir)
    metadata = _read_dataset_json(dataset_dir)
    sources = _source_entries(metadata.get("sources"))
    sha = str(sha or "").lower()
    removed = None
    kept = []
    for entry in sources:
        if removed is None and str(entry.get("sha256") or "").lower() == sha:
            removed = dict(entry)
            continue
        kept.append(entry)
    if removed is None:
        return None
    metadata["schema"] = contracts.DATASET_SCHEMA
    metadata["sources"] = kept
    metadata["updated_at"] = _now()
    _write_json_atomic(dataset_dir / "dataset.json",
                       contracts.validate_dataset(metadata), indent=2)
    if delete_file:
        release_shared_source(sha)
    return removed


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
    for field in _META_PEEL_FIELDS:
        if chunk.get(field) is not None:
            record[field] = chunk[field]
    return {field: record[field] for field in _META_FIELDS if field in record} | {
        field: record[field] for field in _META_PROVENANCE_FIELDS if field in record
    } | {
        field: record[field] for field in _META_PEEL_FIELDS if field in record
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
    from .. import links as links_module

    return (
        protocol,
        base_url,
        str(link.get("model") or ""),
        links_module.api_key(link),
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
            "extraction": dict(entry.get("extraction") or {}),
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
    sources = _dataset_sources_for_digest(dataset_dir)
    reasons = contracts.stale_reasons(
        info, sources, chunker=chunker, extractor=extractor,
        corpus_digest=_corpus_digest(sources),
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


def _collect_corpus_chunks(dataset_dir, dataset, sizes=None, builds=None):
    """Read every extracted source's peel corpus into one chunk sequence.

    ``builds`` maps a source sha256 to the resolved-and-leased current build
    (CP3 wire §2.8); each source is read from **that** build directory, never by
    re-resolving the ``extract/corpus`` symlink mid-build. Returns
    ``(chunks, set_chunks, peels, skipped)``: the canonical chunks, the rows of
    the chunk set for ``sizes`` (empty without sizes), the peels read, and every
    source that could not contribute (with a reason), so callers can report them
    instead of silently dropping data.
    """
    dataset_id = str(dataset.get("id") or Path(dataset_dir).name)
    root = peel_root()
    chunks = []
    set_chunks = []
    peels = []
    skipped = []
    for entry in _source_entries(dataset.get("sources")):
        name = str(entry.get("name") or "")
        block = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
        state = contracts.extraction_state(entry)
        peel_id = str(block.get("peel_id") or "")
        if state != "extracted" or not peel_id:
            if state == "not_extracted":
                reason = "not extracted yet; run Extract with peelfeed"
            else:
                reason = "extraction is %s; re-extract it with peelfeed" % state
            skipped.append({"name": name, "state": state, "reason": reason})
            continue
        build = (builds or {}).get(str(entry.get("sha256") or "").lower())
        peel = _read_peel(dataset_dir, entry, root, dataset_id, sizes, build)
        chunks.extend(peel["records"])
        set_chunks.extend(peel["set_records"])
        peels.append({"name": name, "peel": peel["peel"]})
    if not chunks:
        raise NoExtractedSources(
            "no_extracted_sources: no source in dataset '%s' has a usable "
            "peelfeed corpus; upload files, configure peelfeed and Extract them "
            "first." % dataset_id,
            skipped=skipped,
        )
    return chunks, set_chunks, peels, skipped


def _same_chunker(recorded, chunker):
    """Same chunk contract and, when the build names them, the same chunk sizes."""
    recorded = recorded if isinstance(recorded, dict) else {}
    keys = ("name", "version") + tuple(key for key in CHUNK_KEYS if key in chunker)
    return all(recorded.get(key) == chunker.get(key) for key in keys)


def _current_info_if_up_to_date(dataset_dir, space, n, digest, corpus, chunker,
                                extractor):
    """The live ``info.json`` when it already describes these exact inputs."""
    try:
        loaded = load_index(dataset_dir, space)
    except NeedsReindex:
        return None
    info = loaded["info"]
    if int(info.get("n") or 0) != int(n):
        return None
    if str(info.get("documents_digest") or "") != str(digest):
        return None
    if str(info.get("corpus_digest") or "") != str(corpus):
        return None
    if not _same_chunker(info.get("chunker"), chunker):
        return None
    if (info.get("extractor") or {}) != extractor:
        return None
    if not contracts.has_probe(info):
        return None
    return info


# --- source summaries -----------------------------------------------------


def _source_file(dataset_dir, entry):
    """The on-disk file for one source entry, if it can be found."""
    dataset_dir = Path(dataset_dir)
    name = str(entry.get("name") or "")
    raw_path = str(entry.get("path") or "")
    if raw_path.startswith("sources/"):
        from .. import config

        p = config.paths()
        sources_dir = p.sources_dir.resolve()
        try:
            candidate = (p.shared_dir / raw_path).resolve()
            candidate.relative_to(sources_dir)
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_file() else None
    # Legacy datasets (pre-livefield, original/ copies): read-only support.
    original = (dataset_dir / "original").resolve()
    candidates = []
    if raw_path:
        candidates.append(dataset_dir / raw_path)
    if name:
        candidates.append(original / name)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(original)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


# --- peelfeed corpus reader -----------------------------------------------


def peel_root():
    """The only tree peekseed may read a peel corpus from.

    ``$PEEKSEED_SHARED_DIR/peels`` (peelfeed writes it). Every read is
    realpath-checked against this root, so a symlink or a hand-written
    ``peel_id`` cannot escape it.
    """
    from .. import config

    return config.paths().peels_dir.resolve()


def _resolve_peel_dir(root, peel_id):
    """Resolve one peel directory under ``root`` (realpath allowlist gate)."""
    identifier = str(peel_id or "").strip()
    if (not identifier or identifier in (".", "..") or "/" in identifier
            or "\\" in identifier or "\x00" in identifier):
        raise IngestError("invalid peel id '%s'." % identifier)
    root = Path(root).resolve()
    candidate = (root / identifier).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise IngestError(
            "peel '%s' resolves outside the peel root; refusing to read it."
            % identifier
        ) from None
    if not candidate.is_dir():
        raise IngestError("peel '%s' does not exist under the peel root."
                          % identifier)
    return candidate


def _read_jsonl(path, label):
    """Read a JSON Lines corpus file; a bad line is a per-peel refusal."""
    path = Path(path)
    if not path.is_file():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError as exc:
                    raise IngestError("%s line %d is not valid JSON: %s"
                                      % (label, number, exc)) from None
                if not isinstance(record, dict):
                    raise IngestError("%s line %d is not a JSON object."
                                      % (label, number))
                rows.append(record)
    except OSError as exc:
        raise IngestError("Could not read %s: %s." % (label, exc)) from None
    return rows


def _validation_error_count(validation):
    """Count validation errors (the corpus stores a list; older ones an int)."""
    if not isinstance(validation, dict):
        return 0
    errors = validation.get("errors")
    if isinstance(errors, list):
        return len(errors)
    if isinstance(errors, int) and not isinstance(errors, bool):
        return errors
    return 0


def _review_item_count(validation, review_queue):
    if isinstance(validation, dict):
        value = validation.get("review_items")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    if isinstance(review_queue, dict):
        warnings = review_queue.get("warnings")
        if isinstance(warnings, list):
            return len(warnings)
    return 0


def _peel_source_file(peel_dir, name):
    """The read-only source copy inside a peel (by name, else the only file)."""
    source_dir = Path(peel_dir) / "source"
    candidate = source_dir / str(name or "")
    if name and candidate.is_file():
        return candidate
    try:
        files = sorted(path for path in source_dir.iterdir() if path.is_file())
    except OSError:
        return None
    return files[0] if len(files) == 1 else None


# --- CP3 current build, reader lease and shared dataset reconcile ----------

#: CP3 build layout (wire-contract §2.1): the current pointer is the relative
#: symlink ``extract/corpus -> conversions/<build_id>``; one build per
#: directory carries the corpus files plus ``build_summary.json``.
CONVERSIONS_DIR = "conversions"
CORPUS_LINK = "corpus"
READER_LOCKS_DIR = "reader-locks"
#: ``b<UTC ts>-<sha8>`` and ``cutover-<conversion_id>`` both fit; the cap keeps
#: a hand-written id from escaping the reader-locks directory.
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")
#: How long a reader waits for a build whose cleanup holds the EX lease.
READER_LEASE_TIMEOUT = 5.0


class ReaderLeaseBusy(IngestError):
    """A build's reader lock is held exclusively (cleanup in progress)."""


def _build_identity(build):
    """The snapshot fields the CP3 recheck compares (wire §2.8)."""
    manifest = build.get("manifest") if isinstance(build, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    return {
        "build_id": str((build or {}).get("build_id") or ""),
        "conversion_id": str(manifest.get("conversion_id") or ""),
        "effective_corrections_hash": str(
            manifest.get("effective_corrections_hash") or ""),
    }


def _cutover_build_id(manifest):
    """The cutover build id for a pre-CP3 real corpus dir (wire §2.1)."""
    return "cutover-" + str((manifest or {}).get("conversion_id") or "")


def resolve_current_build(peel_dir):
    """Resolve one peel's current build directory (CP3 wire §2.1/§2.8).

    Returns ``{"peel_dir", "build_id", "build_dir", "manifest",
    "build_summary", "legacy"}``. ``build_id`` comes from the relative
    ``extract/corpus`` symlink target; a pre-cutover real ``corpus`` directory is
    accepted as the legacy layout with ``build_id =
    cutover-<manifest conversion_id>`` (the id peelfeed's cutover will record).

    Raises :class:`IngestError` with a readable reason when the layout is
    missing, escapes the peel, or has no readable manifest: guessing would let
    the indexer read a half-written or unrelated directory.
    """
    peel_dir = Path(peel_dir).resolve()
    extract = peel_dir / "extract"
    corpus = extract / CORPUS_LINK
    legacy = False
    if corpus.is_symlink():
        try:
            target = os.readlink(str(corpus))
        except OSError as exc:
            raise IngestError(
                "peel '%s': the extract/corpus symlink is unreadable (%s)."
                % (peel_dir.name, exc)) from None
        try:
            build_dir = corpus.resolve()
        except OSError as exc:
            raise IngestError(
                "peel '%s': the extract/corpus symlink cannot be resolved (%s)."
                % (peel_dir.name, exc)) from None
        try:
            build_dir.relative_to(peel_dir)
        except ValueError:
            raise IngestError(
                "peel '%s': the extract/corpus symlink resolves outside the "
                "peel directory (target %r); refusing to read it."
                % (peel_dir.name, str(target))) from None
        if not build_dir.is_dir():
            raise IngestError(
                "peel '%s': the current build directory '%s' does not exist."
                % (peel_dir.name, build_dir.name))
        build_id = build_dir.name
        if not _BUILD_ID_RE.match(build_id):
            raise IngestError(
                "peel '%s': the current build directory name %r is not a "
                "valid build id." % (peel_dir.name, build_id))
    elif corpus.is_dir():
        # Legacy layout: peelfeed has not cut over to conversions/<build_id>/ yet.
        legacy = True
        build_dir = corpus.resolve()
        build_id = ""
    else:
        raise IngestError(
            "peel '%s' has no extract/corpus (neither a symlink to a build nor "
            "a legacy corpus directory); re-extract it with peelfeed."
            % peel_dir.name)
    manifest = _read_json(build_dir / "manifest.json", None)
    if not isinstance(manifest, dict):
        raise IngestError(
            "peel '%s': the current build '%s' has no readable manifest.json."
            % (peel_dir.name, build_dir.name))
    if legacy:
        build_id = _cutover_build_id(manifest)
    summary = _read_json(build_dir / "build_summary.json", None)
    if not isinstance(summary, dict):
        summary = {}
    return {
        "peel_dir": peel_dir,
        "build_id": build_id,
        "build_dir": build_dir,
        "manifest": manifest,
        "build_summary": summary,
        "legacy": legacy,
    }


def reader_lock_path(peel_dir, build_id):
    """The per-build reader lease path (wire §2.8; inode is never replaced)."""
    build_id = str(build_id or "").strip()
    if not _BUILD_ID_RE.match(build_id):
        raise IngestError("invalid build id '%s'." % build_id)
    return (Path(peel_dir).resolve() / "extract" / READER_LOCKS_DIR
            / (build_id + ".lock"))


class ReaderLease(object):
    """A shared (``LOCK_SH``) reader lease on one peelfeed build.

    peelfeed deletes a build only while holding ``LOCK_EX|LOCK_NB`` on the same
    ``extract/reader-locks/<build_id>.lock`` file, so an active index build keeps
    its build directory. The lock file is opened with ``O_CREAT`` and the same
    inode is kept for the whole lease (never temp+rename). Shared readers do not
    rewrite the diagnostic holder content: several readers share the file.
    """

    def __init__(self, peel_dir, build_id):
        self.peel_dir = Path(peel_dir).resolve()
        self.build_id = str(build_id or "").strip()
        self.path = reader_lock_path(self.peel_dir, self.build_id)
        self._fd = None

    def acquire(self, *, blocking=False, timeout=READER_LEASE_TIMEOUT):
        """Take the shared lease or raise :class:`ReaderLeaseBusy`.

        ``blocking=False`` (the default) fails fast; the index builder re-resolves
        the current build and retries when cleanup is in flight. The fd is opened
        before ``flock`` and closed on every failure path.
        """
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        flags = fcntl.LOCK_SH | fcntl.LOCK_NB
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while True:
            try:
                fcntl.flock(fd, flags)
                self._fd = fd
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN,
                                     errno.EWOULDBLOCK):
                    os.close(fd)
                    raise IngestError(
                        "could not take the reader lease on build '%s': %s"
                        % (self.build_id, exc)) from None
                if not blocking or time.monotonic() >= deadline:
                    os.close(fd)
                    raise ReaderLeaseBusy(
                        "build '%s' is held by an exclusive reader lease "
                        "(cleanup in progress); re-resolve the current build."
                        % self.build_id) from None
                time.sleep(0.05)

    def release(self):
        """Release the lease (idempotent); the lock file stays on disk."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()
        return False


def _require_build_complete(build):
    """Refuse to read a build directory that lacks a required corpus file."""
    build_dir = Path(build["build_dir"])
    for filename in PEEL_CORPUS_FILES:
        if not (build_dir / filename).is_file():
            raise IngestError(
                "peel '%s': current build '%s' is incomplete (missing %s); "
                "re-extract or rebuild it with peelfeed."
                % (Path(build["peel_dir"]).name, build["build_id"], filename))


def _acquire_current_build(peel_dir, name, timeout=READER_LEASE_TIMEOUT):
    """Resolve + lease + recheck one peel's current build (wire §2.8).

    Order: resolve ``extract/corpus`` -> build_id -> SH flock -> recheck that
    the symlink still names the same build and the build directory is complete.
    When the current build changed between resolution and the lease, the lease is
    released and the current is re-resolved (a new reader must never read a
    deleted/switched build).
    """
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    while True:
        build = resolve_current_build(peel_dir)
        _require_build_complete(build)
        lease = ReaderLease(peel_dir, build["build_id"])
        try:
            lease.acquire()
        except ReaderLeaseBusy:
            if time.monotonic() >= deadline:
                raise IngestError(
                    "source '%s': peel '%s' build '%s' is being cleaned up and "
                    "did not become readable in %.1fs; retry the index build."
                    % (name, Path(peel_dir).name, build["build_id"], timeout)
                ) from None
            time.sleep(0.05)
            continue
        try:
            again = resolve_current_build(peel_dir)
            _require_build_complete(again)
        except IngestError:
            lease.release()
            raise
        if (again["build_id"] == build["build_id"]
                and again["build_dir"] == build["build_dir"]):
            return again, lease
        lease.release()
        if time.monotonic() >= deadline:
            raise IngestError(
                "source '%s': peel '%s' current build changed repeatedly while "
                "the reader lease was being acquired; retry the index build."
                % (name, Path(peel_dir).name)) from None
        time.sleep(0.05)


def _recheck_build_snapshots(builds):
    """Abort an index generation when a peel's current build changed (§2.8).

    Called before ``CURRENT`` is flipped: the index was read from a fixed build
    directory under a reader lease, so a changed current conversion/effective
    hash/build id means the generation would describe stale inputs. The previous
    generation keeps serving and the job fails with a readable ``needs_reindex``
    reason; never flip ``CURRENT`` on stale inputs.
    """
    for name, item in (builds or {}).items():
        snapshot = item.get("snapshot") or {}
        try:
            current = resolve_current_build(item["peel_dir"])
        except IngestError as exc:
            raise NeedsReindex(
                "source '%s': peel '%s' became unreadable while the index was "
                "being built (%s); the previous index keeps serving — rebuild "
                "the index (needs_reindex)."
                % (name, Path(item["peel_dir"]).name, exc)) from None
        if _build_identity(current) != snapshot:
            now = _build_identity(current)
            raise NeedsReindex(
                "source '%s': peel '%s' current build changed while the index "
                "was being built (was %s/%s, now %s/%s); the previous index "
                "keeps serving — rebuild the index (needs_reindex)."
                % (name, Path(item["peel_dir"]).name,
                   snapshot.get("build_id") or "?",
                   str(snapshot.get("conversion_id") or "?")[:12],
                   now.get("build_id") or "?",
                   str(now.get("conversion_id") or "?")[:12])
            )


def _peel_current_identity(peel_dir):
    """The current conversion/engine/validation identity of one peel, or None.

    Reads the current build's manifest/build_summary/validation through the
    ``extract/corpus`` symlink; a broken peel returns ``None`` so a reconcile
    never fails a dataset read.
    """
    try:
        build = resolve_current_build(peel_dir)
    except IngestError:
        return None
    manifest = build["manifest"]
    build_dir = Path(build["build_dir"])
    validation = _read_json(build_dir / "validation.json", None)
    review_queue = _read_json(build_dir / "review_queue.json", None)
    summary = build.get("build_summary") if isinstance(
        build.get("build_summary"), dict) else {}
    engine_info = summary.get("engine") if isinstance(
        summary.get("engine"), dict) else {}
    counts = summary.get("counts") if isinstance(
        summary.get("counts"), dict) else {}
    engine = str(
        engine_info.get("fingerprint")
        or engine_info.get("version")
        or manifest.get("engine")
        or ""
    )
    pages = _int(counts.get("pages") or manifest.get("pages") or 0, 0)
    return {
        "build_id": str(build["build_id"]),
        "conversion_id": str(manifest.get("conversion_id") or ""),
        "effective_corrections_hash": str(
            manifest.get("effective_corrections_hash") or ""),
        "profile": str(manifest.get("profile") or ""),
        "engine": engine,
        "pages": pages,
        "validation": {
            "errors": _validation_error_count(validation),
            "review_items": _review_item_count(validation, review_queue),
        },
    }


def _reconcile_extractor_label(sources):
    """The dataset-level ``extractor`` label implied by the source identities."""
    versions = set()
    for entry in sources or []:
        block = entry.get("extraction") if isinstance(
            entry.get("extraction"), dict) else {}
        version = str(block.get("engine") or "")
        if version:
            versions.add(version)
    if len(versions) == 1:
        return "peelfeed@" + versions.pop()
    if versions:
        return "peelfeed@mixed"
    return None


def reconcile_peels(dataset_dir, peel_id=None):
    """Refresh every peel-backed source of one dataset from the peel's current.

    Shared-dataset refresh (CP3 wire §2.7 / contract v1.3 §5.3): after a publish
    (or an external CLI publish) **every** dataset that references the peel keeps
    the current ``conversion_id``/``profile``/``engine``/``validation``, including
    terminal ``extracted`` sources. Only a changed ``conversion_id`` makes an
    existing index stale (``index_state`` derives it from ``corpus_digest``); a
    chunk-set-only publish keeps the same conversion_id, so existing indexes stay
    valid. Broken/missing peels are left untouched: a read must never fail
    because one peel is being rebuilt.

    ``peel_id`` limits the refresh to one peel (used after publish/discard/
    rollback); ``None`` refreshes every source. Returns
    ``{"dataset", "checked", "updated"}``.
    """
    dataset_dir = Path(dataset_dir)
    result = {"dataset": dataset_dir.name, "checked": 0, "updated": []}
    try:
        metadata = _read_dataset_json(dataset_dir)
    except IngestError:
        return result
    sources = _source_entries(metadata.get("sources"))
    root = peel_root()
    changed = False
    for entry in sources:
        block = entry.get("extraction") if isinstance(
            entry.get("extraction"), dict) else {}
        identifier = str(block.get("peel_id") or "")
        if not identifier or (peel_id is not None and identifier != peel_id):
            continue
        result["checked"] += 1
        try:
            peel_dir = _resolve_peel_dir(root, identifier)
        except IngestError:
            continue  # broken peel: leave the recorded identity, never crash
        current = _peel_current_identity(peel_dir)
        if current is None:
            continue
        updated = dict(block)
        for field in ("conversion_id", "profile", "engine"):
            value = str(current.get(field) or "")
            if value and str(updated.get(field) or "") != value:
                updated[field] = value
        validation = current.get("validation")
        if isinstance(validation, dict) and updated.get("validation") != validation:
            updated["validation"] = dict(validation)
        if updated != block:
            entry["extraction"] = updated
            changed = True
            result["updated"].append(str(entry.get("sha256") or ""))
    if not changed:
        return result
    metadata["schema"] = contracts.DATASET_SCHEMA
    metadata["sources"] = sources
    metadata["updated_at"] = _now()
    label = _reconcile_extractor_label(sources)
    if label:
        metadata["extractor"] = label
    try:
        validated = contracts.validate_dataset(metadata)
    except contracts.ContractError:
        return result  # a broken metadata must not fail the read
    _write_json_atomic(dataset_dir / "dataset.json", validated, indent=2)
    result["dataset_json"] = validated
    return result


def _datasets_root(paths):
    """The datasets directory for a ``config.paths()`` object or a directory."""
    datasets_dir = getattr(paths, "datasets_dir", None)
    if datasets_dir is not None:
        return Path(datasets_dir)
    path = Path(paths)
    if (path / "datasets").is_dir():
        return path / "datasets"
    return path


def reconcile_all_datasets(paths):
    """Reconcile every dataset under a data root (startup and dataset reads).

    Cheap and fault-tolerant: a broken dataset or peel is skipped, never raised.
    Returns ``{"datasets": n, "updated": {dataset: [sha256, …]}}``.
    """
    root = _datasets_root(paths)
    result = {"datasets": 0, "updated": {}}
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return result
    for name in names:
        if name.startswith("."):
            continue
        try:
            one = reconcile_peels(root / name)
        except Exception:  # noqa: BLE001 - reconcile must never break a read
            continue
        result["datasets"] += 1
        if one.get("updated"):
            result["updated"][name] = one["updated"]
    return result


def reconcile_peel(peel_id, paths=None):
    """Reconcile every dataset that references one peel (after publish/rollback).

    ``paths`` is a ``config.paths()`` object (default: the current data root).
    Returns ``{"peel_id", "datasets", "updated"}``.
    """
    from .. import config

    identifier = str(peel_id or "").strip()
    root = _datasets_root(config.paths() if paths is None else paths)
    result = {"peel_id": identifier, "datasets": 0, "updated": {}}
    if not identifier:
        return result
    try:
        names = sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return result
    for name in names:
        if name.startswith("."):
            continue
        try:
            one = reconcile_peels(root / name, peel_id=identifier)
        except Exception:  # noqa: BLE001 - a refresh must never fail a publish
            continue
        if one.get("checked"):
            result["datasets"] += 1
        if one.get("updated"):
            result["updated"][name] = one["updated"]
    return result


def read_review_jobs(dataset_dir, sha):
    """The persisted ``extraction.review_jobs`` descriptors of one source.

    CP3 wire §2.7: ``{job_id: {"type", "created_at", "batch_id"}}``; terminal
    descriptors stay so a restarted service can re-attach by id. Returns ``{}``
    when the source or mapping does not exist.
    """
    entry = find_source(Path(dataset_dir), sha)
    if entry is None:
        return {}
    block = entry.get("extraction") if isinstance(
        entry.get("extraction"), dict) else {}
    jobs = block.get("review_jobs")
    return dict(jobs) if isinstance(jobs, dict) else {}


def record_review_job(dataset_dir, sha, job_id, *, job_type, batch_id=None,
                      created_at=None):
    """Persist one peelfeed job descriptor in ``extraction.review_jobs``.

    Schema-4 mapping ``{job_id: {"type", "created_at", "batch_id"}}`` (CP3 wire
    §2.7): terminal descriptors stay so a restarted service can re-attach by id;
    peekseed never re-runs a job automatically.
    """
    identifier = str(job_id or "").strip()
    if not identifier:
        raise IngestError("a peelfeed review job id is required.")
    dataset_dir = Path(dataset_dir)
    entry = find_source(dataset_dir, sha)
    if entry is None:
        raise IngestError("no source with sha256 '%s'." % str(sha or "")[:12])
    descriptor = contracts.review_job_descriptor(
        job_type, created_at=created_at, batch_id=batch_id)
    block = dict(entry.get("extraction") or contracts.extraction_default())
    jobs = dict(block.get("review_jobs") or {})
    jobs[identifier] = descriptor
    block["review_jobs"] = jobs
    update_source(dataset_dir, str(entry.get("sha256") or sha), extraction=block)
    return descriptor


# --- chunk sets ------------------------------------------------------------

_SET_DIR = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")


def link_chunk_sizes(link):
    """The embed link's chunk sizes; a link that names none cannot build an index."""
    sizes = (link or {}).get("chunk")
    if not isinstance(sizes, dict) or any(
            isinstance(sizes.get(key), bool) or not isinstance(sizes.get(key), int)
            for key in CHUNK_KEYS):
        raise NoChunkSizes(
            "embed link '%s' has no chunk sizes; set target chars, max chars and "
            "image cap on the link (Settings > Model links) before building an "
            "index." % str((link or {}).get("id") or ""))
    return {key: int(sizes[key]) for key in CHUNK_KEYS}


def chunk_set_name(sizes):
    """The peelfeed chunk set for these sizes, e.g. ``c8000-12000-i3``."""
    return "c%d-%d-i%d" % tuple(int(sizes[key]) for key in CHUNK_KEYS)


def _peel_chunk_sets(corpus_dir):
    """``{set dir: sizes}`` recorded in a corpus' ``versions.json``."""
    registry = _read_json(Path(corpus_dir) / "versions.json", None)
    versions = registry.get("versions") if isinstance(registry, dict) else None
    sets = {}
    for name, version in (versions.items() if isinstance(versions, dict) else ()):
        if not isinstance(version, dict):
            continue
        directory = str(version.get("dir") or name)
        if not _SET_DIR.match(directory):
            continue
        try:
            sets[directory] = {"target_chars": int(version["chunk_target_chars"]),
                               "max_chars": int(version["chunk_max_chars"]),
                               "image_cap": int(version["rerank_img_cap"])}
        except (KeyError, TypeError, ValueError):
            continue
    return sets


def _find_chunk_set(corpus_dir, sizes):
    """The corpus' chunk-set directory for ``sizes`` (with its feed), or None."""
    for directory, recorded in _peel_chunk_sets(corpus_dir).items():
        feed = Path(corpus_dir) / "chunks_all_ver" / directory / "chunks.jsonl"
        if recorded == sizes and feed.is_file():
            return directory
    return None


def _ensure_chunk_sets(dataset_dir, dataset, sizes, notify=None):
    """Have peelfeed add the link's chunk set to every extracted peel lacking it.

    ``peelfeed rebuild`` reuses the Docling evidence, keeps the recorded sets and
    leaves the canonical chunks (and normally the conversion id) unchanged, so no
    other index goes stale. The rebuild may auto-publish a system build (contract
    §5.3): afterwards **every** dataset that references the peel is reconciled,
    not just the initiating one. Returns the dataset, re-read when anything was
    rebuilt. This runs **before** any reader lease is taken (wire §2.8).
    """
    root = peel_root()
    name = chunk_set_name(sizes)
    rebuilt = False
    rebuilt_peels = []
    for entry in _source_entries(dataset.get("sources")):
        block = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
        peel_id = str(block.get("peel_id") or "")
        if contracts.extraction_state(entry) != "extracted" or not peel_id:
            continue
        try:
            peel_dir = _resolve_peel_dir(root, peel_id)
            corpus_dir = peel_dir / "extract" / "corpus"
        except IngestError:
            continue  # _read_peel reports the broken peel with its reason
        if _find_chunk_set(corpus_dir, sizes) is not None:
            continue
        table = list(_peel_chunk_sets(corpus_dir).values())
        if sizes not in table:
            table.append(sizes)
        source = str(entry.get("name") or "")
        if notify is not None:
            notify(0.01, "Building chunk set %s for %s" % (name, source))
        try:
            from .. import peelfeed

            peelfeed.client().rebuild(
                peel_id, reasons=["chunk set %s for an index build" % name],
                chunk_versions=table)
        except Exception as exc:  # noqa: BLE001 - report which source and why
            detail = getattr(exc, "detail", "") or str(exc)
            raise IngestError(
                "source '%s' has no chunk set %s and peelfeed could not build it: %s"
                % (source, name, detail)) from None
        rebuilt = True
        rebuilt_peels.append(peel_id)
    if not rebuilt:
        return dataset
    # Shared-dataset refresh: the rebuild may have published a new current build
    # (new conversion_id) for this peel; all datasets that reference it follow.
    for peel_id in rebuilt_peels:
        try:
            reconcile_peel(peel_id)
        except Exception:  # noqa: BLE001 - the build path reports its own errors
            pass
    return _read_dataset_json(Path(dataset_dir))


def _set_chunk_id(set_name, chunk_id):
    """A chunk-set row id: unique across sets, never equal to a canonical id."""
    return hashlib.sha256(("%s|%s" % (set_name, chunk_id)).encode("utf-8")).hexdigest()[:16]


def _corpus_chunk_record(raw, entry, dataset_id, position, ordinal, block_map,
                         manifest, peel_id):
    """Map one peelfeed corpus chunk onto peekseed's chunk contract (§4.2).

    The corpus chunk is the canonical unit; peekseed only adds its content-derived
    id, its source identity and the two views (``doc`` for BM25/embedding,
    ``text`` for the reader). The id uses the ordinal of the chunk's first block
    in ``blocks.jsonl`` when available (stable and page-independent), else the
    chunk's position in the corpus.
    """
    sha = str(entry.get("sha256") or "").lower()
    page_start = _int(raw.get("page_start") or raw.get("page") or 1, 1)
    page_start = max(1, page_start)
    page_end = max(page_start, _int(raw.get("page_end") or page_start, page_start))
    text = str(raw.get("text") or "").strip()
    section = str(raw.get("section") or "")
    path = str(raw.get("path") or section)
    numbering = str(raw.get("numbering") or "")
    parent = str(raw.get("parent") or "")
    chapter = str(raw.get("chapter") or "")
    if not chapter:
        chapter = path.split("/")[0].strip() if "/" in path else (path or section)
    block_ids = raw.get("block_ids") if isinstance(raw.get("block_ids"), list) else []
    block_index = None
    if block_ids:
        block_index = block_map.get(str(block_ids[0]))
    if block_index is None:
        block_index = position
    identifier = contracts.chunk_id(sha, block_index, position, PEEL_CHUNKER_VERSION)
    body = text or section or identifier
    breadcrumb = path or section
    doc = (breadcrumb + "\n\n" + body) if breadcrumb else body
    kind = str(raw.get("kind") or "text")
    image_paths = [str(value) for value in raw.get("image_paths") or []]
    source_pages = [
        max(1, _int(value, page_start)) for value in raw.get("source_pages") or []
    ] or [page_start]
    record = {
        "id": identifier,
        "dataset": dataset_id,
        "source": str(entry.get("name") or ""),
        "source_sha256": sha,
        "page_start": page_start,
        "page_end": page_end,
        "section": section,
        "chapter": chapter,
        "numbering": numbering,
        "parent": parent,
        "path": path,
        "doc": doc,
        "text": body,
        "heading_only": bool(raw.get("heading_only")),
        "block_index": block_index,
        "chunk_index": position,
        "chunker_version": PEEL_CHUNKER_VERSION,
        "ordinal": ordinal,
        "pdf_y": _int(float(raw.get("pdf_y") or 0), 0),
        "block_kind": kind,
        "kind": kind,
        "image_paths": image_paths,
        "source_pages": source_pages,
        "char_count": max(1, len(body)),
        "parts": max(1, _int(raw.get("parts") or 1, 1)),
        "part": max(1, _int(raw.get("part") or 1, 1)),
        "canonical_ids": [str(value) for value in raw.get("canonical_ids") or [identifier]],
        "engine": str(manifest.get("engine") or ""),
        "conversion_id": str(manifest.get("conversion_id") or ""),
        "peel_id": peel_id,
    }
    prev = str(raw.get("prev") or "")
    next_value = str(raw.get("next") or "")
    if prev:
        record["prev"] = prev
    if next_value:
        record["next"] = next_value
    return record


def _read_peel(dataset_dir, entry, root, dataset_id, sizes=None, build=None):
    """Read one extracted source's corpus, refusing anything that fails a gate.

    Gates (design §4.6): the peel dir must resolve under ``peel_root``, the
    corpus manifest's ``source_sha256`` must equal the stored source's sha256,
    the recorded ``conversion_id`` must match the manifest, and
    ``validation.errors`` must be 0. A failure names the source and the reason;
    it never falls back to re-extracting the PDF.

    ``build`` is the resolved current build (``resolve_current_build`` output):
    CP3 reads **that build directory** for the whole index build, never the
    ``extract/corpus`` symlink again mid-build (wire §2.8). When omitted the
    current build is resolved here (internal/test callers).

    ``records`` are the canonical chunks (the reading layer). With ``sizes`` the
    matching chunk set is read too: ``set_records`` are what gets embedded, each
    carrying ``reading_ids`` (the canonical chunk ids it covers).
    """
    name = str(entry.get("name") or "")
    extraction = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
    peel_id = str(extraction.get("peel_id") or "").strip()
    if build is None:
        peel_dir = _resolve_peel_dir(root, peel_id)
        build = resolve_current_build(peel_dir)
    peel_dir = Path(build["peel_dir"])
    corpus_dir = Path(build["build_dir"])
    manifest = build.get("manifest") if isinstance(build.get("manifest"), dict) else {}
    build_summary = build.get("build_summary") if isinstance(
        build.get("build_summary"), dict) else {}
    for filename in PEEL_CORPUS_FILES:
        if not (corpus_dir / filename).is_file():
            raise IngestError(
                "source '%s': peel '%s' has no corpus/%s."
                % (name, peel_id, filename)
            )
    if not isinstance(manifest, dict) or not manifest:
        raise IngestError("source '%s': peel '%s' manifest.json is not an object."
                          % (name, peel_id))
    schema = manifest.get("schema_version") or manifest.get("schema")
    if isinstance(schema, int) and not isinstance(schema, bool) \
            and schema > SUPPORTED_CORPUS_SCHEMA:
        raise IngestError(
            "source '%s': peel '%s' corpus schema %d is newer than this peekseed "
            "build supports (%d); upgrade peekseed."
            % (name, peel_id, schema, SUPPORTED_CORPUS_SCHEMA)
        )
    validation = _read_json(corpus_dir / "validation.json", None)
    review_queue = _read_json(corpus_dir / "review_queue.json", None)
    assets = _read_jsonl(corpus_dir / "assets.jsonl", "assets.jsonl")
    chunks = _read_jsonl(corpus_dir / "chunks.jsonl", "chunks.jsonl")
    blocks_path = corpus_dir / "blocks.jsonl"
    blocks = _read_jsonl(blocks_path, "blocks.jsonl") if blocks_path.is_file() else []

    sha = str(entry.get("sha256") or "").lower()
    manifest_sha = str(manifest.get("source_sha256") or "").lower()
    if not sha or manifest_sha != sha:
        raise IngestError(
            "source '%s': peel '%s' was built from a different source "
            "(manifest sha256 %s != stored %s); re-extract it."
            % (name, peel_id, manifest_sha[:12] or "?", sha[:12] or "?")
        )
    manifest_conversion = str(manifest.get("conversion_id") or "")
    recorded_conversion = str(extraction.get("conversion_id") or "")
    if not recorded_conversion:
        raise IngestError(
            "source '%s': no recorded conversion_id for peel '%s'; re-extract it."
            % (name, peel_id)
        )
    if recorded_conversion != manifest_conversion:
        raise IngestError(
            "source '%s': peel '%s' conversion_id does not match the recorded one "
            "(peel %s, recorded %s); re-extract it."
            % (name, peel_id, manifest_conversion[:12] or "?",
               recorded_conversion[:12] or "?")
        )
    errors = _validation_error_count(validation)
    if errors:
        first = ""
        if isinstance(validation, dict) and isinstance(validation.get("errors"), list):
            first = str(validation["errors"][0])[:160]
        raise IngestError(
            "source '%s': peel '%s' validation has %d error(s)%s; fix it with "
            "peelfeed before indexing."
            % (name, peel_id, errors, (": " + first) if first else "")
        )
    validation_conversion = str((validation or {}).get("conversion_id") or "") \
        if isinstance(validation, dict) else ""
    if validation_conversion and validation_conversion != manifest_conversion:
        raise IngestError(
            "source '%s': peel '%s' validation.json conversion_id does not "
            "belong to this conversion; re-extract it." % (name, peel_id)
        )
    source_file = _peel_source_file(peel_dir, name)
    if source_file is None:
        raise IngestError("source '%s': peel '%s' has no source file."
                          % (name, peel_id))
    expected_bytes = _int(entry.get("bytes") or 0, 0)
    try:
        actual_bytes = source_file.stat().st_size
    except OSError:
        actual_bytes = 0
    if expected_bytes and actual_bytes and actual_bytes != expected_bytes:
        raise IngestError(
            "source '%s': peel '%s' source file is %d bytes, the stored source is "
            "%d; re-extract it."
            % (name, peel_id, actual_bytes, expected_bytes)
        )

    block_map = {}
    for position, block in enumerate(blocks):
        identifier = str(block.get("id") or "")
        if identifier:
            block_map.setdefault(identifier, position)
    records = []
    for position, raw in enumerate(chunks):
        records.append(_corpus_chunk_record(
            raw, entry, dataset_id, position, len(records) + 1, block_map,
            manifest, peel_id,
        ))
    if not records:
        raise IngestError("source '%s': peel '%s' corpus has no chunks."
                          % (name, peel_id))
    engine_info = build_summary.get("engine") \
        if isinstance(build_summary.get("engine"), dict) else {}
    engine = str(
        engine_info.get("fingerprint")
        or engine_info.get("version")
        or manifest.get("engine")
        or manifest.get("engine_fingerprint")
        or extraction.get("engine")
        or ""
    )
    counts = build_summary.get("counts") if isinstance(
        build_summary.get("counts"), dict) else {}
    pages = _int(counts.get("pages") or manifest.get("pages") or 0, 0)
    for record in records:
        record["engine"] = engine
    set_records = []
    if sizes is not None:
        directory = _find_chunk_set(corpus_dir, sizes)
        if directory is None:
            raise IngestError(
                "source '%s': peel '%s' has no chunk set %s; rebuild it with peelfeed."
                % (name, peel_id, chunk_set_name(sizes)))
        feed_label = "chunks_all_ver/%s/chunks.jsonl" % directory
        reading = {str(raw.get("id") or ""): record["id"]
                   for raw, record in zip(chunks, records)}
        for position, raw in enumerate(_read_jsonl(corpus_dir / feed_label, feed_label)):
            record = _corpus_chunk_record(
                raw, entry, dataset_id, position, len(set_records) + 1, block_map,
                manifest, peel_id,
            )
            record["id"] = _set_chunk_id(directory, record["id"])
            record["engine"] = engine
            record["chunk_set"] = directory
            record["reading_ids"] = [reading[unit] for unit in record["canonical_ids"]
                                     if unit in reading]
            set_records.append(record)
        if not set_records:
            raise IngestError("source '%s': peel '%s' chunk set %s is empty."
                              % (name, peel_id, directory))
    peel_info = {
        "peel_id": peel_id,
        "conversion_id": manifest_conversion,
        "profile": str(manifest.get("profile") or extraction.get("profile") or ""),
        "validation": {
            "errors": 0,
            "review_items": _review_item_count(validation, review_queue),
        },
        "engine": engine,
        "pages": pages,
        "assets": len(assets),
    }
    return {"records": records, "set_records": set_records, "peel": peel_info,
            "entry": entry, "manifest": manifest}


# --- source summaries -----------------------------------------------------


def _sources_summary(dataset_dir, dataset):
    """Provenance rows for ``info.json``: identity, pages and extraction state.

    Declared upload order wins over directory order. A hash is computed from the
    file when ``dataset.json`` predates content hashing; when the file is gone
    the entry is marked ``missing`` with no hash rather than inventing one. The
    per-source ``extraction`` block is carried through so an index records which
    peel/conversion produced its chunks.
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
        byte_size = _int(entry.get("bytes"), 0) or (
            _size(file_path) if file_path is not None else 0
        )
        row = {
            "name": name,
            "path": path,
            "sha256": sha,
            "bytes": byte_size,
            "pages": _int(entry.get("pages"), 0),
            "extraction": dict(entry.get("extraction") or
                               contracts.extraction_default()),
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
            "pages": 0,
            "extraction": contracts.extraction_default(),
        })
    return rows


def _extractor_identity(dataset):
    """The peelfeed engine identity recorded for a dataset's extracted sources."""
    label = str((dataset or {}).get("extractor") or "")
    if label.startswith("peelfeed@"):
        return {"name": "peelfeed", "version": label.split("@", 1)[1]}
    versions = set()
    for entry in _source_entries((dataset or {}).get("sources")):
        block = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
        version = str(block.get("engine") or "")
        if version:
            versions.add(version)
    if len(versions) == 1:
        return {"name": "peelfeed", "version": versions.pop()}
    if versions:
        return {"name": "peelfeed", "version": "mixed"}
    return {"name": "", "version": ""}


def _documents_digest(sources):
    try:
        return contracts.documents_digest(sources)
    except contracts.ContractError:
        return ""


def _corpus_digest(sources):
    try:
        return contracts.corpus_digest(sources)
    except contracts.ContractError:
        return ""


# --- resumable generations -------------------------------------------------


def _find_resumable_generation(space_dir, *, space, digest, chunker, n,
                              corpus=None):
    """An interrupted generation that can continue from its checkpoint.

    Compatibility is exact: same space, same documents digest, same peel corpus
    digest, same chunker version, same chunk count, a readable vector memmap of
    that row count and a stored probe whose dimension matches the memmap width.
    Anything else starts a fresh generation, because resuming into a changed
    corpus would write vectors that do not belong to their rows.
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
        if corpus is not None and str(progress.get("corpus_digest") or "") != str(corpus):
            continue
        if not _same_chunker(progress.get("chunker"), chunker):
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


def _write_progress(generation_dir, *, space, digest, chunker, n, dim, row,
                    corpus=None):
    _write_json_atomic(
        Path(generation_dir) / PROGRESS_FILE,
        {
            "space": str(space),
            "documents_digest": str(digest),
            "corpus_digest": str(corpus or ""),
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
                chunker, extractor, digest, corpus, peels, skipped):
    target = _link_target(link)
    embedding = contracts.space_identity(link, dim=dim)
    embedding["probe"] = {
        "text_sha256": contracts.probe_sha256(),
        "file": PROBE_FILE,
    }
    if len(peels) == 1:
        peel_block = dict(peels[0])
    else:
        peel_block = {"peels": [dict(peel) for peel in peels],
                      "count": len(peels)}
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
        "corpus_digest": str(corpus),
        "peel": peel_block,
        "skipped_sources": [dict(item) for item in skipped],
        "sources": sources,
    }
    return contracts.validate_info(info)


def _peel_extractor(peels):
    """The ``info.extractor`` identity for the peels an index was built from."""
    versions = sorted({str(peel.get("engine") or "") for peel in peels})
    versions = [version for version in versions if version]
    if len(versions) == 1:
        return {"name": "peelfeed", "version": versions[0]}
    if len(versions) > 1:
        return {"name": "peelfeed", "version": "mixed"}
    return {"name": "peelfeed", "version": ""}


def _refresh_extraction(dataset_dir, dataset, peels):
    """Write the peel identity/pages/validation back into ``dataset.json``.

    The extract route records ``queued`` and then polls peelfeed, but the corpus
    manifest is the authoritative source for page count/validation/engine. Doing
    this refresh at build time also repairs a dataset whose extraction block was
    written by an older peekseed build.
    """
    dataset_dir = Path(dataset_dir)
    sources = _source_entries(dataset.get("sources"))
    by_name = {str(peel["name"]): peel["peel"] for peel in peels}
    changed = False
    for entry in sources:
        peel = by_name.get(str(entry.get("name") or ""))
        if peel is None:
            continue
        extraction = dict(entry.get("extraction") or contracts.extraction_default())
        updated = dict(extraction)
        updated["state"] = "extracted"
        updated["peel_id"] = str(peel.get("peel_id") or "")
        updated["conversion_id"] = str(peel.get("conversion_id") or "")
        updated["profile"] = str(peel.get("profile") or "")
        updated["engine"] = str(peel.get("engine") or "")
        updated["validation"] = dict(peel.get("validation") or
                                     {"errors": 0, "review_items": 0})
        updated["finished_at"] = extraction.get("finished_at") or _now()
        updated["error"] = None
        if updated != extraction:
            entry["extraction"] = updated
            changed = True
        pages = _int(peel.get("pages") or 0, 0)
        if pages and _int(entry.get("pages") or 0, 0) != pages:
            entry["pages"] = pages
            changed = True
        if str(entry.get("status") or "") != "extracted":
            entry["status"] = "extracted"
            changed = True
    if not changed:
        return dataset
    extractor = _peel_extractor([peel["peel"] for peel in peels])
    metadata = dict(dataset)
    metadata["schema"] = contracts.DATASET_SCHEMA
    metadata["sources"] = sources
    metadata["extractor"] = contracts.extractor_label(extractor)
    metadata["chunker"] = contracts.chunker_label()
    metadata["updated_at"] = _now()
    metadata = contracts.validate_dataset(metadata)
    _write_json_atomic(dataset_dir / "dataset.json", metadata, indent=2)
    return metadata


def _lease_current_builds(dataset, notify=None):
    """Resolve, lease and snapshot the current build of every extracted source.

    CP3 wire §2.8 order: ``_ensure_chunk_sets`` -> resolve ``extract/corpus`` ->
    build_id -> ``flock(LOCK_SH)`` on ``extract/reader-locks/<build_id>.lock`` ->
    recheck current + build completeness. The returned mapping is keyed by source
    sha256 and carries the fixed build dir, the lease (released by the caller) and
    the snapshot the pre-``CURRENT`` recheck compares. On a failure mid-way every
    lease acquired so far is released.
    """
    root = peel_root()
    builds = {}
    try:
        for entry in _source_entries(dataset.get("sources")):
            block = entry.get("extraction") if isinstance(
                entry.get("extraction"), dict) else {}
            peel_id = str(block.get("peel_id") or "")
            if contracts.extraction_state(entry) != "extracted" or not peel_id:
                continue
            sha = str(entry.get("sha256") or "").lower()
            if not sha or sha in builds:
                continue
            name = str(entry.get("name") or "")
            try:
                peel_dir = _resolve_peel_dir(root, peel_id)
            except IngestError:
                continue  # _read_peel reports the broken peel with its reason
            build, lease = _acquire_current_build(peel_dir, name)
            if notify is not None:
                notify(0.01, "Leased current build %s for %s"
                       % (build["build_id"], name))
            builds[sha] = {
                "name": name,
                "peel_id": peel_id,
                "peel_dir": peel_dir,
                "build": build,
                "lease": lease,
                "snapshot": _build_identity(build),
            }
    except BaseException:
        for item in builds.values():
            try:
                item["lease"].release()
            except Exception:  # noqa: BLE001 - never mask the real failure
                pass
        raise
    return builds


def build_index(
    dataset_dir,
    link,
    *,
    progress=None,
    batch_size=DEFAULT_BATCH_SIZE,
    job=None,
    cancel=None,
    lock=None,
    force=False,
):
    """Build (or rebuild) one dataset's index from its peelfeed corpora.

    Every source whose ``extraction.state == "extracted"`` is read from its peel
    corpus (``<peel_root>/<peel_id>/extract/corpus``), gated on the realpath
    allowlist, the source sha256, the recorded ``conversion_id`` and
    ``validation.errors == 0``. Sources that are not extracted are listed in
    ``info.json`` ``skipped_sources`` -- never silently ignored -- and when no
    source is extracted :class:`NoExtractedSources` is raised (the HTTP layer maps
    it to 409). There is **no in-process extraction fallback**.

    ``chunks.jsonl`` is rewritten from the corpus chunks (the canonical input).
    The vectors are built from the chunk set matching the link's ``chunk`` sizes
    (:class:`NoChunkSizes` when the link names none); a peel without that set
    gets it from ``peelfeed rebuild`` first. Each embedded row records the
    canonical ``reading_ids`` a search hit maps back to.
    An index already built from the same corpora/chunker/extractor is returned
    unchanged, so a second build makes **zero** embedding calls unless
    ``force=True`` (``reindex`` passes it). Then:

    * the fixed probe text is embedded and stored (the space-mismatch gate);
    * every chunk is embedded in batches into a **preallocated** memmap, with a
      per-batch checkpoint (no ``vstack`` of the whole corpus in memory);
    * transient failures (429/5xx/timeouts) are retried with backoff and jitter;
    * the new generation's ``info.json`` is written, and ``CURRENT`` is flipped
      **last**, so a crash or cancel leaves the previous generation serving;
    * the job record (``.peekseed/jobs/<id>.json``) tracks the lifecycle.

    ``progress(frac, message)`` is called with a 0..1 fraction at each stage.
    ``cancel`` is a zero-argument callable checked between batches. Returns the
    ``info.json`` content (plus ``up_to_date=True`` when nothing was rebuilt).

    ``job`` is either the persisted job record the caller wants this build to
    own (the API creates the record and passes it, so the job id the client polls
    is the record on disk) or a legacy in-memory dict kept for compatibility.
    ``lock`` is an already-held :class:`peekseed.ingest.lock.CollectionLock`;
    when omitted the builder takes the collection's writer lock itself.
    """
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise IngestError("Dataset directory not found: '%s'." % dataset_dir.name)
    dataset = _read_dataset_json(dataset_dir)
    dataset_id = str(dataset.get("id") or dataset_dir.name)
    target = _link_target(link)
    space = embed_space_key(link)
    sizes = link_chunk_sizes(link)
    set_name = chunk_set_name(sizes)
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

    builds = {}
    try:
        notify(0.0, "Reconciling dataset sources")
        # Shared-dataset refresh first: the extraction identity in dataset.json is
        # what the corpus gates compare, so a publish by another dataset/CLI must
        # be picked up before this build reads anything (contract §5.3).
        try:
            reconcile_peels(dataset_dir)
        except Exception:  # noqa: BLE001 - the gates below report a broken peel
            pass
        dataset = _read_dataset_json(dataset_dir)
        notify(0.0, "Reading peel corpora")
        dataset = _ensure_chunk_sets(dataset_dir, dataset, sizes, notify)
        # CP3 wire §2.8: lease the current build of every source *after* the
        # chunk-set rebuild and read that fixed build dir for the whole build.
        builds = _lease_current_builds(dataset, notify)
        chunks, embedded, peels, skipped = _collect_corpus_chunks(
            dataset_dir, dataset, sizes,
            {sha: item["build"] for sha, item in builds.items()})
        dataset = _refresh_extraction(dataset_dir, dataset, peels)
        total = len(embedded)
        notify(0.02, "Read %d chunk(s) of set %s from %d peel(s)"
               % (total, set_name, len(peels)))
        existing_chunks = read_chunks(dataset_dir)
        if existing_chunks != chunks:
            write_chunks(dataset_dir, chunks)
            notify(0.05, "Wrote %d chunk(s)" % len(chunks))
        if not total:
            raise IngestError("No chunks to index for dataset '%s'." % dataset_id)

        sources = _sources_summary(dataset_dir, dataset)
        digest = _documents_digest(sources)
        corpus = _corpus_digest(sources)
        chunker = contracts.chunker_identity(**sizes)
        extractor = _peel_extractor([item["peel"] for item in peels])
        peel_list = [item["peel"] for item in peels]
        texts = [_embedding_text(chunk) for chunk in embedded]

        space_dir = _space_dir(dataset_dir, space)
        space_dir.mkdir(parents=True, exist_ok=True)
        generation_dirs = _generation_dirs(space_dir)

        # An interrupted generation is resumed before the up-to-date shortcut:
        # the user asked for a build and the checkpoint is already paid for.
        # `force=True` (reindex) always starts a fresh generation.
        resumable, start_row = (None, 0)
        if not force and generation_dirs:
            resumable, start_row = _find_resumable_generation(
                space_dir, space=space, digest=digest, chunker=chunker,
                n=total, corpus=corpus,
            )
        if not force and resumable is None:
            current = _current_info_if_up_to_date(
                dataset_dir, space, total, digest, corpus, chunker, extractor)
            if current is not None:
                # Even an "up to date" answer must not be returned from a build
                # that changed under the reader lease.
                _recheck_build_snapshots(builds)
                notify(1.0, "Index is up to date: %d chunk(s)" % total)
                _job_touch(job_record, jobs_dir, state="succeeded", phase="done",
                           done=total, total=total, fraction=1.0)
                if legacy_job is not None:
                    legacy_job["progress"] = 1.0
                    legacy_job["message"] = "index up to date: %d chunks" % total
                if own_lock is not None:
                    own_lock.release()
                result = dict(current)
                result["up_to_date"] = True
                return result
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
                            chunker=chunker, n=total, dim=dim, row=0,
                            corpus=corpus)
        else:
            notify(0.2, "Resuming generation %s at row %d/%d"
                   % (generation, start_row, total))

        notify(0.25, "Writing %d metadata row(s)" % total)
        _write_json_atomic(
            generation_dir / "meta.json",
            [_meta_record(chunk) for chunk in embedded],
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
                            chunker=chunker, n=total, dim=dim, row=row,
                            corpus=corpus)
            fraction = 0.3 + 0.6 * (row / max(1, total))
            notify(fraction, "Embedded %d/%d chunk(s)" % (row, total))
            _job_touch(job_record, jobs_dir, done=row, fraction=fraction)
            if legacy_job is not None:
                legacy_job["progress"] = max(0.0, min(1.0, fraction))
                legacy_job["message"] = "embedded %d/%d" % (row, total)

        notify(0.93, "Writing index files")
        info = _build_info(dataset, dataset_id, space, generation, dim, total,
                           link, sources, chunker, extractor, digest, corpus,
                           peel_list, skipped)
        _write_json_atomic(generation_dir / "info.json", info, indent=2)
        # CP3 wire §2.8: before the commit point, recheck every peel's current
        # conversion/effective hash/build id against the lease snapshot. A change
        # means this generation would describe stale inputs: abort without
        # flipping CURRENT (the previous generation keeps serving) and fail the
        # job with a readable needs_reindex reason.
        _recheck_build_snapshots(builds)
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
        if isinstance(exc, NeedsReindex):
            # The generation was abandoned because the peel moved on; repair the
            # recorded extraction identity so the dataset reflects the new
            # current build instead of the lease-time snapshot.
            try:
                reconcile_peels(dataset_dir)
            except Exception:  # noqa: BLE001 - the job error is the real report
                pass
        _job_touch(job_record, jobs_dir, state="failed", error=str(exc))
        if legacy_job is not None:
            legacy_job["error"] = str(exc)[:500]
        if own_lock is not None:
            own_lock.release()
        raise
    finally:
        # The reader lease is released after the CURRENT flip and on every error
        # path; peelfeed may then clean up the build (wire §2.8).
        for item in builds.values():
            try:
                item["lease"].release()
            except Exception:  # noqa: BLE001 - release must never mask an error
                pass


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

    info = build_index(dataset_dir, link, progress=progress, force=True)
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
