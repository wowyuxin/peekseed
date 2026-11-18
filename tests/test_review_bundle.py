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
    # CP5 F3: the fixture peel is UNPUBLISHED — corpus/manifest.json carries
    # no build_id/engine_fingerprint (those live in build_summary.json until a
    # publish binds them into the manifest), exactly like a real peelfeed peel
    # before its first publish. The facts file lives under corpus/ like
    # peelfeed's store.review_facts_path.
    _write_json(peel_dir / "extract" / "corpus" / "review_facts.json", {
        "schema_version": 1,
        "uncorrected_identity": manifest.get("uncorrected_identity"),
    })
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


def _publish(peel_dir, manifest, build):
    """Simulate a peelfeed publish: bind the current build identity into
    corpus/manifest.json (the ONLY identity source peelfeed export reads)."""
    published = dict(manifest)
    published["build_id"] = build.get("build_id")
    published["engine_fingerprint"] = (build.get("engine") or {}).get(
        "fingerprint")
    _write_json(peel_dir / "extract" / "corpus" / "manifest.json", published)
    return published


def _uncorrected_identity(manifest):
    """The peelfeed export shape (items.uncorrected_identity): the facts +
    manifest snapshot dict, not the bare identity string."""
    return {
        "uncorrected_identity": str(manifest.get("uncorrected_identity")
                                    or ""),
        "converter_code_hash": "",
        "profile": str(manifest.get("profile") or ""),
        "manifest": manifest,
    }


def _bundle(peel, manifest, build, *, path, tamper=None, plan_version=2,
            extra_member=None, drop=()):
    # CP5 F3: a peelfeed export manifest reads build_id/engine_fingerprint
    # from corpus/manifest.json (None when unpublished) — NEVER from
    # build_summary.json. ``build`` is kept in the signature so tests can
    # tamper with the candidate-build values explicitly.
    payload = {
        "schema_version": 1,
        "peel_id": peel["peel_id"],
        "source_sha256": manifest.get("source_sha256"),
        "uncorrected_identity": _uncorrected_identity(manifest),
        "build_id": manifest.get("build_id"),
        "conversion_id": manifest.get("conversion_id"),
        "engine_fingerprint": manifest.get("engine_fingerprint"),
        "effective_corrections_hash": manifest.get(
            "effective_corrections_hash"),
        "correction_engine_version": manifest.get(
            "correction_engine_version"),
        "chunk_sets": manifest.get("chunk_sets") or [],
        "plan_version": plan_version,
        "evidence_hashes": {"it-1": "h-1"},
        "schema_versions": {"items": 2, "review_facts": 1,
                            "batch": 1, "job": 1},
    }
    if tamper:
        payload.update(tamper)
    for key in drop:
        payload.pop(key, None)
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


