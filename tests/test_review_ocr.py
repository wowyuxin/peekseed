"""CP4 (M2.1) on-demand OCR seam tests: API routes, fake CLI and review payloads.

The peekseed stream owns the proxy + UI only (peelfeed owns OCR): these tests
drive the frozen routes against :class:`tests.fixtures.FakePeelfeedCli` and the
schema-2 review payload fixtures. They cover the plan §6 matrix rows #25–#30 on
the peekseed side:

* #25 client argv/stdin/env forwarding + error mapping (``test_peelfeed.py``);
* #26 ``POST …/review/ocr/check`` and ``…/ocr/refresh``: 200/202/503/409/400,
  strict request validation, the persisted ``type=ocr`` job descriptor and
  restart re-attachment (never rerun);
* #27/#28 served Review UI controls and behaviour (``test_review_ui.py``);
* #29 no-OCR fallback (capabilities ``ocr=false`` keeps the strategies grayed
  with a reason while the review/report still work);
* #30 an OCR refresh changes no conversion identity / ``needs_reindex`` and the
  dataset schema stays 4 (only the later OCR batch publish does).

No network: the fake CLI is the only engine and every job is terminal at once.
"""

import contextlib
import hashlib
import os
import time
import unittest

from tests.fixtures import (EMBED_CHUNK, REVIEW_OCR, REVIEW_OCR_EMPTY,
                            REVIEW_OCR_LOW_CONFIDENCE, REVIEW_OCR_UNAVAILABLE,
                            REVIEW_OCR_UNCOVERED, FakeModelServer,
                            FakePeelfeedCli, make_pdf, make_peel,
                            temp_data_root)

_PEELFEED_ENV_KEYS = ("PEEKSEED_PEELFEED_CLI", "PEEKSEED_PEELFEED_POLL")


@contextlib.contextmanager
def _peelfeed_cli_env(cli_path):
    """Point the peelfeed adapter at one fake CLI and reset its caches."""
    from peekseed import peelfeed

    previous = {key: os.environ.get(key) for key in _PEELFEED_ENV_KEYS}
    for key in _PEELFEED_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ["PEEKSEED_PEELFEED_CLI"] = str(cli_path)
    os.environ["PEEKSEED_PEELFEED_POLL"] = "0.01"
    peelfeed.reset_cache()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        peelfeed.reset_cache()


def _client():
    from fastapi.testclient import TestClient
    from peekseed import app as app_module

    app_module.reset_state()
    return TestClient(app_module.create_app())


def _wait_extracted(client, dataset_id, sha, timeout=15.0):
    deadline = time.time() + timeout
    state = ""
    while time.time() < deadline:
        rows = client.get(
            "/api/datasets/%s/sources" % dataset_id).json()["sources"]
        row = next((item for item in rows if item["sha256"] == sha), {})
        state = (row.get("extraction") or {}).get("state")
        if state in ("extracted", "failed", "stale"):
            return state
        time.sleep(0.02)
    raise AssertionError("source %s did not extract: %r" % (sha[:8], state))


def _extracted_review_source(client, root):
    """Upload + extract one PDF through the fake CLI; return the source base."""
    pdf = make_pdf(root / "manual.pdf")
    created = client.post(
        "/api/datasets", data={"name": "Docs"},
        files=[("files", ("manual.pdf", pdf.read_bytes(), "application/pdf"))])
    assert created.status_code == 201, created.text
    dataset_id = created.json()["dataset"]["id"]
    rows = client.get(
        "/api/datasets/%s/sources" % dataset_id).json()["sources"]
    sha = rows[0]["sha256"]
    base = "/api/datasets/%s/sources/%s" % (dataset_id, sha)
    extracted = client.post(base + "/extract", json={})
    assert extracted.status_code == 200, extracted.text
    assert _wait_extracted(client, dataset_id, sha) == "extracted"
    return base, dataset_id, sha


def _review_jobs(client, dataset_id, sha):
    rows = client.get(
        "/api/datasets/%s/sources" % dataset_id).json()["sources"]
    row = next((item for item in rows if item["sha256"] == sha), {})
    return (row.get("extraction") or {}).get("review_jobs") or {}


