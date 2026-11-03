"""End-to-end acceptance tests for audit §6.10 gates 1-6 and 9-11.

These exercise the storage + ingestion core the way a user does: a real home and
shared folder, real PDFs from ``tests.fixtures``, a fake model server, a fake
peelfeed peel corpus and the CLI/app entry points. Unit coverage for each module
lives beside it (``test_workspace``/``test_ingest``/``test_index``/
``test_security``); this file is the gate evidence that they compose.

Uploads are storage-only (one copy in ``shared/sources``); extraction is a
peelfeed job. A gate therefore reads "upload -> fake peel -> build index ->
indexed".
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import tracemalloc
import unittest
from pathlib import Path

import numpy as np

from tests.fixtures import (EMBED_CHUNK, FakeModelServer, make_pdf, make_peel,
                            temp_data_root)


def fake_link(fake, model="test-embed", **extra):
    link = {
        "id": "fake",
        "role": "embed",
        "label": "Fake",
        "protocol": "openai",
        "base_url": fake.openai_base,
        "model": model,
        "api_key": "",
        "chunk": dict(EMBED_CHUNK),
        "enabled": True,
    }
    link.update(extra)
    return link


@contextlib.contextmanager
def scratch_dir():
    path = Path(tempfile.mkdtemp(prefix="peekseed-acc-"))
    try:
        yield path
    finally:
        shutil.rmtree(str(path), ignore_errors=True)


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


def dataset_sources(dataset_dir):
    from peekseed import contracts
    return contracts.validate_dataset(
        contracts.read_json(Path(dataset_dir) / "dataset.json"))["sources"]


def _dataset_with_source(name, pages, filename="a.pdf"):
    """Create a dataset and store one real PDF in the shared sources pool."""
    from peekseed import config, ingest
    dataset = ingest.create_dataset(config.paths(), name)
    with scratch_dir() as scratch:
        pdf = make_pdf(scratch / filename, pages=pages)
        ingest.add_source(dataset, pdf)
    return dataset


class GateOneTests(unittest.TestCase):
    """1. init creates the home/shared tree; upload stores one shared copy."""

    def test_init_and_upload_leave_one_shared_copy(self):
        with temp_data_root() as home:
            from peekseed import config, workspace
            with contextlib.redirect_stdout(io.StringIO()):
                report = workspace.init()
            self.assertEqual(report["home"], str(home.resolve()))
            shared = config.paths().shared_dir
            self.assertTrue((home / "config" / "links.json").is_file())
            self.assertTrue((shared / "sources").is_dir())
            self.assertTrue((shared / "peels").is_dir())
            dataset = _dataset_with_source("Docs", ["Widget text."])
            stored = list((shared / "sources").rglob("*.pdf"))
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].read_bytes()[:4], b"%PDF")
            self.assertFalse((Path(dataset["dir"]) / "original").exists())
            sources = dataset_sources(dataset["dir"])
            self.assertEqual(sources[0]["status"], "uploaded")
            self.assertEqual(sources[0]["extraction"]["state"], "not_extracted")


class GateTwoTests(unittest.TestCase):
    """2. Upload A -> one stored copy; build twice -> the second is free."""

    def test_upload_is_incremental_and_a_second_index_is_free(self):
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            link = fake_link(fake)
            pages = ["Alpha. The widget is mounted with two screws. " * 20,
                     "Beta. Press the green button to start the widget. " * 20]
            dataset = _dataset_with_source("cxl", pages)
            self.assertEqual(len(list(config.paths().sources_dir.rglob("*.pdf"))), 1)
            make_peel(dataset, pages=pages, source="a.pdf")
            info = ingest.build_index(dataset["dir"], link)
            self.assertGreater(info["n"], 0)
            requests_after_build = len(fake.requests)
            self.assertGreater(requests_after_build, 0)
            chunks = (Path(dataset["dir"]) / "chunks.jsonl").read_text().splitlines()
            self.assertGreaterEqual(len(chunks), 1)
            record = json.loads(chunks[0])
            self.assertEqual(len(record["source_sha256"]), 64)
            self.assertEqual(len(record["id"]), 16)

            again = ingest.build_index(dataset["dir"], link)
            self.assertTrue(again.get("up_to_date"))
            self.assertEqual(len(fake.requests), requests_after_build)
            from peekseed import workspace
            self.assertEqual(
                collection_state(workspace.status(), "cxl")["status"], "indexed")


class GateThreeTests(unittest.TestCase):
    """3. Add PDF B, extract it, build -> B searchable; A's ids stable."""

    def test_adding_a_file_makes_it_searchable_without_renumbering_others(self):
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import config, engine, ingest
            link = fake_link(fake)
            alpha = ["Alpha. The widget is mounted with two screws. " * 20]
            dataset = _dataset_with_source("cxl", alpha, filename="a.pdf")
            make_peel(dataset, pages=alpha, source="a.pdf")
            ingest.build_index(dataset["dir"], link)
            dataset_dir = Path(dataset["dir"])
            a_ids = sorted(row["id"] for row in ingest.read_chunks(dataset_dir)
                           if row.get("source") == "a.pdf")
            self.assertGreaterEqual(len(a_ids), 1)

            sprocket = ["Sprocket calibration. Turn the sprocket clockwise. " * 20]
            with scratch_dir() as scratch:
                ingest.add_source(dataset,
                                  make_pdf(scratch / "b.pdf", pages=sprocket))
            make_peel(dataset, pages=sprocket, source="b.pdf")
            ingest.build_index(dataset["dir"], link)

            after_ids = sorted(row["id"] for row in ingest.read_chunks(dataset_dir)
                               if row.get("source") == "a.pdf")
            self.assertEqual(a_ids, after_ids)

            query = "sprocket calibration"
            qvec = ingest.embed_query(link, query)
            store = engine.IndexStore(str(config.paths().datasets_dir))
            store.load()
            index = store.find("cxl", "fake", "test-embed")
            self.assertIsNotNone(index, "no index loaded for the collection")
            order, _reranked, _scores, _timings = index.search(qvec, query, top=5)
            top_sources = [str(index.meta[i].get("source") or "") for i in order[:3]]
            self.assertIn("b.pdf", top_sources)


