"""End-to-end acceptance tests for audit §6.10 gates 1-6 and 9-11.

These exercise the workspace + ingestion core the way a user does: a real
workspace tree, real PDFs from ``tests.fixtures``, a fake model server, and the
CLI/app entry points. Unit coverage for each module lives beside it
(``test_workspace``/``test_scan``/``test_index``/``test_security``); this file is
the gate evidence that they compose.
"""

import contextlib
import io
import json
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path

import numpy as np

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root


def fake_link(fake, model="test-embed", **extra):
    link = {
        "id": "fake",
        "role": "embed",
        "label": "Fake",
        "protocol": "openai",
        "base_url": fake.openai_base,
        "model": model,
        "api_key": "",
        "enabled": True,
    }
    link.update(extra)
    return link


@contextlib.contextmanager
def isolated_workspace(tmp):
    """A real project checkout with a workspace inside it, and PEEKSEED_* pinned."""
    project = Path(tmp) / "project"
    project.mkdir()
    # A real checkout, so the gitignore guard and `git check-ignore` behave as
    # they do for a user (an empty .git directory makes git exit 128).
    import subprocess
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    workspace_root = project / "ws"
    saved = {key: os.environ.pop(key) for key in list(os.environ)
             if key.startswith("PEEKSEED_")}
    previous_cwd = os.getcwd()
    os.environ["PEEKSEED_WORKSPACE"] = str(workspace_root)
    os.environ["PEEKSEED_DATA"] = str(workspace_root / ".peekseed")
    os.chdir(project)
    from peekseed import app as app_module, config, links, workspace
    config.reset_cache()
    links.reset_cache()
    app_module.reset_state()
    try:
        workspace.init(str(workspace_root))
        yield project, workspace_root, workspace_root / ".peekseed"
    finally:
        app_module.reset_state()
        config.reset_cache()
        links.reset_cache()
        os.chdir(previous_cwd)
        for key in [key for key in os.environ if key.startswith("PEEKSEED_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)


def document_snapshot(documents_dir):
    """Every document's content hash, byte size and mtime (for "unchanged" checks)."""
    from peekseed import contracts
    rows = {}
    for path in sorted(Path(documents_dir).rglob("*")):
        if path.is_file():
            stat = path.stat()
            rows[str(path)] = (contracts.sha256_file(path), stat.st_size,
                               stat.st_mtime_ns)
    return rows


def current_generation_dir(dataset_dir):
    """The live generation directory of a dataset's only index space."""
    from peekseed import ingest
    indexes = ingest.dataset_indexes(Path(dataset_dir))
    spaces = [info for info in indexes if info.get("space")]
    if not spaces:
        raise AssertionError("no index in %s" % dataset_dir)
    space_dir = Path(dataset_dir) / "index" / spaces[0]["space"]
    generation = (space_dir / "CURRENT").read_text().strip()
    return space_dir / "generations" / generation


def collection_state(report, name):
    states = report.get("collections") or {}
    if name not in states:
        raise AssertionError("no collection %r in report: %s" % (name, sorted(states)))
    return states[name]


class GateOneTests(unittest.TestCase):
    """1. init creates the tree + anchored gitignore; scan leaves documents untouched."""

    def test_init_and_scan_do_not_touch_documents(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (project, ws, derived):
            from peekseed.__main__ import main
            from peekseed import scan

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["init", "--workspace", str(ws)]), 0)
            self.assertTrue((ws / "documents").is_dir())
            self.assertTrue((derived / "manifest.json").is_file())
            self.assertTrue((derived / "config" / "links.json").is_file())
            gitignore = (project / ".gitignore").read_text(encoding="utf-8")
            # The anchored entry names the actual workspace path: the default
            # ./workspace gives "/workspace/", any other location gives its own
            # relative pattern (this fixture uses "ws").
            self.assertIn("\n/ws/\n", "\n" + gitignore.strip() + "\n")
            import subprocess
            ignored = subprocess.run(
                ["git", "-C", str(project), "check-ignore", "-q", "ws"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(ignored.returncode, 0)

            make_pdf(ws / "documents" / "manual.pdf",
                     pages=["The widget is mounted with two screws."])
            before = document_snapshot(ws / "documents")
            report = scan.scan(str(ws))
            self.assertEqual(report["counts"]["new"], 1)
            self.assertEqual(report["counts"]["errors"], 0)
            self.assertGreaterEqual(collection_state(report, "default")["chunks"], 1)
            # Not one byte, size or mtime changed in the user's tree.
            self.assertEqual(document_snapshot(ws / "documents"), before)


class GateTwoTests(unittest.TestCase):
    """2. Drop A -> scan -> indexed; scan again -> 0 new, 0 changed, 0 embed calls."""

    def test_scan_is_incremental_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, derived), \
                FakeModelServer(dim=8) as fake:
            from peekseed import scan
            link = fake_link(fake)
            ws.mkdir(parents=True, exist_ok=True)
            scan.scan(str(ws), link=None)      # create the tree
            make_pdf(ws / "documents" / "cxl" / "a.pdf",
                     pages=["Alpha. The widget is mounted with two screws. " * 20,
                            "Beta. Press the green button to start the widget. " * 20])

            first = scan.scan(str(ws), link=link)
            self.assertEqual(first["counts"]["new"], 1)
            self.assertEqual(collection_state(first, "cxl")["status"], "indexed")
            embed_requests = len(fake.requests)
            self.assertGreater(embed_requests, 0)
            chunks = (derived / "datasets" / "cxl" / "chunks.jsonl").read_text().splitlines()
            self.assertGreaterEqual(len(chunks), 1)
            record = json.loads(chunks[0])
            self.assertEqual(len(record["source_sha256"]), 64)
            self.assertEqual(len(record["id"]), 16)

            second = scan.scan(str(ws), link=link)
            self.assertEqual(second["counts"]["new"], 0)
            self.assertEqual(second["counts"]["changed"], 0)
            self.assertEqual(second["counts"]["unchanged"], 1)
            self.assertEqual(second["embed_calls"], 0)
            # Zero embed calls means literally zero requests to the model server.
            self.assertEqual(len(fake.requests), embed_requests)
            self.assertEqual(collection_state(second, "cxl")["status"], "indexed")


class GateThreeTests(unittest.TestCase):
    """3. Add PDF B to an indexed collection -> scan -> B searchable; A's ids stable."""

    def test_adding_a_file_makes_it_searchable_without_renumbering_others(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, derived), \
                FakeModelServer(dim=8) as fake:
            from peekseed import engine, ingest, scan
            link = fake_link(fake)
            scan.scan(str(ws), link=None)
            a_pdf = make_pdf(ws / "documents" / "cxl" / "a.pdf",
                             pages=["Alpha. The widget is mounted with two screws. " * 20])
            scan.scan(str(ws), link=link)
            dataset_dir = derived / "datasets" / "cxl"
            a_ids = sorted(row["id"] for row in ingest.read_chunks(dataset_dir)
                           if row.get("source") == "a.pdf")
            self.assertGreaterEqual(len(a_ids), 1)

            make_pdf(ws / "documents" / "cxl" / "b.pdf",
                     pages=["Sprocket calibration. Turn the sprocket clockwise. " * 20])
            report = scan.scan(str(ws), link=link)
            self.assertEqual(report["counts"]["new"], 1)
            self.assertEqual(collection_state(report, "cxl")["status"], "indexed")

            # A's content-derived ids did not move when B was added.
            after_ids = sorted(row["id"] for row in ingest.read_chunks(dataset_dir)
                               if row.get("source") == "a.pdf")
            self.assertEqual(a_ids, after_ids)

            # B is in the index and ranks first for its own vocabulary.
            query = "sprocket calibration"
            qvec = ingest.embed_query(link, query)
            store = engine.IndexStore(str(derived / "datasets"))
            store.load()
            index = store.find("cxl", "fake", "test-embed")
            self.assertIsNotNone(index, "no index loaded for the collection")
            order, _reranked, _scores, _timings = index.search(qvec, query, top=5)
            top_sources = [str(index.meta[i].get("source") or "") for i in order[:3]]
            self.assertIn("b.pdf", top_sources)


class GateFourTests(unittest.TestCase):
    """4. A cancelled/failed build leaves the previous index serving; restart resumes."""

    def test_interrupted_build_keeps_the_previous_index_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, derived), \
                FakeModelServer(dim=8) as fake:
            from peekseed import ingest, scan
            link = fake_link(fake)
            scan.scan(str(ws), link=None)
            for index in range(3):
                make_pdf(ws / "documents" / "cxl" / ("doc%d.pdf" % index),
                         pages=["Widget paragraph %d. " % index * 20,
                                "Another widget paragraph %d. " % index * 20])
            first = scan.scan(str(ws), link=link)
            self.assertEqual(collection_state(first, "cxl")["status"], "indexed")
            dataset_dir = derived / "datasets" / "cxl"
            generation_dir = current_generation_dir(dataset_dir)
            committed = (generation_dir.parent.parent / "CURRENT").read_text().strip()
            info = ingest.dataset_indexes(dataset_dir)[0]
            self.assertGreaterEqual(info["n"], 3)

            # Cancel after the first batch: CURRENT must not move.
            state = {"calls": 0}

            def cancel():
                state["calls"] += 1
                return state["calls"] > 1

            with self.assertRaises(ingest.IndexCancelled):
                ingest.build_index(dataset_dir, link, batch_size=2,
                                   target_chars=200, max_chars=400, cancel=cancel)
            self.assertEqual(
                (generation_dir.parent.parent / "CURRENT").read_text().strip(),
                committed,
            )
            still_serving = ingest.load_index(dataset_dir, info["space"])
            self.assertEqual(still_serving["info"]["generation"], committed)

            # A process death leaves `running` records; they are marked interrupted
            # (resumable), never converted into a false failure.
            jobs_dir = derived / "jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            from peekseed import contracts
            job = contracts.new_job("index", collection="cxl", job_id="job-crash")
            job["state"] = "running"
            contracts.write_json_atomic(jobs_dir / "job-crash.json", job)
            self.assertEqual(ingest.mark_interrupted_jobs(jobs_dir), ["job-crash"])
            self.assertEqual(
                json.loads((jobs_dir / "job-crash.json").read_text())["state"],
                "interrupted",
            )

            # Restart: resume the incomplete generation and commit it.
            resumed = ingest.build_index(dataset_dir, link, batch_size=2,
                                         target_chars=200, max_chars=400)
            self.assertEqual(
                (generation_dir.parent.parent / "CURRENT").read_text().strip(),
                resumed["generation"],
            )
            self.assertEqual(resumed["n"], info["n"])


class GateFiveTests(unittest.TestCase):
    """5. Corrupt vectors.npy -> collection needs_reindex, never silently vanished."""

    def test_corrupt_vectors_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, derived), \
                FakeModelServer(dim=8) as fake:
            from peekseed import ingest, scan, workspace
            link = fake_link(fake)
            scan.scan(str(ws), link=None)
            make_pdf(ws / "documents" / "cxl" / "a.pdf",
                     pages=["Widget text about buttons and screws. " * 20])
            scan.scan(str(ws), link=link)
            dataset_dir = derived / "datasets" / "cxl"
            generation_dir = current_generation_dir(dataset_dir)
            (generation_dir / "vectors.npy").write_bytes(b"corrupt")

            state = workspace.status(str(ws))
            collection = collection_state(state, "cxl")
            self.assertEqual(collection["status"], "needs_reindex")
            self.assertTrue(collection["reasons"])

            report = scan.scan(str(ws), link=None)
            self.assertEqual(collection_state(report, "cxl")["status"], "needs_reindex")

            info = ingest.dataset_indexes(dataset_dir)[0]
            with self.assertRaises(ingest.NeedsReindex):
                ingest.load_index(dataset_dir, info["space"])
            self.assertEqual(ingest.index_state(dataset_dir, info["space"])["status"],
                             "needs_reindex")


