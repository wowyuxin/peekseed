"""Incremental scan of the user's ``documents/`` folder into datasets.

The scan is the pipeline's entry point: it walks the workspace, hashes every
document, diffs the manifest, extracts and chunks only what changed, writes the
canonical ``chunks.jsonl``/``dataset.json`` for each collection and (when a
model link is supplied) builds the index for collections whose inputs no longer
match their vectors. Running it twice over unchanged documents makes **zero**
embedding calls: an unchanged collection's ``documents_digest`` matches its
``info.json`` and the index build is skipped entirely.

Design rules (the data contracts own the schemas; this module owns the policy):

* One corrupt, encrypted or unsupported file never aborts a scan. The file is
  reported ``error``/``skipped`` with a short user-facing reason (basenames
  only) and the healthy part of its collection is still chunked and indexed.
* Content, not path, is identity: a moved or renamed file keeps its document
  and its content-derived chunk ids. A moved file is a path update
  (``unchanged``), never a new document.
* Extraction is cached by ``(sha256, extractor identity)`` under
  ``.peekseed/extracted/<sha256>.json``: a changed parser version is a cache
  miss because the extracted text (and therefore the chunks) may change.
* ``prune=True`` is explicit data loss: missing manifest entries and their
  chunks are removed. Without it a missing file keeps its last known hash and
  chunks so the collection stays searchable until the user decides.
* Writes are atomic JSON/JSONL files. ``documents/`` is never touched.
"""

import json
import os
import re
from collections import Counter
from pathlib import Path

from . import contracts, workspace as workspace_module

__all__ = ["scan"]

DEFAULT_COLLECTION = "default"
DEFAULT_COLLECTION_NAME = "Default"
CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 2000
CACHE_SCHEMA = 1

_SKIP_SUFFIXES = (".part",)
_SKIP_PREFIXES = ("~$",)
_SKIP_NAMES = (".DS_Store",)


# --- user-facing messages --------------------------------------------------


def _clean_message(message, ws=None):
    """A short, machine-path-free reason string for reports.

    Extraction errors are already written for users (basenames only); this is
    the safety net for unexpected ``OSError``/parser messages that embed an
    absolute path, and it caps the length so a stack-trace-like message cannot
    flood the report. Secrets (API keys) never appear in these code paths.
    """
    text = " ".join(str(message or "").split())
    if ws is not None:
        for absolute in (str(ws.documents_dir), str(ws.root), str(ws.derived_dir)):
            text = text.replace(absolute + os.sep, "").replace(absolute, "")
    text = text.strip().strip("'\"")
    if not text:
        text = "unknown error"
    if len(text) > 240:
        text = text[:237].rstrip() + "..."
    return text


def _notify(progress, fraction, message):
    """Call the optional progress reporter; a broken reporter cannot abort a scan."""
    if progress is None:
        return
    value = max(0.0, min(1.0, float(fraction)))
    try:
        progress(value, str(message))
        return
    except TypeError:
        pass
    except Exception:
        return
    try:
        progress(value)
    except Exception:
        pass


# --- small shared helpers --------------------------------------------------


def _slug(value):
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return text.strip("-._")[:64].strip("-._") or "default"


