"""Tests for the explicit schema migration command (peekseed/migrate.py)."""

import json
import tempfile
import unittest
from pathlib import Path

from peekseed import contracts, migrate


def legacy_dataset():
    return {
        "version": 1,
        "id": "docs",
        "name": "Docs",
        "created_at": "2026-01-01T00:00:00Z",
        "sources": [{"name": "a.pdf", "path": "original/a.pdf", "bytes": 5, "pages": 1}],
        "notes": "",
    }


def legacy_info():
    return {
        "schema": 1,
        "dataset": "docs",
        "name": "Docs",
        "space": "link__model",
        "link_id": "link",
        "model": "model",
        "protocol": "custom",
        "dim": 4,
        "n": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "chunker": {"target_chars": 1200, "max_chars": 2000, "overlap": 0},
        "sources": [{"name": "a.pdf", "pages": 1, "bytes": 5}],
    }


def build_legacy_root(root):
    dataset_dir = root / "datasets" / "docs"
    (dataset_dir / "original").mkdir(parents=True)
    (dataset_dir / "original" / "a.pdf").write_bytes(b"%PDF-1.4\nfake")
    (dataset_dir / "dataset.json").write_text(json.dumps(legacy_dataset()), encoding="utf-8")
    (dataset_dir / "chunks.jsonl").write_text("", encoding="utf-8")
    space_dir = dataset_dir / "index" / "link__model"
    space_dir.mkdir(parents=True)
    (space_dir / "info.json").write_text(json.dumps(legacy_info()), encoding="utf-8")
    return dataset_dir


class MigrateTests(unittest.TestCase):
    def test_dry_run_reports_but_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_legacy_root(root)
            report = migrate.migrate(root, dry_run=True)
            self.assertTrue(report["dry_run"])
            self.assertEqual(len(report["actions"]), 2)
            self.assertEqual(report["errors"], [])
            dataset = json.loads((root / "datasets/docs/dataset.json").read_text())
            info = json.loads((root / "datasets/docs/index/link__model/info.json").read_text())
            self.assertEqual(dataset["version"], 1)
            self.assertEqual(info["schema"], 1)

    def test_apply_upgrades_both_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_legacy_root(root)
            report = migrate.migrate(root, apply=True)
            self.assertFalse(report["dry_run"])
            self.assertEqual(report["errors"], [])
            dataset = contracts.read_json(root / "datasets/docs/dataset.json")
            self.assertEqual(dataset["schema"], 2)
            self.assertEqual(dataset["sources"][0]["sha256"], contracts.sha256_file(
                root / "datasets/docs/original/a.pdf"))
            self.assertEqual(dataset["sources"][0]["status"], "indexed")
            info = contracts.read_json(root / "datasets/docs/index/link__model/info.json")
            self.assertEqual(info["schema"], 2)
            contracts.validate_info(info)
            # A schema-1 index has no stored probe; the report says so rather than
            # pretending the migrated index is gateable.
            self.assertFalse(contracts.has_probe(info))
            actions = {action["kind"]: action for action in report["actions"]}
            self.assertIn("probe", actions["info.json"]["note"])

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_legacy_root(root)
            migrate.migrate(root, apply=True)
            second = migrate.migrate(root, apply=True)
            self.assertEqual(second["actions"], [])
            self.assertEqual(second["errors"], [])

    def test_newer_schema_is_refused_not_downgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset_dir = build_legacy_root(root)
            raw = json.loads((dataset_dir / "dataset.json").read_text())
            raw["schema"] = 3
            (dataset_dir / "dataset.json").write_text(json.dumps(raw), encoding="utf-8")
            report = migrate.migrate(root, apply=True)
            self.assertEqual(len(report["errors"]), 1)
            self.assertIn("newer", report["errors"][0]["error"])
            after = json.loads((dataset_dir / "dataset.json").read_text())
            self.assertEqual(after["schema"], 3)

    def test_format_report_mentions_mode_and_errors(self):
        report = {"root": "/tmp/ws", "dry_run": True,
                  "actions": [{"kind": "dataset.json", "from_schema": 1,
                               "to_schema": 2, "sources": 2, "missing_sources": ["gone.pdf"]}],
                  "errors": []}
        text = migrate.format_report(report)
        self.assertIn("dry-run", text)
        self.assertIn("schema 1 -> 2", text)
        self.assertIn("gone.pdf", text)

    def test_empty_root_reports_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = migrate.migrate(Path(tmp), dry_run=True)
            self.assertEqual(report["actions"], [])
            self.assertEqual(report["errors"], [])
            self.assertIn("already at the current schema", migrate.format_report(report))


if __name__ == "__main__":
    unittest.main()
