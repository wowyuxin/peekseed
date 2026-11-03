"""peekseed's data folders: the home, the shared sources and the peels.

Two required settings name the writable locations (see :mod:`peekseed.config`):

``PEEKSEED_HOME``
    Private state: ``config/`` (links, settings), ``datasets/`` (dataset.json,
    chunks, indexes), ``history/``, ``jobs/`` and ``uploads/`` staging.

``PEEKSEED_SHARED_DIR``
    The folder shared with peelfeed: ``sources/<sha256>/<name>.pdf`` (the one
    stored copy of each uploaded PDF) and ``peels/<peel_id>/`` (peelfeed output,
    read-only for peekseed).

This module owns ``init``/``status``/``doctor`` over
:class:`peekseed.config.Paths`. It never resolves paths from the current
directory and never writes a gitignore guard: the two folders are given by the
deployment (``deploy/env/peekseed.env``).
"""

import os
from pathlib import Path

from . import config, contracts

__all__ = [
    "dataset_dirs",
    "doctor",
    "index_info_files",
    "init",
    "status",
    "status_for",
]


# --- init ------------------------------------------------------------------


def _ensure_dir(path, mode):
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass
    return path


def _write_empty_links(links_file):
    """Create an explicit, empty, 0600 link store (never overwrite one)."""
    if links_file.exists():
        return False
    _ensure_dir(links_file.parent, 0o700)
    contracts.write_json_atomic(links_file, {"version": 1, "links": []},
                                mode=0o600)
    return True


def init(p=None):
    """Create (or repair) the home and shared tree. Idempotent.

    Existing links and datasets are never overwritten, and the private folders
    are chmod-ed 0700. Returns ``{"home", "shared_dir", "created"}``.
    """
    p = p or config.paths()
    created = []
    for directory in (p.config_dir, p.datasets_dir, p.history_dir, p.jobs_dir,
                      p.uploads_dir, p.sources_dir, p.peels_dir):
        if not directory.exists():
            created.append(str(directory))
    config.ensure()
    for directory in (p.root, p.shared_dir, p.config_dir, p.datasets_dir,
                      p.history_dir, p.jobs_dir, p.uploads_dir,
                      p.sources_dir, p.peels_dir):
        _ensure_dir(directory, 0o700)
    if _write_empty_links(p.links_file):
        created.append(str(p.links_file))
    print("home: %s" % p.root)
    for label, value in (("shared", p.shared_dir),
                         ("sources", p.sources_dir),
                         ("peels", p.peels_dir),
                         ("links", p.links_file)):
        print("  %-10s %s" % (label + ":", value))
    return {"home": str(p.root), "shared_dir": str(p.shared_dir),
            "created": created}


# --- doctor ----------------------------------------------------------------


