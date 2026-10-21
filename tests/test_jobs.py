"""Job records, cancellation/resume, writer locks and the workspace API.

The API's job endpoint is the persisted record under ``.peekseed/jobs`` — these
tests exercise that contract end-to-end through the app (not the module in
isolation): a second index request must 409 while a build holds the collection
lock, DELETE latches a cancel that the builder honours between batches, a
restart marks crash-left jobs interrupted, and resume continues the same record.
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root

LONG_PAGE = (
    "Widget installation and operation. " * 20
    + "\n\n"
    + "Service intervals and spare parts. " * 20
)


class JobApiBase(unittest.TestCase):
    def setUp(self):
        self._env = {}
        for key in ("PEEKSEED_DATA", "PEEKSEED_AGENT"):
            self._env[key] = os.environ.get(key)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _client(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module

        app_module.reset_state()
        return TestClient(app_module.create_app())

    def _app(self):
        from peekseed import app as app_module

        return app_module.create_app()

    def _add_link(self, client, fake):
        response = client.post("/api/links", json={
            "role": "embed", "label": "Fake embed", "protocol": "openai",
            "base_url": fake.openai_base, "model": "test-embed",
            "enabled": True})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["link"]

    def _upload(self, client, pages, name="Docs"):
        pdf = make_pdf(self.root / "manual.pdf.tmp", pages=pages)
        response = client.post(
            "/api/datasets", data={"name": name},
            files=[("files", ("manual.pdf", pdf.read_bytes(), "application/pdf"))])
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["dataset"]

    def _wait(self, client, job_id, wanted=("succeeded", "failed", "cancelled",
                                            "interrupted"), timeout=20.0):
        deadline = time.time() + timeout
        state = {}
        while time.time() < deadline:
            state = client.get("/api/jobs/%s" % job_id).json()
            if state.get("state") in wanted:
                return state
            time.sleep(0.02)
        self.fail("job %s did not reach %r: %r" % (job_id, wanted, state))

    def _wait_for_progress(self, client, job_id, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = client.get("/api/jobs/%s" % job_id).json()
            if state.get("state") == "running" and \
                    (state.get("progress") or {}).get("done", 0) > 0:
                return state
            time.sleep(0.02)
        self.fail("job %s never committed a batch" % job_id)


class JobApiTests(JobApiBase):
    def _slow_embeds(self, delay=0.25):
        from peekseed.ingest import index as index_module

        original = index_module.embed_texts

        def wrapper(link, texts, **kwargs):
            time.sleep(delay)
            return original(link, texts, **kwargs)

        return mock.patch.object(index_module, "embed_texts", wrapper)

    def test_one_writer_per_collection_then_cancel_and_resume(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, fake)
            dataset = self._upload(client, [LONG_PAGE] * 90)
            with self._slow_embeds():
                first = client.post("/api/datasets/%s/index" % dataset["id"],
                                    json={})
                self.assertEqual(first.status_code, 202, first.text)
                job_id = first.json()["job"]
                self._wait_for_progress(client, job_id)
                # Second writer while the first holds the lock: fail fast, 409.
                second = client.post("/api/datasets/%s/index" % dataset["id"],
                                     json={})
                self.assertEqual(second.status_code, 409, second.text)
                self.assertIn("already being written", second.json()["detail"])
                cancelled = client.delete("/api/jobs/%s" % job_id)
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["job"]["cancel_requested"])
            state = self._wait(client, job_id, wanted=("cancelled", "succeeded"))
            self.assertEqual(state["state"], "cancelled", state)
            self.assertEqual(state["progress"]["done"] > 0, True, state)

            # The record is listed and the previous generation still serves (a
            # fresh dataset has no previous generation; the state is cancelled).
            jobs = client.get("/api/jobs").json()["jobs"]
            self.assertIn(job_id, [job["id"] for job in jobs])
            self.assertEqual(
                client.get("/api/jobs?state=cancelled&collection=%s"
                           % dataset["id"]).json()["jobs"][0]["id"], job_id)

            resumed = client.post("/api/jobs/%s/resume" % job_id)
            self.assertEqual(resumed.status_code, 202, resumed.text)
            self.assertEqual(resumed.json()["job"], job_id)
            done = self._wait(client, job_id)
            self.assertEqual(done["state"], "succeeded", done)
            self.assertEqual(done["progress"]["fraction"], 1.0)
            self.assertEqual(done["progress"]["done"], done["progress"]["total"])

    def test_cancel_is_refused_on_terminal_jobs(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, fake)
            dataset = self._upload(client, ["One short page about widgets."])
            posted = client.post("/api/datasets/%s/index" % dataset["id"],
                                 json={})
            job_id = posted.json()["job"]
            done = self._wait(client, job_id)
            self.assertEqual(done["state"], "succeeded", done)
            refused = client.delete("/api/jobs/%s" % job_id)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertIn("already succeeded", refused.json()["detail"])
            self.assertEqual(client.delete("/api/jobs/job-missing").status_code,
                             404)

    def test_restart_marks_crash_left_jobs_interrupted_and_resume_works(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, fake)
            dataset = self._upload(client, ["One short page about widgets."])
            posted = client.post("/api/datasets/%s/index" % dataset["id"],
                                 json={})
            self._wait(client, posted.json()["job"])

            from peekseed import config, contracts
            jobs_dir = config.paths().jobs_dir
            record = contracts.new_job("index", collection=dataset["id"],
                                       job_id="job-crash")
            record["state"] = "running"
            record["started_at"] = contracts.utc_now()
            contracts.write_json_atomic(jobs_dir / "job-crash.json", record)

            from fastapi.testclient import TestClient

            # Entering the app runs the lifespan, which reconciles the records.
            with TestClient(self._app()) as restarted:
                crashed = restarted.get("/api/jobs/job-crash").json()
                self.assertEqual(crashed["state"], "interrupted", crashed)
                resumed = restarted.post("/api/jobs/job-crash/resume")
                self.assertEqual(resumed.status_code, 202, resumed.text)
                done = self._wait(restarted, "job-crash")
                self.assertEqual(done["state"], "succeeded", done)

    def test_stale_lock_record_does_not_block_a_new_build(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, fake)
            dataset = self._upload(client, ["One short page about widgets."])
            from peekseed import config
            locks = config.paths().jobs_dir / "locks"
            locks.mkdir(parents=True, exist_ok=True)
            (locks / (dataset["id"] + ".lock")).write_text(json.dumps({
                "pid": 999999, "collection": dataset["id"],
                "started_at": int(time.time()) - 60, "job": "job-stale"}))
            posted = client.post("/api/datasets/%s/index" % dataset["id"],
                                 json={})
            self.assertEqual(posted.status_code, 202, posted.text)
            done = self._wait(client, posted.json()["job"])
            self.assertEqual(done["state"], "succeeded", done)

    def test_job_ids_are_validated(self):
        with temp_data_root() as root:
            self.root = root
            client = self._client()
            self.assertEqual(client.get("/api/jobs/bad!id").status_code, 400)
            self.assertEqual(client.get("/api/jobs/job-missing").status_code, 404)


class WorkspaceApiTests(JobApiBase):
    def test_workspace_collections_and_sources(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, fake)
            dataset = self._upload(client, [
                "Widget installation. The widget is mounted with two screws.",
                "Widget operation. Press the green button to start it."])
            # Before indexing the collection is visible but not indexed.
            before = client.get("/api/workspace").json()
            self.assertEqual(before["collections"][dataset["id"]]["status"],
                             "not_indexed", before)
            posted = client.post("/api/datasets/%s/index" % dataset["id"],
                                 json={})
            self._wait(client, posted.json()["job"])

            report = client.get("/api/workspace").json()
            served = Path(root).resolve()
            self.assertEqual(report["workspace"]["root"], str(served))
            self.assertEqual(report["workspace"]["documents_dir"],
                             str(served / "documents"))
            self.assertEqual(report["workspace"]["derived_dir"], str(served))
            collection = report["collections"][dataset["id"]]
            self.assertEqual(collection["status"], "indexed", collection)
            self.assertEqual(collection["chunks"] > 0, True, collection)
            self.assertEqual(collection["sources"], 1, collection)
            self.assertEqual(report["counts"]["collections"], 1)

            collections = client.get("/api/collections").json()
            self.assertEqual(collections["collections"][0]["id"], dataset["id"])
            self.assertEqual(collections["collections"][0]["status"], "indexed")

            sources = client.get(
                "/api/datasets/%s/sources" % dataset["id"]).json()
            self.assertEqual(sources["dataset"], dataset["id"])
            row = sources["sources"][0]
            self.assertEqual(row["name"], "manual.pdf")
            self.assertEqual(row["present"], True)
            self.assertEqual(len(row["sha256"]), 64)
            self.assertEqual(
                row["path"],
                str(served / "datasets" / dataset["id"] / "original" / "manual.pdf"))
            self.assertEqual(
                client.get("/api/datasets/nope/sources").status_code, 404)


class StreamShapeTests(JobApiBase):
    def test_every_stream_reports_the_same_error_event_shape(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            # No embed link: search/stream must answer a typed error event.
            streamed = client.post("/api/search/stream",
                                   json={"dataset": "nope", "query": "x"})
            events = [json.loads(line) for line in
                      streamed.text.strip().splitlines()]
            errors = [event for event in events if event.get("type") == "error"]
            self.assertEqual(len(errors), 1, events)
            self.assertEqual(errors[0]["status"], 404, events)
            self.assertNotIn("error", errors[0])