class GateSixTests(unittest.TestCase):
    """6. Change chunker version -> collection stale; reindex --dry-run prints scope."""

    def test_chunker_change_is_stale_and_dry_run_prints_the_scope(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, derived), \
                FakeModelServer(dim=8) as fake:
            from peekseed import contracts, ingest, scan, workspace
            link = fake_link(fake)
            scan.scan(str(ws), link=None)
            make_pdf(ws / "documents" / "cxl" / "a.pdf",
                     pages=["Widget text about buttons and screws. " * 20])
            scan.scan(str(ws), link=link)
            dataset_dir = derived / "datasets" / "cxl"
            info = ingest.dataset_indexes(dataset_dir)[0]
            space_dir = dataset_dir / "index" / info["space"]
            committed = (space_dir / "CURRENT").read_text().strip()

            original = contracts.CHUNKER_VERSION
            contracts.CHUNKER_VERSION = original + 1
            try:
                state = workspace.status(str(ws))
                collection = collection_state(state, "cxl")
                self.assertEqual(collection["status"], "stale")
                self.assertTrue(any("chunker" in reason
                                    for reason in collection["reasons"]))

                # reindex --dry-run prints the exact scope and writes nothing.
                from peekseed import links
                saved = links.create({"role": "embed", "label": "Fake",
                                      "protocol": "openai",
                                      "base_url": fake.openai_base,
                                      "model": "test-embed", "enabled": True})
                out = io.StringIO()
                from peekseed.__main__ import main
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["reindex", "--all", "--link",
                                           saved["id"], "--dry-run",
                                           "--workspace", str(ws)]), 0)
                text = out.getvalue()
                self.assertIn("dry-run:", text)
                self.assertIn("cxl", text)
                self.assertIn(str(info["n"]), text)
                self.assertIn("embed call", text)
            finally:
                contracts.CHUNKER_VERSION = original

            # A dry run must not add a generation or flip CURRENT.
            self.assertEqual((space_dir / "CURRENT").read_text().strip(), committed)
            self.assertEqual(
                sorted(path.name for path in (space_dir / "generations").iterdir()),
                [committed],
            )


