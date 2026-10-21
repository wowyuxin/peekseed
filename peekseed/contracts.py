"""Versioned data contracts for peekseed's workspace and ingestion pipeline.

The workspace layout and every JSON artifact written into it are a public data
format: users back them up, copy them between machines and keep them across
upgrades. This module is the single home of those contracts, the version gates
that protect them and the content-derived identifiers that make incremental
ingestion safe. The rest of the package imports the rules from here instead of
re-deriving them, so a schema change happens in exactly one place.

Layout (owned by :mod:`peekseed.workspace`; recorded here so the derived tree is
defined in the same place as the files that live in it)::

    <workspace>/
        documents/                       user-owned sources; peekseed never writes here
        .peekseed/                       derived, rebuildable, safe to delete
            manifest.json                scan state                     schema 1
            config/links.json            model links; 0600
            datasets/<collection>/       dataset.json                   schema 2
                                         chunks.jsonl                   chunk rows
                                         index/<space>/                 vectors + info.json
            extracted/<sha256>.json      extraction cache
            history/
            jobs/<id>.json               job record                     schema 1
        .gitignore                       `init` ensures /workspace/ is ignored

Version rules
-------------

* Readers **refuse** a file whose declared schema is newer than this module
  knows: guessing at a future format would corrupt user data. The message names
  the file, the found schema and the supported one, and tells the user to
  upgrade peekseed.
* Older schemas are upgraded **explicitly** (``upgrade_dataset``,
  ``upgrade_info``) and never silently rewritten on read; ``peekseed migrate``
  is the CLI surface for that.
* Files written here are atomic: a same-directory temp file, ``fsync`` and
  ``os.replace``, so a crash cannot leave a half-written JSON file that the next
  read would treat as corrupt.

Identifiers
-----------

* ``chunk_id(source_sha256, block_index, chunk_index, chunker_version)`` is
  content-derived. Adding file B to a collection cannot renumber file A's chunks,
  which is what makes incremental embedding safe; the human-facing ``ordinal`` is
  a separate display field and may change freely.
* ``space_key(link)`` names one embedding space directory and combines the link
  id with the model string. Two servers that report the same model but produce
  different vectors are told apart by the probe (see ``PROBE_TEXT``), not by the
  name alone.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "CHUNKER_NAME",
    "CHUNKER_VERSION",
    "COLLECTION_STATUSES",
    "ContractError",
    "DATASET_SCHEMA",
    "FILE_STATUSES",
    "INFO_SCHEMA",
    "JOB_SCHEMA",
    "JOB_STATES",
    "MANIFEST_SCHEMA",
    "PROBE_TEXT",
    "SchemaTooNew",
    "chunk_id",
    "chunker_identity",
    "chunker_label",
    "documents_digest",
    "ensure_current",
    "extractor_identity",
    "extractor_label",
    "file_status",
    "has_probe",
    "new_job",
    "new_manifest",
    "probe_sha256",
    "read_json",
    "space_identity",
    "space_key",
    "stale_reasons",
    "utc_now",
    "validate_chunk",
    "validate_dataset",
    "validate_info",
    "validate_job",
    "validate_manifest",
    "write_json_atomic",
    "upgrade_dataset",
    "upgrade_info",
]

# --- contract versions -----------------------------------------------------

MANIFEST_SCHEMA = 1
DATASET_SCHEMA = 2
INFO_SCHEMA = 2
JOB_SCHEMA = 1

CHUNKER_NAME = "headings"
CHUNKER_VERSION = 1

# File-level states. ``indexed`` is the terminal state (vectors exist and the
# file is searchable); ``embedded`` is the same stage reported by an index build
# in progress. ``missing`` keeps the last known hash so a prune is explicit.
FILE_STATUSES = (
    "new",
    "unchanged",
    "changed",
    "missing",
    "extracted",
    "chunked",
    "embedded",
    "indexed",
    "error",
    "skipped",
)

# Collection-level states. The pipeline states (extracted/chunked/embedded) are
# transient; the terminal states are ``indexed``, ``stale`` (the index no longer
# matches its inputs) and ``needs_reindex`` (the index on disk is unreadable or
# invalid). ``not_indexed`` means chunked sources exist with no vectors yet.
COLLECTION_STATUSES = (
    "new",
    "scanned",
    "extracted",
    "chunked",
    "embedding",
    "embedded",
    "indexed",
    "stale",
    "needs_reindex",
    "not_indexed",
    "missing",
    "error",
    "skipped",
)

JOB_STATES = ("queued", "running", "interrupted", "cancelled", "failed", "succeeded")
JOB_KINDS = ("scan", "index", "reindex", "import", "extract")

# A fixed probe text is embedded at index time and re-embedded at query time.
# The cosine between the two proves the live server is the same embedding space
# as the one that built the index. The version tag means the text itself is a
# contract: changing it invalidates stored probe vectors, so it must not change
# without a schema change.
PROBE_TEXT = (
    "peekseed embedding probe v1: the quick brown fox jumps over the lazy dog "
    "while a calm river runs through the quiet valley."
)

_SHA256_LEN = 64
_ID_LEN = 16


class ContractError(ValueError):
    """A stored file does not satisfy its declared schema."""


class SchemaTooNew(ContractError):
    """A stored file declares a schema version this build cannot understand."""


def utc_now():
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SSZ`` (stable across platforms)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- small validation helpers ---------------------------------------------


def _require_dict(value, kind):
    if not isinstance(value, dict):
        raise ContractError("%s must be a JSON object." % kind)
    return value


def _require_str(value, field, kind, *, allow_empty=False):
    if not isinstance(value, str):
        raise ContractError("%s field '%s' must be a string." % (kind, field))
    if not allow_empty and not value.strip():
        raise ContractError("%s field '%s' must not be empty." % (kind, field))
    return value


def _require_int(value, field, kind, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("%s field '%s' must be an integer." % (kind, field))
    if value < minimum:
        raise ContractError(
            "%s field '%s' must be >= %d (got %d)." % (kind, field, minimum, value)
        )
    return value


def _require_bool(value, field, kind):
    if not isinstance(value, bool):
        raise ContractError("%s field '%s' must be a boolean." % (kind, field))
    return value


def _require_sha256(value, field, kind, *, allow_empty=False):
    _require_str(value, field, kind, allow_empty=allow_empty)
    if value == "" and allow_empty:
        return value
    lowered = value.strip().lower()
    if len(lowered) != _SHA256_LEN or any(
        char not in "0123456789abcdef" for char in lowered
    ):
        raise ContractError(
            "%s field '%s' must be a 64-character hex sha256." % (kind, field)
        )
    return lowered


def _require_status(value, field, kind, allowed):
    _require_str(value, field, kind)
    if value not in allowed:
        raise ContractError(
            "%s field '%s' has unknown status %r; expected one of %s."
            % (kind, field, value, ", ".join(allowed))
        )
    return value


def _declared_schema(data, kind, current):
    """The schema version declared by ``data`` (``schema`` or legacy ``version``)."""
    data = _require_dict(data, kind)
    for key in ("schema", "version"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    # A file that predates schema stamping is the first schema by definition.
    if kind in ("dataset.json", "info.json"):
        return 1
    raise ContractError("%s has no 'schema' field." % kind)


def _gate(data, kind, current):
    """Reject a newer schema; return the declared version for older ones."""
    declared = _declared_schema(data, kind, current)
    if declared > current:
        raise SchemaTooNew(
            "%s declares schema %d, but this peekseed build understands at most "
            "schema %d. Upgrade peekseed to read this file." % (kind, declared, current)
        )
    return declared


# --- content-derived identifiers -------------------------------------------


def chunk_id(source_sha256, block_index, chunk_index, chunker_version=None):
    """The stable id of one chunk, derived from its source content and position.

    ``sha256(source_sha256 | block_index | chunk_index | chunker_version)[:16]``.
    The source hash makes the id independent of every other file in the
    collection: adding file B cannot renumber file A's chunks. ``block_index`` is
    the 0-based index of the chunk's first block within its source document and
    ``chunk_index`` the 0-based index of the chunk within the same source.
    """
    sha = _require_sha256(source_sha256, "source_sha256", "chunk id")
    version = CHUNKER_VERSION if chunker_version is None else int(chunker_version)
    block = _require_int(int(block_index), "block_index", "chunk id")
    index = _require_int(int(chunk_index), "chunk_index", "chunk id")
    material = "%s|%d|%d|%d" % (sha, block, index, version)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_ID_LEN]


def chunker_identity(target_chars=1200, max_chars=2000):
    """The provenance record for the chunker: name, version and size bounds."""
    return {
        "name": CHUNKER_NAME,
        "version": CHUNKER_VERSION,
        "target_chars": int(target_chars),
        "max_chars": int(max_chars),
    }


def chunker_label():
    """A compact ``"headings@1"`` label for logs and manifest summaries."""
    return "%s@%d" % (CHUNKER_NAME, CHUNKER_VERSION)


def extractor_identity(name, version):
    """The provenance record for one extractor backend."""
    return {
        "name": _require_str(name, "name", "extractor identity"),
        "version": _require_str(str(version), "version", "extractor identity"),
    }


def extractor_label(extractor):
    """A compact ``"pypdf@6.21.1"`` label from an extractor identity dict."""
    if isinstance(extractor, (tuple, list)) and len(extractor) == 2:
        name, version = extractor
    elif isinstance(extractor, dict):
        name, version = extractor.get("name"), extractor.get("version")
    else:
        raise ContractError("an extractor identity dict or (name, version) is required")
    if not name:
        raise ContractError("an extractor identity needs a name")
    return "%s@%s" % (name, version or "unknown")


def probe_sha256():
    """The sha256 of the fixed probe text (stored in ``info.json``)."""
    return hashlib.sha256(PROBE_TEXT.encode("utf-8")).hexdigest()


def has_probe(info):
    """True when ``info.json`` carries a usable stored probe vector."""
    embedding = info.get("embedding") if isinstance(info, dict) else None
    probe = embedding.get("probe") if isinstance(embedding, dict) else None
    if not isinstance(probe, dict):
        return False
    return bool(str(probe.get("text_sha256") or "")) and bool(str(probe.get("file") or ""))


# --- provenance digests ----------------------------------------------------


def documents_digest(sources):
    """``sha256`` of the sorted ``(path, sha256)`` list naming an index's inputs.

    This is the value ``info.json`` stores and staleness compares: an index is
    current exactly when the digest of the collection's current sources matches
    the digest it was built from. Path is included as well as content so a
    document that moves between folders is re-indexed too (its citation paths
    changed). Raises :class:`ContractError` when a source has no hash.
    """
    lines = []
    for entry in sources or []:
        if not isinstance(entry, dict):
            raise ContractError("each source entry must be a JSON object")
        path = str(entry.get("path") or entry.get("name") or "").strip()
        sha = str(entry.get("sha256") or "").strip().lower()
        if not path:
            raise ContractError("a source entry has no path or name")
        _require_sha256(sha, "sha256", "source '%s'" % path)
        lines.append("%s\x00%s" % (path, sha))
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def stale_reasons(info, sources, *, chunker=None, extractor=None):
    """Explain why an index no longer matches its inputs (empty list = current).

    Three independent things can make an index stale: the source set changed
    (``documents_digest``), the chunker version changed, or the extractor version
    changed (extraction output feeds chunking, so its version invalidates chunks).
    The returned list of short user-facing strings is what ``peekseed status``
    and ``peekseed index --dry-run`` print.
    """
    if not isinstance(info, dict):
        return ["no index metadata"]
    reasons = []
    try:
        digest = documents_digest(sources)
    except ContractError:
        digest = None
    recorded = str(info.get("documents_digest") or "")
    if not recorded:
        reasons.append("index metadata has no documents_digest")
    elif digest is not None and recorded != digest:
        reasons.append("source documents changed since the index was built")

    recorded_chunker = info.get("chunker") if isinstance(info.get("chunker"), dict) else {}
    if chunker is not None:
        wanted_name = chunker.get("name") if isinstance(chunker, dict) else chunker[0]
        wanted_version = chunker.get("version") if isinstance(chunker, dict) else chunker[1]
        if (recorded_chunker.get("name"), recorded_chunker.get("version")) != (
            wanted_name,
            wanted_version,
        ):
            reasons.append(
                "chunker changed (index: %s@%s, current: %s@%s)"
                % (
                    recorded_chunker.get("name") or "?",
                    recorded_chunker.get("version") or "?",
                    wanted_name,
                    wanted_version,
                )
            )
    recorded_extractor = (
        info.get("extractor") if isinstance(info.get("extractor"), dict) else {}
    )
    if extractor is not None:
        name, version = (
            (extractor.get("name"), extractor.get("version"))
            if isinstance(extractor, dict)
            else (extractor[0], extractor[1])
        )
        if (recorded_extractor.get("name"), recorded_extractor.get("version")) != (
            name,
            str(version or ""),
        ):
            reasons.append(
                "extractor changed (index: %s@%s, current: %s@%s)"
                % (
                    recorded_extractor.get("name") or "?",
                    recorded_extractor.get("version") or "?",
                    name or "?",
                    version or "?",
                )
            )
    return reasons


# --- embedding space identity ----------------------------------------------


def _space_slug(value):
    import re

    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return text.strip("-._")[:48] or "default"


def space_key(link):
    """The ``index/<space_key>`` directory name for one embedding link.

    The rule is ``slug(link_id) + "__" + slug(model)``; it matches
    :func:`peekseed.links.embed_space_key` so ingestion, the API and the link
    store can never disagree about which directory holds which vectors.
    """
    if not isinstance(link, dict):
        raise ContractError("an embedding link dictionary is required")
    return "%s__%s" % (_space_slug(link.get("id")), _space_slug(link.get("model")))


def space_identity(link, dim=None, *, normalized=True):
    """The full identity of one embedding space, recorded in ``info.json``.

    The directory name only combines link id and model. This record adds the
    dimensions, protocol, normalization and the asymmetric prefixes -- all of
    which change the vector space -- so a query can compare the live link with
    the space an index was built in instead of trusting the model name alone.
    """
    if not isinstance(link, dict):
        raise ContractError("an embedding link dictionary is required")
    identity = {
        "link_id": str(link.get("id") or ""),
        "model": str(link.get("model") or ""),
        "protocol": str(link.get("protocol") or "openai"),
        "dim": int(dim) if dim is not None else int(link.get("dim") or 0),
        "normalized": bool(normalized),
        "doc_prefix": str(link.get("doc_prefix") or ""),
        "query_prefix": str(link.get("query_prefix") or ""),
        "space": space_key(link),
    }
    return identity


def file_status(record):
    """The status of one manifest/dataset source entry (``new`` when absent)."""
    if not isinstance(record, dict):
        return "new"
    value = str(record.get("status") or "")
    return value if value in FILE_STATUSES else "new"


# --- constructors ----------------------------------------------------------


def new_manifest(created_at=None):
    """An empty ``manifest.json`` at the current schema."""
    now = created_at or utc_now()
    return {
        "schema": MANIFEST_SCHEMA,
        "created_at": now,
        "last_scan": None,
        "extractor": "",
        "chunker": chunker_label(),
        "documents": [],
    }


def new_job(kind, *, collection=None, job_id=None, created_at=None):
    """A new persisted job record (``.peekseed/jobs/<id>.json``)."""
    if kind not in JOB_KINDS:
        raise ContractError(
            "unknown job kind %r; expected one of %s" % (kind, ", ".join(JOB_KINDS))
        )
    now = created_at or utc_now()
    identifier = job_id or ("job-" + os.urandom(6).hex())
    return {
        "schema": JOB_SCHEMA,
        "id": identifier,
        "kind": kind,
        "collection": str(collection) if collection else None,
        "state": "queued",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "progress": {"phase": "", "done": 0, "total": 0, "fraction": 0.0},
        "error": None,
        "cancel_requested": False,
    }


# --- validation ------------------------------------------------------------


def _validate_source_entry(entry, kind):
    """Validate one source entry shared by ``dataset.json`` and ``info.json``."""
    entry = _require_dict(entry, "%s source entry" % kind)
    name = _require_str(entry.get("name"), "name", kind)
    sha = _require_sha256(
        entry.get("sha256") or "",
        "sha256",
        kind,
        allow_empty=file_status(entry) in ("new", "skipped", "error"),
    )
    data = {"name": name, "sha256": sha}
    for field in ("bytes", "pages"):
        if entry.get(field) is not None:
            data[field] = _require_int(entry[field], field, kind)
    path = entry.get("path")
    if path is not None:
        data["path"] = _require_str(path, "path", kind, allow_empty=True)
    extractor = entry.get("extractor")
    if extractor is not None:
        data["extractor"] = _require_str(extractor, "extractor", kind, allow_empty=True)
    if kind == "dataset.json":
        status = entry.get("status")
        if status is not None:
            data["status"] = _require_status(status, "status", kind, FILE_STATUSES)
    return data


def validate_manifest(data):
    """Validate a ``manifest.json`` document; returns a normalised copy."""
    _gate(data, "manifest.json", MANIFEST_SCHEMA)
    created_at = _require_str(data.get("created_at"), "created_at", "manifest.json")
    last_scan = data.get("last_scan")
    if last_scan is not None:
        _require_str(last_scan, "last_scan", "manifest.json")
    documents = data.get("documents")
    if not isinstance(documents, list):
        raise ContractError("manifest.json field 'documents' must be a list.")
    entries = []
    seen_paths = set()
    for raw in documents:
        raw = _require_dict(raw, "manifest.json document entry")
        path = _require_str(raw.get("path"), "path", "manifest.json")
        collection = _require_str(raw.get("collection"), "collection", "manifest.json")
        sha = _require_sha256(
            raw.get("sha256") or "",
            "sha256",
            "manifest.json",
            allow_empty=file_status(raw) in ("new", "skipped", "error"),
        )
        entry = {
            "path": path,
            "collection": collection,
            "sha256": sha,
            "bytes": _require_int(raw.get("bytes"), "bytes", "manifest.json"),
            "pages": _require_int(raw.get("pages"), "pages", "manifest.json"),
            "status": _require_status(
                raw.get("status"), "status", "manifest.json", FILE_STATUSES
            ),
            "seen_at": _require_str(raw.get("seen_at"), "seen_at", "manifest.json"),
        }
        if path in seen_paths:
            raise ContractError(
                "manifest.json lists '%s' more than once." % path
            )
        seen_paths.add(path)
        for field in ("mtime", "error", "extractor"):
            if raw.get(field) is not None:
                entry[field] = raw[field]
        if entry.get("error") is not None:
            _require_str(entry["error"], "error", "manifest.json", allow_empty=True)
        entries.append(entry)
    normalised = {
        "schema": MANIFEST_SCHEMA,
        "created_at": created_at,
        "last_scan": last_scan,
        "extractor": str(data.get("extractor") or ""),
        "chunker": str(data.get("chunker") or ""),
        "documents": entries,
    }
    return normalised


def validate_dataset(data):
    """Validate a ``dataset.json`` document; returns a normalised copy."""
    _gate(data, "dataset.json", DATASET_SCHEMA)
    identifier = _require_str(data.get("id"), "id", "dataset.json")
    name = _require_str(data.get("name"), "name", "dataset.json")
    created_at = _require_str(data.get("created_at"), "created_at", "dataset.json")
    sources_raw = data.get("sources")
    if not isinstance(sources_raw, list):
        raise ContractError("dataset.json field 'sources' must be a list.")
    sources = [_validate_source_entry(entry, "dataset.json") for entry in sources_raw]
    normalised = {
        "schema": DATASET_SCHEMA,
        "id": identifier,
        "name": name,
        "created_at": created_at,
        "notes": str(data.get("notes") or ""),
        "sources": sources,
    }
    for field in ("updated_at", "extractor", "chunker"):
        if data.get(field) is not None:
            normalised[field] = str(data[field])
    status = data.get("status")
    if status is not None:
        normalised["status"] = _require_status(
            status, "status", "dataset.json", COLLECTION_STATUSES
        )
    return normalised


def validate_info(data):
    """Validate an ``info.json`` document; returns a normalised copy."""
    _gate(data, "info.json", INFO_SCHEMA)
    identifier = _require_str(data.get("dataset"), "dataset", "info.json")
    space = _require_str(data.get("space"), "space", "info.json")
    n = _require_int(data.get("n"), "n", "info.json")

    embedding = _require_dict(data.get("embedding"), "info.json field 'embedding'")
    probe = _require_dict(embedding.get("probe"), "info.json field 'embedding.probe'")
    embedding_out = {
        "link_id": _require_str(
            embedding.get("link_id"), "link_id", "info.json embedding", allow_empty=True
        ),
        "model": _require_str(
            embedding.get("model"), "model", "info.json embedding", allow_empty=True
        ),
        "protocol": _require_str(embedding.get("protocol"), "protocol", "info.json embedding"),
        "dim": _require_int(embedding.get("dim"), "dim", "info.json embedding"),
        "normalized": _require_bool(embedding.get("normalized"), "normalized", "info.json embedding"),
        "doc_prefix": str(embedding.get("doc_prefix") or ""),
        "query_prefix": str(embedding.get("query_prefix") or ""),
        "probe": {
            "text_sha256": _require_sha256(
                probe.get("text_sha256") or "",
                "probe.text_sha256",
                "info.json",
                allow_empty=True,
            ),
            "file": _require_str(
                probe.get("file") or "", "probe.file", "info.json", allow_empty=True
            ),
        },
    }

    chunker = _require_dict(data.get("chunker"), "info.json field 'chunker'")
    chunker_out = {
        "name": _require_str(chunker.get("name"), "name", "info.json chunker"),
        "version": _require_int(chunker.get("version"), "version", "info.json chunker", minimum=1),
    }
    for field in ("target_chars", "max_chars"):
        if chunker.get(field) is not None:
            chunker_out[field] = _require_int(chunker[field], field, "info.json chunker", minimum=1)

    extractor = _require_dict(data.get("extractor"), "info.json field 'extractor'")
    extractor_out = {
        "name": _require_str(
            extractor.get("name"), "name", "info.json extractor", allow_empty=True
        ),
        "version": _require_str(
            extractor.get("version"), "version", "info.json extractor", allow_empty=True
        ),
    }

    sources_raw = data.get("sources")
    if not isinstance(sources_raw, list):
        raise ContractError("info.json field 'sources' must be a list.")
    sources = [_validate_source_entry(entry, "info.json") for entry in sources_raw]

    digest = data.get("documents_digest")
    digest_out = _require_sha256(
        digest or "", "documents_digest", "info.json", allow_empty=True
    )

    normalised = {
        "schema": INFO_SCHEMA,
        "dataset": identifier,
        "space": space,
        "generation": _require_str(
            data.get("generation") or "", "generation", "info.json", allow_empty=True
        ),
        "n": n,
        "dim": _require_int(data.get("dim"), "dim", "info.json"),
        "embedding": embedding_out,
        "chunker": chunker_out,
        "extractor": extractor_out,
        "documents_digest": digest_out,
        "sources": sources,
    }
    # Convenience mirrors kept for API/UI compatibility; validated when present.
    for field in ("link_id", "model", "protocol", "protocol_version"):
        if data.get(field) is not None:
            normalised[field] = str(data[field])
    if normalised.get("link_id") and normalised["link_id"] != embedding_out["link_id"]:
        raise ContractError("info.json link_id disagrees with embedding.link_id")
    if normalised.get("model") and normalised["model"] != embedding_out["model"]:
        raise ContractError("info.json model disagrees with embedding.model")
    if normalised.get("dim") is not None and normalised["dim"] != embedding_out["dim"]:
        raise ContractError("info.json dim disagrees with embedding.dim")
    if data.get("created_at") is not None:
        normalised["created_at"] = str(data["created_at"])
    return normalised


def validate_job(data):
    """Validate a persisted job record; returns a normalised copy."""
    _gate(data, "job record", JOB_SCHEMA)
    identifier = _require_str(data.get("id"), "id", "job record")
    kind = _require_str(data.get("kind"), "kind", "job record")
    if kind not in JOB_KINDS:
        raise ContractError("job record kind %r is not one of %s" % (kind, ", ".join(JOB_KINDS)))
    state = _require_status(data.get("state"), "state", "job record", JOB_STATES)
    progress = _require_dict(data.get("progress"), "job record field 'progress'")
    normalised = {
        "schema": JOB_SCHEMA,
        "id": identifier,
        "kind": kind,
        "collection": (
            _require_str(data.get("collection"), "collection", "job record")
            if data.get("collection")
            else None
        ),
        "state": state,
        "created_at": _require_str(data.get("created_at"), "created_at", "job record"),
        "updated_at": _require_str(data.get("updated_at"), "updated_at", "job record"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "progress": {
            "phase": str(progress.get("phase") or ""),
            "done": _require_int(progress.get("done"), "progress.done", "job record"),
            "total": _require_int(progress.get("total"), "progress.total", "job record"),
            "fraction": float(progress.get("fraction") or 0.0),
        },
        "error": data.get("error"),
        "cancel_requested": bool(data.get("cancel_requested")),
    }
    if normalised["error"] is not None:
        normalised["error"] = _require_str(
            normalised["error"], "error", "job record", allow_empty=True
        )
    for field in ("generation", "space", "checkpoint"):
        if data.get(field) is not None:
            normalised[field] = data[field]
    return normalised


def validate_chunk(record, *, derive_id=False):
    """Validate one ``chunks.jsonl`` row; returns a normalised copy.

    ``derive_id=True`` additionally recomputes the content-derived id and
    refuses a row whose stored id does not match, which is what the ingestion
    path does. Imported corpora may keep their historical ids, so the default is
    to validate the present fields without re-deriving.
    """
    record = _require_dict(record, "chunk row")
    identifier = _require_str(record.get("id"), "id", "chunk row")
    normalised = {
        "id": identifier,
        "dataset": _require_str(
            record.get("dataset"), "dataset", "chunk row", allow_empty=True
        ),
        "source": _require_str(record.get("source"), "source", "chunk row"),
        "source_sha256": _require_sha256(record.get("source_sha256"), "source_sha256", "chunk row"),
        "page_start": _require_int(record.get("page_start"), "page_start", "chunk row", minimum=1),
        "page_end": _require_int(record.get("page_end"), "page_end", "chunk row", minimum=1),
    }
    if normalised["page_end"] < normalised["page_start"]:
        raise ContractError("chunk row page_end precedes page_start")
    for field in ("section", "chapter", "numbering", "parent", "path"):
        value = record.get(field)
        normalised[field] = "" if value is None else _require_str(
            str(value), field, "chunk row", allow_empty=True
        )
    normalised["doc"] = _require_str(record.get("doc"), "doc", "chunk row")
    normalised["text"] = _require_str(record.get("text"), "text", "chunk row")
    normalised["heading_only"] = _require_bool(
        record.get("heading_only"), "heading_only", "chunk row"
    )
    normalised["source_sha256"] = normalised["source_sha256"].lower()
    block_index = _require_int(record.get("block_index", 0), "block_index", "chunk row")
    chunk_index = _require_int(record.get("chunk_index", 0), "chunk_index", "chunk row")
    chunker_version = _require_int(
        record.get("chunker_version", CHUNKER_VERSION), "chunker_version", "chunk row", minimum=1
    )
    normalised["block_index"] = block_index
    normalised["chunk_index"] = chunk_index
    normalised["chunker_version"] = chunker_version
    if record.get("ordinal") is not None:
        normalised["ordinal"] = _require_int(record["ordinal"], "ordinal", "chunk row", minimum=1)
    if record.get("pdf_y") is not None:
        normalised["pdf_y"] = _require_int(record["pdf_y"], "pdf_y", "chunk row")
    if record.get("block_kind") is not None:
        normalised["block_kind"] = _require_str(record["block_kind"], "block_kind", "chunk row")
    if derive_id:
        expected = chunk_id(
            normalised["source_sha256"], block_index, chunk_index, chunker_version
        )
        if identifier != expected:
            raise ContractError(
                "chunk id %r does not match its content-derived id %r"
                % (identifier, expected)
            )
        normalised["id"] = expected
    return normalised


# --- version upgrades ------------------------------------------------------


def upgrade_dataset(data, *, sha256_of=None):
    """Upgrade a legacy ``dataset.json`` (schema 1 / ``version: 1``) to schema 2.

    ``sha256_of`` is a callable taking the dataset-relative source path (for
    example ``"original/CXL.pdf"``) and returning the content hash; when it
    returns ``None`` (a file is gone) the source is marked ``new`` with an empty
    hash so a later scan can re-discover it. The upgrade never guesses a hash.
    """
    declared = _declared_schema(data, "dataset.json", DATASET_SCHEMA)
    if declared == DATASET_SCHEMA:
        return validate_dataset(data)
    if declared > DATASET_SCHEMA:
        _gate(data, "dataset.json", DATASET_SCHEMA)
    raw_sources = data.get("sources") or []
    sources = []
    for raw in raw_sources:
        if isinstance(raw, str):
            raw = {"name": raw}
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")
        if not name:
            continue
        relpath = str(raw.get("path") or ("original/" + name))
        sha = str(raw.get("sha256") or "").lower()
        if not sha and sha256_of is not None:
            try:
                sha = str(sha256_of(relpath) or "").lower()
            except OSError:
                sha = ""
        entry = {
            "name": name,
            "path": relpath,
            "sha256": sha,
            "bytes": int(raw.get("bytes") or 0),
            "pages": int(raw.get("pages") or 0),
            "extractor": str(raw.get("extractor") or ""),
            "status": "indexed" if sha else "new",
        }
        sources.append(entry)
    upgraded = {
        "schema": DATASET_SCHEMA,
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or data.get("id") or ""),
        "created_at": str(data.get("created_at") or utc_now()),
        "updated_at": utc_now(),
        "notes": str(data.get("notes") or ""),
        "sources": sources,
    }
    if not upgraded["id"] or not upgraded["name"]:
        raise ContractError("dataset.json has no id or name to upgrade")
    return validate_dataset(upgraded)


def upgrade_info(data):
    """Upgrade a legacy ``info.json`` (schema 1) to schema 2.

    A schema-1 index predates the probe and the provenance records, so the
    upgraded record carries an empty probe (``has_probe`` is False) and empty
    extractor. The index stays readable; ``peekseed migrate`` embeds the probe
    once and writes ``probe.npy`` to make it gateable.
    """
    declared = _declared_schema(data, "info.json", INFO_SCHEMA)
    if declared == INFO_SCHEMA:
        return validate_info(data)
    if declared > INFO_SCHEMA:
        _gate(data, "info.json", INFO_SCHEMA)
    link = {
        "id": data.get("link_id") or "",
        "model": data.get("model") or "",
        "protocol": data.get("protocol") or "openai",
    }
    embedding = space_identity(link, dim=int(data.get("dim") or 0))
    embedding["probe"] = {"text_sha256": "", "file": ""}
    chunker_raw = data.get("chunker") if isinstance(data.get("chunker"), dict) else {}
    chunker = chunker_identity(
        target_chars=int(chunker_raw.get("target_chars") or 1200),
        max_chars=int(chunker_raw.get("max_chars") or 2000),
    )
    sources = []
    for raw in data.get("sources") or []:
        if not isinstance(raw, dict):
            continue
        sources.append(
            {
                "name": str(raw.get("name") or ""),
                "sha256": str(raw.get("sha256") or ""),
                "bytes": int(raw.get("bytes") or 0),
                "pages": int(raw.get("pages") or 0),
            }
        )
    try:
        digest = documents_digest(sources)
    except ContractError:
        digest = ""
    upgraded = {
        "schema": INFO_SCHEMA,
        "dataset": str(data.get("dataset") or ""),
        "space": str(data.get("space") or space_key(link)),
        "generation": "",
        "n": int(data.get("n") or 0),
        "dim": int(data.get("dim") or 0),
        "link_id": link["id"],
        "model": link["model"],
        "protocol": link["protocol"],
        "created_at": str(data.get("created_at") or utc_now()),
        "embedding": embedding,
        "chunker": chunker,
        "extractor": {"name": "", "version": ""},
        "documents_digest": digest,
        "sources": sources,
    }
    return validate_info(upgraded)


def ensure_current(data, kind):
    """Read-side gate: return ``data`` validated at the current schema.

    Refuses a newer schema with :class:`SchemaTooNew`; upgrades older documents
    in memory (never on disk -- use :func:`upgrade_dataset`/:func:`upgrade_info`
    and write the result for a real migration).
    """
    if kind == "manifest.json":
        return validate_manifest(data)
    if kind == "dataset.json":
        return upgrade_dataset(data)
    if kind == "info.json":
        return upgrade_info(data)
    if kind == "job record":
        return validate_job(data)
    raise ContractError("unknown contract kind %r" % kind)


# --- atomic JSON I/O -------------------------------------------------------


def read_json(path, default=None):
    """Read a JSON file, returning ``default`` when it does not exist.

    A malformed file raises :class:`ContractError` naming the file: silently
    treating a corrupt manifest as empty would hide every indexed document.
    """
    path = Path(path)
    if not path.is_file():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise ContractError("'%s' is not valid JSON (%s)." % (path.name, exc)) from None
    except OSError as exc:
        raise ContractError("could not read '%s': %s." % (path.name, exc)) from None


def write_json_atomic(path, data, mode=0o644):
    """Write JSON atomically (same-dir temp file, fsync, ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
