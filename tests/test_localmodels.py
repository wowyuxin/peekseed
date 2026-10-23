"""Tests for the optional machine-local model process control."""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from peekseed import localmodels


def write_script(directory, body="exit 0", name="serve.sh"):
    script = Path(directory) / name
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def write_config(directory, stages):
    path = Path(directory) / "local_models.json"
    path.write_text(json.dumps({"version": 1, "stages": stages}))
    path.chmod(0o600)
    return path


def embed_stage(script, port=8000, models=None):
    return {"script": str(script),
            "health_url": "http://127.0.0.1:%d/health" % port,
            "models": models or [{"id": "m2b", "label": "Model 2B"}]}


class HealthServer(BaseHTTPRequestHandler):
    """A tiny /health endpoint; any other path is a 404."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            raw = json.dumps({"status": "ok", "model": "served-model",
                              "dim": 2048}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class LocalModelsTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(localmodels.FILE_ENV)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.script = write_script(self.dir)
        localmodels.reset_cache()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(localmodels.FILE_ENV, None)
        else:
            os.environ[localmodels.FILE_ENV] = self._saved
        localmodels.reset_cache()

    def use(self, path):
        os.environ[localmodels.FILE_ENV] = str(path)
        localmodels.reset_cache()

    # -- config presence and link matching ---------------------------------
    def test_missing_file_means_no_local_models(self):
        self.use(Path(self.dir) / "absent.json")
        self.assertFalse(localmodels.configured())
        self.assertEqual(localmodels.models(), [])
        self.assertIsNone(localmodels.stage("embed"))
        link = {"id": "e", "role": "embed", "base_url": "http://127.0.0.1:8000"}
        self.assertFalse(localmodels.capabilities(link)["start_stop"])

    def test_valid_config_matches_only_the_loopback_port(self):
        self.use(write_config(self.dir, {"embed": embed_stage(self.script)}))
        self.assertTrue(localmodels.configured())
        self.assertEqual(localmodels.models(),
                         [{"model": "m2b", "label": "Model 2B"}])
        local = {"id": "e", "role": "embed",
                 "base_url": "http://127.0.0.1:8000"}
        wrong_port = {"id": "e", "role": "embed",
                      "base_url": "http://127.0.0.1:8001"}
        remote = {"id": "e", "role": "embed",
                  "base_url": "http://192.0.2.1:8000"}
        self.assertIsNotNone(localmodels.local_stage(local))
        self.assertTrue(localmodels.capabilities(local)["start_stop"])
        self.assertIsNone(localmodels.local_stage(wrong_port))
        self.assertIsNone(localmodels.local_stage(remote))
        self.assertFalse(localmodels.capabilities(remote)["start_stop"])

    def test_cache_tracks_a_changed_file(self):
        path = write_config(self.dir, {"embed": embed_stage(self.script)})
        self.use(path)
        self.assertEqual(localmodels.models()[0]["label"], "Model 2B")
        time.sleep(0.02)
        path.write_text(json.dumps({"version": 1, "stages": {
            "embed": embed_stage(self.script, models=[
                {"id": "m2b", "label": "Renamed"}]),
        }}))
        self.assertEqual(localmodels.models()[0]["label"], "Renamed")

    # -- trust boundary: config file ---------------------------------------
    def test_config_must_be_a_regular_owned_0600ish_file(self):
        path = write_config(self.dir, {"rerank": {
            "script": str(self.script),
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(path)
        path.chmod(0o666)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("writable by other users", str(caught.exception))

        target_dir = Path(self.dir) / "real"
        target_dir.mkdir()
        target = write_config(target_dir, {"rerank": {
            "script": str(self.script),
            "health_url": "http://127.0.0.1:8001/health"}})
        link = Path(self.dir) / "symlink.json"
        link.symlink_to(target)
        self.use(link)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("not a regular file", str(caught.exception))

    def test_config_directory_must_not_be_other_writable(self):
        sub = Path(self.dir) / "shared"
        sub.mkdir()
        sub.chmod(0o777)
        try:
            path = write_config(sub, {"rerank": {
                "script": str(self.script),
                "health_url": "http://127.0.0.1:8001/health"}})
            self.use(path)
            with self.assertRaises(localmodels.LocalModelsError) as caught:
                localmodels.load()
            self.assertIn("writable by other users", str(caught.exception))
        finally:
            sub.chmod(0o700)

    # -- trust boundary: script --------------------------------------------
    def test_script_must_be_absolute_regular_executable_and_not_shared(self):
        relative = write_config(self.dir, {"rerank": {
            "script": "serve.sh",
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(relative)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("absolute path", str(caught.exception))

        missing = write_config(self.dir, {"rerank": {
            "script": str(Path(self.dir) / "nope.sh"),
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(missing)
        with self.assertRaises(localmodels.LocalModelsError):
            localmodels.load()

        not_executable = Path(self.dir) / "plain.sh"
        not_executable.write_text("#!/bin/sh\nexit 0\n")
        not_executable.chmod(0o644)
        noexec = write_config(self.dir, {"rerank": {
            "script": str(not_executable),
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(noexec)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("not executable", str(caught.exception))

        shared = write_script(self.dir, name="shared.sh")
        shared.chmod(0o777)
        writable = write_config(self.dir, {"rerank": {
            "script": str(shared),
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(writable)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("writable by other users", str(caught.exception))

        symlinked = Path(self.dir) / "linked.sh"
        symlinked.symlink_to(self.script)
        linked = write_config(self.dir, {"rerank": {
            "script": str(symlinked),
            "health_url": "http://127.0.0.1:8001/health"}})
        self.use(linked)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("regular file", str(caught.exception))

    def test_health_url_must_be_loopback_with_a_port(self):
        non_loopback = write_config(self.dir, {"rerank": {
            "script": str(self.script),
            "health_url": "http://192.0.2.1:8001/health"}})
        self.use(non_loopback)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("loopback", str(caught.exception))

        no_port = write_config(self.dir, {"rerank": {
            "script": str(self.script),
            "health_url": "http://127.0.0.1/health"}})
        self.use(no_port)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("explicit port", str(caught.exception))

    def test_unknown_fields_bad_models_and_bad_version_are_rejected(self):
        unknown_key = write_config(self.dir, {"rerank": {
            "script": str(self.script),
            "health_url": "http://127.0.0.1:8001/health",
            "command": "rm -rf /"}})
        self.use(unknown_key)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("unknown field", str(caught.exception))

        bad_model = write_config(self.dir, {"embed": {
            "script": str(self.script),
            "health_url": "http://127.0.0.1:8000/health",
            "models": [{"id": "../escape", "label": "bad"}]}})
        self.use(bad_model)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("must match", str(caught.exception))

        bad_version = Path(self.dir) / "bad-version.json"
        bad_version.write_text(json.dumps({
            "version": True,
            "stages": {"rerank": {
                "script": str(self.script),
                "health_url": "http://127.0.0.1:8001/health"}}}))
        bad_version.chmod(0o600)
        self.use(bad_version)
        with self.assertRaises(localmodels.LocalModelsError) as caught:
            localmodels.load()
        self.assertIn("integer", str(caught.exception))

    # -- probing -----------------------------------------------------------
    def test_probe_uses_the_configured_health_url_path(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthServer)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.use(write_config(self.dir, {
            "embed": embed_stage(self.script, port=port)}))
        # The link's base carries a path (like an OpenAI-compatible /v1), so
        # base_url + "/health" would 404; the configured health URL wins.
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:%d/v1" % port}
        status = localmodels.status(link)
        self.assertTrue(status["running"])
        self.assertEqual(status["model"], "served-model")
        self.assertEqual(status["dim"], 2048)

    def test_status_reports_a_failed_probe_as_data(self):
        self.use(Path(self.dir) / "absent.json")
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:9"}
        status = localmodels.status(link)
        self.assertFalse(status["running"])
        self.assertTrue(status["detail"])

    # -- execution ---------------------------------------------------------
    def test_start_runs_the_real_script_with_the_link_port(self):
        from unittest import mock

        marker = Path(self.dir) / "actions.txt"
        script = write_script(
            self.dir,
            body=('printf "%%s %%s\\n" "$1" "$2" >> "%s"\n'
                  'printf "PORT=%%s\\n" "$PORT" >> "%s"\n')
                 % (marker, marker))
        self.use(write_config(self.dir, {"embed": embed_stage(script)}))
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:8000", "model": "m2b"}
        seq = [(False, {}), (True, {"model": "m2b", "dim": 8})]
        with mock.patch.object(localmodels, "_probe_url",
                               side_effect=seq), \
                mock.patch.object(localmodels, "START_WAIT", 0):
            out = localmodels.start(link, model="m2b")
        self.assertTrue(out["running"])
        lines = marker.read_text().splitlines()
        self.assertEqual(lines[0], "start m2b")
        self.assertEqual(lines[1], "PORT=8000")

    def test_start_is_idempotent_when_the_model_already_serves(self):
        from unittest import mock

        marker = Path(self.dir) / "actions.txt"
        script = write_script(self.dir, body='echo ran >> "%s"' % marker)
        self.use(write_config(self.dir, {"embed": embed_stage(script)}))
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:8000"}
        with mock.patch.object(localmodels, "_probe_url",
                               return_value=(True, {"model": "m2b"})):
            out = localmodels.start(link)
        self.assertTrue(out["running"])
        self.assertFalse(marker.exists(), "an already-running model must not re-run")

    def test_a_script_swap_after_load_is_refused(self):
        from unittest import mock

        path = write_config(self.dir, {"embed": embed_stage(self.script)})
        self.use(path)
        localmodels.load()
        replacement = Path(self.dir) / "replacement.sh"
        replacement.write_text("#!/bin/sh\nexit 0\n")
        replacement.chmod(0o755)
        os.replace(replacement, self.script)  # new inode
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:8000"}
        with mock.patch.object(localmodels, "_probe_url",
                               return_value=(False, {})):
            with self.assertRaises(localmodels.LocalModelsError) as caught:
                localmodels.start(link)
        self.assertIn("changed since the config was loaded", str(caught.exception))

    def test_stop_runs_the_real_stop_action(self):
        from unittest import mock

        marker = Path(self.dir) / "actions.txt"
        script = write_script(self.dir, body='echo "$1" >> "%s"' % marker)
        self.use(write_config(self.dir, {"rerank": {
            "script": str(script),
            "health_url": "http://127.0.0.1:8001/health"}}))
        link = {"id": "r", "role": "rerank",
                "base_url": "http://127.0.0.1:8001"}
        with mock.patch.object(localmodels, "_probe_url",
                               return_value=(False, {})):
            out = localmodels.stop(link)
        self.assertFalse(out["running"])
        self.assertEqual(marker.read_text().strip(), "stop")

    def test_stop_fails_when_the_endpoint_still_answers(self):
        from unittest import mock

        marker = Path(self.dir) / "actions.txt"
        script = write_script(self.dir, body="exit 0")
        self.use(write_config(self.dir, {"rerank": {
            "script": str(script),
            "health_url": "http://127.0.0.1:8001/health"}}))
        link = {"id": "r", "role": "rerank",
                "base_url": "http://127.0.0.1:8001"}
        with mock.patch.object(localmodels, "_probe_url",
                               return_value=(True, {})), \
                mock.patch.object(localmodels, "STOP_WAIT", 0):
            with self.assertRaises(localmodels.StartError) as caught:
                localmodels.stop(link)
        self.assertIn("still answers", str(caught.exception))

    def test_start_rejects_an_unconfigured_model(self):
        self.use(write_config(self.dir, {"embed": embed_stage(self.script)}))
        link = {"id": "e", "role": "embed",
                "base_url": "http://127.0.0.1:8000"}
        with self.assertRaises(localmodels.NotLocalError):
            localmodels.start(link, model="other")

    def test_nonzero_script_exit_is_reported_with_the_last_line(self):
        from unittest import mock

        script = write_script(self.dir, body='echo "model file missing" >&2\nexit 1')
        self.use(write_config(self.dir, {"rerank": {
            "script": str(script),
            "health_url": "http://127.0.0.1:8001/health"}}))
        link = {"id": "r", "role": "rerank",
                "base_url": "http://127.0.0.1:8001"}
        with mock.patch.object(localmodels, "_probe_url",
                               return_value=(False, {})):
            with self.assertRaises(localmodels.StartError) as caught:
                localmodels.start(link)
        self.assertIn("exit 1", str(caught.exception))
        self.assertIn("model file missing", str(caught.exception))

    def test_error_tail_redacts_the_service_token(self):
        saved = os.environ.get("PEEKSEED_TOKEN")
        os.environ["PEEKSEED_TOKEN"] = "sekrit-token"
        try:
            tail = localmodels._tail("first\nfailed with sekrit-token\n")
        finally:
            if saved is None:
                os.environ.pop("PEEKSEED_TOKEN", None)
            else:
                os.environ["PEEKSEED_TOKEN"] = saved
        self.assertEqual(tail, "failed with <redacted>")


if __name__ == "__main__":
    unittest.main()