class GateNineTests(unittest.TestCase):
    """9. Traversal/symlink escapes and zip-slip rejected; /pdf is inline."""

    def test_pdf_is_inline_and_escapes_are_rejected(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            from peekseed.app import create_app
            from fastapi.testclient import TestClient

            dataset = ingest.create_dataset(config.paths(), "Docs")
            pdf = make_pdf(root / "doc.pdf", pages=["Widget text."])
            ingest.add_source(dataset, pdf)
            client = TestClient(create_app())

            response = client.get("/pdf/%s/doc.pdf" % dataset["id"])
            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                response.headers["content-disposition"].startswith("inline"),
                response.headers.get("content-disposition"),
            )
            for name in ("..%2F..%2Fetc%2Fpasswd", "%2Fetc%2Fpasswd", "../doc.pdf"):
                response = client.get("/pdf/%s/%s" % (dataset["id"], name))
                self.assertIn(response.status_code, (400, 404), name)

            outside = root / "secret.pdf"
            outside.write_bytes(b"%PDF-1.4\nsecret")
            (Path(dataset["dir"]) / "original" / "escape.pdf").symlink_to(outside)
            response = client.get("/pdf/%s/escape.pdf" % dataset["id"])
            self.assertIn(response.status_code, (400, 404))

            # Upload filename zip-slip: the stored source can never escape original/.
            files = {"files": ("../../evil.pdf", pdf.read_bytes(), "application/pdf")}
            response = client.post("/api/datasets", data={"name": "Slip"}, files=files)
            self.assertEqual(response.status_code, 201, response.text)
            created = response.json()["dataset"]["id"]
            original = Path(config.paths().datasets_dir) / created / "original"
            names = [path.name for path in original.iterdir()]
            self.assertEqual(names, ["evil.pdf"])

    def test_scan_rejects_a_symlink_that_escapes_the_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_workspace(tmp) as (_project, ws, _derived):
            from peekseed import scan
            outside = Path(tmp) / "outside.pdf"
            make_pdf(outside, pages=["outside the workspace"])
            scan.scan(str(ws), link=None)
            (ws / "documents" / "escape.pdf").symlink_to(outside)
            report = scan.scan(str(ws), link=None)
            self.assertEqual(report["counts"]["errors"], 1)
            entry = next(row for row in report["files"]
                         if str(row.get("path") or "").endswith("escape.pdf")
                         or str(row.get("name") or "").endswith("escape.pdf"))
            self.assertIn("symlink escapes", str(entry.get("error") or entry.get("reason") or ""))


