"""M3 CP0.5: decision links, the TypeSafe adapter and the synthetic probe.

Everything here is offline: the "provider" is a local HTTP stub (or the pure
envelope functions). No real decision endpoint is ever called in tests.
"""

import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.fixtures import temp_data_root


def _decision_link(**overrides):
    data = {"role": "decision", "label": "Fake decision",
            "protocol": "typesafe",
            "base_url": "http://127.0.0.1:9",
            "model": "jev-1.14.0", "enabled": True}
    data.update(overrides)
    return data


class LinkStoreDecisionTests(unittest.TestCase):
    def _links(self):
        from peekseed import links
        return links

    def test_decision_crud_and_multiple_enabled(self):
        with temp_data_root():
            links = self._links()
            saved = links.create(_decision_link())
            self.assertEqual(saved["role"], "decision")
            self.assertEqual(saved["protocol"], "typesafe")
            # No mutual exclusion: a second enabled decision link stays on.
            second = links.create(_decision_link(label="Other decision"))
            self.assertTrue(second["enabled"])
            self.assertTrue(links.get(saved["id"])["enabled"])
            self.assertEqual(links.default_decision_link()["id"], saved["id"])
            # Disable/enable round-trip.
            links.set_enabled(saved["id"], False)
            self.assertEqual(links.default_decision_link()["id"], second["id"])
            links.set_enabled(saved["id"], True)
            self.assertEqual(links.default_decision_link()["id"], saved["id"])
            # Decision links never join the stage mutual-exclusion groups.
            self.assertEqual(links.active("decision"), None)

    def test_decision_validation_rules(self):
        with temp_data_root():
            links = self._links()
            bad = [
                _decision_link(model=""),                       # pinned model
                _decision_link(chunk={"target_chars": 1,       # no chunk sizes
                                      "max_chars": 2,
                                      "image_cap": 1}),
                _decision_link(protocol="openai"),              # wrong protocol
                _decision_link(api_key_env="PEEKSEED_TOKEN"),   # secret rule
            ]
            for case in bad:
                with self.assertRaises(links.LinkValidationError, msg=case):
                    links.create(case)

    def test_mask_flags_moving_aliases_but_not_pinned_ids(self):
        with temp_data_root():
            links = self._links()
            pinned = links.mask(links.create(_decision_link()))
            self.assertIsNone(pinned["model_warning"])
            self.assertTrue(pinned["capabilities"]["decision"])
            self.assertEqual(pinned["chunk"], None)
            alias = links.mask(links.create(
                _decision_link(label="Alias link", model="jev-latest")))
            self.assertIn("moving alias", alias["model_warning"])

    def test_old_reader_rejects_a_file_with_decision_links(self):
        """Rollback semantics (setup-contract §2): the deployed reader
        (dbb8ac8, strict v1 role allowlist) must refuse a links.json that
        contains a decision link -- so mixed-version rollbacks fail closed."""
        repo = Path(__file__).resolve().parents[1]
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "dbb8ac8"],
            capture_output=True)
        self.assertEqual(archive.returncode, 0, archive.stderr.decode())
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout,
                           check=True)
            home = Path(tmp) / "home"
            shared = Path(tmp) / "shared"
            (home / "config").mkdir(parents=True)
            shared.mkdir()
            with open(home / "config" / "links.json", "w", encoding="utf-8") as f:
                json.dump({"version": 1,
                           "links": [_decision_link(id="fake-decision")]}, f)
            script = (
                "import peekseed.links as links\n"
                "try:\n"
                "    links.load()\n"
                "except links.LinkConfigError as exc:\n"
                "    print('REJECTED: %s' % exc)\n"
                "else:\n"
                "    print('ACCEPTED')\n"
            )
            run = subprocess.run(
                [sys.executable, "-c", script],
                # cwd=tmp: ``python -c`` puts the cwd FIRST on sys.path, which
                # would shadow the archived old package with the worktree.
                cwd=tmp,
                env={"PYTHONPATH": tmp, "PEEKSEED_HOME": str(home),
                     "PEEKSEED_SHARED_DIR": str(shared),
                     "PATH": os.environ.get("PATH", "")},
                capture_output=True)
            out = run.stdout.decode()
            self.assertIn("REJECTED", out, out + run.stderr.decode())
            self.assertIn("role", out)


