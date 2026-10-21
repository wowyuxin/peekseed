"""The peekseed workspace: one directory a user can back up.

A workspace is a plain folder with two halves:

``documents/``
    User-owned sources. peekseed never writes here; the user drops files in and
    a top-level folder becomes a collection (loose files are ``default``).

``.peekseed/``
    Derived, rebuildable state: the scan manifest, model links, datasets and
    their indexes, the extraction cache, history, job records and upload
    staging. Deleting it loses only rebuildable data -- except model links,
    which are the one derived-tree file worth backing up.

This module owns resolution of that folder. The order is explicit argument,
``PEEKSEED_WORKSPACE``, ``./workspace`` and finally ``~/peekseed`` (only when
the current directory is unwritable, with a warning). ``PEEKSEED_DATA`` keeps
its historical meaning and overrides the derived root alone, so an existing
deployment that points the data root elsewhere keeps working. A legacy
``./data`` root is adopted with a printed migration hint rather than being
duplicated silently.

Resolution never creates anything; :func:`init` does, idempotently, and also
writes the ``/workspace/`` guard into the containing git checkout's
``.gitignore`` so documents and derived data cannot be committed by accident.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import contracts

__all__ = [
    "DOCUMENTS_DIRNAME",
    "DERIVED_DIRNAME",
    "GITIGNORE_ENTRY",
    "Workspace",
    "dataset_dirs",
    "doctor",
    "guard_gitignore",
    "init",
    "move",
    "resolve",
    "status",
]

WORKSPACE_ENV = "PEEKSEED_WORKSPACE"
DATA_ENV = "PEEKSEED_DATA"
WORKSPACE_DIRNAME = "workspace"
DOCUMENTS_DIRNAME = "documents"
DERIVED_DIRNAME = ".peekseed"
GITIGNORE_ENTRY = "/workspace/"

# The .peekseed tree is private (0700): it holds model links with keys, search
# history and derived datasets, none of which belong to other local users.
_DERIVED_MODE = 0o700
_DOCUMENTS_MODE = 0o755
_DERIVED_CHILDREN = ("config", "datasets", "extracted", "history", "jobs",
                     "uploads")

# Resolution warnings are printed once per process and kind: a server resolves
# paths on every request, and repeating "using ~/peekseed" would be noise.
_WARNED = set()


def _absolute(value):
    return Path(value).expanduser().resolve()


def _cwd_writable():
    """True when files can be created in the current working directory."""
    try:
        return os.access(str(Path.cwd()), os.W_OK | os.X_OK)
    except OSError:
        return False


def _warn(key, message):
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message)


def _legacy_data_root():
    """The pre-workspace ``./data`` root, if it looks like one and exists."""
    candidate = Path.cwd() / "data"
    if not candidate.is_dir():
        return None
    if (candidate / "config").is_dir() or (candidate / "datasets").is_dir():
        return candidate.resolve()
    if (candidate / "manifest.json").is_file():
        return candidate.resolve()
    return None


def _derived_override(workspace_root):
    """``PEEKSEED_DATA`` or an adopted legacy ``./data`` root, else ``None``."""
    raw = os.environ.get(DATA_ENV)
    if raw:
        return _absolute(raw)
    # An already-initialised workspace has its own derived tree; a stray ./data
    # next to it is not ours to adopt.
    if (workspace_root / DERIVED_DIRNAME).exists():
        return None
    legacy = _legacy_data_root()
    if legacy is not None:
        _warn(
            "legacy-data",
            "peekseed: adopting the existing legacy data root './data'; "
            "new scans write to %s instead of creating a duplicate tree."
            % (DERIVED_DIRNAME + "/"),
        )
        return legacy
    return None


class Workspace(object):
    """One resolved workspace. All paths are absolute ``pathlib.Path`` values.

    ``root`` is the workspace folder, ``derived_dir`` the hidden tree. The
    class is immutable for a given process; :func:`resolve` builds it and
    :meth:`as_dict` renders it for the CLI/API.
    """

    __slots__ = (
        "root",
        "documents_dir",
        "derived_dir",
        "manifest_file",
        "config_dir",
        "links_file",
        "settings_file",
        "datasets_dir",
        "extracted_dir",
        "history_dir",
        "jobs_dir",
        "uploads_dir",
    )

    def __init__(self, root, derived_root=None):
        self.root = _absolute(root)
        self.documents_dir = self.root / DOCUMENTS_DIRNAME
        self.derived_dir = (
            _absolute(derived_root)
            if derived_root
            else self.root / DERIVED_DIRNAME
        )
        self.manifest_file = self.derived_dir / "manifest.json"
        self.config_dir = self.derived_dir / "config"
        self.links_file = self.config_dir / "links.json"
        self.settings_file = self.config_dir / "settings.json"
        self.datasets_dir = self.derived_dir / "datasets"
        self.extracted_dir = self.derived_dir / "extracted"
        self.history_dir = self.derived_dir / "history"
        self.jobs_dir = self.derived_dir / "jobs"
        self.uploads_dir = self.derived_dir / "uploads"

    def as_dict(self):
        """Every path in this workspace as an absolute string."""
        return {
            "root": str(self.root),
            "documents_dir": str(self.documents_dir),
            "derived_dir": str(self.derived_dir),
            "manifest_file": str(self.manifest_file),
            "config_dir": str(self.config_dir),
            "links_file": str(self.links_file),
            "settings_file": str(self.settings_file),
            "datasets_dir": str(self.datasets_dir),
            "extracted_dir": str(self.extracted_dir),
            "history_dir": str(self.history_dir),
            "jobs_dir": str(self.jobs_dir),
            "uploads_dir": str(self.uploads_dir),
        }

    def __repr__(self):  # pragma: no cover - debugging aid only
        return "Workspace(%r)" % str(self.root)


def resolve(explicit=None):
    """Resolve the workspace for this process.

    ``explicit`` (the CLI ``--workspace``) wins, then ``PEEKSEED_WORKSPACE``,
    then ``./workspace``. ``~/peekseed`` is the last resort and is only chosen
    when the current directory cannot be written, with a warning. The derived
    root is ``<workspace>/.peekseed`` unless ``PEEKSEED_DATA`` or an adopted
    legacy ``./data`` root overrides it.
    """
    if explicit:
        workspace_root = _absolute(explicit)
    else:
        env_root = os.environ.get(WORKSPACE_ENV)
        if env_root:
            workspace_root = _absolute(env_root)
        else:
            candidate = Path.cwd() / WORKSPACE_DIRNAME
            if candidate.exists() or _cwd_writable():
                workspace_root = candidate.resolve()
            else:
                workspace_root = _absolute(Path.home() / "peekseed")
                _warn(
                    "readonly-cwd",
                    "peekseed: the current directory is not writable; using "
                    "~/peekseed as the workspace (set PEEKSEED_WORKSPACE to "
                    "choose another).",
                )
    derived = _derived_override(workspace_root)
    return Workspace(workspace_root, derived)


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
    _ensure_dir(links_file.parent, _DERIVED_MODE)
    contracts.write_json_atomic(links_file, {"version": 1, "links": []},
                                mode=0o600)
    return True


def _write_text_atomic(path, text, mode=0o644):
    """Atomically write UTF-8 text (used only for the .gitignore guard)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with open(str(temporary), "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(str(temporary), mode)
    os.replace(str(temporary), str(path))


def _find_git_root(start):
    """The nearest ancestor of ``start`` that contains a ``.git`` entry."""
    current = Path(start).expanduser().resolve()
    for candidate in [current] + list(current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _gitignore_has_entry(gitignore, entry):
    try:
        lines = gitignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.strip() == entry for line in lines)


def gitignore_entry(root, base=None):
    """The anchored ignore pattern that covers ``root`` from ``base``.

    A workspace can live in a directory with any name, so hardcoding
    ``/workspace/`` would leave ``<repo>/ws`` unignored. The pattern is derived
    from the workspace's location relative to the checkout (``/ws/`` for
    ``<repo>/ws``); for the default ``./workspace`` at the repository root it is
    exactly :data:`GITIGNORE_ENTRY` (``/workspace/``).
    """
    root = Path(root).expanduser().resolve()
    base = Path(base).expanduser().resolve() if base else root.parent
    try:
        relative = root.relative_to(base)
        parts = relative.as_posix()
    except ValueError:
        parts = root.name
    if parts in ("", "."):
        parts = root.name
    return "/" + parts.strip("/") + "/"


def guard_gitignore(root):
    """Ensure the workspace is ignored in the containing git checkout.

    The checkout is the nearest ancestor of ``root`` with a ``.git``; when
    ``root`` is not inside one the current directory's ``.gitignore`` is used
    (the CLI is normally run from the project directory). The entry is anchored
    and derived from the workspace path, so a custom-named workspace is really
    ignored. Returns a small report instead of raising: a read-only checkout
    must not break ``init``.
    """
    root = Path(root).expanduser().resolve()
    git_root = _find_git_root(root)
    # A workspace outside any checkout is normally created from the project
    # directory, so the guard is written next to the code the user just cloned.
    base = git_root if git_root else Path.cwd().resolve()
    gitignore = base / ".gitignore"
    entry = gitignore_entry(root, base)
    present = _gitignore_has_entry(gitignore, entry)
    changed = False
    if not present:
        try:
            existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
            if existing and not existing.endswith("\n"):
                existing += "\n"
            if existing:
                existing += "\n"
            text = existing + "# peekseed workspace (documents + derived data)\n" + entry + "\n"
            _write_text_atomic(gitignore, text)
            present = True
            changed = True
        except OSError:
            pass
    return {
        "git_root": str(git_root) if git_root else None,
        "path": str(gitignore),
        "entry": entry,
        "present": present,
        "changed": changed,
    }


def init(explicit=None):
    """Create (or repair) the workspace tree and the gitignore guard.

    Idempotent: existing links, manifest and user documents are never
    overwritten, and directories are only chmod-ed to their contract modes.
    Returns the absolute paths plus the gitignore report.
    """
    ws = resolve(explicit)
    _ensure_dir(ws.root, _DOCUMENTS_MODE)
    _ensure_dir(ws.documents_dir, _DOCUMENTS_MODE)
    for name in _DERIVED_CHILDREN:
        _ensure_dir(ws.derived_dir / name, _DERIVED_MODE)
    try:
        os.chmod(str(ws.derived_dir), _DERIVED_MODE)
    except OSError:
        pass
    _write_empty_links(ws.links_file)
    if not ws.manifest_file.exists():
        manifest = contracts.new_manifest()
        contracts.validate_manifest(manifest)
        contracts.write_json_atomic(ws.manifest_file, manifest)
    report = ws.as_dict()
    report["gitignore"] = guard_gitignore(ws.root)
    print("workspace: %s" % ws.root)
    for label, value in (("documents", ws.documents_dir),
                         ("derived", ws.derived_dir),
                         ("links", ws.links_file),
                         ("manifest", ws.manifest_file)):
        print("  %-10s %s" % (label + ":", value))
    return report


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


def _git_report(root):
    """Gitignore/tracked state for a workspace (never raises)."""
    git_root = _find_git_root(root)
    if git_root is None:
        return {"root": None, "ignored": None, "tracked": False,
                "gitignore": None}
    relpath = os.path.relpath(str(root), str(git_root))
    ignored = None
    tracked = False
    git = shutil.which("git")
    if git:
        try:
            result = subprocess.run(
                [git, "-C", str(git_root), "check-ignore", "-q", relpath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            ignored = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ignored = None
        try:
            result = subprocess.run(
                [git, "-C", str(git_root), "ls-files", "--", relpath],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            tracked = bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            tracked = False
    entry = gitignore_entry(root, git_root)
    if ignored is None:
        ignored = _gitignore_has_entry(git_root / ".gitignore", entry)
    return {
        "root": str(git_root),
        "entry": entry,
        "ignored": bool(ignored),
        "tracked": bool(tracked),
        "gitignore": str(git_root / ".gitignore"),
    }


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


def _dataset_dirs(ws):
    if not ws.datasets_dir.is_dir():
        return []
    return sorted(
        entry
        for entry in ws.datasets_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def dataset_dirs(ws):
    """The collection directories of a workspace, sorted by name (read-only)."""
    return _dataset_dirs(ws)


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


def _schemas_report(ws):
    """Schema versions (and upgrade needs) of manifest, dataset and info files."""
    report = {"manifest": None, "datasets": [], "indexes": [], "issues": []}
    data, error = _read_json(ws.manifest_file)
    if error:
        report["issues"].append("manifest.json is unreadable: %s" % error)
    elif data is None:
        report["issues"].append("manifest.json is missing; run `peekseed init`.")
    else:
        schema = _declared_schema(data, contracts.MANIFEST_SCHEMA)
        report["manifest"] = {"schema": schema, "ok": schema == contracts.MANIFEST_SCHEMA}
        if schema > contracts.MANIFEST_SCHEMA:
            report["issues"].append(
                "manifest.json declares schema %d; upgrade peekseed." % schema
            )
        elif schema < contracts.MANIFEST_SCHEMA:
            report["issues"].append(
                "manifest.json uses schema %d; run `peekseed migrate`." % schema
            )
    for dataset_dir in _dataset_dirs(ws):
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


def doctor(explicit=None):
    """Inspect the resolved workspace and report actionable problems.

    Returns ``{"paths", "issues", "git", "links", "schemas"}``: ``issues`` are
    short user-facing strings (basenames only, no machine paths), each naming
    the command that fixes it. Read-only: doctor never writes.
    """
    ws = resolve(explicit)
    issues = []
    git = _git_report(ws.root)
    if git.get("root"):
        if not git.get("ignored"):
            issues.append(
                "the workspace is inside a git checkout but %s is not in "
                ".gitignore; run `peekseed init`." % git.get("entry")
            )
        if git.get("tracked"):
            issues.append(
                "workspace files are tracked by git; documents and derived "
                "data could be committed."
            )
    links = _links_report(ws.links_file)
    if links.get("error"):
        issues.append("model links: %s" % links["error"])
    elif not links.get("present"):
        issues.append("the model link store is missing; run `peekseed init`.")
    schemas = _schemas_report(ws)
    issues.extend(schemas.pop("issues"))
    if not ws.documents_dir.is_dir():
        issues.append("documents/ is missing; run `peekseed init`.")
    if not ws.derived_dir.is_dir():
        issues.append("the derived tree is missing; run `peekseed init`.")
    return {
        "paths": ws.as_dict(),
        "issues": issues,
        "git": git,
        "links": links,
        "schemas": schemas,
    }


# --- status ----------------------------------------------------------------


def _read_manifest(ws):
    """``(manifest, error)``: the validated manifest or a short reason."""
    data, error = _read_json(ws.manifest_file)
    if error:
        return contracts.new_manifest(), error
    if not isinstance(data, dict):
        return contracts.new_manifest(), "manifest.json is missing or invalid."
    try:
        return contracts.validate_manifest(data), None
    except contracts.ContractError as exc:
        return contracts.new_manifest(), str(exc)


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


def status(explicit=None):
    """A read-only summary of the workspace: paths, last scan and collections.

    Collection state is delegated to ``.peekseed/datasets`` and the manifest,
    so status never needs the index module: it only reports what is on disk.
    """
    ws = resolve(explicit)
    manifest, manifest_error = _read_manifest(ws)
    entries = list(manifest.get("documents") or [])
    manifest_paths = {str(entry.get("path") or "") for entry in entries}
    collections = {}
    for dataset_dir in _dataset_dirs(ws):
        data, error = _read_json(dataset_dir / "dataset.json")
        dataset = data if isinstance(data, dict) else {}
        sources = _dataset_sources(dataset)
        chunks = _count_lines(dataset_dir / "chunks.jsonl")
        state, reasons = _index_state(dataset_dir, dataset)
        reported = str(dataset.get("status") or "")
        # The live index verdict (needs_reindex/stale) always wins: a corrupt
        # generation must never hide behind a stale scan-time status. The
        # dataset's own status only adds what the index cannot know (source
        # errors or deleted documents).
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
    # A collection whose dataset directory is gone is still worth reporting.
    for entry in entries:
        collection = str(entry.get("collection") or "")
        if collection and collection not in collections:
            collections[collection] = {
                "name": collection,
                "status": "missing",
                "sources": 0,
                "chunks": 0,
                "indexes": [],
                "reasons": ["dataset directory is missing"],
            }
    return {
        "workspace": ws.as_dict(),
        "last_scan": None if manifest_error else manifest.get("last_scan"),
        "collections": collections,
        "counts": {
            "documents": len(entries),
            "missing": sum(1 for entry in entries
                           if str(entry.get("status") or "") == "missing"),
            "collections": len(collections),
            "chunks": sum(item["chunks"] for item in collections.values()),
        },
        "manifest": {"present": ws.manifest_file.is_file(),
                     "error": manifest_error,
                     "documents": len(manifest_paths)},
    }


# --- move ------------------------------------------------------------------


def move(target, *, explicit=None):
    """Move the whole workspace tree to ``target`` and return the new paths.

    The destination must not exist or must be an empty directory. The source
    tree is moved as one unit (``shutil.move``), so a crash cannot leave half
    the documents behind; ``explicit`` names the workspace to move when the
    process has more than the default one.
    """
    ws = resolve(explicit)
    destination = _absolute(target)
    if destination == ws.root:
        return ws.as_dict()
    if destination.exists():
        try:
            if any(destination.iterdir()):
                raise ValueError("the move target is not empty")
            destination.rmdir()
        except OSError:
            raise ValueError("the move target is not empty") from None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ws.root), str(destination))
    moved = Workspace(destination)
    # The derived root follows the workspace unless the user pinned it.
    if os.environ.get(DATA_ENV):
        moved = Workspace(destination, _absolute(os.environ[DATA_ENV]))
    return moved.as_dict()