class GateTenTests(unittest.TestCase):
    """10. Hostile Origin/Host rejected on mutating routes; token required when set."""

    def test_token_and_origin_gate_mutations_not_reads(self):
        with temp_data_root() as root:
            os.environ["PEEKSEED_TOKEN"] = "s3cret-token"
            try:
                from peekseed.app import create_app
                from fastapi.testclient import TestClient
                client = TestClient(create_app())
                payload = {"title": "t", "tagline": "x", "examples": []}

                self.assertEqual(client.put("/api/settings", json=payload).status_code, 401)
                self.assertEqual(
                    client.put("/api/settings", json=payload,
                               headers={"Authorization": "Bearer wrong"}).status_code,
                    401,
                )
                accepted = client.put("/api/settings", json=payload,
                                      headers={"Authorization": "Bearer s3cret-token"})
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(
                    client.put("/api/settings", json=payload,
                               headers={"X-Peekseed-Token": "s3cret-token",
                                        "Origin": "http://evil.example"}).status_code,
                    403,
                )
                # A same-origin browser request is fine.
                self.assertEqual(
                    client.put("/api/settings", json=payload,
                               headers={"X-Peekseed-Token": "s3cret-token",
                                        "Origin": "http://testserver"}).status_code,
                    200,
                )
                # Reads stay open: the UI must load without a token.
                self.assertEqual(client.get("/api/datasets").status_code, 200)
                self.assertEqual(client.get("/health").status_code, 200)
            finally:
                os.environ.pop("PEEKSEED_TOKEN", None)