def _wait_review_job(client, base, job_id, timeout=10.0):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        response = client.get(base + "/review/jobs/" + job_id)
        assert response.status_code == 200, response.text
        state = response.json()
        if state.get("status") in ("done", "failed", "interrupted"):
            return state
        time.sleep(0.02)
    raise AssertionError("review job %s did not finish: %r" % (job_id, state))


class ReviewOcrRouteTests(unittest.TestCase):
    """Plan §6 #26: the two frozen OCR routes and their validation/mapping."""

    def test_ocr_check_route_returns_the_probe_payload(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                response = client.post(base + "/review/ocr/check", json={})
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertTrue(payload["available"])
                self.assertEqual(payload["engine"], "rapidocr@1.4.4")
                for key in ("provider", "probe", "checked_at", "error"):
                    self.assertIn(key, payload)
                self.assertIsNone(payload["error"])
                self.assertEqual(payload["probe"]["labels"], 0)
                # Frozen stdin: provider is always the env-config object.
                self.assertEqual(cli.state()["ocr_check_requests"][-1],
                                 {"provider": {}})
                # A provider object is forwarded verbatim (CLI/testing override).
                override = {"command": ["/tmp/provider"], "engine":
                            "rapidocr@1.4.4"}
                forwarded = client.post(base + "/review/ocr/check",
                                        json={"provider": override})
                self.assertEqual(forwarded.status_code, 200, forwarded.text)
                self.assertEqual(cli.state()["ocr_check_requests"][-1],
                                 {"provider": override})
                # The check is short: no job and no corpus write.
                self.assertEqual(_review_jobs(client, _dataset_id, _sha), {})
                self.assertNotIn("ocr-refresh",
                                 [call["argv"][0] for call in cli.calls()])

    def test_ocr_check_error_mapping(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                cli.update(ocr_check_error={"code": "ocr_unavailable",
                                            "message": "no OCR provider"})
                unavailable = client.post(base + "/review/ocr/check", json={})
                self.assertEqual(unavailable.status_code, 503,
                                 unavailable.text)
                self.assertIn("ocr_unavailable",
                              unavailable.json()["detail"])
                cli.update(ocr_check_error={"code": "ocr_config_invalid",
                                            "message": "bad engine"})
                invalid = client.post(base + "/review/ocr/check", json={})
                self.assertEqual(invalid.status_code, 400, invalid.text)
                cli.update(ocr_check_error={"code": "ocr_timeout",
                                            "message": "probe timed out"})
                timeout = client.post(base + "/review/ocr/check", json={})
                self.assertEqual(timeout.status_code, 502, timeout.text)

    def test_ocr_check_request_validation(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                route = base + "/review/ocr/check"
                for body in ({"provider": ["not", "an", "object"]},
                             {"provider": "ocr"},
                             {"item_ids": ["it-1"]},
                             {"provider": {}, "mode": "vlm"}):
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400, response.text)
                # An absent body means "use the env config" (provider {}).
                empty = client.post(route)
                self.assertEqual(empty.status_code, 200, empty.text)
                self.assertEqual(cli.state()["ocr_check_requests"][-1],
                                 {"provider": {}})

    def test_ocr_refresh_creates_a_job_and_persists_the_descriptor(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, sha = _extracted_review_source(client, root)
                response = client.post(base + "/review/ocr/refresh",
                                       json={"item_ids": ["it-figure-1",
                                                          "it-figure-2"]})
                self.assertEqual(response.status_code, 202, response.text)
                created = response.json()
                self.assertEqual(set(created),
                                 {"job_id", "status", "result_ref"})
                self.assertEqual(created["status"], "queued")
                self.assertIsNone(created["result_ref"])
                self.assertTrue(created["job_id"].startswith("ocr-"))
                # Frozen stdin: item ids + provider {} always.
                self.assertEqual(
                    cli.state()["ocr_refresh_requests"][-1],
                    {"item_ids": ["it-figure-1", "it-figure-2"],
                     "provider": {}})
                # The descriptor is stored in extraction.review_jobs (schema 4,
                # no migration) for restart re-attachment.
                descriptors = _review_jobs(client, dataset_id, sha)
                self.assertIn(created["job_id"], descriptors)
                descriptor = descriptors[created["job_id"]]
                self.assertEqual(set(descriptor),
                                 {"type", "created_at", "batch_id"})
                self.assertEqual(descriptor["type"], "ocr")
                self.assertIsNone(descriptor["batch_id"])
                self.assertTrue(descriptor["created_at"])
                # GET …/review/jobs/{id} re-attaches by id (poll, never rerun).
                job = _wait_review_job(client, base, created["job_id"])
                self.assertEqual(job["type"], "ocr")
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["result_ref"],
                                 "jobs/" + created["job_id"] + ".result.json")
                summary = cli.state()["ocr_job_results"][created["job_id"]]
                self.assertEqual(summary["requested_item_ids"],
                                 ["it-figure-1", "it-figure-2"])
                self.assertEqual(summary["counts"]["requested"], 2)
                self.assertEqual(summary["counts"]["refreshed"], 2)
                # The job is only created once; polling never re-submits.
                refresh_calls = [call for call in cli.calls()
                                 if call["argv"][0] == "ocr-refresh"]
                self.assertEqual(len(refresh_calls), 1)
                job_shows = [call for call in cli.calls()
                             if call["argv"][0] == "job"]
                self.assertEqual(len(job_shows), 1)

    def test_ocr_refresh_request_validation(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                route = base + "/review/ocr/refresh"
                cases = [
                    {},
                    {"item_ids": []},
                    {"item_ids": "it-1"},
                    {"item_ids": [""]},
                    {"item_ids": ["  "]},
                    {"item_ids": [7]},
                    {"item_ids": ["it-1", "it-1"]},
                    {"item_ids": ["it-1"], "provider": "env"},
                    {"item_ids": ["it-1"], "tier": "R2"},
                    {"item_ids": ["it-1"], "scope": "peel"},
                    {"item_ids": ["it-1"], "operations": []},
                ]
                for body in cases:
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 400,
                                     "%r -> %s" % (body, response.text))
                # No job was created for any rejected request.
                self.assertEqual(_review_jobs(client, _dataset_id, _sha), {})
                self.assertNotIn("ocr-refresh",
                                 [call["argv"][0] for call in cli.calls()])

    def test_ocr_refresh_error_mapping(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                route = base + "/review/ocr/refresh"
                # not_ready -> 409 (no current build; no job is created).
                cli.update(ocr_not_ready=True)
                not_ready = client.post(route, json={"item_ids": ["it-1"]})
                self.assertEqual(not_ready.status_code, 409, not_ready.text)
                self.assertIn("not_ready", not_ready.json()["detail"])
                # conflict -> 409.
                cli.update(ocr_not_ready=False,
                           ocr_refresh_error={"code": "conflict",
                                              "message": "review lock"})
                conflict = client.post(route, json={"item_ids": ["it-1"]})
                self.assertEqual(conflict.status_code, 409, conflict.text)
                # ocr_unavailable -> 503.
                cli.update(ocr_refresh_error={"code": "ocr_unavailable",
                                              "message": "provider missing"})
                unavailable = client.post(route, json={"item_ids": ["it-1"]})
                self.assertEqual(unavailable.status_code, 503,
                                 unavailable.text)
                # Other OCR codes -> 502.
                cli.update(ocr_refresh_error={"code": "ocr_bad_output",
                                              "message": "bad stdout"})
                bad = client.post(route, json={"item_ids": ["it-1"]})
                self.assertEqual(bad.status_code, 502, bad.text)

    def test_ocr_routes_require_review_schema_2(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                cli.update(capabilities={"review_schema": 1})
                from peekseed import peelfeed

                peelfeed.invalidate_handshake()
                for route, body in ((base + "/review/ocr/check", {}),
                                    (base + "/review/ocr/refresh",
                                     {"item_ids": ["it-1"]})):
                    response = client.post(route, json=body)
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("review schema", response.json()["detail"])

    def test_ocr_refresh_keeps_dataset_schema_4(self):
        """No dataset schema migration: the schema-4 descriptor accepts ocr."""
        import json

        from peekseed import config, contracts

        self.assertEqual(contracts.DATASET_SCHEMA, 4)
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, dataset_id, sha = _extracted_review_source(client, root)
                response = client.post(base + "/review/ocr/refresh",
                                       json={"item_ids": ["it-1"]})
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["job_id"]
                dataset_file = (config.paths().datasets_dir / dataset_id
                                / "dataset.json")
                raw = json.loads(dataset_file.read_text(encoding="utf-8"))
                self.assertEqual(raw["schema"], contracts.DATASET_SCHEMA)
                row = next(item for item in raw["sources"]
                           if item["sha256"] == sha)
                descriptor = row["extraction"]["review_jobs"][job_id]
                self.assertEqual(descriptor["type"], "ocr")
                self.assertIsNone(descriptor["batch_id"])
                # The schema-4 extraction block still validates on read.
                validated = contracts.validate_dataset(raw)
                self.assertEqual(validated["schema"], 4)
                self.assertIn(job_id,
                              validated["sources"][0]["extraction"]
                              ["review_jobs"])


def _wait_index_job(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = client.get("/api/jobs/%s" % job_id).json()
        if state.get("state") in ("succeeded", "failed", "cancelled",
                                  "interrupted"):
            return state
        time.sleep(0.02)
    raise AssertionError("index job %s did not finish: %r" % (job_id, state))


def _collection_status(client, dataset_id):
    rows = client.get("/api/collections").json()["collections"]
    row = next((item for item in rows if item["id"] == dataset_id), {})
    return str(row.get("status") or "")


class ReviewOcrPayloadTests(unittest.TestCase):
    """Plan §6 #28: the review payload/coverage/evidence shapes the UI renders."""

    def test_review_payload_carries_ocr_capabilities_coverage_and_evidence(self):
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            cli.update(review=REVIEW_OCR)
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                response = client.get(base + "/review?group=cluster")
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                caps = payload["capabilities"]
                self.assertEqual(caps["review_schema"], 2)
                self.assertTrue(caps["ocr"])
                self.assertTrue(caps["ocr_configured"])
                self.assertIsNone(caps["ocr_detail"])
                self.assertEqual(caps["ocr_engine"], "rapidocr@1.4.4")
                self.assertEqual(caps["ocr_min_confidence"], 0.6)
                self.assertEqual(caps["ocr_status"]["available"], True)
                self.assertIsNone(caps["ocr_status"]["error"])
                self.assertFalse(caps["vlm"])
                coverage = payload["ocr"]
                for key in ("items", "with_evidence", "empty",
                            "low_confidence", "uncovered", "invalid_evidence",
                            "evidence_files", "engine", "min_confidence"):
                    self.assertIn(key, coverage)
                self.assertEqual(coverage["items"], 1)
                self.assertEqual(coverage["with_evidence"], 1)
                self.assertEqual(coverage["uncovered"], 0)
                self.assertEqual(coverage["evidence_files"], 1)
                figure = next(item for item in payload["items"]
                              if item["code"] == "no_native_labels")
                evidence = figure["evidence"]["ocr_evidence"]
                self.assertEqual(len(evidence), 1)
                entry = evidence[0]
                for key in ("path", "crop_ref", "image_sha256", "engine",
                            "params_sha256", "evidence_sha256", "label_count",
                            "max_confidence", "min_confidence", "created_at"):
                    self.assertIn(key, entry)
                self.assertEqual(entry["engine"], "rapidocr@1.4.4")
                self.assertEqual(entry["label_count"], 3)
                self.assertEqual(entry["max_confidence"], 0.99)
                labels = entry["labels"]
                self.assertEqual([label["text"] for label in labels],
                                 ["Compute Express Link", "CXL 3.1",
                                  "Figure 1-1"])
                self.assertTrue(all(label["confidence"] >= 0.6
                                    for label in labels))
                ocr_option = next(option for option in figure["strategies"]
                                  if option["id"] == "use_ocr_labels")
                self.assertTrue(ocr_option["available"])
                self.assertIsNone(ocr_option["unavailable_reason"])
                # The single-item route returns the same additive evidence keys.
                item = client.get(
                    base + "/review/items/" + figure["id"])
                self.assertEqual(item.status_code, 200, item.text)
                self.assertEqual(
                    item.json()["item"]["evidence"]["ocr_evidence"][0]
                    ["label_count"], 3)

    def test_ocr_refresh_updates_the_coverage_after_the_job(self):
        """#28: refresh -> 202 -> poll done -> review refetched, coverage updated."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            cli.update(review=REVIEW_OCR_UNCOVERED,
                       ocr_review_after_refresh=REVIEW_OCR,
                       ocr_job_counts={"requested": 1, "refreshed": 1,
                                       "cache_hits": 0, "failed": 0,
                                       "empty": 0, "low_confidence": 0})
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                before = client.get(base + "/review?group=cluster").json()
                self.assertEqual(before["ocr"]["uncovered"], 1)
                self.assertEqual(before["ocr"]["with_evidence"], 0)
                figure = next(item for item in before["items"]
                              if item["code"] == "no_native_labels")
                self.assertEqual(figure["evidence"]["ocr_evidence"], [])
                ocr_option = next(option for option in figure["strategies"]
                                  if option["id"] == "use_ocr_labels")
                self.assertFalse(ocr_option["available"])
                self.assertIn("先运行局部 OCR",
                              ocr_option["unavailable_reason"])
                refreshed = client.post(
                    base + "/review/ocr/refresh",
                    json={"item_ids": [figure["id"]]})
                self.assertEqual(refreshed.status_code, 202, refreshed.text)
                job = _wait_review_job(client, base,
                                       refreshed.json()["job_id"])
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["type"], "ocr")
                after = client.get(base + "/review?group=cluster").json()
                self.assertEqual(after["ocr"]["uncovered"], 0)
                self.assertEqual(after["ocr"]["with_evidence"], 1)
                self.assertEqual(after["ocr"]["evidence_files"], 1)
                figure_after = next(item for item in after["items"]
                                    if item["code"] == "no_native_labels")
                self.assertEqual(len(
                    figure_after["evidence"]["ocr_evidence"]), 1)
                self.assertEqual(figure_after["evidence"]["ocr_evidence"][0]
                                 ["max_confidence"], 0.99)
                self.assertEqual(
                    next(option for option in figure_after["strategies"]
                         if option["id"] == "use_ocr_labels")["available"],
                    True)
                # The frozen result summary is the source of the UI counts.
                summary = cli.state()["ocr_job_results"][
                    refreshed.json()["job_id"]]
                self.assertEqual(summary["counts"],
                                 {"requested": 1, "refreshed": 1,
                                  "cache_hits": 0, "failed": 0, "empty": 0,
                                  "low_confidence": 0})
                # The UI also shows empty/low-confidence variants without
                # inventing client-side state: the coverage payload drives it.
                cli.update(review=REVIEW_OCR_EMPTY)
                empty = client.get(base + "/review").json()
                self.assertEqual(empty["ocr"]["empty"], 1)
                cli.update(review=REVIEW_OCR_LOW_CONFIDENCE)
                low = client.get(base + "/review").json()
                self.assertEqual(low["ocr"]["low_confidence"], 1)
                self.assertLess(
                    low["items"][1]["evidence"]["ocr_evidence"][0]
                    ["max_confidence"],
                    low["capabilities"]["ocr_min_confidence"])

    def test_no_ocr_fallback_keeps_review_and_report_working(self):
        """#29: capabilities ocr=false -> grayed strategy, check 503, review OK."""
        with temp_data_root() as root:
            cli = FakePeelfeedCli(root / "fake")
            cli.update(review=REVIEW_OCR_UNAVAILABLE,
                       ocr_check_error={"code": "ocr_unavailable",
                                        "message":
                                        "PEELFEED_OCR_PROVIDER is not set"})
            with _peelfeed_cli_env(cli.path):
                client = _client()
                base, _dataset_id, _sha = _extracted_review_source(client, root)
                payload = client.get(base + "/review").json()
                caps = payload["capabilities"]
                self.assertFalse(caps["ocr"])
                self.assertFalse(caps["ocr_configured"])
                self.assertIn("OCR", caps["ocr_detail"])
                figure = next(item for item in payload["items"]
                              if item["code"] == "no_native_labels")
                options = {option["id"]: option
                           for option in figure["strategies"]}
                self.assertFalse(options["use_ocr_labels"]["available"])
                self.assertIn("局部 OCR",
                              options["use_ocr_labels"]["unavailable_reason"])
                self.assertTrue(options["keep_original_figure"]["available"])
                # The review report job still works without OCR.
                report = client.post(base + "/review/reports", json={})
                self.assertEqual(report.status_code, 202, report.text)
                self.assertEqual(
                    _wait_review_job(client, base,
                                     report.json()["job_id"])["status"],
                    "done")
                # A check on the unconfigured provider is the frozen 503.
                check = client.post(base + "/review/ocr/check", json={})
                self.assertEqual(check.status_code, 503, check.text)
                self.assertIn("ocr_unavailable", check.json()["detail"])

    def test_ocr_refresh_does_not_invalidate_an_existing_index(self):
        """#30: OCR evidence refresh changes no conversion identity/reindex."""
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            cli = FakePeelfeedCli(root / "fake")
            with _peelfeed_cli_env(cli.path):
                client = _client()
                pdf = make_pdf(root / "manual.pdf")
                created = client.post(
                    "/api/datasets", data={"name": "Docs"},
                    files=[("files", ("manual.pdf", pdf.read_bytes(),
                                      "application/pdf"))])
                self.assertEqual(created.status_code, 201, created.text)
                dataset_id = created.json()["dataset"]["id"]
                sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
                peel = make_peel(dataset_id, pages=[
                    "Widget installation. The widget is mounted with two "
                    "screws.",
                    "Widget operation. Press the green button to start it."])
                cli.update(
                    peels={peel["peel_id"]: {
                        "peel_id": peel["peel_id"], "state": "ready",
                        "source": {"name": "manual.pdf", "sha256": sha,
                                   "pages": 2},
                        "profile": "generic",
                        "conversion_id": peel["conversion_id"],
                        "counts": {"pages": 2, "chunks": 2},
                        "validation": {"errors": [], "review_items": 0},
                        "review_items": 0, "error": None,
                    }},
                    review=REVIEW_OCR_UNCOVERED,
                    ocr_review_after_refresh=REVIEW_OCR)
                link = client.post("/api/links", json={
                    "role": "embed", "label": "Fake embed",
                    "protocol": "openai", "base_url": fake.openai_base,
                    "model": "test-embed", "enabled": True,
                    "chunk": dict(EMBED_CHUNK)})
                self.assertEqual(link.status_code, 201, link.text)
                index = client.post(
                    "/api/datasets/%s/index" % dataset_id, json={})
                self.assertEqual(index.status_code, 202, index.text)
                job = _wait_index_job(client, index.json()["job"])
                self.assertEqual(job["state"], "succeeded", job)
                self.assertEqual(_collection_status(client, dataset_id),
                                 "indexed")
                base = "/api/datasets/%s/sources/%s" % (dataset_id, sha)
                rows = client.get(
                    "/api/datasets/%s/sources" % dataset_id).json()["sources"]
                extraction_before = dict(rows[0]["extraction"])
                self.assertEqual(extraction_before["conversion_id"],
                                 peel["conversion_id"])
                # The OCR evidence refresh writes extract/ocr/ only: no
                # conversion/build change, so the index stays valid.
                refreshed = client.post(base + "/review/ocr/refresh",
                                        json={"item_ids":
                                              ["it-8b7a6c5d4e3f2a10"]})
                self.assertEqual(refreshed.status_code, 202, refreshed.text)
                self.assertEqual(
                    _wait_review_job(client, base,
                                     refreshed.json()["job_id"])["status"],
                    "done")
                rows = client.get(
                    "/api/datasets/%s/sources" % dataset_id).json()["sources"]
                extraction_after = dict(rows[0]["extraction"])
                self.assertEqual(extraction_after["conversion_id"],
                                 extraction_before["conversion_id"])
                self.assertEqual(extraction_after["state"], "extracted")
                self.assertEqual(extraction_after["review_jobs"].keys(),
                                 extraction_before["review_jobs"].keys()
                                 | {refreshed.json()["job_id"]})
                self.assertEqual(_collection_status(client, dataset_id),
                                 "indexed")
                info = client.get("/api/datasets").json()["datasets"]
                row = next(item for item in info if item["id"] == dataset_id)
                self.assertEqual(len(row["indexes"]), 1)
                # The OCR refresh never runs the publish/reconcile path.
                commands = [call["argv"][0] for call in cli.calls()]
                for command in ("publish", "discard", "rollback"):
                    self.assertNotIn(command, commands)


if __name__ == "__main__":
    unittest.main()
