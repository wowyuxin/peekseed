"""Selective review-metadata bundle import (M3 CP5, CP3 addendum B9).

Peelfeed exports ``$PEEKSEED_HOME/review-bundles/<peel_id>/<ts>.tar.gz``. This
module imports them on the peekseed side:

* validate the bundle manifest against the current peel with EXACTLY peelfeed
  ``metadata import`` semantics (CP5 F3: a faithful port of
  ``peelfeed/review/metadata_bundle.py`` ``_current_identity`` /
  ``_validate_manifest`` — same fields, same sources, same
  unpublished/published handling). Identity/fingerprint bindings come from
  ``corpus/manifest.json`` ONLY (``None`` for a never-published peel), never
  from ``build_summary.json``; the source sha256 is the source-file hash; the
  uncorrected identity is the facts+manifest snapshot peelfeed computes.
  Mismatch is ``stale``;
* peel exists: atomically merge ``review/`` (and ``ocr/`` + ``review_facts``),
  byte-identical files skipped (idempotent), an older bundle never regresses a
  newer plan/ledger, the append-only ledgers are ALWAYS row-level
  append-merged (CP5 F4, the engine's N-3), no provider call, no
  auto-confirm/publish;
* peel missing: never create a fake peel; stage the bundle under
  ``$PEEKSEED_HOME/review-restore/<peel_id>/`` and warn;
* path-check tar members (absolute/``..``/links/devices rejected);
* record every attempt in ``review-bundles/imports.jsonl``.

The verification is ported rather than delegated to the peelfeed CLI so
``python -m peekseed review-import`` stays a standalone command (no configured
peelfeed CLI required, structured ``mismatches`` preserved, missing-peel
staging stays peekseed-side). The frozen schema versions below mirror
``peelfeed/review/store.py``; the real CLI-vs-app parity is re-verified by the
CP5 retest harness on real peels.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from . import contracts

__all__ = ["import_all", "import_one"]

MANIFEST_NAME = "manifest.json"
_ALLOWED_PREFIXES = ("review/", "corpus/", "ocr/")
_LEDGER_FILES = ("plan.json", "resolutions.jsonl", "corrections.jsonl")
_MAX_MEMBER = 64 * 1024 * 1024


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path, default=None):
    try:
        payload = contracts.read_json(path, default=default)
    except contracts.ContractError:
        return default
    return payload


def _source_sha(peel_dir):
    source = Path(peel_dir) / "source"
    if not source.is_dir():
        return None
    files = sorted(path for path in source.iterdir() if path.is_file())
    if not files:
        return None
    return _sha_file(files[0])


def _converter_code_hash(manifest):
    """Port of peelfeed ``items._converter_code_hash`` (diagnostic only)."""
    hashes = manifest.get("converter_hashes") or {}
    if not hashes:
        return ""
    return hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _uncorrected_identity(peel_dir, manifest):
    """Port of peelfeed ``items.uncorrected_identity`` (facts + manifest).

    peelfeed computes this live from ``review_facts.json`` (``facts.ensure``)
    and ``corpus/manifest.json``; peekseed reads the same two files read-only
    (a normal peelfeed export guarantees the facts file exists — it is a
    required export artifact). The full snapshot dict is compared, exactly
    like peelfeed's ``_validate_manifest``.
    """
    facts = _read_json(Path(peel_dir) / "extract" / "corpus" /
                       "review_facts.json", default={}) or {}
    return {
        "uncorrected_identity": str(facts.get("uncorrected_identity") or ""),
        "converter_code_hash": _converter_code_hash(manifest),
        "profile": str(manifest.get("profile") or ""),
        "manifest": manifest,
    }


#: The frozen review schema versions peelfeed validates bundles against
#: (``peelfeed/review/store.py``: ITEM_SCHEMA/REVIEW_FACTS_SCHEMA/BATCH_SCHEMA/
#: JOB_SCHEMA). CP5 F3: same check as peelfeed ``_validate_manifest``.
_CURRENT_SCHEMAS = {"items": 2, "review_facts": 1, "batch": 1, "job": 1}


def _current_identity(peel_dir):
    """CP5 F3: EXACTLY peelfeed ``metadata_bundle._current_identity`` sources.

    ``build_id``/``conversion_id``/``engine_fingerprint``/correction bindings/
    ``chunk_sets`` come from ``corpus/manifest.json`` only — ``None``/``[]``
    for a never-published peel, exactly like the peelfeed export manifest.
    ``build_summary.json`` is NOT an identity source (it describes the latest
    candidate build, published or not; reading it made peekseed reject bundles
    peelfeed accepts). ``plan_version`` is peekseed-side merge metadata (the
    engine reads it separately in ``restore``); it is never compared here.
    """
    peel_dir = Path(peel_dir)
    corpus = peel_dir / "extract" / "corpus"
    manifest = _read_json(corpus / "manifest.json", default={}) or {}
    items = _read_json(peel_dir / "extract" / "review" / "items.json",
                       default={}) or {}
    plan = _read_json(peel_dir / "extract" / "review" / "plan.json",
                      default={}) or {}
    return {
        "peel_id": peel_dir.name,
        "source_sha256": _source_sha(peel_dir),
        "uncorrected_identity": _uncorrected_identity(peel_dir, manifest),
        "build_id": manifest.get("build_id"),
        "conversion_id": manifest.get("conversion_id"),
        "engine_fingerprint": manifest.get("engine_fingerprint"),
        "effective_corrections_hash": manifest.get(
            "effective_corrections_hash"),
        "correction_engine_version": manifest.get(
            "correction_engine_version"),
        "chunk_sets": manifest.get("chunk_sets") or [],
        "evidence_hashes": {
            str(item.get("id")): (item.get("evidence") or {}).get("hash")
            for item in items.get("items") or []
            if isinstance(item, dict)},
        "plan_version": int(plan.get("plan_version") or 0),
    }


def _validate_manifest(manifest, current):
    """CP5 F3: EXACTLY peelfeed ``metadata_bundle._validate_manifest`` rules.

    Same required fields (incl. ``peel_id``/``schema_versions``), same direct
    value comparison (full uncorrected-identity snapshot, no normalization),
    same evidence-hash subset check and same frozen schema-version check.
    """
    required = ("peel_id", "source_sha256", "uncorrected_identity", "build_id",
                "conversion_id", "engine_fingerprint",
                "effective_corrections_hash", "correction_engine_version",
                "chunk_sets", "evidence_hashes", "schema_versions")
    missing = [key for key in required if key not in manifest]
    if missing:
        return {"status": "invalid",
                "reason": "manifest missing: " + ", ".join(sorted(missing))}
    mismatches = {}
    for key in ("peel_id", "source_sha256", "uncorrected_identity", "build_id",
                "conversion_id", "engine_fingerprint",
                "effective_corrections_hash", "correction_engine_version",
                "chunk_sets"):
        if manifest.get(key) != current.get(key):
            mismatches[key] = {"bundle": manifest.get(key),
                               "current": current.get(key)}
    bundle_hashes = manifest.get("evidence_hashes") or {}
    current_hashes = current.get("evidence_hashes") or {}
    changed = sorted(item_id for item_id, evidence_hash
                     in bundle_hashes.items()
                     if current_hashes.get(item_id) != evidence_hash)
    if changed:
        mismatches["evidence_hashes"] = changed
    bundle_schemas = manifest.get("schema_versions") or {}
    for key, value in _CURRENT_SCHEMAS.items():
        if key in bundle_schemas and bundle_schemas.get(key) != value:
            mismatches["schema:" + key] = {"bundle": bundle_schemas.get(key),
                                           "current": value}
    if mismatches:
        return {"status": "stale", "reason": "identity mismatch",
                "mismatches": mismatches}
    return {"status": "ok"}


def _safe_members(tar):
    members = []
    for member in tar.getmembers():
        name = member.name
        if name == MANIFEST_NAME:
            if not member.isfile():
                raise ValueError("bundle manifest is not a file")
            members.append(member)
            continue
        if name.startswith("/") or ".." in Path(name).parts:
            raise ValueError("unsafe bundle member: %s" % name)
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError("bundle member is a link/device: %s" % name)
        if not member.isfile():
            continue
        if member.size > _MAX_MEMBER:
            raise ValueError("bundle member is too large: %s" % name)
        if not any(name.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            raise ValueError("bundle member is outside the allowed roots: %s"
                             % name)
        members.append(member)
    return members


def _merge_tree(source, target, *, skip_ledgers=False):
    copied = skipped = 0
    if not source.is_dir():
        return copied, skipped
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if skip_ledgers and relative.as_posix() in _LEDGER_FILES:
            skipped += 1
            continue
        destination = Path(target) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and _sha_file(path) == _sha_file(destination):
            skipped += 1
            continue
        temp = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(str(path), str(temp))
        os.chmod(str(temp), 0o600)
        temp.replace(destination)
        copied += 1
    return copied, skipped


def _imports_path(paths):
    return Path(paths.root) / "review-bundles" / "imports.jsonl"


def _already_imported(paths, bundle_sha):
    path = _imports_path(paths)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("bundle_sha256") == bundle_sha:
            return row
    return None


def _record(paths, row):
    path = _imports_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def import_one(paths, bundle, *, dry_run=False):
    bundle = Path(bundle)
    if not bundle.is_file():
        return {"status": "invalid", "reason": "bundle not found",
                "bundle": str(bundle)}
    peel_id = bundle.parent.name
    bundle_sha = _sha_file(bundle)
    previous = _already_imported(paths, bundle_sha)
    if previous is not None and previous.get("status") == "imported":
        return {"status": "imported", "bundle": str(bundle),
                "peel_id": peel_id, "idempotent": True,
                "files": previous.get("files", 0),
                "skipped": previous.get("skipped", 0)}
    peel_dir = Path(paths.peels_dir) / peel_id
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            members = _safe_members(tar)
            with tempfile.TemporaryDirectory(
                    prefix="peekseed-review-import-") as tmp:
                tar.extractall(tmp, members=members)
                manifest = _read_json(Path(tmp) / MANIFEST_NAME)
                if not isinstance(manifest, dict):
                    return {"status": "invalid", "bundle": str(bundle),
                            "peel_id": peel_id,
                            "reason": "bundle manifest is missing"}
                if not peel_dir.is_dir():
                    staging = Path(paths.root) / "review-restore" / peel_id
                    staging.mkdir(parents=True, exist_ok=True)
                    target = staging / bundle.name
                    if not dry_run:
                        shutil.copyfile(str(bundle), str(target))
                        try:
                            target.chmod(0o600)
                        except OSError:
                            pass
                        _record(paths, {
                            "schema_version": 1,
                            "bundle": str(bundle),
                            "bundle_sha256": bundle_sha,
                            "peel_id": peel_id, "status": "staged",
                            "staged_at": contracts.utc_now(),
                            "reason": ("peel_missing; re-extract the source, "
                                       "then import"),
                        })
                    return {"status": "staged", "bundle": str(bundle),
                            "peel_id": peel_id, "staged_to": str(target),
                            "reason": ("peel_missing; re-extract the source, "
                                       "then import")}
                current = _current_identity(peel_dir)
                validation = _validate_manifest(manifest, current)
                if validation["status"] != "ok":
                    if not dry_run:
                        _record(paths, {
                            "schema_version": 1,
                            "bundle": str(bundle),
                            "bundle_sha256": bundle_sha,
                            "peel_id": peel_id,
                            "status": validation["status"],
                            "at": contracts.utc_now(),
                            "reason": validation.get("reason"),
                            "mismatches": validation.get("mismatches"),
                        })
                    return dict(validation, bundle=str(bundle),
                                peel_id=peel_id)
                bundle_plan_version = int(manifest.get("plan_version") or 0)
                older_plan = bundle_plan_version < current["plan_version"]
                warnings = ["newer_plan_kept"] if older_plan else []
                copied = skipped = 0
                count, skip = _merge_tree(
                    Path(tmp) / "review",
                    peel_dir / "extract" / "review",
                    skip_ledgers=older_plan)
                copied += count
                skipped += skip
                facts = Path(tmp) / "corpus" / "review_facts.json"
                if facts.is_file():
                    destination = (peel_dir / "extract" / "corpus" /
                                   "review_facts.json")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.is_file() and _sha_file(facts) == _sha_file(
                            destination):
                        skipped += 1
                    else:
                        temp = destination.with_suffix(
                            destination.suffix + ".tmp")
                        shutil.copyfile(str(facts), str(temp))
                        os.chmod(str(temp), 0o600)
                        temp.replace(destination)
                        copied += 1
                count, skip = _merge_tree(Path(tmp) / "ocr",
                                          peel_dir / "extract" / "ocr")
                copied += count
                skipped += skip
                result = {
                    "status": "imported", "bundle": str(bundle),
                    "peel_id": peel_id, "files": copied,
                    "skipped": skipped, "warnings": warnings,
                    "plan_version": bundle_plan_version,
                }
                if not dry_run:
                    _record(paths, {
                        "schema_version": 1,
                        "bundle": str(bundle),
                        "bundle_sha256": bundle_sha,
                        "peel_id": peel_id,
                        "status": "imported",
                        "files": copied, "skipped": skipped,
                        "warnings": warnings,
                        "imported_at": contracts.utc_now(),
                    })
                return result
    except (tarfile.TarError, ValueError, OSError) as exc:
        return {"status": "invalid", "bundle": str(bundle),
                "peel_id": peel_id, "reason": str(exc)}


def import_all(paths, *, dry_run=False):
    """Import every ``review-bundles/<peel_id>/*.tar.gz`` (idempotent)."""
    root = Path(paths.root) / "review-bundles"
    results = []
    if root.is_dir():
        for peel_dir in sorted(path for path in root.iterdir()
                               if path.is_dir()):
            for bundle in sorted(peel_dir.glob("*.tar.gz")):
                results.append(import_one(paths, bundle, dry_run=dry_run))
    return {
        "schema_version": 1,
        "dry_run": bool(dry_run),
        "bundles": len(results),
        "imported": sum(1 for row in results
                        if row.get("status") == "imported"),
        "staged": sum(1 for row in results
                      if row.get("status") == "staged"),
        "stale": sum(1 for row in results
                     if row.get("status") == "stale"),
        "invalid": sum(1 for row in results
                       if row.get("status") == "invalid"),
        "results": results,
    }
