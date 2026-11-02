"""Incremental scan of the user's ``documents/`` folder into datasets (storage only).

After M-1 the scan no longer extracts or chunks: extraction belongs to the
peelfeed engine. The scan is now the **discovery and reconciliation** step:

* it walks the workspace, hashes every document and diffs ``manifest.json``;
* it creates/updates ``datasets/<collection>/dataset.json`` with one source entry
  per document (name, path, sha256, bytes, ``status="uploaded"`` and
  ``extraction.state="not_extracted"``), preserving the extraction block of a
  source whose content did not change;
* it reports the existing index state read-only (``indexed``/``stale``/
  ``needs_reindex``) so the UI can offer Build index when sources are extracted;
* it never writes ``chunks.jsonl``, never builds an index and never calls an
  embedding link. ``link`` is accepted for CLI/API compatibility but ignored --
  extraction is triggered per file through peelfeed (design §4.5).

Design rules (the data contracts own the schemas; this module owns the policy):

* One corrupt, encrypted or unsupported file never aborts a scan. The file is
  reported ``error``/``skipped`` with a short user-facing reason (basenames only)
  and the healthy part of its collection is still reconciled.
* Content, not path, is identity: a moved or renamed file keeps its document and
  its extraction block. A moved file is a path update (``unchanged``), never a
  new document.
* ``prune=True`` is explicit data loss: missing manifest entries and their
  dataset sources are removed. Without it a missing file keeps its last known
  hash and extraction state so the collection stays searchable until the user
  decides.
* Writes are atomic JSON files. ``documents/`` is never touched.
"""

import os
import re
from pathlib import Path

from . import contracts, workspace as workspace_module

__all__ = ["scan"]

DEFAULT_COLLECTION = "default"
DEFAULT_COLLECTION_NAME = "Default"

_SKIP_SUFFIXES = (".part",)
_SKIP_PREFIXES = ("~$",)
_SKIP_NAMES = (".DS_Store",)

#: File-level states that mean "stored but not extracted yet".
_UPLOADED_STATES = ("uploaded", "not_extracted")


# --- user-facing messages --------------------------------------------------


def _clean_message(message, ws=None):
    """A short, machine-path-free reason string for reports."""
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
    """Every non-hidden file under ``documents/`` with symlink escapes flagged."""
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


# --- dataset / source helpers ---------------------------------------------


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
            entries.append(dict(raw))
        elif str(raw or "").strip():
            entries.append({"name": str(raw).strip()})
    return entries


def _read_chunk_count(dataset_dir):
    """How many canonical chunks the last index build wrote (report only)."""
    path = Path(dataset_dir) / "chunks.jsonl"
    if not path.is_file():
        return 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    count += 1
    except OSError:
        return 0
    return count


