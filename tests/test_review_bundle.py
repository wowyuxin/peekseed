"""CP5 review-metadata bundle import tests (CP3 addendum B9).

The bundle format is the peelfeed CP4 export. Import is peekseed-side: validate
the manifest against the peel, merge atomically, stage when the peel is missing,
never create a fake peel, never call a provider, never auto-confirm.
"""
import hashlib
import io
import json
import tarfile
import unittest
from pathlib import Path

from tests.fixtures import make_dataset, make_peel, temp_data_root


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _peel_with_review():
    dataset = make_dataset("Docs")
    peel = make_peel(dataset)
    peel_dir = Path(peel["peel_dir"])
    manifest = json.loads(
        (peel_dir / "extract" / "corpus" / "manifest.json").read_text())
    build = json.loads(
        (peel_dir / "extract" / "corpus" / "build_summary.json").read_text())
    _write_json(peel_dir / "extract" / "review" / "items.json", {
        "schema_version": 2,
        "derived_from": {
            "uncorrected_identity": manifest.get("uncorrected_identity"),
        },
        "items": [{"id": "it-1", "evidence": {"hash": "h-1"}}],
    })
    _write_json(peel_dir / "extract" / "review" / "plan.json", {
        "schema_version": 2, "peel_id": peel["peel_id"],
        "plan_version": 2, "entries": []})
    _write_json(peel_dir / "extract" / "review" / "review_facts.json", {
        "schema_version": 1, "uncorrected_identity": "facts"})
    return dataset, peel, peel_dir, manifest, build


def _bundle(peel, manifest, build, *, path, tamper=None, plan_version=2,
            extra_member=None):
    payload = {
        "schema_version": 1,
        "peel_id": peel["peel_id"],
        "source_sha256": manifest.get("source_sha256"),
        "uncorrected_identity": manifest.get("uncorrected_identity"),
        "build_id": build.get("build_id"),
        "conversion_id": manifest.get("conversion_id"),
        "engine_fingerprint": build["engine"]["fingerprint"],
        "effective_corrections_hash": manifest.get(
            "effective_corrections_hash"),
        "correction_engine_version": manifest.get(
            "correction_engine_version"),
        "chunk_sets": [],
        "plan_version": plan_version,
        "evidence_hashes": {"it-1": "h-1"},
        "schema_versions": {"items": 2},
    }
    if tamper:
        payload.update(tamper)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps(payload).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for name, body in (
                ("review/plan.json", json.dumps({"plan_version": plan_version,
                                                "entries": []})),
                ("review/items.json", json.dumps({"items": []})),
                ("review/conversations/conv-1.jsonl",
                 json.dumps({"turn": 1})),
                ("corpus/review_facts.json", json.dumps({"facts": True})),
                ("ocr/figure-1/evidence.json", json.dumps({"labels": []}))):
            blob = body.encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size = len(blob)
            tar.addfile(member, io.BytesIO(blob))
        if extra_member:
            blob = b"x"
            member = tarfile.TarInfo(extra_member)
            member.size = len(blob)
            tar.addfile(member, io.BytesIO(blob))
    return path


class ReviewBundleImportTests(unittest.TestCase):
    def test_import_merges_and_is_idempotent(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            bundle = _bundle(peel, manifest, build,
                             path=paths.root / "review-bundles" /
                             peel["peel_id"] / "b1.tar.gz")
            report = review_bundle.import_all(paths)
            self.assertEqual(report["imported"], 1, report)
            self.assertTrue((peel_dir / "extract" / "review" /
                             "plan.json").is_file())
            self.assertTrue((peel_dir / "extract" / "corpus" /
                             "review_facts.json").is_file())
            self.assertTrue((peel_dir / "extract" / "ocr" / "figure-1" /
                             "evidence.json").is_file())
            self.assertTrue((peel_dir / "extract" / "review" /
                             "conversations" / "conv-1.jsonl").is_file())
            imports = (paths.root / "review-bundles" /
                       "imports.jsonl").read_text().splitlines()
            self.assertEqual(json.loads(imports[-1])["status"], "imported")
            # Second run is idempotent (same bundle sha -> no rewrite).
            again = review_bundle.import_all(paths)
            self.assertEqual(again["imported"], 1)
            self.assertTrue(again["results"][0]["idempotent"])

    def test_stale_manifest_is_rejected(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            _dataset, peel, _peel_dir, manifest, build = _peel_with_review()
            bundle = _bundle(peel, manifest, build,
                             path=paths.root / "review-bundles" /
                             peel["peel_id"] / "b1.tar.gz",
                             tamper={"engine_fingerprint": "tampered"})
            report = review_bundle.import_all(paths)
            self.assertEqual(report["stale"], 1, report)
            self.assertIn("engine_fingerprint",
                          report["results"][0]["mismatches"])

    def test_missing_peel_is_staged_not_created(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            fake_peel = {"peel_id": "missing-peel-1234"}
            bundle = _bundle(fake_peel, {
                "source_sha256": "a" * 64,
                "uncorrected_identity": "u",
                "conversion_id": "c",
                "effective_corrections_hash": hashlib.sha256(b"[]").hexdigest(),
                "correction_engine_version": "v",
            }, {"build_id": "b", "engine": {"fingerprint": "f"}},
                path=paths.root / "review-bundles" /
                "missing-peel-1234" / "b1.tar.gz")
            report = review_bundle.import_all(paths)
            self.assertEqual(report["staged"], 1, report)
            self.assertFalse((paths.peels_dir / "missing-peel-1234").exists())
            staged = paths.root / "review-restore" / "missing-peel-1234" / \
                bundle.name
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)

    def test_corrupt_bundle_is_invalid(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            peel = {"peel_id": "corrupt-peel"}
            bundle = paths.root / "review-bundles" / "corrupt-peel" / \
                "bad.tar.gz"
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_bytes(b"this is not a tar archive")
            report = review_bundle.import_all(paths)
            self.assertEqual(report["invalid"], 1, report)
            self.assertFalse((paths.peels_dir / "corrupt-peel").exists())

    def test_unsafe_tar_member_is_rejected(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            _dataset, peel, _peel_dir, manifest, build = _peel_with_review()
            bundle = _bundle(peel, manifest, build,
                             path=paths.root / "review-bundles" /
                             peel["peel_id"] / "b1.tar.gz",
                             extra_member="../escape.txt")
            report = review_bundle.import_all(paths)
            self.assertEqual(report["invalid"], 1, report)
            self.assertFalse((paths.root / "escape.txt").exists())

    def test_older_plan_does_not_regress_current_plan(self):
        with temp_data_root() as root:
            from peekseed import config, review_bundle
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            # Current plan version 2; bundle version 1.
            bundle = _bundle(peel, manifest, build,
                             path=paths.root / "review-bundles" /
                             peel["peel_id"] / "b1.tar.gz", plan_version=1)
            report = review_bundle.import_all(paths)
            self.assertEqual(report["imported"], 1, report)
            self.assertIn("newer_plan_kept", report["results"][0]["warnings"])
            current = json.loads((peel_dir / "extract" / "review" /
                                  "plan.json").read_text())
            self.assertEqual(current["plan_version"], 2)


if __name__ == "__main__":
    unittest.main()
