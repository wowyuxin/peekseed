"""Explicit schema migration for an existing workspace.

Peekseed's on-disk formats are a public contract: a file written by a newer
version is refused, never guessed at, and an older file is upgraded only when the
user asks for it. ``peekseed migrate`` is that request. It is dry-run by default,
prints exactly what would change, and only rewrites files (atomically) with
``--apply``.

Two artifacts are versioned today:

* ``dataset.json`` -- schema 1 (``version: 1``) gains per-source ``sha256``,
  ``extractor`` and ``status``; the hash is computed from the file on disk, never
  invented (a source whose file is missing is marked ``new`` with no hash).
* ``info.json`` -- schema 1 gains the full embedding-space identity, chunker
  version, documents digest and the probe slots. A schema-1 index has no stored
  probe vector; :func:`peekseed.contracts.has_probe` is false until the index is
  rebuilt (or ``peekseed index`` re-embeds the probe), which the report says
  explicitly instead of pretending the index is gateable.

Downgrades are unsupported: restoring a backup is the rollback path.
"""

from pathlib import Path

from . import contracts

__all__ = ["migrate"]

# Bumped when the migration logic itself changes shape; informational only.
MIGRATION_VERSION = 1


def _dataset_hash_resolver(dataset_dir, workspace_root):
    """Resolve and hash a source path named by ``dataset.json``.

    Legacy dataset dirs store sources under ``original/``; workspace-managed
    collections reference ``documents/<collection>/<file>``. Both are resolved
    relative to the dataset dir first, then the workspace root, so a migration can
    run before or after the workspace layout exists.
    """

    def resolve(relpath):
        relpath = str(relpath or "").replace("\\", "/")
        if not relpath:
            return None
        candidates = []
        base = Path(dataset_dir)
        candidates.append(base / relpath)
        candidates.append(base / "original" / Path(relpath).name)
        if workspace_root is not None:
            candidates.append(Path(workspace_root) / relpath)
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return contracts.sha256_file(candidate)
            except OSError:
                continue
        return None

    return resolve


def _migrate_dataset(path, workspace_root, dry_run, actions, errors):
    dataset_dir = path.parent
    try:
        raw = contracts.read_json(path)
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    if not isinstance(raw, dict):
        errors.append({"file": str(path), "error": "dataset.json is not a JSON object"})
        return
    try:
        declared = contracts._declared_schema(raw, "dataset.json", contracts.DATASET_SCHEMA)
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    if declared > contracts.DATASET_SCHEMA:
        errors.append(
            {
                "file": str(path),
                "error": "%s declares schema %d, newer than this build's %d"
                % (path.name, declared, contracts.DATASET_SCHEMA),
            }
        )
        return
    if declared == contracts.DATASET_SCHEMA:
        return
    try:
        upgraded = contracts.upgrade_dataset(
            raw, sha256_of=_dataset_hash_resolver(dataset_dir, workspace_root)
        )
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    missing = [entry["name"] for entry in upgraded["sources"] if not entry["sha256"]]
    actions.append(
        {
            "file": str(path),
            "kind": "dataset.json",
            "from_schema": declared,
            "to_schema": contracts.DATASET_SCHEMA,
            "sources": len(upgraded["sources"]),
            "missing_sources": missing,
        }
    )
    if not dry_run:
        contracts.write_json_atomic(path, upgraded)


def _migrate_info(path, dry_run, actions, errors):
    try:
        raw = contracts.read_json(path)
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    if not isinstance(raw, dict):
        errors.append({"file": str(path), "error": "info.json is not a JSON object"})
        return
    try:
        declared = contracts._declared_schema(raw, "info.json", contracts.INFO_SCHEMA)
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    if declared > contracts.INFO_SCHEMA:
        errors.append(
            {
                "file": str(path),
                "error": "%s declares schema %d, newer than this build's %d"
                % (path.name, declared, contracts.INFO_SCHEMA),
            }
        )
        return
    if declared == contracts.INFO_SCHEMA:
        return
    try:
        upgraded = contracts.upgrade_info(raw)
    except contracts.ContractError as exc:
        errors.append({"file": str(path), "error": str(exc)})
        return
    probe_note = ""
    if not contracts.has_probe(upgraded):
        probe_note = (
            "no stored probe vector: run `peekseed index` to re-embed it before "
            "the probe gate can protect this index"
        )
    actions.append(
        {
            "file": str(path),
            "kind": "info.json",
            "from_schema": declared,
            "to_schema": contracts.INFO_SCHEMA,
            "note": probe_note,
        }
    )
    if not dry_run:
        contracts.write_json_atomic(path, upgraded)


def migrate(root=None, *, workspace_root=None, dry_run=True, apply=False):
    """Upgrade every older artifact under a derived root.

    ``root`` is the derived root (the directory holding ``datasets/``); it
    defaults to :func:`peekseed.config.paths().root`. ``workspace_root`` is used
    to resolve workspace-relative source paths and defaults to ``root.parent``
    when the derived root is a ``.peekseed`` directory. ``dry_run`` is the
    default; pass ``apply=True`` to write the upgraded files atomically.
    """
    if apply:
        dry_run = False
    if root is None:
        from . import config

        root = config.paths().root
    root = Path(root)
    if workspace_root is None:
        workspace_root = root.parent if root.name == ".peekseed" else None

    actions = []
    errors = []
    datasets_dir = root / "datasets"
    if datasets_dir.is_dir():
        for dataset_dir in sorted(datasets_dir.iterdir()):
            if not dataset_dir.is_dir():
                continue
            dataset_file = dataset_dir / "dataset.json"
            if dataset_file.is_file():
                _migrate_dataset(dataset_file, workspace_root, dry_run, actions, errors)
            index_root = dataset_dir / "index"
            if index_root.is_dir():
                for space_dir in sorted(index_root.iterdir()):
                    info_file = space_dir / "info.json"
                    if info_file.is_file():
                        _migrate_info(info_file, dry_run, actions, errors)
    return {
        "root": str(root),
        "workspace_root": str(workspace_root) if workspace_root else None,
        "dry_run": dry_run,
        "migration_version": MIGRATION_VERSION,
        "actions": actions,
        "errors": errors,
    }


def format_report(report):
    """A short, human-readable rendering of :func:`migrate`'s report."""
    lines = []
    mode = "dry-run (nothing written)" if report["dry_run"] else "applied"
    lines.append("migrate %s: %s" % (mode, report["root"]))
    if not report["actions"] and not report["errors"]:
        lines.append("  everything is already at the current schema")
    for action in report["actions"]:
        line = "  %s: schema %d -> %d" % (
            action["kind"], action["from_schema"], action["to_schema"]
        )
        if action.get("sources"):
            line += " (%d source(s))" % action["sources"]
        lines.append(line)
        if action.get("missing_sources"):
            lines.append(
                "    missing files, marked new: %s" % ", ".join(action["missing_sources"])
            )
        if action.get("note"):
            lines.append("    note: %s" % action["note"])
    for error in report["errors"]:
        lines.append("  error: %s" % error["error"])
    return "\n".join(lines)


def main(root=None, *, dry_run=True):
    """CLI helper: run the migration and print the report."""
    report = migrate(root, dry_run=dry_run, apply=not dry_run)
    print(format_report(report))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(main())