class GateElevenTests(unittest.TestCase):
    """11. A huge upload is rejected without RAM growth; file-count/total limits hold."""

    def test_declared_ten_gigabyte_body_is_rejected_without_buffering(self):
        with temp_data_root() as root:
            from peekseed.app import create_app
            from fastapi.testclient import TestClient
            client = TestClient(create_app())
            state = {"chunks": 0}

            def body():
                yield (b'--b\r\nContent-Disposition: form-data; name="name"'
                       b'\r\n\r\nDocs\r\n'
                       b'--b\r\nContent-Disposition: form-data; name="files"; '
                       b'filename="huge.pdf"\r\nContent-Type: application/pdf\r\n\r\n')
                chunk = b"x" * (64 * 1024)
                while state["chunks"] < 163840:     # would be 10 GiB
                    state["chunks"] += 1
                    yield chunk
                yield b"\r\n--b--\r\n"

            tracemalloc.start()
            response = client.post(
                "/api/datasets", content=body(),
                headers={"Content-Type": "multipart/form-data; boundary=b",
                         "Content-Length": str(10 * 1024 ** 3)})
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(response.status_code, 413, response.text)
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertEqual(state["chunks"], 0)     # the body was never read

    def test_streaming_limits_are_bounded_and_leave_no_staging(self):
        import asyncio
        from peekseed import security

        with temp_data_root() as root:
            class HugeUpload(object):
                """Claims 10 GB; yields 64 KiB chunks until the limit trips."""

                def __init__(self):
                    self.chunk = b"%PDF-" + b"x" * (security.UPLOAD_CHUNK_SIZE - 5)

                def read(self, size):
                    return self.chunk

            destination = Path(root) / "staging" / "huge.pdf"
            tracemalloc.start()
            with self.assertRaises(security.UploadLimitExceeded):
                asyncio.run(security.stream_upload(HugeUpload(), destination))
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertFalse(destination.exists())
            self.assertEqual(security.MAX_UPLOAD_FILES, 50)
            self.assertEqual(security.MAX_UPLOAD_TOTAL_BYTES, 200 * 1024 * 1024)

            with self.assertRaises(security.UploadLimitExceeded):
                asyncio.run(security.stream_upload(
                    HugeUpload(), destination, max_bytes=4096,
                    max_total=1024 * 1024))


if __name__ == "__main__":
    unittest.main()