class GateFourTests(unittest.TestCase):
    """4. A cancelled/failed build leaves the previous index serving; restart resumes."""

    def test_interrupted_build_keeps_the_previous_index_and_resumes(self):
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import config, contracts, ingest
            link = fake_link(fake)
            dataset = ingest.create_dataset(config.paths(), "cxl")
            pages_by_source = {}
            with scratch_dir() as scratch:
                for index in range(3):
                    pages = ["Widget paragraph %d. " % index * 20,
                             "Another widget paragraph %d. " % index * 20]
                    name = "doc%d.pdf" % index
                    pages_by_source[name] = pages
                    ingest.add_source(dataset, make_pdf(scratch / name, pages=pages))
            dataset_dir = Path(dataset["dir"])
            for name, pages in pages_by_source.items():
                make_peel(dataset, pages=pages, source=name)
            first = ingest.build_index(dataset_dir, link, batch_size=2)
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
                                   force=True, cancel=cancel)
            self.assertEqual(
                (generation_dir.parent.parent / "CURRENT").read_text().strip(),
                committed,
            )
            still_serving = ingest.load_index(dataset_dir, info["space"])
            self.assertEqual(still_serving["info"]["generation"], committed)

            # A process death leaves `running` records; they are marked interrupted
            # (resumable), never converted into a false failure.
            jobs_dir = config.paths().jobs_dir
            jobs_dir.mkdir(parents=True, exist_ok=True)
            job = contracts.new_job("index", collection="cxl", job_id="job-crash")
            job["state"] = "running"
            contracts.write_json_atomic(jobs_dir / "job-crash.json", job)
            self.assertEqual(ingest.mark_interrupted_jobs(jobs_dir), ["job-crash"])
            self.assertEqual(
                json.loads((jobs_dir / "job-crash.json").read_text())["state"],
                "interrupted",
            )

            # Restart: resume the incomplete generation and commit it.
            resumed = ingest.build_index(dataset_dir, link, batch_size=2)
            self.assertEqual(
                (generation_dir.parent.parent / "CURRENT").read_text().strip(),
                resumed["generation"],
            )
            self.assertEqual(resumed["n"], info["n"])