class EnvelopeTests(unittest.TestCase):
    def _decision(self):
        from peekseed import decision
        return decision

    def _good_payload(self):
        return {
            "model": "jev-1.14.0",
            "answers": {
                "probe_mentions_dog": {"type": "noul", "noul": 0.97},
                "probe_animal": {
                    "type": "choice", "choice": "dog",
                    "confidence": 0.9,
                    "probabilities": {"fox": 0.2, "dog": 0.7, "cat": 0.1},
                },
            },
            "usage": {"input_tokens": 42, "output_tokens": 7},
        }

    def test_text_and_image_probe_fixtures_are_frozen(self):
        decision = self._decision()
        text = decision.text_probe_fixture()
        self.assertEqual(sorted(text["questions"]),
                         ["probe_animal", "probe_mentions_dog"])
        self.assertNotIn("assets", text)
        image = decision.image_probe_fixture()
        self.assertIn("assets", image)
        ref = decision.IMAGE_PROBE_REFERENCE
        self.assertEqual(image["state"], {"probe_image": ref})
        raw = base64.b64decode(image["assets"][ref]["data_base64"])
        self.assertTrue(raw.startswith(b"\x89PNG"))
        width, height = struct.unpack(">II", raw[16:24])
        self.assertEqual((width, height), (280, 280))
        self.assertGreater(len(zlib.decompress(
            raw[raw.find(b"IDAT") + 4:raw.rfind(b"IEND") - 8])), 0)

    def test_validate_response_accepts_a_good_payload(self):
        decision = self._decision()
        result = decision.validate_response(
            self._good_payload(), decision.text_probe_fixture()["questions"])
        self.assertEqual(result["resolved_model"], "jev-1.14.0")
        self.assertEqual(result["usage"]["input_tokens"], 42)

    def test_validate_response_rejects_every_illegal_shape(self):
        decision = self._decision()
        questions = decision.text_probe_fixture()["questions"]

        def broken(**answers_and_rest):
            payload = self._good_payload()
            payload.update(answers_and_rest)
            return payload

        cases = [
            ("not an object", "x"),
            ("missing model", broken(model="")),
            ("answers not dict", broken(answers=[1])),
            ("missing answer", broken(answers={
                "probe_mentions_dog": {"type": "noul", "noul": 0.5}})),
            ("extra answer", broken(answers=dict(
                self._good_payload()["answers"],
                surprise={"type": "noul", "noul": 0.5}))),
            ("wrong answer type", broken(answers=dict(
                self._good_payload()["answers"],
                probe_mentions_dog={"type": "choice"}))),
            ("choice outside criteria", broken(answers=dict(
                self._good_payload()["answers"],
                probe_animal={"type": "choice", "choice": "wolf",
                              "confidence": 0.9,
                              "probabilities": {"fox": 0.5, "dog": 0.5,
                                                "cat": 0.0}}))),
            ("probability keys mismatch", broken(answers=dict(
                self._good_payload()["answers"],
                probe_animal={"type": "choice", "choice": "dog",
                              "confidence": 0.9,
                              "probabilities": {"dog": 1.0}}))),
            ("probability out of range", broken(answers=dict(
                self._good_payload()["answers"],
                probe_mentions_dog={"type": "noul", "noul": 1.5}))),
            ("probabilities do not sum to 1", broken(answers=dict(
                self._good_payload()["answers"],
                probe_animal={"type": "choice", "choice": "dog",
                              "confidence": 0.9,
                              "probabilities": {"fox": 0.1, "dog": 0.2,
                                                "cat": 0.1}}))),
            ("confidence missing", broken(answers=dict(
                self._good_payload()["answers"],
                probe_animal={"type": "choice", "choice": "dog",
                              "probabilities": {"fox": 0.2, "dog": 0.7,
                                                "cat": 0.1}}))),
            ("usage missing", broken(usage=None)),
            ("usage not integer", broken(usage={
                "input_tokens": 1.5, "output_tokens": 0})),
        ]
        for name, payload in cases:
            with self.assertRaises(decision.DecisionError, msg=name) as ctx:
                decision.validate_response(payload, questions)
            self.assertEqual(ctx.exception.code,
                             decision.DECISION_BAD_RESPONSE, name)

    def test_probe_png_stays_within_provider_bounds(self):
        decision = self._decision()
        self.assertGreaterEqual(decision.ERROR_IMAGE_SIDE,
                                decision.IMAGE_SIDE_MIN)
        self.assertLessEqual(decision.ERROR_IMAGE_SIDE,
                             decision.IMAGE_SIDE_MAX)