def _declared_schema(data, default):
    """The schema a stored JSON file declares (``schema`` or legacy ``version``)."""
    if not isinstance(data, dict):
        return default
    for key in ("schema", "version"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return default


def _read_json(path):
    """``(data, error)``: a JSON file or a short user-facing reason it failed."""
    if not path.is_file():
        return None, None
    try:
        return contracts.read_json(path), None
    except contracts.ContractError as exc:
        return None, str(exc)


def _links_report(links_file):
    data, error = _read_json(links_file)
    report = {
        "path": links_file.name,
        "present": links_file.is_file(),
        "ok": False,
        "count": 0,
        "error": None,
    }
    if error:
        report["error"] = error
        return report
    if data is None:
        return report
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        report["error"] = "%s is not a peekseed link store." % links_file.name
        return report
    report["ok"] = True
    report["count"] = len(data["links"])
    return report


def _dataset_dirs(p):
    if not p.datasets_dir.is_dir():
        return []
    return sorted(
        entry
        for entry in p.datasets_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def dataset_dirs(p):
    """The dataset directories of a home, sorted by name (read-only)."""
    return _dataset_dirs(p)


def _index_dirs(dataset_dir):
    index_root = Path(dataset_dir) / "index"
    if not index_root.is_dir():
        return []
    return sorted(
        entry
        for entry in index_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def index_info_files(dataset_dir):
    """``[(space_name, info_path)]`` for every readable index in a dataset.

    Two layouts exist: the historical flat ``index/<space>/info.json`` and the
    contract layout ``index/<space>/CURRENT`` plus
    ``generations/<uuid>/info.json``. Both are accepted so a workspace can be
    upgraded without losing the old index; ``CURRENT`` is preferred and a
    generation is only consulted when ``CURRENT`` is missing.
    """
    dataset_dir = Path(dataset_dir)
    found = []
    for space_dir in _index_dirs(dataset_dir):
        flat = space_dir / "info.json"
        if flat.is_file():
            found.append((space_dir.name, flat))
            continue
        candidate = None
        current = space_dir / "CURRENT"
        if current.is_file():
            try:
                generation = current.read_text(encoding="utf-8").strip().splitlines()[0]
            except (OSError, IndexError):
                generation = ""
            if generation:
                candidate = space_dir / "generations" / generation.strip() / "info.json"
        if candidate is None or not candidate.is_file():
            generations = space_dir / "generations"
            candidate = None
            if generations.is_dir():
                for entry in sorted(generations.iterdir(), reverse=True):
                    if entry.is_dir() and (entry / "info.json").is_file():
                        candidate = entry / "info.json"
                        break
        if candidate is not None and candidate.is_file():
            found.append((space_dir.name, candidate))
    return sorted(found, key=lambda item: item[0])


def _schemas_report(p):
    """Schema versions (and upgrade needs) of dataset and info files."""
    report = {"datasets": [], "indexes": [], "issues": []}
    for dataset_dir in _dataset_dirs(p):
        data, error = _read_json(dataset_dir / "dataset.json")
        name = dataset_dir.name
        if error:
            report["issues"].append("dataset '%s' has an unreadable dataset.json: %s"
                                    % (name, error))
            continue
        if data is None:
            report["issues"].append("dataset '%s' has no dataset.json." % name)
            continue
        schema = _declared_schema(data, contracts.DATASET_SCHEMA)
        entry = {"id": name, "schema": schema,
                 "ok": schema == contracts.DATASET_SCHEMA}
        report["datasets"].append(entry)
        if schema > contracts.DATASET_SCHEMA:
            report["issues"].append(
                "dataset '%s' declares schema %d; upgrade peekseed." % (name, schema)
            )
        elif schema < contracts.DATASET_SCHEMA:
            report["issues"].append(
                "dataset '%s' uses schema %d; run `peekseed migrate`." % (name, schema)
            )
        else:
            try:
                contracts.validate_dataset(data)
            except contracts.ContractError as exc:
                report["issues"].append(
                    "dataset '%s' does not validate: %s" % (name, exc)
                )
        for space, info_path in index_info_files(dataset_dir):
            info_data, info_error = _read_json(info_path)
            if info_error:
                report["issues"].append(
                    "index '%s' in dataset '%s' has an unreadable info.json: %s"
                    % (space, name, info_error)
                )
                continue
            if info_data is None:
                report["issues"].append(
                    "index '%s' in dataset '%s' has no info.json; run `peekseed "
                    "reindex`." % (space, name)
                )
                continue
            schema = _declared_schema(info_data, contracts.INFO_SCHEMA)
            probe = False
            try:
                normalised = contracts.ensure_current(info_data, "info.json")
                probe = contracts.has_probe(normalised)
            except contracts.ContractError as exc:
                report["issues"].append(
                    "index '%s' in dataset '%s' does not validate: %s"
                    % (space, name, exc)
                )
            entry = {"dataset": name, "space": space, "schema": schema,
                     "probe": probe}
            report["indexes"].append(entry)
            if schema > contracts.INFO_SCHEMA:
                report["issues"].append(
                    "index '%s' in dataset '%s' declares schema %d; upgrade "
                    "peekseed." % (space, name, schema)
                )
            elif schema < contracts.INFO_SCHEMA:
                report["issues"].append(
                    "index '%s' in dataset '%s' uses schema %d; run `peekseed "
                    "migrate`." % (space, name, schema)
                )
            elif not probe:
                report["issues"].append(
                    "index '%s' in dataset '%s' has no embedding probe; run "
                    "`peekseed migrate`." % (space, name)
                )
    return report


def _not_private(path):
    """True when a directory is group- or other-writable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return True
    return bool(mode & 0o022)


def doctor(p=None):
    """Inspect the home and shared folders and report actionable problems.

    Returns ``{"paths", "issues", "links", "schemas", "peelfeed"}``: ``issues``
    are short user-facing strings, each naming the command that fixes it.
    Read-only: doctor never writes.
    """
    p = p or config.paths()
    issues = []
    for label, directory in (("home", p.root), ("shared", p.shared_dir),
                             ("sources", p.sources_dir), ("peels", p.peels_dir)):
        if not directory.is_dir():
            issues.append("%s folder is missing; run `peekseed init`." % label)
        elif _not_private(directory):
            issues.append("%s folder is group- or other-writable; chmod 700 it."
                          % label)
    links = _links_report(p.links_file)
    if links.get("error"):
        issues.append("model links: %s" % links["error"])
    elif not links.get("present"):
        issues.append("the model link store is missing; run `peekseed init`.")
    schemas = _schemas_report(p)
    issues.extend(schemas.pop("issues"))
    return {
        "paths": p.as_dict(),
        "issues": issues,
        "links": links,
        "schemas": schemas,
        "peelfeed": _peelfeed_report(p, probe=True),
    }


def _peelfeed_report(p, probe=False):
    """The peelfeed config of this home and, optionally, CLI reachability."""
    try:
        from . import peelfeed
    except Exception:  # pragma: no cover - optional module
        return {"configured": False, "error": "peelfeed module is unavailable"}
    try:
        return peelfeed.status(probe=probe, timeout=2.0, root=p.root,
                               seed=False)
    except Exception as exc:  # noqa: BLE001 - doctor must not crash
        return {"configured": False,
                "error": "peelfeed config is not readable (%s)"
                         % type(exc).__name__}


# --- status ----------------------------------------------------------------


def _count_lines(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _dataset_sources(dataset):
    """Source entries of a dataset.json (legacy or current shape)."""
    sources = []
    for raw in dataset.get("sources") or []:
        if isinstance(raw, dict):
            sources.append(raw)
        elif str(raw or "").strip():
            sources.append({"name": str(raw).strip()})
    return sources


def _extractor_from_label(label):
    """The extractor identity recorded at dataset level, when it parses."""
    label = str(label or "")
    if "@" in label:
        name, _, version = label.partition("@")
        return contracts.extractor_identity(name, version)
    return None


def _module_index_state(dataset_dir, space, extractor=None):
    """The index module's own loader/gate verdict for one index space.

    ``ingest.index.index_state`` runs the same row/alignment/dim/unique-id
    validation as ``load_index``, so a truncated ``vectors.npy`` is reported as
    ``needs_reindex`` instead of looking healthy from ``info.json`` alone. The
    import is lazy and any failure falls back to the metadata-only reader below,
    so headless/older trees keep working.
    """
    try:
        from .ingest.index import index_state
    except Exception:
        return None
    try:
        state = index_state(
            dataset_dir, space, chunker=contracts.chunker_identity(),
            extractor=extractor,
        )
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _index_state(dataset_dir, dataset):
    """One collection's index state: status + reasons.

    The index module's validation is authoritative when available; otherwise
    this falls back to a metadata-only check (schema/staleness/readability).
    """
    sources = [
        {"path": str(entry.get("path") or entry.get("name") or ""),
         "sha256": str(entry.get("sha256") or "")}
        for entry in _dataset_sources(dataset)
        if str(entry.get("sha256") or "")
    ]
    extractor = _extractor_from_label(dataset.get("extractor"))
    indexes = _index_dirs(dataset_dir)
    if not indexes:
        return "not_indexed", ["no index"]
    statuses = []
    reasons = []
    for space, info_path in index_info_files(dataset_dir):
        verified = _module_index_state(dataset_dir, space, extractor)
        if verified is not None:
            status = str(verified.get("status") or "")
            if status in ("needs_reindex", "stale", "not_indexed", "indexed"):
                statuses.append(status)
                reasons.extend(str(reason) for reason in verified.get("reasons") or [])
                continue
        data, error = _read_json(info_path)
        if error or data is None:
            statuses.append("needs_reindex")
            reasons.append("index metadata is unreadable")
            continue
        try:
            info = contracts.ensure_current(data, "info.json")
        except contracts.ContractError:
            statuses.append("needs_reindex")
            reasons.append("index metadata is invalid")
            continue
        stale = contracts.stale_reasons(
            info, sources, chunker=contracts.chunker_identity(), extractor=extractor
        )
        if stale:
            statuses.append("stale")
            reasons.extend(stale)
        else:
            statuses.append("indexed")
    for wanted in ("needs_reindex", "stale", "indexed"):
        if wanted in statuses:
            return wanted, reasons
    return "not_indexed", reasons


def status(p=None):
    """A read-only summary of the home: paths, datasets and peelfeed."""
    return status_for(p or config.paths())


def status_for(p):
    """The same summary for explicit paths.

    The server uses this with the paths its process actually resolved:
    ``GET /api/workspace`` must describe the folders being served, not ones
    re-derived from the server's cwd.
    """
    collections = {}
    for dataset_dir in _dataset_dirs(p):
        data, error = _read_json(dataset_dir / "dataset.json")
        dataset = data if isinstance(data, dict) else {}
        sources = _dataset_sources(dataset)
        chunks = _count_lines(dataset_dir / "chunks.jsonl")
        state, reasons = _index_state(dataset_dir, dataset)
        reported = str(dataset.get("status") or "")
        # The live index verdict (needs_reindex/stale) always wins: a corrupt
        # generation must never hide behind a stale stored status. The
        # dataset's own status only adds what the index cannot know (source
        # errors or deleted files).
        if state == "indexed" and reported in ("error", "missing", "skipped"):
            state = reported
        elif state == "not_indexed" and reported in (
            "chunked", "error", "missing", "skipped"
        ):
            state = reported
        collections[dataset_dir.name] = {
            "name": str(dataset.get("name") or dataset_dir.name),
            "status": state,
            "scan_status": reported if reported in contracts.COLLECTION_STATUSES else None,
            "sources": len(sources),
            "chunks": chunks,
            "indexes": [index_dir.name for index_dir in _index_dirs(dataset_dir)],
            "reasons": reasons,
        }
    return {
        "paths": p.as_dict(),
        "collections": collections,
        "links": _links_report(p.links_file),
        "peelfeed": _peelfeed_report(p, probe=False),
        "counts": {
            "collections": len(collections),
            "chunks": sum(item["chunks"] for item in collections.values()),
        },
    }
