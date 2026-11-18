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
            extra_member=None, drop=(), ledgers=None):
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
        members = [
            ("review/plan.json", json.dumps({"plan_version": plan_version,
                                             "entries": []})),
            # Realistic identity-stable members: a peelfeed export carries the
            # FULL items/facts payloads, so merging them keeps the current
            # identity (evidence hashes / facts identity) unchanged.
            ("review/items.json", json.dumps(
                {"items": [{"id": "it-1", "evidence": {"hash": "h-1"}}]})),
            ("review/conversations/conv-1.jsonl", json.dumps({"turn": 1})),
            ("corpus/review_facts.json", json.dumps(
                {"schema_version": 1,
                 "uncorrected_identity": manifest.get("uncorrected_identity")})),
            ("ocr/figure-1/evidence.json", json.dumps({"labels": []})),
        ]
        for name, rows in (ledgers or {}).items():
            blob = rows if isinstance(rows, bytes) else "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            members.append(("review/" + name, blob))
        for name, body in members:
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


def _ac_row(ac_id, outcome="confirmed", *, event="", batch_id="",
            revert_of="", entry_ids=None, created_at="2026-11-18T00:00:00Z"):
    row = {"auto_confirmation_id": ac_id, "outcome": outcome,
           "entry_ids": entry_ids or ["e-" + ac_id],
           "created_at": created_at}
    if event:
        row["event"] = event
    if batch_id:
        row["batch_id"] = batch_id
    if revert_of:
        row["revert_of"] = revert_of
    return row


def _un_row(item_id, reason_code, reason, source="manual"):
    return {"item_id": item_id, "reason_code": reason_code,
            "source": source, "reason": reason,
            "created_at": "2026-11-18T00:00:00Z"}


