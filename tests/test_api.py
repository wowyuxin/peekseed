import io
import json
import os
import time
import unittest

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._env = {}
        for key in ("PEEKSEED_AGENT", "PEEKSEED_DATA"):
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

    def _add_link(self, client, **overrides):
        payload = {"role": "embed", "label": "Fake embed", "protocol": "openai",
                   "base_url": "http://127.0.0.1:9/v1", "model": "test-embed",
                   "enabled": True}
        payload.update(overrides)
        response = client.post("/api/links", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["link"]

    def _upload(self, client, name="Docs", pages=None, filename="manual.pdf"):
        pdf = make_pdf(self.root / (filename + ".tmp"), pages=pages)
        data = pdf.read_bytes()
        response = client.post(
            "/api/datasets", data={"name": name},
            files=[("files", (filename, data, "application/pdf"))])
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["dataset"]

    def _index(self, client, dataset_id, link_id=None):
        response = client.post("/api/datasets/%s/index" % dataset_id,
                               json={"link": link_id} if link_id else {})
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["job"]
        for _ in range(100):
            job = client.get("/api/jobs/%s" % job_id).json()
            if job["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "done", job)
        return job

    def test_boot_health_ui_and_settings(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            self.assertEqual(client.get("/health").json()["ok"], True)
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"peekseed", page.content.lower())
            self.assertEqual(page.headers["cache-control"], "no-cache")
            settings = client.get("/api/settings").json()
            self.assertEqual(settings["title"], "peekseed")
            updated = client.put("/api/settings",
                                 json={"title": "My docs", "examples": ["question?"]})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["title"], "My docs")

    def test_links_crud_through_the_api(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            link = self._add_link(client, base_url=fake.openai_base,
                                  api_key="top-secret")
            self.assertTrue(link["api_key_set"])
            self.assertNotIn("top-secret", json.dumps(link))
            listed = client.get("/api/links").json()
            self.assertEqual(len(listed["links"]), 1)
            updated = client.put("/api/links/%s" % link["id"],
                                 json={"label": "Renamed"})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["link"]["label"], "Renamed")
            tested = client.post("/api/links/%s/test" % link["id"]).json()
            self.assertTrue(tested["ok"], tested)
            status = client.get("/api/links/status").json()
            self.assertTrue(status["links"][link["id"]]["ok"])
            self.assertEqual(client.delete("/api/links/%s" % link["id"]).status_code, 200)
            self.assertEqual(client.delete("/api/links/%s" % link["id"]).status_code, 404)

    def test_upload_index_and_search(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, base_url=fake.openai_base)
            dataset = self._upload(client, pages=[
                "Widget installation. The widget is mounted with two screws.",
                "Widget operation. Press the green button to start it."])
            self.assertIn("manual.pdf", dataset["sources"])
            self._index(client, dataset["id"])
            listed = client.get("/api/datasets").json()["datasets"]
            self.assertEqual(listed[0]["chunks"] > 0, True)
            self.assertEqual(len(listed[0]["indexes"]), 1)
            response = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "green button"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertTrue(body["results"])
            first = body["results"][0]
            self.assertTrue(first["pdf_url"].startswith("/pdf/%s/" % dataset["id"]))
            self.assertEqual(first["rank"], 1)
            # the stream variant reports the same result
            stream = client.post("/api/search/stream", json={
                "dataset": dataset["id"], "query": "green button"})
            events = [json.loads(line) for line in stream.text.strip().splitlines()]
            result = [event for event in events if "result" in event][0]["result"]
            self.assertEqual(len(result["results"]), len(body["results"]))
            self.assertIn(result.get("history_id"), client.get("/api/history").json()
                          and [meta["id"] for meta in client.get("/api/history").json()["entries"]])

    def test_search_without_an_embed_link_is_a_clear_409(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client)
            response = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "anything"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("embedding link", response.json()["detail"])

    def test_ask_stream_and_history(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            client = self._client()
            self._add_link(client, base_url=fake.openai_base)
            self._add_link(client, role="answer", label="Fake answer",
                           base_url=fake.openai_base, model="test-answer-model")
            dataset = self._upload(client, pages=[
                "Widget operation. Press the green button to start the widget."])
            self._index(client, dataset["id"])
            fake.set_default_chat([{"content": "Press the green button to start it [1].",
                                    "finish": "stop"}])
            search = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "start widget"}).json()
            response = client.post("/api/ask/stream", json={
                "dataset": dataset["id"], "question": "How do I start it?",
                "results": search["results"], "history_id": search["history_id"]})
            self.assertEqual(response.status_code, 200, response.text)
            events = [json.loads(line) for line in response.text.strip().splitlines()]
            kinds = [event.get("type") for event in events]
            self.assertIn("context", kinds)
            self.assertIn("text", kinds)
            self.assertIn("done", kinds)
            done = [event for event in events if event.get("type") == "done"][-1]
            self.assertIn("green button", done["answer"])
            history = client.get("/api/history").json()["entries"]
            self.assertTrue(history)
            entry_id = history[0]["id"]
            entry = client.get("/api/history/%s" % entry_id).json()
            self.assertEqual(entry["kind"], "llm")
            self.assertEqual(entry["runs"][0]["dataset"], dataset["id"])

    def test_agent_stream_is_opt_in_and_researches(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            self.root = root
            os.environ["PEEKSEED_AGENT"] = "1"
            client = self._client()
            self._add_link(client, base_url=fake.openai_base)
            self._add_link(client, role="answer", label="Fake answer",
                           base_url=fake.openai_base, model="test-answer-model")
            dataset = self._upload(client, pages=[
                "Widget operation. Press the green button to start the widget."])
            self._index(client, dataset["id"])
            search = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "start widget"}).json()
            fake.set_chat_script([
                [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                  "function": {"name": "grep",
                                               "arguments": '{"pattern": "green"}'}}],
                  "finish": "tool_calls"}],
                [{"content": "Press the green button [2].", "finish": "stop"}],
            ])
            response = client.post("/api/agent/stream", json={
                "dataset": dataset["id"], "question": "How do I start it?",
                "results": search["results"]})
            self.assertEqual(response.status_code, 200, response.text)
            events = [json.loads(line) for line in response.text.strip().splitlines()]
            kinds = [event.get("type") for event in events]
            for kind in ("context", "step", "tool_call", "tool_result", "done"):
                self.assertIn(kind, kinds, events)
            done = [event for event in events if event.get("type") == "done"][-1]
            self.assertIn("green button", done["answer"])
            self.assertIn("ledger", done)
            self.assertEqual(done["ledger"][0]["n"], 1)

    def test_agent_is_off_by_default(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            os.environ.pop("PEEKSEED_AGENT", None)
            client = self._client()
            response = client.post("/api/agent/stream", json={
                "dataset": "docs", "question": "anything"})
            self.assertEqual(response.status_code, 404)

    def test_pdf_serving_and_traversal_guard(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            self._add_link(client, base_url=fake.openai_base)
            dataset = self._upload(client)
            served = client.get("/pdf/%s/manual.pdf" % dataset["id"])
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.headers["content-type"], "application/pdf")
            for bad in ("..%2F..%2Fetc%2Fpasswd", "../config/links.json"):
                response = client.get("/pdf/%s/%s" % (dataset["id"], bad))
                self.assertIn(response.status_code, (400, 404))

    def test_dataset_delete(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client)
            self.assertEqual(client.delete("/api/datasets/%s"
                                           % dataset["id"]).status_code, 200)
            self.assertEqual(client.delete("/api/datasets/%s"
                                           % dataset["id"]).status_code, 404)


class ProbeQueryGateTests(unittest.TestCase):
    """Query-time probe gate: a swapped embedder must 409, never compare spaces.

    Audit §6.10 gate 7. The gate is verified for search, ask and research; the
    stored probe is what makes the check cheap enough to run on every query.
    """

    def setUp(self):
        self._agent = os.environ.get("PEEKSEED_AGENT")

    def tearDown(self):
        if self._agent is None:
            os.environ.pop("PEEKSEED_AGENT", None)
        else:
            os.environ["PEEKSEED_AGENT"] = self._agent

    def _client(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module
        app_module.reset_state()
        return TestClient(app_module.create_app())

    def _add_link(self, client, base_url):
        response = client.post("/api/links", json={
            "role": "embed", "label": "Fake embed", "protocol": "openai",
            "base_url": base_url, "model": "test-embed", "enabled": True})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["link"]

    def _upload_and_index(self, client, root):
        pdf = make_pdf(root / "manual.pdf", pages=[
            "Widget installation. The widget is mounted with two screws.",
            "Widget operation. Press the green button to start it."])
        response = client.post(
            "/api/datasets", data={"name": "Docs"},
            files=[("files", ("manual.pdf", pdf.read_bytes(), "application/pdf"))])
        self.assertEqual(response.status_code, 201, response.text)
        dataset = response.json()["dataset"]
        job = client.post("/api/datasets/%s/index" % dataset["id"],
                          json={}).json()["job"]
        for _ in range(100):
            state = client.get("/api/jobs/%s" % job).json()
            if state["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "done", state)
        return dataset

    def _stream_error(self, response):
        """The error event of a streamed endpoint.

        ``/api/search/stream`` reports failures as ``{"error": {...}}`` while
        ask/research emit ``{"type": "error", ...}``; both are error events.
        """
        events = [json.loads(line) for line in response.text.strip().splitlines()]
        for event in events:
            if event.get("type") == "error":
                return event
            if isinstance(event.get("error"), dict):
                return {"status": event["error"].get("status"),
                        "detail": event["error"].get("detail")}
        self.fail("no error event in %r" % (events,))

    def test_swapped_embedder_409s_search_ask_and_research(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake, \
                FakeModelServer(dim=16) as swapped:
            os.environ["PEEKSEED_AGENT"] = "1"
            client = self._client()
            link = self._add_link(client, fake.openai_base)
            dataset = self._upload_and_index(client, root)
            payload = {"dataset": dataset["id"], "query": "green button"}
            ok = client.post("/api/search", json=payload)
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertIn("probe_sec", ok.json()["timings"])

            # Same link id and model string, different server/weights: only the
            # probe can tell that this is a different embedding space.
            updated = client.put("/api/links/%s" % link["id"],
                                 json={"base_url": swapped.openai_base})
            self.assertEqual(updated.status_code, 200, updated.text)
            bad = client.post("/api/search", json=payload)
            self.assertEqual(bad.status_code, 409, bad.text)
            self.assertIn("embedding space", bad.json()["detail"])
            self.assertIn("reindex", bad.json()["detail"].lower())

            streamed = client.post("/api/search/stream", json=payload)
            self.assertEqual(self._stream_error(streamed).get("status"), 409,
                             streamed.text)

            asked = client.post("/api/ask/stream", json={
                "dataset": dataset["id"], "query": "green button",
                "question": "How do I start it?"})
            self.assertEqual(self._stream_error(asked).get("status"), 409,
                             asked.text)

            researched = client.post("/api/agent/stream", json={
                "dataset": dataset["id"], "question": "How do I start it?"})
            self.assertEqual(self._stream_error(researched).get("status"), 409,
                             researched.text)

    def test_index_without_a_probe_is_not_silently_compared(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            client = self._client()
            self._add_link(client, fake.openai_base)
            dataset = self._upload_and_index(client, root)
            from pathlib import Path

            from peekseed import config
            space_dir = Path(config.paths().datasets_dir) / dataset["id"] / "index"
            space = next(space_dir.iterdir())
            generation = (space / "generations"
                          / (space / "CURRENT").read_text().strip())
            (generation / "probe.npy").unlink()
            client = self._client()  # reload the store from disk
            bad = client.post("/api/search", json={
                "dataset": dataset["id"], "query": "green button"})
            self.assertEqual(bad.status_code, 409, bad.text)
            self.assertIn("no stored probe", bad.json()["detail"])


if __name__ == "__main__":
    unittest.main()