def _declared_schema(data, default):
    if not isinstance(data, dict):
        return default
    for key in ("schema", "version"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return default


def _read_json_or_error(path):
    """``(data, error)``: a JSON file, or a short reason it could not be read."""
    if not Path(path).is_file():
        return None, None
    try:
        return contracts.read_json(path), None
    except contracts.ContractError as exc:
        return None, _clean_message(exc)


def _write_lines_atomic(path, lines):
    """Atomically write JSON Lines (same-directory temp file + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        with open(str(temporary), "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass
        raise


def _is_skipped_name(name):
    if not name or name.startswith("."):
        return True
    if name in _SKIP_NAMES:
        return True
    if name.endswith(_SKIP_SUFFIXES):
        return True
    if name.startswith(_SKIP_PREFIXES):
        return True
    return False


def _collection_of(rel):
    """``(collection id, collection display name)`` for a documents-relative path."""
    parts = rel.split("/")
    if len(parts) < 2:
        return DEFAULT_COLLECTION, DEFAULT_COLLECTION_NAME
    return _slug(parts[0]), parts[0]


def _source_name(rel, collection):
    """The source name stored in ``dataset.json``: path relative to its collection."""
    if collection == DEFAULT_COLLECTION:
        return rel
    parts = rel.split("/")
    if len(parts) < 2:
        return rel
    return "/".join(parts[1:])


# --- walking documents/ ----------------------------------------------------


def _walk_documents(documents_dir):
    """Every non-hidden file under ``documents/`` with symlink escapes flagged.

    Symlinked directories are followed only while their target stays inside
    ``documents/`` (a loop is broken by tracking resolved directory paths);
    anything that resolves outside the workspace is reported as an escape rather
    than read.
    """
    documents_dir = documents_dir.resolve()
    entries = []
    visited = set()
    stack = [(documents_dir, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            items = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except OSError:
            continue
        for item in items:
            name = item.name
            if _is_skipped_name(name):
                continue
            rel = (prefix + "/" + name) if prefix else name
            try:
                if item.is_symlink():
                    target = item.resolve()
                    try:
                        target.relative_to(documents_dir)
                        inside = True
                    except ValueError:
                        inside = False
                    if not inside:
                        entries.append(
                            {"rel": rel, "name": name, "abs": item,
                             "escape": True}
                        )
                        continue
                    if target.is_dir():
                        if target not in visited:
                            visited.add(target)
                            stack.append((target, rel))
                    elif target.is_file():
                        entries.append(
                            {"rel": rel, "name": name, "abs": target}
                        )
                    else:
                        entries.append(
                            {"rel": rel, "name": name, "abs": item}
                        )
                    continue
                if item.is_dir():
                    if item.resolve() not in visited:
                        visited.add(item.resolve())
                        stack.append((item, rel))
                elif item.is_file():
                    entries.append({"rel": rel, "name": name, "abs": item})
                else:
                    entries.append({"rel": rel, "name": name, "abs": item})
            except OSError:
                entries.append(
                    {"rel": rel, "name": name, "abs": item, "escape": False}
                )
    return sorted(entries, key=lambda entry: entry["rel"])


# --- manifest --------------------------------------------------------------


def _load_manifest(ws):
    """``(manifest, error)``: the validated manifest or a rebuilt empty one."""
    data, error = _read_json_or_error(ws.manifest_file)
    if error:
        return contracts.new_manifest(), error
    if not isinstance(data, dict):
        return contracts.new_manifest(), None
    try:
        return contracts.validate_manifest(data), None
    except contracts.ContractError as exc:
        return contracts.new_manifest(), _clean_message(exc)


# --- extraction cache ------------------------------------------------------


def _extract_module():
    """The extraction module, imported lazily so scan works before it is read."""
    from . import extract

    return extract


def _cache_file(ws, sha256, identity):
    extract_module = _extract_module()
    try:
        name = extract_module.cache_key(sha256, identity)
    except Exception:
        name = sha256 + ".json"
    return ws.extracted_dir / name


def _read_cache(cache_file, sha256, identity):
    """The cached extraction for this content+extractor pair, or ``None``."""
    data, error = _read_json_or_error(cache_file)
    if error or not isinstance(data, dict):
        return None
    if str(data.get("sha256") or "").strip().lower() != sha256:
        return None
    stored = data.get("extractor") if isinstance(data.get("extractor"), dict) else {}
    if str(stored.get("name") or "") != str(identity.get("name") or ""):
        return None
    if str(stored.get("version") or "") != str(identity.get("version") or ""):
        return None
    raw_pages = data.get("pages")
    if not isinstance(raw_pages, list):
        return None
    pages = []
    for item in raw_pages:
        if not isinstance(item, dict):
            return None
        try:
            number = int(item.get("page"))
        except (TypeError, ValueError):
            return None
        if number < 1:
            return None
        pages.append({"page": number, "text": str(item.get("text") or "")})
    pages.sort(key=lambda item: item["page"])
    return {
        "pages": pages,
        "extractor": dict(identity),
        "pages_count": len(pages),
        "needs_ocr": bool(data.get("needs_ocr")),
    }


def _write_cache(cache_file, sha256, identity, result):
    payload = {
        "schema": CACHE_SCHEMA,
        "sha256": sha256,
        "extractor": dict(identity),
        "pages": result["pages"],
        "pages_count": int(result.get("pages_count") or len(result["pages"])),
        "needs_ocr": bool(result.get("needs_ocr")),
    }
    contracts.write_json_atomic(cache_file, payload)


def _extract_source(ws, path, sha256):
    """Extract one file, using and filling the content-addressed cache."""
    extract_module = _extract_module()
    identity = extract_module.extractor_identity_for(path)
    cache_file = _cache_file(ws, sha256, identity)
    cached = _read_cache(cache_file, sha256, identity)
    if cached is not None:
        return cached
    result = extract_module.extract(path)
    identity = result.get("extractor") or identity
    result.setdefault("pages", [])
    result["extractor"] = identity
    result.setdefault("pages_count", len(result["pages"]))
    try:
        _write_cache(_cache_file(ws, sha256, identity), sha256, identity, result)
    except OSError:
        # The cache is an optimisation; a read-only derived tree must not fail
        # the scan after a successful extraction.
        pass
    return result


# --- dataset / chunk helpers ----------------------------------------------


def _read_dataset(dataset_dir):
    """``(dataset, declared_schema, error)`` for one collection."""
    raw, error = _read_json_or_error(Path(dataset_dir) / "dataset.json")
    if error:
        return {}, 0, error
    if not isinstance(raw, dict):
        return {}, 0, None
    declared = _declared_schema(raw, contracts.DATASET_SCHEMA)
    try:
        return contracts.ensure_current(raw, "dataset.json"), declared, None
    except contracts.ContractError as exc:
        return {}, declared, _clean_message(exc)


def _dataset_sources(dataset):
    entries = []
    for raw in dataset.get("sources") or []:
        if isinstance(raw, dict):
            entries.append(raw)
        elif str(raw or "").strip():
            entries.append({"name": str(raw).strip()})
    return entries


def _read_chunks(dataset_dir):
    """``(rows, error)``: ``chunks.jsonl`` rows, tolerant of a corrupt file."""
    path = Path(dataset_dir) / "chunks.jsonl"
    if not path.is_file():
        return [], None
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError:
                    return rows, "chunks.jsonl line %d is not valid JSON" % number
                if not isinstance(record, dict):
                    return rows, "chunks.jsonl line %d is not an object" % number
                rows.append(record)
    except OSError as exc:
        return rows, _clean_message(exc)
    return rows, None


def _chunk_rows(chunks):
    """Validate (and re-derive the ids of) chunker output before it is stored."""
    validated = []
    for record in chunks:
        validated.append(contracts.validate_chunk(record, derive_id=True))
    return validated


def _entry_signature(entry):
    return (
        str(entry.get("name") or ""),
        str(entry.get("path") or ""),
        str(entry.get("sha256") or "").lower(),
        int(entry.get("bytes") or 0),
        int(entry.get("pages") or 0),
        str(entry.get("extractor") or ""),
        str(entry.get("status") or ""),
    )


def _sources_equal(left, right):
    return sorted(_entry_signature(entry) for entry in left) == sorted(
        _entry_signature(entry) for entry in right
    )


def _dataset_changed(old_dataset, dataset):
    """True when the new dataset differs from disk (``updated_at`` ignored)."""
    if not old_dataset:
        return True
    for field in ("id", "name", "created_at", "notes", "chunker", "extractor",
                  "status"):
        if str(old_dataset.get(field) or "") != str(dataset.get(field) or ""):
            return True
    return not _sources_equal(
        _dataset_sources(old_dataset), _dataset_sources(dataset)
    )


def _space_index_info(dataset_dir, space):
    for name, info_path in workspace_module.index_info_files(dataset_dir):
        if name == space:
            return info_path
    return None


def _index_covers(info, entries):
    """True when ``info.json`` already describes every healthy source.

    The comparison is done on sha256 (content) rather than paths: an index that
    was built by a different module version may record different path strings,
    but the same content means the vectors are still valid. Extra recorded
    sources are allowed when they are exactly the error/missing sources this scan
    knows about (the builder may or may not declare them).
    """
    healthy = Counter(
        str(entry.get("sha256") or "").lower()
        for entry in entries
        if str(entry.get("sha256") or "")
        and str(entry.get("status") or "") not in ("error", "skipped", "missing")
    )
    other = Counter(
        str(entry.get("sha256") or "").lower()
        for entry in entries
        if str(entry.get("sha256") or "")
        and str(entry.get("status") or "") in ("error", "skipped", "missing")
    )
    recorded = Counter(
        str(entry.get("sha256") or "").lower()
        for entry in info.get("sources") or []
        if isinstance(entry, dict) and str(entry.get("sha256") or "")
    )
    if healthy - recorded:
        return False
    return not ((recorded - healthy) - other)


def _module_index_state(dataset_dir, space):
    """The index module's own state check, when this build provides one."""
    try:
        from .ingest.index import index_state
    except Exception:
        return None
    try:
        state = index_state(dataset_dir, space,
                            chunker=contracts.chunker_identity())
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _index_dirty(dataset_dir, link, entries, content_changed, chunker_changed,
                 extractor_changed):
    """``(dirty, reasons)``: does this collection need a new index for ``link``?"""
    space = contracts.space_key(link)
    info_path = _space_index_info(dataset_dir, space)
    if info_path is None:
        return True, ["no index for this link"]
    raw, error = _read_json_or_error(info_path)
    if error or not isinstance(raw, dict):
        return True, ["index metadata is unreadable"]
    changed = content_changed or chunker_changed or extractor_changed
    state = _module_index_state(dataset_dir, space)
    if state is not None:
        status = str(state.get("status") or "")
        reasons = [str(reason) for reason in state.get("reasons") or []]
        if status == "needs_reindex":
            # A corrupt generation must be reported, not silently rebuilt:
            # `peekseed reindex` (and the report's status) is the repair path.
            # Input changes still trigger a rebuild here, since they would
            # replace the corrupt generation anyway.
            return changed, reasons
        if status in ("stale", "not_indexed"):
            return True, reasons
        return changed, (["inputs changed since the index was built"] if changed
                         else [])
    if _declared_schema(raw, contracts.INFO_SCHEMA) < contracts.INFO_SCHEMA:
        # A schema-1 index predates the provenance records (`peekseed migrate`
        # upgrades it). Rebuilding it on every scan would burn embeddings for no
        # reason, so trust the scan's own change detection instead.
        return changed, []
    try:
        info = contracts.ensure_current(raw, "info.json")
    except contracts.ContractError as exc:
        return True, [_clean_message(exc)]
    return changed or not _index_covers(info, entries), []


def _build_index(dataset_dir, link, progress):
    """Call the index builder through its pinned interface (lazy import)."""
    from .ingest.index import build_index

    def report(fraction, message):
        _notify(progress, fraction, message)

    return build_index(dataset_dir, link, progress=report)


# --- per-collection scan ---------------------------------------------------


def _collection_status(old_status, has_error, has_missing, rows, index_states,
                       built):
    if has_error:
        return "error"
    if built:
        return "indexed"
    if not rows:
        if has_missing:
            return "missing"
        return "not_indexed"
    if has_missing:
        return "missing"
    if "needs_reindex" in index_states:
        return "needs_reindex"
    if "stale" in index_states:
        return "stale"
    if "indexed" in index_states:
        return "indexed"
    return "chunked"


def _process_collection(
    ws,
    collection,
    display_name,
    current_files,
    missing_prior,
    all_shas,
    *,
    prune,
    link,
    progress,
    index_span,
):
    """Scan one collection: reuse/refresh chunks, write dataset.json, maybe index.

    Returns the per-collection report entry plus the pages/statuses the top-level
    report needs. ``index_span`` is ``(base, width)`` used to map index-build
    progress into the overall scan progress.
    """
    report = {
        "status": "chunked",
        "sources": 0,
        "chunks": 0,
        "embed_calls": 0,
        "reasons": [],
    }
    if link is None:
        report["reasons"].append("no embedding link supplied")
    dataset_dir = ws.datasets_dir / collection
    old_dataset, declared_schema, dataset_error = _read_dataset(dataset_dir)
    old_rows, rows_error = _read_chunks(dataset_dir)
    legacy_dataset = 0 < declared_schema < contracts.DATASET_SCHEMA
    old_by_source = {}
    for row in old_rows:
        old_by_source.setdefault(str(row.get("source") or ""), []).append(row)
    old_entries = {
        str(entry.get("name") or ""): entry
        for entry in _dataset_sources(old_dataset)
    }
    old_chunker = str(old_dataset.get("chunker") or "")
    old_extractor = str(old_dataset.get("extractor") or "")
    chunker_changed = bool(old_dataset) and old_chunker != contracts.chunker_label()
    reasons = []
    if dataset_error:
        reasons.append("dataset.json was unreadable and has been rebuilt")
    if rows_error:
        reasons.append("chunks.jsonl was unreadable and has been rebuilt")
    old_status = str(old_dataset.get("status") or "")
    if legacy_dataset:
        reasons.append(
            "dataset.json uses schema %d; run `peekseed migrate`." % declared_schema
        )
    if not current_files and not missing_prior:
        # A dataset this workspace's documents/ does not describe (for example
        # one created by the legacy upload path) is left untouched: a scan must
        # never delete data it did not create.
        index_states, index_reasons = _index_states(dataset_dir)
        report["reasons"].extend(reasons)
        report["reasons"].extend(index_reasons)
        report["status"] = (
            str(old_dataset.get("status") or "")
            if str(old_dataset.get("status") or "") in contracts.COLLECTION_STATUSES
            else _collection_status(old_status, False, False, old_rows,
                                    index_states, False)
        )
        report["sources"] = len(_dataset_sources(old_dataset))
        report["chunks"] = len(old_rows)
        report["reasons"] = list(dict.fromkeys(report["reasons"]))
        return report

    rows = []
    entries = []
    used_names = set()
    seen_ids = set()
    content_changed = bool(dataset_error or rows_error)
    has_error = False
    has_missing = False
    pruned = 0

    def add_rows(source_rows):
        for row in source_rows:
            identifier = str(row.get("id") or "")
            if identifier and identifier in seen_ids:
                # Two files with byte-identical content produce identical
                # content-derived ids; keep the first and drop the duplicate so
                # the index keeps its unique-id invariant.
                continue
            seen_ids.add(identifier)
            rows.append(row)

    for position, current in enumerate(current_files):
        _notify(
            progress,
            0.2 + 0.45 * ((position + 0.5) / max(1, len(current_files))),
            "Reading %s" % current["name"],
        )
        name = current["name"]
        used_names.add(name)
        label = ""
        pages = 0
        if current["status"] == "error":
            has_error = True
            entries.append({
                "name": name,
                "path": current["rel"],
                "sha256": current.get("sha") or "",
                "bytes": current.get("bytes") or 0,
                "pages": 0,
                "extractor": "",
                "status": "error",
            })
            continue
        try:
            identity = _extract_module().extractor_identity_for(current["abs"])
            label = contracts.extractor_label(identity)
        except Exception as exc:
            current["status"] = "skipped"
            current["reason"] = _clean_message(exc, ws)
            entries.append({
                "name": name,
                "path": current["rel"],
                "sha256": current["sha"],
                "bytes": current["bytes"],
                "pages": 0,
                "extractor": "",
                "status": "skipped",
            })
            continue
        existing = old_by_source.get(name) or []
        old_entry = old_entries.get(name) or {}
        old_label = str(old_entry.get("extractor") or "")
        reusable = (
            current["status"] == "unchanged"
            and existing
            and not chunker_changed
            and old_label == label
            and all(
                str(row.get("source_sha256") or "").lower() == current["sha"]
                and int(row.get("chunker_version") or contracts.CHUNKER_VERSION)
                == contracts.CHUNKER_VERSION
                for row in existing
            )
        )
        if reusable:
            pages = int(old_entry.get("pages") or 0)
            add_rows(existing)
        else:
            try:
                extracted = _extract_source(ws, current["abs"], current["sha"])
                pages = int(extracted.get("pages_count") or len(extracted["pages"]))
                if extracted.get("needs_ocr") and not any(
                    str(page.get("text") or "").strip()
                    for page in extracted["pages"]
                ):
                    raise ValueError(
                        "the document has no text layer; scanning needs OCR"
                    )
                produced = _chunk_rows(
                    _chunk_pages(
                        extracted["pages"],
                        collection=collection,
                        source=name,
                        sha256=current["sha"],
                    )
                )
                if len(produced) != len(existing):
                    content_changed = True
                add_rows(produced)
                content_changed = True
            except Exception as exc:
                current["status"] = "error"
                current["reason"] = _clean_message(exc, ws)
                has_error = True
                entries.append({
                    "name": name,
                    "path": current["rel"],
                    "sha256": current["sha"],
                    "bytes": current["bytes"],
                    "pages": 0,
                    "extractor": label,
                    "status": "error",
                })
                continue
        current["pages"] = pages
        entries.append({
            "name": name,
            "path": current["rel"],
            "sha256": current["sha"],
            "bytes": current["bytes"],
            "pages": pages,
            "extractor": label,
            "status": "chunked",
        })

    for prior in missing_prior:
        name = _source_name(prior["path"], collection)
        if name in used_names:
            continue
        used_names.add(name)
        sha = str(prior.get("sha256") or "").strip().lower()
        if sha and sha in all_shas:
            # The content still exists somewhere in the workspace: this is a
            # path update, not data loss, so its old rows are dropped.
            content_changed = True
            continue
        has_missing = True
        if prune:
            pruned += 1
            content_changed = True
            continue
        old_entry = old_entries.get(name) or {}
        entries.append({
            "name": name,
            "path": prior["path"],
            "sha256": sha,
            "bytes": int(old_entry.get("bytes") or prior.get("bytes") or 0),
            "pages": int(old_entry.get("pages") or prior.get("pages") or 0),
            "extractor": str(old_entry.get("extractor") or ""),
            "status": "missing",
        })
        add_rows(old_by_source.get(name) or [])

    extractor_label = _collection_extractor_label(entries)
    extractor_changed = bool(old_dataset) and old_extractor != extractor_label
    if chunker_changed or extractor_changed:
        content_changed = True

    dirty, stale_reasons = _index_dirty(
        dataset_dir, link, entries, content_changed, chunker_changed,
        extractor_changed,
    ) if link is not None else (False, [])
    report["reasons"].extend(stale_reasons)
    report["reasons"].extend(reasons)


    # The dataset on disk is the chunker/index input, so it is written before
    # vectors are built; an unchanged collection writes nothing at all (which is
    # what makes a second scan free).
    built = False
    embed_calls = 0
    rows_changed = rows != old_rows or bool(rows_error)
    if rows_changed:
        _write_lines_atomic(
            dataset_dir / "chunks.jsonl",
            (json.dumps(row, ensure_ascii=False, separators=(",", ":"))
             for row in rows),
        )
    created_at = str(old_dataset.get("created_at") or contracts.utc_now())

    def make_dataset(status_value, sources=None):
        return contracts.validate_dataset({
            "schema": contracts.DATASET_SCHEMA,
            "id": collection,
            "name": display_name,
            "created_at": created_at,
            "updated_at": contracts.utc_now(),
            "notes": str(old_dataset.get("notes") or ""),
            "sources": entries if sources is None else sources,
            "chunker": contracts.chunker_label(),
            "extractor": extractor_label,
            "status": status_value,
        })

    needs_build = link is not None and dirty and bool(rows)
    if needs_build:
        # The builder reads dataset.json/chunks.jsonl from disk, so the
        # provisional dataset has to be visible before the vectors are written.
        # Sources that produced no chunks (error/skipped) are left out of it:
        # an index can only record provenance for sources whose text it
        # actually embedded. The final dataset.json below still lists them.
        build_sources = [
            entry for entry in entries
            if str(entry.get("sha256") or "")
            and str(entry.get("status") or "") not in ("error", "skipped")
        ]
        contracts.write_json_atomic(dataset_dir / "dataset.json",
                                    make_dataset("chunked", build_sources))
    index_states, index_reasons = _index_states(dataset_dir)
    report["reasons"].extend(index_reasons)
    if needs_build:
        base, width = index_span
        try:
            info = _build_index(
                dataset_dir,
                link,
                (lambda fraction, message, base=base, width=width:
                 _notify(progress, base + width * float(fraction), message)),
            )
            built = True
            embed_calls = int(info.get("n") or 0)
            index_states = ["indexed"]
            status = "error" if has_error else "indexed"
            if has_error:
                failures = sum(
                    1 for entry in entries
                    if str(entry.get("status") or "") == "error"
                )
                report["reasons"].append(
                    "%d source file(s) failed extraction; the healthy part is "
                    "indexed" % failures
                )
        except Exception as exc:
            status = "error"
            report["reasons"].append(_clean_message(exc, ws))
            index_states = ["needs_reindex"]
            embed_calls = 0
    elif link is not None and dirty and not rows:
        status = "error" if has_error else (
            "missing" if has_missing else "not_indexed"
        )
    elif has_error:
        status = "error"
    elif link is not None:
        status = "indexed" if ("indexed" in index_states and not dirty) else (
            _collection_status(old_status, has_error, has_missing, rows,
                               index_states, False)
        )
    else:
        status = _collection_status(old_status, has_error, has_missing, rows,
                                    index_states, False)
        if "stale" in index_states:
            status = "stale"

    indexed_entries = built or (
        "indexed" in index_states and not dirty and not has_error
    )
    healthy_status = "indexed" if indexed_entries else "chunked"
    for entry in entries:
        if str(entry.get("status") or "") in ("chunked", "new", "unchanged",
                                              "changed"):
            entry["status"] = healthy_status
    if has_missing:
        report["reasons"].append(
            "missing source(s) kept (run `scan --prune` to remove)"
        )
    if pruned:
        report["reasons"].append(
            "pruned %d missing source(s) (data loss)" % pruned
        )
    final = make_dataset(status)
    if _dataset_changed(old_dataset, final):
        contracts.write_json_atomic(dataset_dir / "dataset.json", final)

    report["status"] = status
    report["sources"] = len(entries)
    report["chunks"] = len(rows)
    report["embed_calls"] = embed_calls
    report["reasons"] = list(dict.fromkeys(report["reasons"]))
    _notify(progress, 0.65, "Collection '%s': %s" % (collection, status))
    return report


def _chunk_pages(pages, *, collection, source, sha256):
    """Chunk one source with content-derived ids (the workspace's normal path)."""
    from .ingest.chunk import chunk_pages

    return chunk_pages(
        pages,
        target_chars=CHUNK_TARGET_CHARS,
        max_chars=CHUNK_MAX_CHARS,
        dataset_id=collection,
        source=source,
        source_sha256=sha256,
    )


def _collection_extractor_label(entries):
    """The extractor label recorded at dataset level (``"name@version"``).

    Missing sources keep the label they were extracted with, so deleting a file
    (before a prune) does not look like an extractor change to a later scan.
    """
    labels = {
        str(entry.get("extractor") or "")
        for entry in entries
        if str(entry.get("extractor") or "")
    }
    if len(labels) == 1:
        return labels.pop()
    if labels:
        return "mixed"
    return ""


def _index_states(dataset_dir):
    """``(states, reasons)`` for the indexes currently on disk (read-only).

    The index module's loader/gate runs the full row/dim/unique-id validation,
    so a truncated ``vectors.npy`` shows up as ``needs_reindex`` instead of a
    healthy-looking ``info.json``. The metadata-only check is kept as a fallback
    for trees where the module cannot answer.
    """
    states = []
    reasons = []
    for space, info_path in workspace_module.index_info_files(dataset_dir):
        verified = _module_index_state(dataset_dir, space)
        if verified is not None:
            status = str(verified.get("status") or "")
            if status in ("needs_reindex", "stale", "not_indexed", "indexed"):
                states.append(status)
                reasons.extend(str(reason) for reason in verified.get("reasons") or [])
                continue
        raw, error = _read_json_or_error(info_path)
        if error or not isinstance(raw, dict):
            states.append("needs_reindex")
            reasons.append("index metadata is unreadable")
            continue
        if _declared_schema(raw, contracts.INFO_SCHEMA) < contracts.INFO_SCHEMA:
            states.append("stale")
            reasons.append("index metadata uses schema 1; run `peekseed migrate`")
            continue
        try:
            info = contracts.ensure_current(raw, "info.json")
        except contracts.ContractError:
            states.append("needs_reindex")
            reasons.append("index metadata is invalid")
            continue
        states.append("indexed" if contracts.has_probe(info) else "stale")
        if not contracts.has_probe(info):
            reasons.append("index has no embedding probe")
    return states, reasons


# --- the scan --------------------------------------------------------------


def scan(workspace=None, *, prune=False, link=None, progress=None):
    """Scan ``documents/`` and bring the workspace's derived state up to date.

    ``workspace`` may be a resolved :class:`peekseed.workspace.Workspace` or a
    path; ``None`` resolves the default. ``link`` is an embed link dict (only
    then are indexes built). ``progress(frac, message)`` is optional and is
    forwarded to the index builder. Returns the report described in
    ``docs/contracts.md``: counts, per-file statuses and per-collection state.
    """
    if isinstance(workspace, workspace_module.Workspace):
        ws = workspace
    else:
        ws = workspace_module.resolve(workspace)
    started_at = contracts.utc_now()
    _notify(progress, 0.0, "Scanning documents")
    if not ws.documents_dir.is_dir():
        raise RuntimeError("documents/ is missing; run `peekseed init` first.")

    manifest, manifest_error = _load_manifest(ws)
    prior = {
        str(entry.get("path") or ""): entry
        for entry in manifest.get("documents") or []
    }
    reasons = []
    if manifest_error:
        reasons.append("manifest.json was unreadable and has been rebuilt: %s"
                       % manifest_error)

    walk = _walk_documents(ws.documents_dir)
    documents = []
    for position, item in enumerate(walk):
        _notify(
            progress,
            0.02 + 0.18 * (float(position) / max(1, len(walk))),
            "Reading %s" % item["name"],
        )
        collection, display = _collection_of(item["rel"])
        record = {
            "rel": item["rel"],
            "name": item["name"],
            "collection": collection,
            "display": display,
            "abs": item["abs"],
            "sha": "",
            "bytes": 0,
            "mtime": 0,
            "status": "",
            "reason": "",
            "pages": 0,
        }
        if item.get("escape"):
            record["status"] = "error"
            record["reason"] = "symlink escapes the workspace"
            documents.append(record)
            continue
        try:
            stat = os.stat(str(item["abs"]))
            record["sha"] = contracts.sha256_file(item["abs"])
            record["bytes"] = int(stat.st_size)
            record["mtime"] = float(stat.st_mtime)
        except OSError:
            record["status"] = "error"
            record["reason"] = "could not read the file"
        documents.append(record)

    # --- manifest diff ----------------------------------------------------
    current_paths = {record["rel"] for record in documents}
    prior_sha_paths = {}
    for path, entry in prior.items():
        sha = str(entry.get("sha256") or "").strip().lower()
        if sha:
            prior_sha_paths.setdefault(sha, []).append(path)
    matched = set()
    for record in documents:
        entry = prior.get(record["rel"])
        if entry is not None and str(entry.get("sha256") or "").lower() == record["sha"]:
            record["status"] = "unchanged"
            matched.add(record["rel"])
    for record in documents:
        if record["status"]:
            continue
        entry = prior.get(record["rel"])
        if entry is not None:
            record["status"] = "changed"
            matched.add(record["rel"])
            continue
        moved_from = None
        for path in prior_sha_paths.get(record["sha"], []):
            if path not in matched and path not in current_paths:
                moved_from = path
                break
        if moved_from:
            record["status"] = "unchanged"
            record["moved_from"] = moved_from
            matched.add(moved_from)
        else:
            record["status"] = "new"
    missing_prior = [
        prior[path] for path in sorted(prior)
        if path not in matched and path not in current_paths
    ]

    all_shas = {record["sha"] for record in documents if record["sha"]}
    collections = {}
    displays = {}
    by_collection = {}
    for record in documents:
        by_collection.setdefault(record["collection"], []).append(record)
        displays.setdefault(record["collection"], record["display"])
    missing_by_collection = {}
    for entry in missing_prior:
        collection = str(entry.get("collection") or "")
        if not collection:
            collection = _collection_of(str(entry.get("path") or ""))[0]
        missing_by_collection.setdefault(collection, []).append(entry)

    collection_ids = sorted(
        set(by_collection) | set(missing_by_collection)
        | {entry.name for entry in workspace_module.dataset_dirs(ws)}
    )
    for position, collection in enumerate(collection_ids):
        display = displays.get(collection)
        if not display:
            old_dataset, _declared, _error = _read_dataset(
                ws.datasets_dir / collection
            )
            display = str(old_dataset.get("name") or collection)
        base = 0.7 + 0.3 * (float(position) / max(1, len(collection_ids)))
        width = 0.3 / max(1, len(collection_ids))
        collections[collection] = _process_collection(
            ws,
            collection,
            display,
            sorted(by_collection.get(collection, []),
                   key=lambda record: record["rel"]),
            missing_by_collection.get(collection, []),
            all_shas,
            prune=prune,
            link=link,
            progress=progress,
            index_span=(base, width),
        )

    # --- report + manifest ------------------------------------------------
    finished_at = contracts.utc_now()
    counts = {"new": 0, "changed": 0, "unchanged": 0, "missing": 0, "errors": 0}
    for record in documents:
        if record["status"] in counts:
            counts[record["status"]] += 1
        elif record["status"] == "error":
            counts["errors"] += 1
    counts["missing"] = len(missing_prior)
    files_report = []
    for record in documents:
        item = {
            "path": record["rel"],
            "collection": record["collection"],
            "sha256": record["sha"],
            "bytes": record["bytes"],
            "pages": int(record.get("pages") or 0),
            "status": record["status"] or "error",
        }
        if record.get("reason"):
            item["reason"] = record["reason"]
        if record.get("moved_from"):
            item["moved_from"] = record["moved_from"]
        files_report.append(item)
    for entry in missing_prior:
        item = {
            "path": str(entry.get("path") or ""),
            "collection": str(entry.get("collection") or ""),
            "sha256": str(entry.get("sha256") or ""),
            "bytes": int(entry.get("bytes") or 0),
            "pages": int(entry.get("pages") or 0),
            "status": "missing",
        }
        if prune:
            item["pruned"] = True
        files_report.append(item)
    files_report.sort(key=lambda item: item["path"])

    # The manifest's extractor label summarises the extractors actually used;
    # "mixed" says the workspace holds more than one format family.
    extractor_labels = set()
    for path in by_collection:
        dataset, _declared, _error = _read_dataset(ws.datasets_dir / path)
        label = str(dataset.get("extractor") or "")
        if label:
            extractor_labels.add(label)
    manifest_label = (
        next(iter(extractor_labels)) if len(extractor_labels) == 1
        else ("mixed" if extractor_labels else "")
    )
    documents_manifest = []
    for record in documents:
        entry = {
            "path": record["rel"],
            "collection": record["collection"],
            "sha256": record["sha"],
            "bytes": record["bytes"],
            "pages": int(record.get("pages") or 0),
            "status": record["status"] or "error",
            "seen_at": finished_at,
            "mtime": record["mtime"],
        }
        if record.get("reason"):
            entry["error"] = record["reason"]
        if record["status"] in ("unchanged", "new", "changed"):
            dataset, _declared, _error = _read_dataset(
                ws.datasets_dir / record["collection"]
            )
            for source in _dataset_sources(dataset):
                if str(source.get("name") or "") == _source_name(
                    record["rel"], record["collection"]
                ):
                    entry["extractor"] = str(source.get("extractor") or "")
                    break
            else:
                entry["extractor"] = ""
        documents_manifest.append(entry)
    if not prune:
        by_path = {entry["path"]: entry for entry in documents_manifest}
        for entry in missing_prior:
            path = str(entry.get("path") or "")
            if path in by_path:
                continue
            documents_manifest.append({
                "path": path,
                "collection": str(entry.get("collection") or ""),
                "sha256": str(entry.get("sha256") or ""),
                "bytes": int(entry.get("bytes") or 0),
                "pages": int(entry.get("pages") or 0),
                "status": "missing",
                "seen_at": str(entry.get("seen_at") or finished_at),
                "mtime": entry.get("mtime"),
            })
    documents_manifest.sort(key=lambda item: item["path"])
    new_manifest = {
        "schema": contracts.MANIFEST_SCHEMA,
        "created_at": str(manifest.get("created_at") or finished_at),
        "last_scan": finished_at,
        "extractor": manifest_label,
        "chunker": contracts.chunker_label(),
        "documents": documents_manifest,
    }
    new_manifest = contracts.validate_manifest(new_manifest)
    contracts.write_json_atomic(ws.manifest_file, new_manifest)

    embed_calls = sum(item["embed_calls"] for item in collections.values())
    _notify(progress, 1.0, "Scan complete")
    return {
        "workspace": ws.as_dict(),
        "workspace_root": str(ws.root),
        "started_at": started_at,
        "finished_at": finished_at,
        "counts": counts,
        "scanned": len(walk),
        "files": files_report,
        "collections": collections,
        "embed_calls": embed_calls,
        "prune": bool(prune),
        "reasons": reasons,
    }