class ReviewBundleAppendMergeTests(unittest.TestCase):
    """CP5 F4: review-import implements the engine's N-3 append-merge.

    ``auto_confirmations.jsonl`` / ``unsupported.jsonl`` are ALWAYS row-level
    append-merged (keep ALL local rows byte-prefix intact, natural-key dedupe,
    append bundle-only rows, never shrink, idempotent) — including
    same-plan-version bundles (``tag_auto_batch`` does not bump plan_version).
    Mirrors peelfeed ``tests/test_metadata_bundle_v2.py`` and the CP5 retest
    adversarial cases; outcomes must match the peelfeed CLI on the same cases
    (re-verified on real peels by the retest harness self-check).
    """

    def _setup(self, root, *, local_rows, bundle_rows, plan_version=1,
               local_un=None, bundle_un=None, extra_local=(),
               ledgers_extra=None):
        from peekseed import config
        paths = config.paths()
        _dataset, peel, peel_dir, manifest, build = _peel_with_review()
        review = peel_dir / "extract" / "review"
        ac_path = review / "auto_confirmations.jsonl"
        ac_path.write_text("".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in local_rows), encoding="utf-8")
        un_path = review / "unsupported.jsonl"
        if local_un is not None:
            un_path.write_text("".join(
                json.dumps(row, ensure_ascii=False) + "\n"
                for row in local_un), encoding="utf-8")
        for name, body in extra_local:
            (review / name).write_text(body, encoding="utf-8")
        ledgers = {"auto_confirmations.jsonl": bundle_rows}
        if bundle_un is not None:
            ledgers["unsupported.jsonl"] = bundle_un
        ledgers.update(ledgers_extra or {})
        bundle = _bundle(peel, manifest, build,
                         path=paths.root / "review-bundles" /
                         peel["peel_id"] / "b1.tar.gz",
                         plan_version=plan_version, ledgers=ledgers)
        return paths, peel, peel_dir, manifest, build, bundle, ac_path, un_path

    def _reimport_variant(self, paths, peel, manifest, build, bundle,
                          ledgers, name="b2.tar.gz", plan_version=1):
        """A DIFFERENT bundle (new sha) with the same ledger rows: exercises
        the merge again past the sha-level idempotency shortcut."""
        variant = _bundle(peel, manifest, build,
                          path=paths.root / "review-bundles" /
                          peel["peel_id"] / name,
                          plan_version=plan_version, ledgers=ledgers,
                          tamper={"audit_marker": name})
        from peekseed import review_bundle
        return review_bundle.import_one(paths, variant)

    def test_older_bundle_never_shrinks_4_to_2(self):
        """The retest F4 scenario: an older bundle with 2 ledger rows must
        keep all 4 local rows (and the newer plan/resolutions/corrections)."""
        with temp_data_root() as root:
            from peekseed import review_bundle
            local = [_ac_row("ac-1"), _ac_row("ac-1", event="batch_linked",
                                              batch_id="batch-1"),
                     _ac_row("ac-2"), _ac_row("ac-2", event="batch_linked",
                                              batch_id="batch-1")]
            bundle_rows = local[:2]
            paths, peel, peel_dir, manifest, build, bundle, ac_path, _un = \
                self._setup(root, local_rows=local, bundle_rows=bundle_rows,
                            plan_version=1,
                            ledgers_extra={
                                "resolutions.jsonl":
                                    [{"resolution_id": "r-old"}],
                                "corrections.jsonl":
                                    [{"correction_id": "c-old"}]},
                            extra_local=[
                                ("resolutions.jsonl",
                                 json.dumps({"resolution_id": "r-new"})
                                 + "\n"),
                                ("corrections.jsonl",
                                 json.dumps({"correction_id": "c-new"})
                                 + "\n")])
            before = ac_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertIn("newer_plan_kept", result["warnings"])
            self.assertEqual(ac_path.read_bytes(), before)
            rows = [json.loads(line) for line in
                    ac_path.read_text().splitlines()]
            self.assertEqual(len(rows), 4)
            review = peel_dir / "extract" / "review"
            self.assertEqual(json.loads((review / "plan.json").read_text())
                             ["plan_version"], 2)
            self.assertIn("r-new", (review / "resolutions.jsonl").read_text())
            self.assertIn("c-new", (review / "corrections.jsonl").read_text())
            # Re-import (new sha, same ledger rows): still 0 ledger changes.
            again = self._reimport_variant(
                paths, peel, manifest, build, bundle,
                {"auto_confirmations.jsonl": bundle_rows})
            self.assertEqual(again["status"], "imported", again)
            self.assertEqual(ac_path.read_bytes(), before)

    def test_same_key_different_payload_local_wins(self):
        with temp_data_root() as root:
            from peekseed import review_bundle
            local = [_ac_row("ac-1"), _ac_row("ac-2")]
            forged = _ac_row("ac-1", entry_ids=["e-forged"],
                             created_at="1999-01-01T00:00:00Z")
            paths, _peel, _peel_dir, _m, _b, bundle, ac_path, _un = \
                self._setup(root, local_rows=local, bundle_rows=[forged])
            before = ac_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertEqual(ac_path.read_bytes(), before)
            rows = [json.loads(line) for line in
                    ac_path.read_text().splitlines()]
            self.assertEqual(rows, local)

    def test_interleaved_revert_and_bundle_only_row_appended(self):
        with temp_data_root() as root:
            from peekseed import review_bundle
            local = [_ac_row("ac-1"), _ac_row("ac-2"),
                     _ac_row("ac-2", outcome="reverted", revert_of="ac-2")]
            row_z = _ac_row("ac-" + "z" * 16, entry_ids=["e-z"])
            bundle_rows = [local[0], row_z]
            paths, peel, peel_dir, manifest, build, bundle, ac_path, _un = \
                self._setup(root, local_rows=local, bundle_rows=bundle_rows)
            prefix = ac_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertTrue(ac_path.read_bytes().startswith(prefix))
            rows = [json.loads(line) for line in
                    ac_path.read_text().splitlines()]
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[-1]["auto_confirmation_id"],
                             "ac-" + "z" * 16)
            self.assertEqual([row["outcome"] for row in rows],
                             ["confirmed", "confirmed", "reverted",
                              "confirmed"])
            again = self._reimport_variant(
                paths, peel, manifest, build, bundle,
                {"auto_confirmations.jsonl": bundle_rows})
            self.assertEqual(again["status"], "imported", again)
            self.assertEqual(len([json.loads(line) for line in
                                  ac_path.read_text().splitlines()]), 4)

    def test_natural_key_includes_event_batch_and_revert(self):
        """confirmed / batch_linked / reverted rows for the SAME ac id are
        distinct natural keys: the bundle's additive rows append, the shared
        confirmed row does not duplicate."""
        with temp_data_root() as root:
            from peekseed import review_bundle
            local = [_ac_row("ac-1")]
            bundle_rows = [_ac_row("ac-1"),
                           _ac_row("ac-1", event="batch_linked",
                                   batch_id="batch-9"),
                           _ac_row("ac-1", outcome="reverted",
                                   revert_of="ac-1")]
            paths, _peel, _pd, _m, _b, bundle, ac_path, _un = \
                self._setup(root, local_rows=local, bundle_rows=bundle_rows)
            prefix = ac_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertTrue(ac_path.read_bytes().startswith(prefix))
            rows = [json.loads(line) for line in
                    ac_path.read_text().splitlines()]
            self.assertEqual([row["outcome"] for row in rows],
                             ["confirmed", "confirmed", "reverted"])
            self.assertEqual(rows[1]["event"], "batch_linked")
            self.assertEqual(rows[1]["batch_id"], "batch-9")

    def test_deleted_local_ledger_recovered_byte_identical(self):
        with temp_data_root() as root:
            from peekseed import review_bundle
            bundle_rows = [_ac_row("ac-1"), _ac_row("ac-2")]
            paths, peel, peel_dir, manifest, build, bundle, ac_path, _un = \
                self._setup(root, local_rows=[_ac_row("ac-9")],
                            bundle_rows=bundle_rows)
            ac_path.unlink()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            expected = "".join(json.dumps(row, ensure_ascii=False) + "\n"
                               for row in bundle_rows).encode("utf-8")
            self.assertEqual(ac_path.read_bytes(), expected)
            again = self._reimport_variant(
                paths, peel, manifest, build, bundle,
                {"auto_confirmations.jsonl": bundle_rows})
            self.assertEqual(again["status"], "imported", again)
            self.assertEqual(ac_path.read_bytes(), expected)

    def test_same_plan_version_bundle_never_shrinks(self):
        """tag_auto_batch appends a batch_linked row WITHOUT bumping
        plan_version: a same-version bundle must not byte-overwrite either."""
        with temp_data_root() as root:
            from peekseed import review_bundle
            local = [_ac_row("ac-1"),
                     _ac_row("ac-1", event="batch_linked",
                             batch_id="batch-1")]
            paths, _peel, _pd, _m, _b, bundle, ac_path, _un = \
                self._setup(root, local_rows=local,
                            bundle_rows=[local[0]], plan_version=2)
            before = ac_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertNotIn("newer_plan_kept", result["warnings"])
            self.assertEqual(ac_path.read_bytes(), before)
            self.assertEqual(len([json.loads(line) for line in
                                  ac_path.read_text().splitlines()]), 2)

    def test_unsupported_ledger_append_merge(self):
        with temp_data_root() as root:
            from peekseed import review_bundle
            local_un = [_un_row("it-a", "unsupported_evidence", "first"),
                        _un_row("it-b", "grid_conflict", "second")]
            bundle_un = [_un_row("it-a", "unsupported_evidence", "first"),
                         _un_row("it-c", "unsupported_evidence", "third")]
            paths, peel, peel_dir, manifest, build, bundle, ac_path, un_path = \
                self._setup(root, local_rows=[_ac_row("ac-1")],
                            bundle_rows=[_ac_row("ac-1")],
                            local_un=local_un, bundle_un=bundle_un)
            prefix = un_path.read_bytes()
            result = review_bundle.import_one(paths, bundle)
            self.assertEqual(result["status"], "imported", result)
            self.assertIn("newer_plan_kept", result["warnings"])
            self.assertTrue(un_path.read_bytes().startswith(prefix))
            rows = [json.loads(line) for line in
                    un_path.read_text().splitlines()]
            self.assertEqual([row["item_id"] for row in rows],
                             ["it-a", "it-b", "it-c"])
            # Same natural key (item_id+reason_code+source+reason), differing
            # non-key payload: local wins (reason IS part of the key).
            forged = [dict(local_un[0], created_at="1999-01-01T00:00:00Z")]
            again = self._reimport_variant(
                paths, peel, manifest, build, bundle,
                {"auto_confirmations.jsonl": [_ac_row("ac-1")],
                 "unsupported.jsonl": forged})
            self.assertEqual(again["status"], "imported", again)
            self.assertEqual(un_path.read_bytes(), prefix + (
                json.dumps(bundle_un[1], ensure_ascii=False) + "\n"
                ).encode("utf-8"))
            # Deleted local unsupported ledger: byte-identical recovery.
            un_path.unlink()
            third = self._reimport_variant(
                paths, peel, manifest, build, bundle,
                {"auto_confirmations.jsonl": [_ac_row("ac-1")],
                 "unsupported.jsonl": bundle_un}, name="b3.tar.gz")
            self.assertEqual(third["status"], "imported", third)
            expected = "".join(json.dumps(row, ensure_ascii=False) + "\n"
                               for row in bundle_un).encode("utf-8")
            self.assertEqual(un_path.read_bytes(), expected)


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