class ReviewBundleIdentityParityTests(unittest.TestCase):
    """CP5 F3: peekseed review-import accepts/rejects EXACTLY the bundles
    peelfeed ``metadata import`` accepts/rejects (same fields, same sources,
    same unpublished/published handling).

    peelfeed's export/current identity reads ``corpus/manifest.json`` only
    (``metadata_bundle._manifest`` / ``_current_identity``): a never-published
    peel has ``build_id=None``/``engine_fingerprint=None`` on BOTH sides, so
    its bundles import cleanly; a published peel binds the current build into
    the manifest. ``build_summary.json`` (the latest candidate build, published
    or not) is never an identity source. Real CLI-vs-app parity on real peels
    is re-verified by the CP5 retest harness; these tests pin the semantics.
    """

    def _import(self, paths, peel, manifest, build, **kwargs):
        from peekseed import review_bundle
        bundle = _bundle(peel, manifest, build,
                         path=paths.root / "review-bundles" /
                         peel["peel_id"] / "b1.tar.gz", **kwargs)
        return review_bundle.import_one(paths, bundle)

    def test_unpublished_peelfeed_style_bundle_is_accepted(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            # The candidate build exists (build_summary.json) but the peel was
            # never published: the peelfeed export manifest carries None.
            self.assertTrue((peel_dir / "extract" / "corpus" /
                             "build_summary.json").is_file())
            self.assertIsNone(manifest.get("build_id"))
            result = self._import(paths, peel, manifest, build)
            self.assertEqual(result["status"], "imported", result)
            self.assertGreaterEqual(result["files"], 1)

    def test_unpublished_candidate_build_identity_is_rejected(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, _peel_dir, manifest, build = _peel_with_review()
            # A bundle claiming the CANDIDATE build identity (build_summary)
            # is stale for peelfeed (current manifest binding is None) and
            # must be stale for peekseed too — the pre-F3 divergence.
            result = self._import(paths, peel, manifest, build, tamper={
                "build_id": build.get("build_id"),
                "engine_fingerprint": build["engine"]["fingerprint"],
            })
            self.assertEqual(result["status"], "stale", result)
            self.assertIn("build_id", result["mismatches"])
            self.assertIn("engine_fingerprint", result["mismatches"])
            self.assertIsNone(result["mismatches"]["build_id"]["current"])

    def test_unpublished_tamper_matrix_matches_peelfeed(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, _peel_dir, manifest, build = _peel_with_review()
            identity = _uncorrected_identity(manifest)
            forged_identity = dict(identity)
            forged_identity["uncorrected_identity"] = "forged"
            cases = [
                ({"peel_id": "other-peel"}, "peel_id"),
                ({"source_sha256": "f" * 64}, "source_sha256"),
                ({"conversion_id": "forged"}, "conversion_id"),
                ({"effective_corrections_hash": "forged"},
                 "effective_corrections_hash"),
                ({"correction_engine_version": "forged"},
                 "correction_engine_version"),
                ({"chunk_sets": ["forged"]}, "chunk_sets"),
                ({"evidence_hashes": {"it-1": "forged"}}, "evidence_hashes"),
                ({"uncorrected_identity": forged_identity},
                 "uncorrected_identity"),
                ({"schema_versions": {"items": 99}}, "schema:items"),
            ]
            for tamper, key in cases:
                result = self._import(paths, peel, manifest, build,
                                      tamper=tamper)
                self.assertEqual(result["status"], "stale",
                                 (tamper, result))
                self.assertIn(key, result["mismatches"], (tamper, result))

    def test_missing_required_fields_are_invalid(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, _peel_dir, manifest, build = _peel_with_review()
            for field in ("peel_id", "schema_versions", "build_id",
                          "engine_fingerprint", "evidence_hashes"):
                result = self._import(paths, peel, manifest, build,
                                      drop=(field,))
                self.assertEqual(result["status"], "invalid",
                                 (field, result))
                self.assertIn(field, result["reason"])

    def test_published_bundle_aligned_to_manifest_is_accepted(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            published = _publish(peel_dir, manifest, build)
            result = self._import(paths, peel, published, build)
            self.assertEqual(result["status"], "imported", result)

    def test_published_rejects_unpublished_style_bundle(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            _publish(peel_dir, manifest, build)
            # The bundle was exported BEFORE the publish (build_id None):
            # peelfeed rejects it as stale now; peekseed must too.
            result = self._import(paths, peel, manifest, build)
            self.assertEqual(result["status"], "stale", result)
            self.assertIn("build_id", result["mismatches"])
            self.assertIn("engine_fingerprint", result["mismatches"])
            self.assertIn("uncorrected_identity", result["mismatches"])

    def test_published_rejects_bundle_from_a_previous_build(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            first = _publish(peel_dir, manifest, build)
            # A second publish rebinds the manifest to a new build.
            newer_build = dict(build)
            newer_build["build_id"] = "b-newer-build"
            newer_build["engine"] = dict(build["engine"])
            newer_build["engine"]["fingerprint"] = "fp-newer"
            _publish(peel_dir, manifest, newer_build)
            result = self._import(paths, peel, first, build)
            self.assertEqual(result["status"], "stale", result)
            self.assertIn("build_id", result["mismatches"])
            self.assertIn("engine_fingerprint", result["mismatches"])

    def test_stale_rejection_writes_nothing_into_the_peel(self):
        with temp_data_root():
            from peekseed import config
            paths = config.paths()
            _dataset, peel, peel_dir, manifest, build = _peel_with_review()
            plan_path = peel_dir / "extract" / "review" / "plan.json"
            before = plan_path.read_bytes()
            result = self._import(paths, peel, manifest, build,
                                  tamper={"build_id": "forged"})
            self.assertEqual(result["status"], "stale", result)
            self.assertEqual(plan_path.read_bytes(), before)
            self.assertFalse((peel_dir / "extract" / "review" /
                              "conversations").exists())


if __name__ == "__main__":
    unittest.main()