class GateFiveTests(unittest.TestCase):
    """5. Corrupt vectors.npy -> collection needs_reindex, never silently vanished."""

    def test_corrupt_vectors_are_reported(self):
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import ingest, workspace
            link = fake_link(fake)
            pages = ["Widget text about buttons and screws. " * 20]
            dataset = _dataset_with_source("cxl", pages)
            make_peel(dataset, pages=pages, source="a.pdf")
            dataset_dir = Path(dataset["dir"])
            ingest.build_index(dataset_dir, link)
            generation_dir = current_generation_dir(dataset_dir)
            (generation_dir / "vectors.npy").write_bytes(b"corrupt")

            collection = collection_state(workspace.status(), "cxl")
            self.assertEqual(collection["status"], "needs_reindex")
            self.assertTrue(collection["reasons"])

            info = ingest.dataset_indexes(dataset_dir)[0]
            with self.assertRaises(ingest.NeedsReindex):
                ingest.load_index(dataset_dir, info["space"])
            self.assertEqual(ingest.index_state(dataset_dir, info["space"])["status"],
                             "needs_reindex")


class GateSixTests(unittest.TestCase):
    """6. Change chunker version -> collection stale; reindex --dry-run prints scope."""

    def test_chunker_change_is_stale_and_dry_run_prints_the_scope(self):
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import config, contracts, ingest, links, workspace
            link = fake_link(fake)
            pages = ["Widget text about buttons and screws. " * 20]
            dataset = _dataset_with_source("cxl", pages)
            make_peel(dataset, pages=pages, source="a.pdf")
            dataset_dir = Path(dataset["dir"])
            ingest.build_index(dataset_dir, link)
            info = ingest.dataset_indexes(dataset_dir)[0]
            space_dir = dataset_dir / "index" / info["space"]
            committed = (space_dir / "CURRENT").read_text().strip()

            original = contracts.CHUNKER_VERSION
            contracts.CHUNKER_VERSION = original + 1
            try:
                collection = collection_state(workspace.status(), "cxl")
                self.assertEqual(collection["status"], "stale")
                self.assertTrue(any("chunker" in reason
                                    for reason in collection["reasons"]))

                # reindex --dry-run prints the exact scope and writes nothing.
                saved = links.create({"role": "embed", "label": "Fake",
                                      "protocol": "openai",
                                      "base_url": fake.openai_base,
                                      "model": "test-embed", "enabled": True,
                                      "chunk": dict(EMBED_CHUNK)})
                out = io.StringIO()
                from peekseed.__main__ import main
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["reindex", "--all", "--link",
                                           saved["id"], "--dry-run"]), 0)
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
        with temp_data_root() as home, FakeModelServer(dim=8) as fake:
            from peekseed import config, contracts, ingest
            from peekseed.app import create_app
            from fastapi.testclient import TestClient

            dataset = ingest.create_dataset(config.paths(), "Docs")
            with scratch_dir() as scratch:
                pdf = make_pdf(scratch / "doc.pdf", pages=["Widget text."])
                ingest.add_source(dataset, pdf)
                pdf_bytes = pdf.read_bytes()
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

            dataset_dir = Path(dataset["dir"])
            raw = contracts.read_json(dataset_dir / "dataset.json")
            sha = raw["sources"][0]["sha256"]
            folder = config.paths().sources_dir / sha
            outside = home / "secret.pdf"
            outside.write_bytes(b"%PDF-1.4\nsecret")
            os.symlink(str(outside), str(folder / "escape.pdf"))
            raw["sources"].append({
                "name": "escape.pdf",
                "path": "sources/%s/escape.pdf" % sha,
                "sha256": sha, "bytes": 10, "status": "uploaded",
                "extraction": contracts.extraction_default(),
            })
            contracts.write_json_atomic(dataset_dir / "dataset.json",
                                        contracts.validate_dataset(raw))
            response = client.get("/pdf/%s/escape.pdf" % dataset["id"])
            self.assertIn(response.status_code, (400, 404))

            # Upload filename zip-slip: the stored source can never escape the
            # shared sources pool (the same bytes reuse the existing copy).
            files = {"files": ("../../evil.pdf", pdf_bytes, "application/pdf")}
            response = client.post("/api/datasets", data={"name": "Slip"}, files=files)
            self.assertEqual(response.status_code, 201, response.text)
            created = response.json()["dataset"]["id"]
            self.assertFalse((config.paths().datasets_dir / created / "original").exists())
            self.assertFalse((home / "evil.pdf").exists())
            self.assertFalse((home.parent / "evil.pdf").exists())
            self.assertEqual(
                len([path for path in config.paths().sources_dir.rglob("*.pdf")
                     if not path.is_symlink()]), 1)
            row = client.get("/api/datasets/%s/sources"
                             % created).json()["sources"][0]
            self.assertTrue(row["present"])
            self.assertTrue(Path(row["path"]).resolve().is_relative_to(
                config.paths().sources_dir))


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