def _entry_signature(entry):
    extraction = entry.get("extraction") if isinstance(entry.get("extraction"), dict) else {}
    return (
        str(entry.get("name") or ""),
        str(entry.get("path") or ""),
        str(entry.get("sha256") or "").lower(),
        int(entry.get("bytes") or 0),
        int(entry.get("pages") or 0),
        str(entry.get("extractor") or ""),
        str(entry.get("status") or ""),
        str(extraction.get("state") or ""),
        str(extraction.get("peel_id") or ""),
        str(extraction.get("conversion_id") or ""),
        str(extraction.get("engine") or ""),
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


def _index_states(dataset_dir):
    """``(states, reasons)`` for the indexes currently on disk (read-only)."""
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


def _extractor_label(entries):
    """Dataset-level extractor label (``peelfeed@<engine>``) or ``mixed``."""
    labels = {
        str((entry.get("extraction") or {}).get("engine") or "")
        for entry in entries
        if isinstance(entry.get("extraction"), dict)
        and str((entry.get("extraction") or {}).get("engine") or "")
    }
    if len(labels) == 1:
        return "peelfeed@" + labels.pop()
    if labels:
        return "peelfeed@mixed"
    return ""


# --- per-collection reconciliation ----------------------------------------


def _collection_status(has_error, has_missing, entries, index_states):
    """The collection's state from storage + extraction + index state."""
    if has_error:
        return "error"
    if has_missing:
        return "missing"
    states = [contracts.extraction_state(entry) for entry in entries]
    extracted = any(state == "extracted" for state in states)
    uploaded = any(state in _UPLOADED_STATES for state in states)
    if "needs_reindex" in index_states:
        return "needs_reindex"
    if "stale" in index_states:
        return "stale"
    if "indexed" in index_states and extracted:
        return "indexed"
    if extracted:
        return "extracted"
    if uploaded:
        return "uploaded"
    return "not_indexed"


def _process_collection(
    ws,
    collection,
    display_name,
    current_files,
    missing_prior,
    *,
    prune,
    progress,
):
    """Reconcile one collection's ``dataset.json`` with the scanned files.

    Returns the per-collection report entry. No chunks are written and no index
    is built: extraction is a peelfeed job triggered per source.
    """
    report = {
        "status": "uploaded",
        "sources": 0,
        "chunks": 0,
        "embed_calls": 0,
        "reasons": [],
    }
    dataset_dir = ws.datasets_dir / collection
    old_dataset, declared_schema, dataset_error = _read_dataset(dataset_dir)
    old_entries = {
        str(entry.get("name") or ""): entry
        for entry in _dataset_sources(old_dataset)
    }
    reasons = []
    if dataset_error:
        reasons.append("dataset.json was unreadable and has been rebuilt")
    if 0 < declared_schema < contracts.DATASET_SCHEMA:
        reasons.append(
            "dataset.json uses schema %d; run `peekseed migrate`." % declared_schema
        )

    if not current_files and not missing_prior:
        # A dataset this workspace's documents/ does not describe (for example
        # one created by the upload path) is left untouched: a scan must never
        # delete data it did not create.
        index_states, index_reasons = _index_states(dataset_dir)
        report["reasons"].extend(reasons)
        report["reasons"].extend(index_reasons)
        entries = _dataset_sources(old_dataset)
        report["status"] = (
            str(old_dataset.get("status") or "")
            if str(old_dataset.get("status") or "") in contracts.COLLECTION_STATUSES
            else _collection_status(False, False, entries, index_states)
        )
        report["sources"] = len(entries)
        report["chunks"] = _read_chunk_count(dataset_dir)
        report["reasons"] = list(dict.fromkeys(report["reasons"]))
        return report

    entries = []
    used_names = set()
    has_error = False
    has_missing = False
    pruned = 0
    for position, current in enumerate(current_files):
        _notify(
            progress,
            0.55 + 0.35 * ((position + 0.5) / max(1, len(current_files))),
            "Reading %s" % current["name"],
        )
        name = current["name"]
        used_names.add(name)
        old_entry = old_entries.get(name) or {}
        if current["status"] == "error":
            # A file that could not be read (or a symlink escaping the workspace)
            # is reported in the manifest/report but is not a stored source: the
            # dataset must never claim to own bytes it does not have.
            has_error = True
            continue
        same_content = str(old_entry.get("sha256") or "").lower() == str(
            current.get("sha") or "").lower()
        if not same_content and current.get("sha"):
            # A rename is a path update: reuse the extraction block of the old
            # entry with the same content (content, not name, is identity).
            for candidate in old_entries.values():
                if str(candidate.get("sha256") or "").lower() == current["sha"]:
                    old_entry = candidate
                    same_content = True
                    break
        extraction = old_entry.get("extraction") if isinstance(
            old_entry.get("extraction"), dict) else None
        if same_content and extraction is not None:
            entry = {
                "name": name,
                "path": current["rel"],
                "sha256": current["sha"],
                "bytes": current["bytes"],
                "pages": int(old_entry.get("pages") or 0),
                "extractor": str(old_entry.get("extractor") or ""),
                "status": str(old_entry.get("status") or "uploaded"),
                "extraction": dict(extraction),
            }
            state = contracts.extraction_state(entry)
            if state != "extracted" and entry["status"] == "uploaded":
                entry["extraction"]["state"] = "not_extracted"
        else:
            entry = {
                "name": name,
                "path": current["rel"],
                "sha256": current["sha"],
                "bytes": current["bytes"],
                "pages": 0,
                "extractor": "",
                "status": "uploaded",
                "extraction": contracts.extraction_default(),
            }
        entries.append(entry)

    for prior in missing_prior:
        name = _source_name(prior["path"], collection)
        if name in used_names:
            continue
        used_names.add(name)
        sha = str(prior.get("sha256") or "").strip().lower()
        if prune:
            pruned += 1
            continue
        has_missing = True
        old_entry = old_entries.get(name) or {}
        entries.append({
            "name": name,
            "path": prior["path"],
            "sha256": sha,
            "bytes": int(old_entry.get("bytes") or prior.get("bytes") or 0),
            "pages": int(old_entry.get("pages") or prior.get("pages") or 0),
            "extractor": str(old_entry.get("extractor") or ""),
            "status": "missing",
            "extraction": dict(old_entry.get("extraction") or
                               contracts.extraction_default()),
        })

    extractor_label = _extractor_label(entries)
    created_at = str(old_dataset.get("created_at") or contracts.utc_now())

    def make_dataset(status_value):
        return contracts.validate_dataset({
            "schema": contracts.DATASET_SCHEMA,
            "id": collection,
            "name": display_name,
            "created_at": created_at,
            "updated_at": contracts.utc_now(),
            "notes": str(old_dataset.get("notes") or ""),
            "sources": entries,
            "chunker": contracts.chunker_label(),
            "extractor": extractor_label,
            "status": status_value,
        })

    # The index state is judged against the *new* source set, so the dataset is
    # written once without the index verdict (which may be stale) before the
    # read-only index check, then rewritten with the final status.
    provisional = make_dataset(
        _collection_status(has_error, has_missing, entries, []))
    if _dataset_changed(old_dataset, provisional):
        contracts.write_json_atomic(dataset_dir / "dataset.json", provisional)
    index_states, index_reasons = _index_states(dataset_dir)
    report["reasons"].extend(reasons)
    report["reasons"].extend(index_reasons)
    status = _collection_status(has_error, has_missing, entries, index_states)
    if status == "uploaded":
        report["reasons"].append(
            "no source is extracted yet; run Extract with peelfeed, then build "
            "the index")
    if has_missing:
        report["reasons"].append(
            "missing source(s) kept (run `scan --prune` to remove)")
    if pruned:
        report["reasons"].append(
            "pruned %d missing source(s) (data loss)" % pruned)

    final = make_dataset(status)
    if _dataset_changed(provisional, final):
        contracts.write_json_atomic(dataset_dir / "dataset.json", final)

    report["status"] = status
    report["sources"] = len(entries)
    report["chunks"] = _read_chunk_count(dataset_dir)
    report["embed_calls"] = 0
    report["reasons"] = list(dict.fromkeys(report["reasons"]))
    _notify(progress, 0.92, "Collection '%s': %s" % (collection, status))
    return report


# --- the scan --------------------------------------------------------------


def scan(workspace=None, *, prune=False, link=None, progress=None):
    """Scan ``documents/`` and reconcile datasets/manifest (storage only).

    ``workspace`` may be a resolved :class:`peekseed.workspace.Workspace` or a
    path; ``None`` resolves the default. ``link`` is accepted for compatibility
    with the old scan/CLI shape but ignored: peekseed no longer builds indexes
    from a scan (extraction is a peelfeed job, then ``build_index``). ``progress
    (frac, message)`` is optional. Returns the report described in
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
            0.02 + 0.4 * (float(position) / max(1, len(walk))),
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
    for collection in collection_ids:
        display = displays.get(collection)
        if not display:
            old_dataset, _declared, _error = _read_dataset(
                ws.datasets_dir / collection
            )
            display = str(old_dataset.get("name") or collection)
        collections[collection] = _process_collection(
            ws,
            collection,
            display,
            sorted(by_collection.get(collection, []),
                   key=lambda record: record["rel"]),
            missing_by_collection.get(collection, []),
            prune=prune,
            progress=progress,
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
            "status": "missing",
        }
        if prune:
            item["pruned"] = True
        files_report.append(item)
    files_report.sort(key=lambda item: item["path"])

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
            "pages": 0,
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
            wanted = _source_name(record["rel"], record["collection"])
            for source in _dataset_sources(dataset):
                if str(source.get("name") or "") == wanted:
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
        "embed_calls": 0,
        "prune": bool(prune),
        "reasons": reasons,
    }