class _StubHandler(BaseHTTPRequestHandler):
    behavior = "ok"

    def log_message(self, *args):  # keep test output clean
        pass

    def do_POST(self):
        behavior = type(self).behavior
        if behavior.startswith("http_"):
            status = int(behavior.split("_", 1)[1])
            body = json.dumps({"error": "stub",
                               "secret": "do-not-echo"}).encode()
            self.send_response(status)
            self._reply(body)
            return
        if behavior == "slow":
            import time
            time.sleep(2.0)
            self.send_response(200)
            self._reply(b"{}")
            return
        if behavior == "badjson":
            self.send_response(200)
            self._reply(b"not json")
            return
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        answers = {}
        for qid, question in (request.get("questions") or {}).items():
            qtype = (question or {}).get("type")
            if qtype == "noul":
                answers[qid] = {"type": "noul", "noul": 0.97}
            elif qtype == "choice":
                options = sorted((question.get("criteria") or {}))
                probabilities = {option: 0.01 for option in options}
                probabilities[options[0]] = round(
                    1.0 - 0.01 * (len(options) - 1), 2)
                answers[qid] = {"type": "choice", "choice": options[0],
                                "confidence": 0.93,
                                "probabilities": probabilities}
        if behavior == "sum" and answers:
            qid = sorted(answers)[0]
            if answers[qid].get("type") == "choice":
                answers[qid]["probabilities"] = {
                    option: 0.1 for option in answers[qid]["probabilities"]}
        if behavior == "missing_answer" and answers:
            del answers[sorted(answers)[0]]
        model = "jev-9.9.9" if behavior == "drift" else "jev-1.14.0"
        body = json.dumps({"model": model, "answers": answers,
                           "usage": {"input_tokens": 120,
                                     "output_tokens": 9}}).encode()
        self.send_response(200)
        self._reply(body)

    def _reply(self, body):
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProbeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _StubHandler.behavior = "ok"

    def _decision(self):
        from peekseed import decision
        return decision

    def test_text_probe_ok_and_headers(self):
        decision = self._decision()
        seen = {}

        class Recorder(_StubHandler):
            def do_POST(self):
                seen["images_header"] = self.headers.get(
                    "TypeSafe-Danger-Zone-Images")
                seen["auth"] = self.headers.get("Authorization")
                super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            link = {"base_url": "http://127.0.0.1:%d" % server.server_address[1],
                    "model": "jev-1.14.0"}
            result = decision.probe(link, api_key="test-key")
            self.assertTrue(result["ok"])
            self.assertTrue(result["model_match"])
            self.assertEqual(result["probe_version"], decision.PROBE_VERSION)
            self.assertIsNone(seen["images_header"])
            self.assertEqual(seen["auth"], "Bearer test-key")
            # No key -> no Authorization header (local endpoint allowed).
            result = decision.probe(link, api_key="")
            self.assertTrue(result["ok"])
            self.assertIsNone(seen["auth"])
        finally:
            server.shutdown()
            server.server_close()

    def test_image_probe_sets_the_danger_zone_header(self):
        decision = self._decision()
        seen = {}

        class Recorder(_StubHandler):
            def do_POST(self):
                seen["images_header"] = self.headers.get(
                    "TypeSafe-Danger-Zone-Images")
                super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            link = {"base_url": "http://127.0.0.1:%d" % server.server_address[1],
                    "model": "jev-1.14.0"}
            result = decision.probe(link, images=True, api_key="k")
            self.assertTrue(result["images"])
            self.assertEqual(seen["images_header"], "true")
        finally:
            server.shutdown()
            server.server_close()

    def test_failure_classification(self):
        decision = self._decision()
        link = {"base_url": self.base, "model": "jev-1.14.0"}
        secret = "sk-secretkey-0123456789"

        def expect(code, behavior):
            _StubHandler.behavior = behavior
            with self.assertRaises(decision.DecisionError) as ctx:
                decision.probe(link, api_key=secret, timeout=0.5)
            self.assertEqual(ctx.exception.code, code, behavior)
            # Error strings never carry the provider body or the key.
            self.assertNotIn("do-not-echo", ctx.exception.detail)
            self.assertNotIn(secret, ctx.exception.detail)

        expect(decision.DECISION_RATE_LIMITED, "http_429")
        expect(decision.DECISION_UNAVAILABLE, "http_401")
        expect(decision.DECISION_UNAVAILABLE, "http_500")
        expect(decision.DECISION_BAD_RESPONSE, "badjson")
        expect(decision.DECISION_BAD_RESPONSE, "sum")
        expect(decision.DECISION_BAD_RESPONSE, "missing_answer")
        expect(decision.DECISION_TIMEOUT, "slow")

    def test_model_drift_is_reported_not_hidden(self):
        decision = self._decision()
        _StubHandler.behavior = "drift"
        result = decision.probe({"base_url": self.base,
                                 "model": "jev-1.14.0"}, api_key="k")
        self.assertTrue(result["ok"])
        self.assertFalse(result["model_match"])
        self.assertEqual(result["resolved_model"], "jev-9.9.9")


class DecisionApiTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module
        app_module.reset_state()
        return TestClient(app_module.create_app())

    def test_decision_link_api_roundtrip(self):
        with temp_data_root():
            client = self._client()
            response = client.post("/api/links", json=_decision_link())
            self.assertEqual(response.status_code, 201, response.text)
            link = response.json()["link"]
            self.assertEqual(link["role"], "decision")
            self.assertTrue(link["capabilities"]["decision"])
            self.assertIsNone(link["model_warning"])
            listed = client.get("/api/links").json()["links"]
            self.assertEqual([row["id"] for row in listed], [link["id"]])
            # Use/not-use works like any non-stage link.
            response = client.post("/api/links/%s/use" % link["id"],
                                   json={"enabled": False})
            self.assertFalse(response.json()["link"]["enabled"])
            response = client.delete("/api/links/%s" % link["id"])
            self.assertEqual(response.status_code, 200)

    def test_test_route_calls_the_synthetic_probe(self):
        with temp_data_root():
            client = self._client()
            server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                payload = _decision_link(
                    base_url="http://127.0.0.1:%d" % server.server_address[1])
                link = client.post("/api/links", json=payload).json()["link"]
                response = client.post("/api/links/%s/test" % link["id"])
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["resolved_model"], "jev-1.14.0")
                self.assertTrue(body["model_match"])
                self.assertIn("latency_ms", body)
                # Image opt-in accepted on decision links only.
                response = client.post("/api/links/%s/test" % link["id"],
                                       json={"images": True})
                self.assertTrue(response.json()["ok"])
                self.assertTrue(response.json()["images"])
            finally:
                server.shutdown()
                server.server_close()

    def test_test_payload_is_decision_only_and_strict(self):
        with temp_data_root():
            client = self._client()
            link = client.post("/api/links", json=_decision_link()).json()["link"]
            answer = client.post("/api/links", json={
                "role": "answer", "label": "Chat", "protocol": "openai",
                "base_url": "http://127.0.0.1:9/v1", "model": "m",
                "enabled": True}).json()["link"]
            response = client.post("/api/links/%s/test" % answer["id"],
                                   json={"images": True})
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/links/%s/test" % link["id"],
                                   json={"surprise": 1})
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/links/%s/test" % link["id"],
                                   json={"images": "yes"})
            self.assertEqual(response.status_code, 400)

    def test_links_status_never_calls_decision_providers(self):
        with temp_data_root():
            client = self._client()
            # Port 9 (discard) is closed: any implicit call would fail loudly.
            link = client.post(
                "/api/links",
                json=_decision_link(base_url="http://127.0.0.1:9")).json()["link"]
            response = client.get("/api/links/status")
            self.assertEqual(response.status_code, 200)
            row = response.json()["links"][link["id"]]
            self.assertTrue(row["decision"])
            self.assertTrue(row["configured"])
            self.assertFalse(row["key_set"])
            self.assertIn("not probed", row["detail"])

    def test_test_failure_is_a_sanitized_200(self):
        with temp_data_root():
            client = self._client()
            _StubHandler.behavior = "http_401"
            server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                payload = _decision_link(
                    base_url="http://127.0.0.1:%d" % server.server_address[1])
                link = client.post("/api/links", json=payload).json()["link"]
                response = client.post("/api/links/%s/test" % link["id"])
                body = response.json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["code"], "decision_unavailable")
                self.assertIn("HTTP 401", body["detail"])
                self.assertNotIn("do-not-echo", body["detail"])
            finally:
                _StubHandler.behavior = "ok"
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
