"""Security regression tests: audit §6.10 gates 9, 10 and 11.

Everything here runs offline against ``tests.fixtures`` (a real one-page PDF and
a fake model server) and the FastAPI ``TestClient``. The 10 GB cases do not
write 10 GB: an early Content-Length rejection is checked with a lazy multipart
generator that is never consumed, and the streaming path is driven with a fake
UploadFile-like object whose chunks stop at the configured limit.
"""

import asyncio
import json
import os
import stat
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root

ENV_KEYS = ("PEEKSEED_DATA", "PEEKSEED_TOKEN", "PEEKSEED_ALLOWED_HOSTS",
            "PEEKSEED_HOST", "PEEKSEED_AGENT")
CONFIGURED_KEYS = ("PEEKSEED_TOKEN", "PEEKSEED_ALLOWED_HOSTS", "PEEKSEED_HOST",
                   "PEEKSEED_AGENT")


class SecurityTestCase(unittest.TestCase):
    """Env isolation: every test starts with the security features unset."""

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in CONFIGURED_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # --- app helpers -----------------------------------------------------
    def _client(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module
        app_module.reset_state()
        return TestClient(app_module.create_app())

    def _pdf_bytes(self, pages=None):
        with tempfile.TemporaryDirectory() as tmp:
            return make_pdf(Path(tmp) / "source.pdf", pages=pages).read_bytes()

    def _upload(self, client, name="Docs", filename="manual.pdf", data=None,
                status=201):
        data = self._pdf_bytes() if data is None else data
        response = client.post(
            "/api/datasets", data={"name": name},
            files=[("files", (filename, data, "application/pdf"))])
        self.assertEqual(response.status_code, status, response.text)
        return response.json().get("dataset")

    def _staging_leftovers(self, root):
        uploads = Path(root) / "uploads"
        if not uploads.is_dir():
            return []
        return sorted(entry.name for entry in uploads.iterdir()
                      if entry.name.startswith("upload-"))

    def _dataset_entries(self, root):
        datasets = Path(root) / "datasets"
        if not datasets.is_dir():
            return []
        return sorted(entry.name for entry in datasets.iterdir())


# ---------------------------------------------------------------------------
# gate 9: /pdf inline + traversal/symlink escapes + upload zip-slip
# ---------------------------------------------------------------------------

class PdfServingTests(SecurityTestCase):
    def test_pdf_is_served_inline(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client)
            served = client.get("/pdf/%s/manual.pdf" % dataset["id"])
            self.assertEqual(served.status_code, 200, served.text)
            self.assertEqual(served.headers["content-type"], "application/pdf")
            disposition = served.headers.get("content-disposition") or ""
            self.assertTrue(disposition.lower().startswith("inline"), disposition)
            self.assertIn("manual.pdf", disposition)

    def test_pdf_traversal_and_absolute_names_are_rejected(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client)
            for bad in ("..%2F..%2Fetc%2Fpasswd",
                        "../config/links.json",
                        "%2Fetc%2Fpasswd",
                        "..%5C..%5Cwindows%5Cwin.ini",
                        "....//....//etc/passwd"):
                response = client.get("/pdf/%s/%s" % (dataset["id"], bad))
                self.assertIn(response.status_code, (400, 404, 422),
                              (bad, response.status_code))
                self.assertNotIn(b"%PDF-", response.content, bad)

    def test_pdf_symlink_escaping_the_dataset_is_rejected(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client)
            dataset_dir = Path(root) / "datasets" / dataset["id"]
            original = dataset_dir / "original"
            outside = make_pdf(Path(root) / "outside.pdf")
            os.symlink(str(outside), str(original / "escape.pdf"))
            response = client.get("/pdf/%s/escape.pdf" % dataset["id"])
            self.assertIn(response.status_code, (400, 404), response.status_code)
            # A symlink that stays inside the dataset but outside original/ is
            # still not a served source.
            os.symlink(str(dataset_dir / "dataset.json"),
                       str(original / "metadata.pdf"))
            response = client.get("/pdf/%s/metadata.pdf" % dataset["id"])
            self.assertIn(response.status_code, (400, 404), response.status_code)


class UploadPathSafetyTests(SecurityTestCase):
    def test_zip_slip_filename_is_sanitized_before_directory_creation(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            dataset = self._upload(client, filename="../../evil.pdf")
            dataset_dir = Path(root) / "datasets" / dataset["id"]
            stored = sorted(entry.name for entry in (dataset_dir / "original").iterdir())
            self.assertEqual(stored, ["evil.pdf"])
            matches = sorted(str(path) for path in Path(root).rglob("evil.pdf"))
            self.assertEqual(matches, [str(dataset_dir / "original" / "evil.pdf")])
            self.assertFalse((Path(root) / "evil.pdf").exists())
            self.assertFalse((Path(root).parent / "evil.pdf").exists())
            self.assertEqual(Path(dataset_dir).parent, Path(root) / "datasets")
            self.assertEqual(self._staging_leftovers(root), [])

    def test_invalid_upload_creates_no_dataset_directory(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            response = client.post(
                "/api/datasets", data={"name": "Docs"},
                files=[("files", ("../../evil.txt", self._pdf_bytes(), "text/plain"))])
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])
            response = client.post(
                "/api/datasets", data={"name": "Docs"},
                files=[("files", ("fake.pdf", b"definitely not a pdf",
                                  "application/pdf"))])
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("not a valid PDF", response.json()["detail"])
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])


