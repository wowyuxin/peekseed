"""CP5 dataset review settings/authorization/auto-review API tests.

No network: the fake peelfeed CLI is the only engine and every job is terminal
at once. Settings/authorizations are additive files under
``datasets/<id>/review/`` (DATASET_SCHEMA untouched).
"""
import json
import os
import time
import unittest
from pathlib import Path

from tests.fixtures import FakePeelfeedCli, make_pdf, make_peel, temp_data_root
from tests.test_review_ocr import (_client, _extracted_review_source,
                                   _peelfeed_cli_env, _review_jobs)


def _dataset_dir(dataset_id):
    from peekseed import config
    return Path(config.paths().datasets_dir) / dataset_id


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wait_call(cli, command, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for call in cli.calls():
            if call.get("argv") and call["argv"][0] == command:
                return call
        time.sleep(0.02)
    raise AssertionError("fake CLI did not receive %s: %r"
                         % (command, cli.commands()))


def _enable_auto(settings=None):
    payload = {
        "auto_review": {"enabled": True},
        "thresholds": {"apply": {"enabled": True, "value": 90},
                       "keep": {"enabled": True, "value": 90}},
        "caps": {"cap_rule_disagree": 50, "cap_uncertain": 70},
        "minimum_validated_cutoff": 50,
    }
    if settings:
        payload.update(settings)
    return payload


class ReviewSettingsTests(unittest.TestCase):
    def test_settings_defaults_validation_and_persistence(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                settings_url = "/api/datasets/%s/review/settings" % dataset_id
                response = client.get(settings_url)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertFalse(payload["settings"]["auto_review"]["enabled"])
                self.assertIn("authorization_missing_or_inactive",
                              payload["auto_trigger"]["blockers"])
                # Invalid threshold is a 400 and does not persist.
                bad = client.put(settings_url, json=_enable_auto({
                    "thresholds": {"apply": {"enabled": True,
                                             "value": 150}}}))
                self.assertEqual(bad.status_code, 400, bad.text)
                saved = client.put(settings_url, json=_enable_auto())
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertTrue(saved.json()["settings"]["auto_review"]
                                ["enabled"])
                self.assertEqual(saved.json()["settings"]
                                 ["minimum_validated_cutoff"], 50)
                path = _dataset_dir(dataset_id) / "review" / "settings.json"
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                # The dataset schema is untouched.
                from peekseed import contracts
                metadata = contracts.read_json(
                    _dataset_dir(dataset_id) / "dataset.json")
                self.assertEqual(metadata["schema"], 4)

    def test_authorization_create_revoke_and_provider_binding(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                _base, dataset_id, _sha = _extracted_review_source(client, root)
                url = "/api/datasets/%s/review/authorizations" % dataset_id
                created = client.post(url, json={
                    "scope": {"text": True, "decision": True},
                    "expires_at": "2099-01-01T00:00:00Z",
                    "note": "CP5 test",
                })
                self.assertEqual(created.status_code, 201, created.text)
                record = created.json()["authorization"]
                self.assertTrue(record["authorization_id"].startswith("auth-"))
                self.assertEqual(record["status"], "active")
                self.assertEqual(created.json()["status"], "active")
                settings = client.get(
                    "/api/datasets/%s/review/settings" % dataset_id).json()
                self.assertEqual(settings["active_authorization"],
                                 record["authorization_id"])
                revoked = client.delete(
                    url + "/" + record["authorization_id"])
                self.assertEqual(revoked.status_code, 200, revoked.text)
                self.assertEqual(revoked.json()["status"], "revoked")
                settings = client.get(
                    "/api/datasets/%s/review/settings" % dataset_id).json()
                self.assertIsNone(settings["active_authorization"])
                self.assertEqual(settings["authorizations"][0]["status"],
                                 "revoked")
                # The append-only ledger keeps both rows.
                path = (_dataset_dir(dataset_id) / "review" /
                        "authorizations.jsonl")
                self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_manual_adjudicate_requires_authorization_and_forwards_request(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, sha = _extracted_review_source(client, root)
                route = base + "/review/adjudicate"
                denied = client.post(route, json={})
                self.assertEqual(denied.status_code, 409, denied.text)
                client.post(
                    "/api/datasets/%s/review/authorizations" % dataset_id,
                    json={"scope": {"text": True}})
                created = client.post(route, json={
                    "scope": {"all_unresolved": True}})
                self.assertEqual(created.status_code, 202, created.text)
                self.assertEqual(created.json()["type"], "adjudicate")
                request = cli.state()["adjudicate_requests"][-1]
                self.assertEqual(request["mode"], "manual")
                self.assertEqual(request["dataset_id"], dataset_id)
                self.assertIn("settings", request)
                self.assertIn("authorization", request)
                self.assertEqual(request["scope"], {"all_unresolved": True})
                jobs = _review_jobs(client, dataset_id, sha)
                self.assertIn("adjudicate", [job.get("type")
                                             for job in jobs.values()])


class AutoReviewTests(unittest.TestCase):
    def _seed_auto(self, dataset_id, *, include_resolution=True):
        peel = make_peel(dataset_id)
        review_dir = Path(peel["peel_dir"]) / "extract" / "review"
        _write_json(review_dir / "items.json", {
            "items": [
                {"id": "it-op", "code": "table_header_absent", "kind": "table",
                 "cluster_id": "cl-1", "asset_id": "table-1",
                 "evidence": {"hash": "h-op"}},
                {"id": "it-keep", "code": "visual_fallback_page_only",
                 "kind": "table", "cluster_id": "cl-2",
                 "asset_id": "table-2", "evidence": {"hash": "h-keep"}},
            ]})
        entries = [{
            "entry_id": "e-op", "item_id": "it-op", "status": "draft",
            "stale": False, "source": "decision_auto",
            "strategy": {"id": "use_header_rows", "version": 1},
            "action": {"type": "operation", "name": "set_header_rows",
                       "params": {"rows": [0]}},
            "evidence": {"hash": "h-op"},
            "origin": {"job_id": "adj-1", "effective_score": 96,
                       "score_kind": "decision_selected_probability",
                       "hard_blocks": []},
        }]
        if include_resolution:
            entries.append({
                "entry_id": "e-keep", "item_id": "it-keep", "status": "draft",
                "stale": False, "source": "vlm_auto",
                "strategy": {"id": "keep_visual_fallback", "version": 1},
                "action": {"type": "resolution",
                           "name": "keep_visual_fallback", "params": {}},
                "evidence": {"hash": "h-keep"},
                "origin": {"job_id": "adj-1", "effective_score": 95,
                           "score_kind": "vlm_self_confidence",
                           "hard_blocks": []},
            })
        _write_json(review_dir / "plan.json", {
            "schema_version": 2, "peel_id": peel["peel_id"],
            "plan_version": 3, "entries": entries})
        _write_json(review_dir / "adjudicate" / "adj-1" / "summary.json", {
            "status": "done",
            "counts": {"manual_pending": 2, "auto_drafts": len(entries)},
            "calls": {"decision": 1, "vlm": 1},
        })
        _write_json(review_dir / "jobs" / "adjudicate-adj-1.json", {
            "job_id": "adj-1", "type": "adjudicate", "status": "done",
            "progress": {"stage": "done", "done": 1, "total": 1},
            "result_ref": "adjudicate/adj-1/summary.json",
        })
        return peel, review_dir

    def test_auto_list_groups_and_status(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                self._seed_auto(dataset_id)
                response = client.get(base + "/review/auto")
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["plan_version"], 3)
                self.assertEqual(payload["counts"]["apply"], 1)
                self.assertEqual(payload["counts"]["keep"], 1)
                self.assertEqual(payload["counts"]["escalate"], 2)
                self.assertEqual(payload["counts"]["by_source"]
                                 ["decision_auto"], 1)
                self.assertEqual(payload["counts"]["by_source"]["vlm_auto"], 1)
                self.assertEqual(len(payload["items"]), 2)
                op = next(item for item in payload["items"]
                          if item["entry_id"] == "e-op")
                self.assertEqual(op["status"], "ready_for_confirmation")
                self.assertFalse(op["keep"])
                self.assertEqual(op["origin"]["effective_score"], 96)
                self.assertEqual(payload["job"]["status"], "done")
                self.assertEqual(len(payload["groups"]), 2)

    def test_auto_confirm_creates_batch_and_mirror_then_revert(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                peel, review_dir = self._seed_auto(dataset_id)
                plan_payload = json.loads(
                    (review_dir / "plan.json").read_text())
                cli.update(plan=plan_payload,
                           peels={peel["peel_id"]: {
                               "peel_id": peel["peel_id"], "state": "ready",
                               "source": {"sha256": _sha},
                           }})
                client.post(
                    "/api/datasets/%s/review/authorizations" % dataset_id,
                    json={"scope": {"text": True}})
                confirmed = client.post(base + "/review/auto/confirm", json={
                    "base_plan_version": 3,
                    "entry_ids": ["e-op", "e-keep"],
                })
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                payload = confirmed.json()
                self.assertTrue(payload["auto_confirmation_id"]
                                .startswith("ac-"))
                self.assertEqual(payload["operation_entry_ids"], ["e-op"])
                self.assertTrue(payload["batch_id"])
                self.assertTrue(payload["job_id"])
                plan = cli.state()["plan"]
                statuses = {entry["entry_id"]: entry["status"]
                            for entry in plan["entries"]}
                self.assertEqual(statuses["e-op"], "confirmed")
                self.assertEqual(statuses["e-keep"], "applied")
                self.assertTrue(any("tag-batch" in call["argv"]
                                    for call in cli.calls()))
                mirror = (_dataset_dir(dataset_id) / "review" /
                          "auto_confirmations.jsonl")
                rows = [json.loads(line) for line in
                        mirror.read_text().splitlines() if line.strip()]
                self.assertEqual(rows[-1]["outcome"], "confirmed")
                self.assertEqual(rows[-1]["auto_confirmation_id"],
                                 payload["auto_confirmation_id"])
                # Repeat confirm with the same id+set is idempotent and does not
                # create another batch/build job.
                applies = len(cli.state().get("apply_requests") or [])
                again = client.post(base + "/review/auto/confirm", json={
                    "base_plan_version": 4,
                    "entry_ids": ["e-op", "e-keep"],
                    "auto_confirmation_id": payload["auto_confirmation_id"],
                })
                self.assertEqual(again.status_code, 200, again.text)
                self.assertTrue(again.json().get("idempotent"))
                self.assertEqual(again.json()["batch_id"],
                                 payload["batch_id"])
                self.assertEqual(len(cli.state().get("apply_requests") or []),
                                 applies)
                reverted = client.post(
                    base + "/review/auto/" + payload["auto_confirmation_id"]
                    + "/revert", json={"base_plan_version": 4})
                self.assertEqual(reverted.status_code, 200, reverted.text)
                rows = [json.loads(line) for line in
                        mirror.read_text().splitlines() if line.strip()]
                self.assertEqual(rows[-1]["outcome"], "reverted")

    def test_auto_trigger_after_extraction_when_enabled(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            env = {
                "PEELFEED_DECISION_BASE": "http://127.0.0.1:9/v1",
                "PEELFEED_DECISION_MODEL": "fake-decision",
                "PEELFEED_DECISION_TOKEN": "test-token",
                "PEELFEED_DECISION_LINK_ID": "env",
                "PEELFEED_LLM_BASE": "http://127.0.0.1:9/v1",
                "PEELFEED_LLM_MODEL": "fake-vlm",
                "PEELFEED_LLM_TOKEN": "test-token",
                "PEELFEED_LLM_LINK_ID": "env",
            }
            previous = {key: os.environ.get(key) for key in env}
            os.environ.update(env)
            try:
                with _peelfeed_cli_env(cli.path):
                    client = _client()
                    # Configure before the extraction so the worker sees it.
                    pdf = make_pdf(root / "manual.pdf")
                    created = client.post(
                        "/api/datasets", data={"name": "Docs"},
                        files=[("files", ("manual.pdf", pdf.read_bytes(),
                                          "application/pdf"))])
                    self.assertEqual(created.status_code, 201, created.text)
                    dataset_id = created.json()["dataset"]["id"]
                    client.put(
                        "/api/datasets/%s/review/settings" % dataset_id,
                        json=_enable_auto())
                    client.post(
                        "/api/datasets/%s/review/authorizations"
                        % dataset_id, json={"scope": {"text": True}})
                    rows = client.get(
                        "/api/datasets/%s/sources" % dataset_id).json()
                    sha = rows["sources"][0]["sha256"]
                    base = "/api/datasets/%s/sources/%s" % (dataset_id, sha)
                    extracted = client.post(base + "/extract", json={})
                    self.assertEqual(extracted.status_code, 200,
                                     extracted.text)
                    call = _wait_call(cli, "adjudicate")
                    request = cli.state()["adjudicate_requests"][-1]
                    self.assertEqual(request["mode"], "auto")
                    self.assertTrue(call["argv"])
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_auto_trigger_skipped_without_decision_provider(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                client.put(
                    "/api/datasets/%s/review/settings" % dataset_id,
                    json=_enable_auto())
                client.post(
                    "/api/datasets/%s/review/authorizations" % dataset_id,
                    json={"scope": {"text": True}})
                settings = client.get(
                    "/api/datasets/%s/review/settings" % dataset_id).json()
                self.assertIn("decision_unavailable",
                              settings["auto_trigger"]["blockers"])
                self.assertNotIn("adjudicate", cli.commands())


class ReviewJobRoutesTests(unittest.TestCase):
    def test_job_resume_and_cancel_routes(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                cli.update(jobs={"adjudicate-1": {
                    "job_id": "adjudicate-1", "type": "adjudicate",
                    "status": "paused", "progress": {},
                    "result_ref": None,
                }})
                resumed = client.post(
                    base + "/review/jobs/adjudicate-1/resume")
                self.assertEqual(resumed.status_code, 202, resumed.text)
                self.assertEqual(resumed.json()["job"]["status"], "queued")
                cli.update(jobs={"adjudicate-1": {
                    "job_id": "adjudicate-1", "type": "adjudicate",
                    "status": "running", "progress": {},
                    "result_ref": None,
                }})
                cancelled = client.post(
                    base + "/review/jobs/adjudicate-1/cancel")
                self.assertEqual(cancelled.status_code, 202, cancelled.text)
                self.assertEqual(cancelled.json()["job"]["status"],
                                 "interrupted")


if __name__ == "__main__":
    unittest.main()


class CalibrationTests(unittest.TestCase):
    def test_clopper_pearson_known_intervals(self):
        from peekseed import calibration
        low, high = calibration.clopper_pearson(0, 56)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.0636, places=3)
        low, high = calibration.clopper_pearson(56, 56)
        self.assertAlmostEqual(low, 0.9364, places=3)
        self.assertEqual(high, 1.0)
        self.assertIsNone(calibration.clopper_pearson(0, 0))

    def test_calibration_route_ingests_and_aggregates(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                peel = make_peel(dataset_id)
                calibration_dir = (Path(peel["peel_dir"]) / "extract" /
                                   "review")
                rows = []
                for index in range(24):
                    rows.append({
                        "schema_version": 1,
                        "record_id": "cal-adj-%02d" % index,
                        "dataset_id": dataset_id,
                        "peel_id": peel["peel_id"],
                        "item_id": "it-%02d" % index,
                        "source": "decision",
                        "partition": "calibration",
                        "modality": "text",
                        "decision": "apply",
                        "observed_correctness": index < 22,
                        "acceptance": True,
                        "harmful_error": False,
                        "lost_labels": 0,
                        "created_at": "2026-11-15T00:00:00Z",
                        "version_bundle_hash": "v1",
                    })
                _write_json(calibration_dir / "calibration.jsonl", rows)
                # write_json writes a JSON list; calibration.jsonl must be JSONL.
                (calibration_dir / "calibration.jsonl").write_text(
                    "\n".join(json.dumps(row) for row in rows) + "\n")
                response = client.get(
                    "/api/datasets/%s/review/calibration" % dataset_id)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["records"], 24)
                self.assertEqual(payload["evaluated"], 24)
                self.assertFalse(payload["empty"])
                bucket = payload["calibration"][0]
                self.assertEqual(bucket["correct"], 22)
                self.assertEqual(bucket["evaluated"], 24)
                self.assertAlmostEqual(bucket["accuracy"], 22 / 24, places=5)
                self.assertIsNotNone(bucket["ci"])
                self.assertLess(bucket["ci"][0], bucket["ci"][1])
                self.assertEqual(payload["breaker"]["state"], "paused_accuracy")
                # Second read is idempotent (no duplicate record rows).
                again = client.get(
                    "/api/datasets/%s/review/calibration" % dataset_id)
                self.assertEqual(again.json()["records"], 24)

    def test_calibration_empty_state_is_explicit(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                _base, dataset_id, _sha = _extracted_review_source(client, root)
                response = client.get(
                    "/api/datasets/%s/review/calibration" % dataset_id)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertTrue(payload["empty"])
                self.assertEqual(payload["records"], 0)
                self.assertEqual(payload["breaker"]["state"], "insufficient")
                self.assertIn("CP1/CP2", payload["note"])
