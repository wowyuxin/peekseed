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
                self.assertTrue(saved.json()["settings"]
                                ["blind_spotcheck"]["enabled"])
                self.assertEqual(saved.json()["settings"]
                                 ["blind_spotcheck"]["count"], 3)
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

    def test_auto_exposes_partial_attempts_when_paused(self):
        """CP5 ⑤: while paused, GET auto exposes the partial summary's
        cumulative attempts/calls additively (summary stays null for old
        readers), plus the per-run cap and resume_count — exactly the data
        the resume decision needs."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                _peel, review_dir = self._seed_auto(dataset_id)
                (review_dir / "adjudicate" / "adj-1" /
                 "summary.json").unlink()
                _write_json(review_dir / "adjudicate" / "adj-1" /
                            "summary.partial.json", {
                                "status": "paused",
                                "attempts": {"decision": 2, "vlm": 2},
                                "calls": {"decision": 1, "vlm": 0},
                                "counts": {"manual_pending": 2},
                            })
                _write_json(review_dir / "jobs" / "adjudicate-adj-1.json", {
                    "job_id": "adj-1", "type": "adjudicate",
                    "status": "paused",
                    "progress": {"stage": "vlm", "done": 1, "total": 2},
                    "result_ref": None,
                    "partial_ref": "adjudicate/adj-1/summary.partial.json",
                    "error": {"code": "paused", "message": "paused"},
                    "payload": {
                        "resume_count": 1,
                        "snapshot": {"limits": {"vlm_max_attempts": 2}},
                    },
                })
                payload = client.get(base + "/review/auto").json()
                # Additive only: the done-state summary field is unchanged.
                self.assertIsNone(payload["summary"])
                job = payload["job"]
                self.assertEqual(job["status"], "paused")
                self.assertEqual(job["attempts"], {"decision": 2, "vlm": 2})
                self.assertEqual(job["calls"], {"decision": 1, "vlm": 0})
                self.assertEqual(job["vlm_max_attempts"], 2)
                self.assertEqual(job["resume_count"], 1)
                self.assertEqual(job["partial_ref"],
                                 "adjudicate/adj-1/summary.partial.json")

    def test_auto_exposes_partial_attempts_when_interrupted(self):
        """CP5 ⑤: crash/cancel-interrupted jobs expose the partial attempts
        too; the conventional partial path works without partial_ref."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                _peel, review_dir = self._seed_auto(dataset_id)
                (review_dir / "adjudicate" / "adj-1" /
                 "summary.json").unlink()
                _write_json(review_dir / "adjudicate" / "adj-1" /
                            "summary.partial.json", {
                                "status": "interrupted",
                                "attempts": {"decision": 1, "vlm": 0},
                                "calls": {"decision": 1, "vlm": 0},
                            })
                _write_json(review_dir / "jobs" / "adjudicate-adj-1.json", {
                    "job_id": "adj-1", "type": "adjudicate",
                    "status": "interrupted",
                    "progress": {"stage": "decision", "done": 0, "total": 2},
                    "result_ref": None, "partial_ref": None,
                    "error": {"code": "interrupted", "message": "died"},
                    "payload": {
                        "resume_count": 0,
                        "snapshot": {"limits": {"vlm_max_attempts": 2000}},
                    },
                })
                job = client.get(base + "/review/auto").json()["job"]
                self.assertEqual(job["attempts"], {"decision": 1, "vlm": 0})
                self.assertEqual(job["calls"], {"decision": 1, "vlm": 0})
                self.assertEqual(job["vlm_max_attempts"], 2000)
                self.assertEqual(job["resume_count"], 0)

    def test_auto_exposes_cumulative_attempts_when_done(self):
        """CP5 ⑤: the done state exposes the cumulative job-level attempts
        across resumes (attempts include retries; calls are successes only)."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                _peel, review_dir = self._seed_auto(dataset_id)
                _write_json(review_dir / "adjudicate" / "adj-1" /
                            "summary.json", {
                                "status": "done",
                                "counts": {"manual_pending": 2,
                                           "auto_drafts": 2},
                                "attempts": {"decision": 7, "vlm": 3},
                                "calls": {"decision": 1, "vlm": 1},
                            })
                _write_json(review_dir / "jobs" / "adjudicate-adj-1.json", {
                    "job_id": "adj-1", "type": "adjudicate", "status": "done",
                    "progress": {"stage": "done", "done": 1, "total": 1},
                    "result_ref": "adjudicate/adj-1/summary.json",
                    "payload": {
                        "resume_count": 2,
                        "snapshot": {"limits": {"vlm_max_attempts": 2}},
                    },
                })
                payload = client.get(base + "/review/auto").json()
                self.assertEqual(payload["summary"]["attempts"],
                                 {"decision": 7, "vlm": 3})
                job = payload["job"]
                self.assertEqual(job["attempts"], {"decision": 7, "vlm": 3})
                self.assertEqual(job["calls"], {"decision": 1, "vlm": 1})
                self.assertEqual(job["resume_count"], 2)
                self.assertEqual(job["vlm_max_attempts"], 2)

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

    def test_auto_spotcheck_records_blind_judgement_then_reveals(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, _sha = _extracted_review_source(client, root)
                peel, review_dir = self._seed_auto(dataset_id)
                cli.update(peels={peel["peel_id"]: {
                    "peel_id": peel["peel_id"], "state": "ready",
                    "source": {"sha256": _sha}}})
                client.put(
                    "/api/datasets/%s/review/settings" % dataset_id,
                    json=_enable_auto())
                spot = client.post(base + "/review/auto/spotcheck", json={
                    "base_plan_version": 3,
                    "seed": "spot-test-1",
                    "entry_ids": ["e-op", "e-keep"],
                    "judgements": [
                        {"entry_id": "e-op", "verdict": "correct"},
                        {"entry_id": "e-keep", "verdict": "incorrect"},
                    ],
                })
                self.assertEqual(spot.status_code, 200, spot.text)
                payload = spot.json()
                self.assertEqual(payload["recorded"]["seed"], "spot-test-1")
                reveal = {row["entry_id"]: row for row in payload["reveal"]}
                self.assertEqual(reveal["e-op"]["source"], "decision_auto")
                self.assertEqual(reveal["e-op"]["decision"], "apply")
                self.assertEqual(reveal["e-op"]["score"], 96)
                self.assertEqual(reveal["e-keep"]["decision"], "keep")
                path = (_dataset_dir(dataset_id) / "review" /
                        "spotchecks.jsonl")
                rows = [json.loads(line) for line in
                        path.read_text().splitlines() if line.strip()]
                self.assertEqual(rows[-1]["judgements"][1]["verdict"],
                                 "incorrect")
                # Every sampled entry needs one valid judgement.
                bad = client.post(base + "/review/auto/spotcheck", json={
                    "base_plan_version": 3, "seed": "s",
                    "entry_ids": ["e-op"],
                    "judgements": [{"entry_id": "e-op",
                                    "verdict": "maybe"}],
                })
                self.assertEqual(bad.status_code, 400, bad.text)
                # Disabling spotcheck refuses the route.
                disabled = dict(_enable_auto())
                disabled["blind_spotcheck"] = {"enabled": False, "count": 3}
                client.put("/api/datasets/%s/review/settings" % dataset_id,
                           json=disabled)
                refused = client.post(base + "/review/auto/spotcheck", json={
                    "base_plan_version": 3, "seed": "s",
                    "entry_ids": ["e-op"],
                    "judgements": [{"entry_id": "e-op",
                                    "verdict": "correct"}],
                })
                self.assertEqual(refused.status_code, 409, refused.text)

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

    def test_auto_retrigger_after_drafts_is_idempotent(self):
        """N-8: the AUTO trigger passes a stable trigger-scoped dedupe_key, so
        re-triggering after the job wrote drafts returns the SAME job with no
        new provider run; a MANUAL request keeps the computed key (a new
        intent -> a new job)."""
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
                    _wait_call(cli, "adjudicate")
                    state = cli.state()
                    first = state["adjudicate_requests"][-1]
                    self.assertEqual(first["mode"], "auto")
                    block = next(
                        row["extraction"] for row in client.get(
                            "/api/datasets/%s/sources" % dataset_id
                        ).json()["sources"] if row["sha256"] == sha)
                    expected_key = "auto:%s:%s:%s" % (
                        dataset_id, block["peel_id"], block["conversion_id"])
                    self.assertEqual(first.get("dedupe_key"), expected_key)
                    first_job = next(
                        job for job in state["jobs"].values()
                        if job.get("type") == "adjudicate")
                    self.assertEqual(state.get("adjudicate_runs"), 1)
                    # The job "wrote drafts": the plan version advances (the
                    # computed B2 key would change; the auto key must not).
                    cli.update(plan={"plan_version": 9, "entries": []})
                    again = client.post(base + "/extract", json={})
                    self.assertEqual(again.status_code, 200, again.text)
                    deadline = time.time() + 10.0
                    while time.time() < deadline:
                        state = cli.state()
                        if len(state.get("adjudicate_requests") or []) >= 2:
                            break
                        time.sleep(0.02)
                    state = cli.state()
                    self.assertGreaterEqual(
                        len(state["adjudicate_requests"]), 2,
                        "the auto trigger did not re-fire")
                    second = state["adjudicate_requests"][-1]
                    self.assertEqual(second["mode"], "auto")
                    self.assertEqual(second.get("dedupe_key"), expected_key)
                    adjudicate_jobs = [
                        job for job in state["jobs"].values()
                        if job.get("type") == "adjudicate"]
                    self.assertEqual(len(adjudicate_jobs), 1,
                                     "auto re-trigger created a new job")
                    self.assertEqual(adjudicate_jobs[0]["job_id"],
                                     first_job["job_id"])
                    self.assertEqual(state.get("adjudicate_runs"), 1,
                                     "auto re-trigger ran the provider again")
                    # MANUAL keeps the computed key: an explicit manual re-run
                    # is a new intent and must create a new job.
                    manual = client.post(base + "/review/adjudicate", json={})
                    self.assertEqual(manual.status_code, 202, manual.text)
                    state = cli.state()
                    third = state["adjudicate_requests"][-1]
                    self.assertEqual(third["mode"], "manual")
                    self.assertNotIn("dedupe_key", third)
                    self.assertNotEqual(manual.json()["job_id"],
                                        first_job["job_id"])
                    self.assertEqual(state.get("adjudicate_runs"), 2)
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

    def _seed_paused_job(self, cli):
        cli.update(jobs={"adjudicate-1": {
            "job_id": "adjudicate-1", "type": "adjudicate",
            "status": "paused", "progress": {}, "result_ref": None,
        }})

    def _resume_call(self, cli):
        return next(call for call in cli.calls()
                    if call["argv"][:2] == ["job", "resume"])

    def test_resume_injects_the_same_provider_env_as_adjudicate(self):
        """CP5 F1: the app resume route injects the SAME decision/VLM child
        env the adjudicate call path injects, so the resumed runner can
        continue provider work (no decision_unavailable/vlm_unavailable)."""
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
                    base, dataset_id, _sha = _extracted_review_source(
                        client, root)
                    client.post(
                        "/api/datasets/%s/review/authorizations" % dataset_id,
                        json={"scope": {"text": True}})
                    adjudicated = client.post(base + "/review/adjudicate",
                                              json={})
                    self.assertEqual(adjudicated.status_code, 202,
                                     adjudicated.text)
                    self._seed_paused_job(cli)
                    resumed = client.post(
                        base + "/review/jobs/adjudicate-1/resume")
                    self.assertEqual(resumed.status_code, 202, resumed.text)
                    calls = cli.calls()
                    adjudicate_call = next(
                        call for call in calls
                        if call["argv"][0] == "adjudicate")
                    resume_call = self._resume_call(cli)
                    self.assertEqual(resume_call["decision_env"],
                                     adjudicate_call["decision_env"])
                    self.assertEqual(resume_call["llm_env"],
                                     adjudicate_call["llm_env"])
                    self.assertEqual(
                        resume_call["decision_env"]["PEELFEED_DECISION_BASE"],
                        "http://127.0.0.1:9/v1")
                    self.assertEqual(
                        resume_call["decision_env"]["PEELFEED_DECISION_TOKEN"],
                        "test-token")
                    self.assertEqual(
                        resume_call["llm_env"]["PEELFEED_LLM_TOKEN"],
                        "test-token")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_resume_keyless_local_decision_and_absent_vlm(self):
        """CP5 F1: keyless-local decision semantics survive resume (base+model
        without a token); an absent VLM provider stays stripped."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            env = {
                "PEELFEED_DECISION_BASE": "http://127.0.0.1:9/v1",
                "PEELFEED_DECISION_MODEL": "local-decision",
                "PEELFEED_DECISION_LINK_ID": "env",
            }
            cleared = [key for key in os.environ
                       if key.startswith(("PEELFEED_DECISION_",
                                          "PEELFEED_LLM_"))]
            previous = {key: os.environ.get(key)
                        for key in set(cleared) | set(env)}
            for key in cleared:
                os.environ.pop(key, None)
            os.environ.update(env)
            try:
                with _peelfeed_cli_env(cli.path):
                    client = _client()
                    base, _dataset_id, _sha = _extracted_review_source(
                        client, root)
                    self._seed_paused_job(cli)
                    resumed = client.post(
                        base + "/review/jobs/adjudicate-1/resume")
                    self.assertEqual(resumed.status_code, 202, resumed.text)
                    resume_call = self._resume_call(cli)
                    self.assertEqual(resume_call["decision_env"], {
                        "PEELFEED_DECISION_BASE": "http://127.0.0.1:9/v1",
                        "PEELFEED_DECISION_MODEL": "local-decision",
                        "PEELFEED_DECISION_TOKEN": "",
                        "PEELFEED_DECISION_LINK_ID": "env",
                    })
                    self.assertEqual(resume_call["llm_env"], {})
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_resume_without_providers_stays_stripped_and_succeeds(self):
        """CP5 F1: with no provider configured the resume env stays stripped
        (the runner degrades honestly); resume never bypasses the allowlist."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            cleared = [key for key in os.environ
                       if key.startswith(("PEELFEED_DECISION_",
                                          "PEELFEED_LLM_"))]
            previous = {key: os.environ[key] for key in cleared}
            for key in cleared:
                os.environ.pop(key, None)
            try:
                with _peelfeed_cli_env(cli.path):
                    client = _client()
                    base, _dataset_id, _sha = _extracted_review_source(
                        client, root)
                    self._seed_paused_job(cli)
                    resumed = client.post(
                        base + "/review/jobs/adjudicate-1/resume")
                    self.assertEqual(resumed.status_code, 202, resumed.text)
                    resume_call = self._resume_call(cli)
                    self.assertEqual(resume_call["decision_env"], {})
                    self.assertEqual(resume_call["llm_env"], {})
            finally:
                os.environ.update(previous)


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