# ---------------------------------------------------------------------------
# gate 10: token + Host/Origin allowlist
# ---------------------------------------------------------------------------

class TokenTests(SecurityTestCase):
    def test_mutating_routes_require_the_token_when_set(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            os.environ["PEEKSEED_TOKEN"] = "s3cret-value"
            client = self._client()
            self.assertEqual(
                client.put("/api/settings", json={"title": "x"}).status_code, 401)
            self.assertEqual(
                client.put("/api/settings", json={"title": "x"},
                            headers={"Authorization": "Bearer wrong"}).status_code,
                401)
            self.assertEqual(
                client.put("/api/settings", json={"title": "x"},
                           headers={"X-Peekseed-Token": "wrong"}).status_code, 401)
            self.assertEqual(client.delete("/api/datasets/nope").status_code, 401)
            body = client.put("/api/settings", json={"title": "x"},
                               headers={"Authorization": "Bearer wrong"}).text
            self.assertNotIn("s3cret-value", body)

    def test_both_headers_are_accepted_and_reads_stay_open(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            os.environ["PEEKSEED_TOKEN"] = "s3cret-value"
            client = self._client()
            bearer = client.put("/api/settings", json={"title": "x"},
                                 headers={"Authorization": "Bearer s3cret-value"})
            self.assertEqual(bearer.status_code, 200, bearer.text)
            header = client.put("/api/settings", json={"title": "y"},
                                headers={"X-Peekseed-Token": "s3cret-value"})
            self.assertEqual(header.status_code, 200, header.text)
            self.assertEqual(client.get("/api/settings").status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/api/datasets").status_code, 200)

    def test_check_token_helper(self):
        from peekseed import security
        self.assertFalse(security.token_enabled())
        self.assertFalse(security.check_token("anything"))
        os.environ["PEEKSEED_TOKEN"] = "s3cret"
        self.assertTrue(security.token_enabled())
        for value in ("s3cret", "Bearer s3cret", "bearer s3cret",
                      "X-Peekseed-Token: s3cret",
                      "Authorization: Bearer s3cret"):
            self.assertTrue(security.check_token(value), value)
        for value in ("", "wrong", "Bearer wrong", "s3cret-extra"):
            self.assertFalse(security.check_token(value), value)
        self.assertTrue(security.request_authorized(
            {"authorization": "Bearer s3cret"}))
        self.assertTrue(security.request_authorized(
            {"x-peekseed-token": "s3cret"}))
        self.assertFalse(security.request_authorized({}))


class OriginHostTests(SecurityTestCase):
    def test_hostile_origin_is_rejected(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            hostile = client.put("/api/settings", json={"title": "x"},
                                  headers={"Origin": "http://evil.example"})
            self.assertEqual(hostile.status_code, 403, hostile.text)
            null_origin = client.put("/api/settings", json={"title": "x"},
                                      headers={"Origin": "null"})
            self.assertEqual(null_origin.status_code, 403, null_origin.text)
            # Reads are always open, even from a foreign origin.
            read = client.get("/api/settings", headers={"Origin": "http://evil.example"})
            self.assertEqual(read.status_code, 200, read.text)

    def test_hostile_host_is_rejected_when_allowlist_is_set(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            os.environ["PEEKSEED_ALLOWED_HOSTS"] = "127.0.0.1,localhost"
            client = self._client()
            hostile = client.put("/api/settings", json={"title": "x"},
                                  headers={"Host": "evil.example"})
            self.assertEqual(hostile.status_code, 403, hostile.text)
            unlisted = client.put("/api/settings", json={"title": "x"},
                                   headers={"Host": "testserver"})
            self.assertEqual(unlisted.status_code, 403, unlisted.text)
            loopback = client.put("/api/settings", json={"title": "x"},
                                   headers={"Host": "127.0.0.1:8030"})
            self.assertEqual(loopback.status_code, 200, loopback.text)
            # Reads are not gated by the Host allowlist.
            read = client.get("/api/settings", headers={"Host": "evil.example"})
            self.assertEqual(read.status_code, 200, read.text)

    def test_matching_origin_is_accepted(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            same = client.put("/api/settings", json={"title": "x"},
                               headers={"Origin": "http://testserver"})
            self.assertEqual(same.status_code, 200, same.text)
            os.environ["PEEKSEED_ALLOWED_HOSTS"] = "127.0.0.1,localhost"
            client = self._client()
            matching = client.put(
                "/api/settings", json={"title": "x"},
                headers={"Host": "127.0.0.1:8030",
                         "Origin": "http://127.0.0.1:8030"})
            self.assertEqual(matching.status_code, 200, matching.text)
            listed_name = client.put(
                "/api/settings", json={"title": "y"},
                headers={"Host": "127.0.0.1:8030",
                         "Origin": "http://localhost:8030"})
            self.assertEqual(listed_name.status_code, 200, listed_name.text)

    def test_default_loopback_is_unchanged_when_unset(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            self.assertEqual(
                client.put("/api/settings", json={"title": "x"}).status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)
            dataset = self._upload(client)
            self.assertIn("manual.pdf", dataset["sources"])


# ---------------------------------------------------------------------------
# gate 11: streaming upload limits (per-file, file count, total)
# ---------------------------------------------------------------------------

class UploadLimitTests(SecurityTestCase):
    def test_declared_ten_gigabyte_body_is_rejected_before_it_is_read(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            state = {"chunks": 0}

            def body():
                yield (b'--peekseed\r\nContent-Disposition: form-data; '
                       b'name="name"\r\n\r\nDocs\r\n'
                       b'--peekseed\r\nContent-Disposition: form-data; '
                       b'name="files"; filename="huge.pdf"\r\n'
                       b'Content-Type: application/pdf\r\n\r\n')
                chunk = b"x" * (64 * 1024)
                while state["chunks"] < 163840:   # would be 10 GiB in total
                    state["chunks"] += 1
                    yield chunk
                yield b"\r\n--peekseed--\r\n"

            tracemalloc.start()
            response = client.post(
                "/api/datasets", content=body(),
                headers={"Content-Type": "multipart/form-data; boundary=peekseed",
                         "Content-Length": str(10 * 1024 ** 3)})
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(response.status_code, 413, response.text)
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertEqual(state["chunks"], 0)
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])

    def test_streaming_handler_counts_a_ten_gigabyte_upload_to_the_limit(self):
        from fastapi import HTTPException
        from peekseed import app as app_module, security

        with temp_data_root() as root:
            self.root = root
            chunk_size = security.UPLOAD_CHUNK_SIZE

            class LazilyHugePdf(object):
                """A fake UploadFile whose bytes stop at the limit, not at 10 GB."""

                filename = "huge.pdf"

                def __init__(self):
                    self.served = 0

                def read(self, size=-1):
                    self.served += 1
                    return b"%PDF-1.4\n" + b"0" * ((size or chunk_size) - 9)

            upload = LazilyHugePdf()
            tracemalloc.start()
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app_module._store_uploads("Huge", "", [upload]))
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(caught.exception.status_code, 413)
            self.assertLess(peak, 4 * 1024 * 1024)
            self.assertEqual(upload.served * chunk_size,
                             security.MAX_UPLOAD_BYTES + chunk_size)
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])

    def test_file_count_limit(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            pdf = self._pdf_bytes()
            too_many = [("files", ("doc-%02d.pdf" % index, pdf,
                                   "application/pdf")) for index in range(51)]
            response = client.post("/api/datasets", data={"name": "Too many"},
                                   files=too_many)
            self.assertEqual(response.status_code, 413, response.text)
            from peekseed import security
            self.assertIn(str(security.MAX_UPLOAD_FILES),
                          response.json()["detail"])
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])
            at_limit = [("files", ("doc-%02d.pdf" % index, pdf,
                                   "application/pdf")) for index in range(50)]
            accepted = client.post("/api/datasets", data={"name": "At limit"},
                                   files=at_limit)
            self.assertEqual(accepted.status_code, 201, accepted.text)
            self.assertEqual(len(accepted.json()["dataset"]["sources"]), 50)

    def test_total_size_limit_across_files(self):
        from peekseed import security

        with temp_data_root() as root:
            self.root = root
            chunk = b"%PDF-1.4\n" + b"0" * (1024 - 9)

            class FakeUpload(object):
                filename = "part.pdf"

                def __init__(self, chunks):
                    self._chunks = chunks

                def read(self, size=-1):
                    if self._chunks <= 0:
                        return b""
                    self._chunks -= 1
                    return chunk

            destination = Path(root) / "staging" / "part.pdf"
            written = asyncio.run(security.stream_upload(
                FakeUpload(2), destination, max_bytes=4096, max_total=4096))
            self.assertEqual(written, 2048)
            with self.assertRaises(security.UploadLimitExceeded) as caught:
                asyncio.run(security.stream_upload(
                    FakeUpload(3), destination, max_bytes=4096,
                    max_total=4096, total_used=written))
            self.assertEqual(caught.exception.status, 413)
            self.assertFalse(destination.exists())
            # A request that already spent its total budget is refused up front.
            with self.assertRaises(security.UploadLimitExceeded):
                asyncio.run(security.stream_upload(
                    FakeUpload(1), destination, max_bytes=4096,
                    max_total=4096, total_used=4096))
            self.assertFalse(destination.exists())
            # And the per-file cap is separate from the total cap.
            with self.assertRaises(security.UploadLimitExceeded):
                asyncio.run(security.stream_upload(
                    FakeUpload(10), destination, max_bytes=2048,
                    max_total=1024 * 1024))
            self.assertFalse(destination.exists())

    def test_chunked_body_without_content_length_is_capped(self):
        from peekseed import security

        with temp_data_root() as root:
            self.root = root
            client = self._client()
            original = security.MAX_REQUEST_BYTES
            state = {"chunks": 0}

            def body():
                yield (b'--b\r\nContent-Disposition: form-data; name="name"'
                       b'\r\n\r\nDocs\r\n'
                       b'--b\r\nContent-Disposition: form-data; name="files"; '
                       b'filename="big.pdf"\r\nContent-Type: application/pdf'
                       b'\r\n\r\n')
                chunk = b"x" * 1024
                while state["chunks"] < 1024:    # about 1 MB, no Content-Length
                    state["chunks"] += 1
                    yield chunk
                yield b"\r\n--b--\r\n"

            # The real cap is 202 MB; a test-sized cap exercises the mechanism.
            security.MAX_REQUEST_BYTES = 4096
            try:
                tracemalloc.start()
                response = client.post(
                    "/api/datasets", content=body(),
                    headers={"Content-Type": "multipart/form-data; boundary=b"})
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                reads, sent = self._drive_middleware()
            finally:
                security.MAX_REQUEST_BYTES = original
            self.assertEqual(response.status_code, 413, response.text)
            self.assertLess(peak, 4 * 1024 * 1024)
            # The TestClient buffers a chunked body before the app sees it, so
            # the generator count proves nothing here. Drive the middleware
            # directly: it must stop reading the request at the cap and answer
            # 413 without ever running the application.
            self.assertLessEqual(reads, 6)
            self.assertEqual(sent[0]["status"], 413)
            self.assertEqual(self._dataset_entries(root), [])
            self.assertEqual(self._staging_leftovers(root), [])

    def _drive_middleware(self):
        """Run the security middleware over a fake chunked request.

        Returns ``(reads, sent)``: how many request messages the middleware
        consumed and the ASGI messages it sent. The app raises if it is ever
        called, which proves an oversized body is rejected before parsing.
        """
        from peekseed.security import SecurityMiddleware

        reads = [0]

        async def receive():
            reads[0] += 1
            return {"type": "http.request", "body": b"x" * 1024,
                    "more_body": True}

        sent = []

        async def send(message):
            sent.append(message)

        async def app(scope, receive, send):
            raise AssertionError("the app must not run for an oversized body")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/datasets",
            "headers": [(b"content-type", b"multipart/form-data")],
        }
        asyncio.run(SecurityMiddleware(app)(scope, receive, send))
        return reads[0], sent

    def test_limits_are_the_expected_constants(self):
        from peekseed import security
        self.assertEqual(security.MAX_UPLOAD_BYTES, 50 * 1024 * 1024)
        self.assertEqual(security.MAX_UPLOAD_FILES, 50)
        self.assertEqual(security.MAX_UPLOAD_TOTAL_BYTES, 200 * 1024 * 1024)
        self.assertEqual(security.UPLOAD_CHUNK_SIZE, 64 * 1024)


# ---------------------------------------------------------------------------
# SSRF guard, data modes
# ---------------------------------------------------------------------------

class SsrfTests(SecurityTestCase):
    def test_validate_url_blocks_metadata_link_local_and_bad_schemes(self):
        from peekseed import security
        blocked = (
            "http://169.254.169.254/latest/meta-data",
            "http://169.254.1.1/",
            "http://[fd00:ec2::254]/latest/meta-data",
            "http://100.100.100.200/",
            "http://192.0.0.192/",
            "http://[fe80::1]/",
            "ftp://127.0.0.1/model",
            "file:///etc/passwd",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://user:pass@127.0.0.1/v1",
        )
        for url in blocked:
            self.assertIsNotNone(security.validate_url(url), url)
        allowed = (
            "http://127.0.0.1:11434/v1",
            "http://localhost:8080",
            "http://[::1]:8030/v1",
            "https://192.168.1.10:11434/v1",
            "http://10.0.0.5/v1",
            "http://172.16.3.4:11434/v1",
            "http://100.64.0.7/v1",
            "https://models.example.com/v1",
        )
        for url in allowed:
            self.assertIsNone(security.validate_url(url), url)

    def test_link_endpoints_reject_metadata_endpoints(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            response = client.post("/api/links", json={
                "role": "embed", "label": "metadata", "protocol": "openai",
                "base_url": "http://169.254.169.254/latest/meta-data",
                "model": "x", "enabled": True})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("metadata", response.json()["detail"])
            created = client.post("/api/links", json={
                "role": "embed", "label": "local", "protocol": "openai",
                "base_url": fake.openai_base, "model": "test-embed",
                "enabled": True})
            self.assertEqual(created.status_code, 201, created.text)
            bad = client.put("/api/links/%s" % created.json()["link"]["id"],
                             json={"base_url": "http://100.100.100.200/v1"})
            self.assertEqual(bad.status_code, 400, bad.text)

    def test_failed_link_probe_does_not_echo_the_provider_body(self):
        with temp_data_root() as root, FakeModelServer() as fake:
            self.root = root
            client = self._client()
            created = client.post("/api/links", json={
                "role": "embed", "label": "probe", "protocol": "custom",
                "base_url": fake.base + "/nowhere", "model": "x",
                "enabled": True})
            self.assertEqual(created.status_code, 201, created.text)
            result = client.post("/api/links/%s/test"
                                 % created.json()["link"]["id"]).json()
            self.assertFalse(result["ok"])
            body = json.dumps(result).lower()
            self.assertNotIn("not found", body)
            self.assertIn("404", body)


class DataModeTests(SecurityTestCase):
    def test_derived_dirs_and_history_files_are_owner_only(self):
        from fastapi.testclient import TestClient
        from peekseed import app as app_module

        with temp_data_root() as root:
            self.root = root
            history_dir = Path(root) / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            stale = history_dir / "index.json"
            stale.write_text("{}", encoding="utf-8")
            os.chmod(str(stale), 0o644)
            app_module.reset_state()
            app = app_module.create_app()
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json()["ok"], True)
                dataset = self._upload(client)
                self.assertEqual(stat.S_IMODE(os.stat(str(root)).st_mode), 0o700)
                for name in ("config", "datasets", "history", "uploads"):
                    path = Path(root) / name
                    self.assertTrue(path.is_dir(), name)
                    self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode),
                                     0o700, name)
                self.assertEqual(stat.S_IMODE(os.stat(str(stale)).st_mode), 0o600)
                dataset_dir = Path(root) / "datasets" / dataset["id"]
                self.assertEqual(stat.S_IMODE(os.stat(str(dataset_dir)).st_mode),
                                 0o700)
                self.assertEqual(
                    stat.S_IMODE(os.stat(str(dataset_dir / "original")).st_mode),
                    0o700)


if __name__ == "__main__":
    unittest.main()
