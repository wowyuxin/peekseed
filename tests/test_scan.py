"""Tests for the workspace scan: audit §6.10 gates 1, 2, 3 plus isolation/prune.

Gate 1: ``init`` creates the tree and a scan leaves ``documents/`` untouched.
Gate 2: drop PDF A -> scan -> indexed; scan again -> 0 new/0 changed and the
        fake model server receives **no** additional embedding requests.
Gate 3: add PDF B to an indexed collection -> scan -> B is searchable through
        the engine's index.

Also covered: corrupt-file isolation, unsupported extensions, symlink escapes,
moved/renamed files (path update, stable content ids), the extraction cache and
``prune`` (explicit data loss). All tests are offline: embeddings come from
``tests.fixtures.FakeModelServer``.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests.fixtures import FakeModelServer, make_pdf

from peekseed import config, contracts, scan, workspace


# --- environment helpers ---------------------------------------------------


@contextlib.contextmanager
def working_directory(path):
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def clean_peekseed_env():
    """Remove every PEEKSEED_* variable, then restore the process environment."""
    saved = {key: os.environ.pop(key) for key in list(os.environ)
             if key.startswith("PEEKSEED_")}
    workspace._WARNED.clear()
    config.reset_cache()
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("PEEKSEED_")]:
            os.environ.pop(key, None)
        os.environ.update(saved)
        workspace._WARNED.clear()
        config.reset_cache()


@contextlib.contextmanager
def initialized_workspace():
    """A temporary workspace rooted at ``<tmp>/workspace`` with cwd inside tmp."""
    with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
        with working_directory(tmp):
            with contextlib.redirect_stdout(io.StringIO()):
                workspace.init()
            yield workspace.resolve()


def _link(fake):
    return {
        "id": "fake",
        "role": "embed",
        "label": "Fake",
        "protocol": "openai",
        "base_url": fake.openai_base,
        "model": "test-embed",
        "api_key": "",
        "enabled": True,
    }


def _embed_requests(fake):
    return sum(
        1 for path, _body, _headers in fake.requests
        if path.rstrip("/") in ("/v1/embeddings", "/embeddings", "/embed")
    )


def _snapshot(documents_dir):
    """Path -> (sha256, size, mtime_ns) for every file under ``documents/``."""
    snapshot = {}
    for path in sorted(Path(documents_dir).rglob("*")):
        if path.is_file():
            stat_result = path.stat()
            snapshot[str(path.relative_to(documents_dir))] = (
                contracts.sha256_file(path),
                stat_result.st_size,
                stat_result.st_mtime_ns,
            )
    return snapshot


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- extract module availability ------------------------------------------


def _install_extract_stub():
    """Inject a minimal ``peekseed.extract`` only if the real one is missing.

    W3 owns the real module (on disk); this keeps the scan tests runnable in a
    checkout where it has not landed yet, without hiding a broken real module.
    """
    try:
        from peekseed import extract  # noqa: F401
        return
    except Exception:
        pass
    from peekseed.ingest.pdf import IngestError, extract_pdf

    stub = types.ModuleType("peekseed.extract")
    stub.EXTRACTOR_API = 1
    stub.SUPPORTED_EXTENSIONS = {"pdf": ".pdf", "markdown": ".md", "text": ".txt"}
    stub.OcrRequired = IngestError

    def extract(path, *, ocr=None):
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            pages = extract_pdf(path)
        elif suffix in (".md", ".txt"):
            pages = [{"page": 1, "text": path.read_text(encoding="utf-8")}]
        else:
            raise IngestError(
                "Unsupported file type '%s'." % (suffix or "unknown")
            )
        return {
            "pages": pages,
            "extractor": {"name": "stub", "version": "1"},
            "pages_count": len(pages),
            "needs_ocr": False,
        }

    def extractor_identity_for(path):
        suffix = Path(path).suffix.lower()
        if suffix not in (".pdf", ".md", ".txt"):
            raise IngestError(
                "Unsupported file type '%s'." % (suffix or "unknown")
            )
        return {"name": "stub", "version": "1"}

    def cache_key(sha256, extractor):
        return str(sha256) + ".json"

    stub.extract = extract
    stub.extractor_identity_for = extractor_identity_for
    stub.cache_key = cache_key
    stub.available_extractors = lambda: ["stub@1"]
    stub.ocr_available = lambda: False
    sys.modules["peekseed.extract"] = stub
    from peekseed import extract as package
    package.extract = stub


_install_extract_stub()


# --- gate 1: init tree + documents untouched -------------------------------


class InitAndDocumentsTests(unittest.TestCase):
    def test_scan_never_writes_documents(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "CXL_spec.pdf",
                     pages=["CXL overview. The CXL protocol is coherent."])
            make_pdf(ws.documents_dir / "manual.pdf",
                     pages=["Manual page for the widget: mount it on the frame "
                            "with two screws."])
            before = _snapshot(ws.documents_dir)
            self.assertEqual(len(before), 2)
            with FakeModelServer(dim=8) as fake:
                report = scan.scan(link=_link(fake))
            self.assertEqual(_snapshot(ws.documents_dir), before)
            self.assertEqual(report["counts"], {
                "new": 2, "changed": 0, "unchanged": 0, "missing": 0,
                "errors": 0,
            })
            self.assertEqual(report["scanned"], 2)
            self.assertTrue(ws.manifest_file.is_file())
            self.assertTrue((ws.datasets_dir / "cxl" / "dataset.json").is_file())
            self.assertTrue((ws.datasets_dir / "cxl" / "chunks.jsonl").is_file())
            self.assertTrue((Path(ws.root).parent / ".gitignore").is_file())
            manifest = contracts.validate_manifest(_read_json(ws.manifest_file))
            self.assertEqual(len(manifest["documents"]), 2)
            for entry in manifest["documents"]:
                self.assertIn(entry["status"], contracts.FILE_STATUSES)

    def test_report_shape(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                     pages=["CXL is a coherent interconnect protocol for devices."])
            with FakeModelServer(dim=8) as fake:
                report = scan.scan(link=_link(fake))
            self.assertEqual(set(report["workspace"]), {
                "root", "documents_dir", "derived_dir", "manifest_file",
                "config_dir", "links_file", "settings_file", "datasets_dir",
                "extracted_dir", "history_dir", "jobs_dir", "uploads_dir",
            })
            self.assertEqual(report["workspace_root"], str(ws.root))
            for key in ("started_at", "finished_at", "counts", "scanned",
                        "files", "collections", "embed_calls"):
                self.assertIn(key, report)
            self.assertEqual(set(report["collections"]["cxl"]), {
                "status", "sources", "chunks", "embed_calls", "reasons",
            })

    def test_scan_requires_an_initialized_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with self.assertRaises(RuntimeError) as caught:
                    scan.scan()
                self.assertIn("peekseed init", str(caught.exception))


# --- gate 2: a second scan is free ----------------------------------------


class IncrementalScanTests(unittest.TestCase):
    def test_second_scan_makes_zero_embed_calls(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                     pages=["CXL overview. The CXL protocol is coherent and "
                            "device-attached."])
            with FakeModelServer(dim=8) as fake:
                first = scan.scan(link=_link(fake))
                self.assertEqual(first["counts"]["new"], 1)
                self.assertEqual(first["collections"]["cxl"]["status"], "indexed")
                self.assertGreater(first["collections"]["cxl"]["embed_calls"], 0)
                requests_after_first = _embed_requests(fake)
                self.assertGreater(requests_after_first, 0)

                second = scan.scan(link=_link(fake))
                self.assertEqual(second["counts"], {
                    "new": 0, "changed": 0, "unchanged": 1, "missing": 0,
                    "errors": 0,
                })
                self.assertEqual(second["embed_calls"], 0)
                self.assertEqual(second["collections"]["cxl"]["embed_calls"], 0)
                # No additional HTTP embedding request reached the fake server.
                self.assertEqual(_embed_requests(fake), requests_after_first)

                # The canonical chunk file was not rewritten either.
                chunks_path = ws.datasets_dir / "cxl" / "chunks.jsonl"
                before = chunks_path.read_bytes()
                third = scan.scan(link=_link(fake))
                self.assertEqual(third["counts"]["unchanged"], 1)
                self.assertEqual(chunks_path.read_bytes(), before)
                self.assertEqual(_embed_requests(fake), requests_after_first)


# --- gate 3: added file is searchable --------------------------------------


class AddedFileTests(unittest.TestCase):
    def test_added_file_becomes_searchable(self):
        with initialized_workspace() as ws:
            first_pdf = make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                                 pages=["CXL overview. The CXL protocol is coherent and device-attached."])
            with FakeModelServer(dim=8) as fake:
                scan.scan(link=_link(fake))
                before_ids = _chunk_ids(ws.datasets_dir / "cxl")
                second_pdf = make_pdf(
                    ws.documents_dir / "cxl" / "b.pdf",
                    pages=["Torque wrench. Tighten the bolt with the torque "
                           "wrench to four newton metres."],
                )
                report = scan.scan(link=_link(fake))
                self.assertEqual(report["counts"]["new"], 1)
                self.assertEqual(report["counts"]["changed"], 0)
                after_ids = _chunk_ids(ws.datasets_dir / "cxl")
                # Content-derived ids: file A's ids are untouched by file B.
                self.assertTrue(before_ids.issubset(after_ids))
                self.assertTrue(_searchable(ws, fake, _link(fake), "cxl",
                                            "torque wrench"))
                self.assertEqual(report["collections"]["cxl"]["status"],
                                 "indexed")
                # Both files are recorded as sources with content hashes.
                dataset = contracts.validate_dataset(
                    _read_json(ws.datasets_dir / "cxl" / "dataset.json"))
                names = sorted(source["name"] for source in dataset["sources"])
                self.assertEqual(names, ["a.pdf", "b.pdf"])
                for source in dataset["sources"]:
                    self.assertTrue(source["sha256"])
                self.assertTrue(first_pdf.is_file() and second_pdf.is_file())


def _chunk_ids(dataset_dir):
    ids = set()
    path = Path(dataset_dir) / "chunks.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return ids


def _words(text):
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _searchable(ws, fake, link, collection, query):
    """True when the engine's loaded index returns a chunk containing ``query``."""
    from peekseed.engine import IndexStore

    store = IndexStore(ws.datasets_dir)
    store.load()
    index = store.get(collection, contracts.space_key(link))
    if index is None:
        return False
    order, _reranked, _scores, _timings = index.search(
        fake.vector(query), query, top=5, rerank=False
    )
    wanted = _words(query)
    for row in order[:5]:
        if wanted.issubset(_words(index.meta[row].get("text"))):
            return True
    return False


# --- failure isolation -----------------------------------------------------


class FailureIsolationTests(unittest.TestCase):
    def test_one_corrupt_file_does_not_abort_the_scan(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "good.pdf",
                     pages=["Widget operation. Press the green button to start "
                            "the widget."])
            broken = ws.documents_dir / "cxl" / "broken.pdf"
            broken.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
            with FakeModelServer(dim=8) as fake:
                report = scan.scan(link=_link(fake))
            self.assertEqual(report["counts"]["errors"], 1)
            self.assertEqual(report["counts"]["new"], 1)
            broken_status = next(
                item for item in report["files"]
                if item["path"] == "cxl/broken.pdf"
            )
            self.assertEqual(broken_status["status"], "error")
            self.assertTrue(broken_status["reason"])
            self.assertNotIn(str(ws.root), broken_status["reason"])
            collection = report["collections"]["cxl"]
            self.assertEqual(collection["status"], "error")
            self.assertGreaterEqual(collection["sources"], 2)
            # The healthy part was chunked and indexed anyway.
            self.assertGreater(collection["chunks"], 0)
            self.assertGreater(collection["embed_calls"], 0)
            with FakeModelServer(dim=8) as fake2:
                self.assertTrue(_searchable(ws, fake2, _link(fake2), "cxl",
                                            "green button"))
            dataset = contracts.validate_dataset(
                _read_json(ws.datasets_dir / "cxl" / "dataset.json"))
            statuses = sorted(source["status"] for source in dataset["sources"])
            self.assertIn("error", statuses)
            self.assertIn("indexed", statuses)

    def test_unsupported_file_is_skipped_not_an_error(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "good.pdf", pages=["Widget text describes how the widget is mounted on the frame."])
            (ws.documents_dir / "cxl" / "notes.xyz").write_text("not a document")
            with FakeModelServer(dim=8) as fake:
                report = scan.scan(link=_link(fake))
            skipped = next(
                item for item in report["files"]
                if item["path"] == "cxl/notes.xyz"
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertIn("Unsupported", skipped["reason"])
            self.assertEqual(report["counts"]["errors"], 0)
            self.assertNotEqual(report["collections"]["cxl"]["status"], "error")

    def test_rebuild_reports_the_final_state_not_pre_build_reasons(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                     pages=["CXL overview. The CXL protocol is coherent and "
                            "device-attached."])
            with FakeModelServer(dim=8) as fake:
                scan.scan(link=_link(fake))
                make_pdf(ws.documents_dir / "cxl" / "b.pdf",
                         pages=["Torque wrench instructions: tighten the bolt "
                                "to four newton metres."])
                report = scan.scan(link=_link(fake))
            collection = report["collections"]["cxl"]
            self.assertEqual(collection["status"], "indexed")
            self.assertGreater(collection["embed_calls"], 0)
            # A successful rebuild replaces stale-index diagnostics: the final
            # line a CLI prints must describe the rebuilt index only.
            for reason in collection["reasons"]:
                self.assertNotIn("source documents changed", reason)
                self.assertNotIn("inputs changed since the index", reason)
                self.assertNotIn("no index for this link", reason)

    def test_symlink_escape_is_rejected_without_reading(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "good.pdf", pages=["Widget text describes how the widget is mounted on the frame."])
            outside = Path(ws.root).parent / "outside-secret.txt"
            outside.write_text("a secret outside the workspace")
            link_path = ws.documents_dir / "cxl" / "escape.pdf"
            os.symlink(str(outside), str(link_path))
            with FakeModelServer(dim=8) as fake:
                report = scan.scan(link=_link(fake))
                escaped = next(
                    item for item in report["files"]
                    if item["path"] == "cxl/escape.pdf"
                )
                self.assertEqual(escaped["status"], "error")
                self.assertEqual(escaped["reason"],
                                 "symlink escapes the workspace")
                # The in-workspace document next to it was still indexed.
                self.assertTrue(_searchable(ws, fake, _link(fake), "cxl",
                                            "Widget"))
                self.assertFalse(_searchable(ws, fake, _link(fake), "cxl",
                                             "secret outside"))

    def test_rename_is_a_path_update_with_stable_ids(self):
        with initialized_workspace() as ws:
            first = ws.documents_dir / "cxl" / "a.pdf"
            make_pdf(first, pages=["Torque wrench. Tighten the bolt."])
            with FakeModelServer(dim=8) as fake:
                scan.scan(link=_link(fake))
                ids_before = _chunk_ids(ws.datasets_dir / "cxl")
                cache_files = list(ws.extracted_dir.glob("*.json"))
                self.assertEqual(len(cache_files), 1)
                cache_mtime = cache_files[0].stat().st_mtime_ns
                renamed = ws.documents_dir / "cxl" / "renamed.pdf"
                os.rename(str(first), str(renamed))
                report = scan.scan(link=_link(fake))
                self.assertEqual(report["counts"]["new"], 0)
                self.assertEqual(report["counts"]["changed"], 0)
                self.assertEqual(report["counts"]["unchanged"], 1)
                moved = next(
                    item for item in report["files"]
                    if item["path"] == "cxl/renamed.pdf"
                )
                self.assertEqual(moved["status"], "unchanged")
                self.assertEqual(moved["moved_from"], "cxl/a.pdf")
                # Content ids survive a rename; only the source name changed.
                self.assertEqual(_chunk_ids(ws.datasets_dir / "cxl"), ids_before)
                rows = [
                    json.loads(line)
                    for line in (ws.datasets_dir / "cxl" / "chunks.jsonl")
                    .read_text(encoding="utf-8").splitlines() if line.strip()
                ]
                self.assertEqual({row["source"] for row in rows}, {"renamed.pdf"})
                # The extraction cache was hit, not re-written.
                self.assertEqual(cache_files[0].stat().st_mtime_ns, cache_mtime)


class CorruptIndexTests(unittest.TestCase):
    def test_corrupt_vectors_are_needs_reindex_not_indexed(self):
        """Gate 5: a damaged vectors.npy is surfaced, never silently served."""
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "good.pdf",
                     pages=["Widget operation. Press the green button to start "
                            "the widget."])
            with FakeModelServer(dim=8) as fake:
                first = scan.scan(link=_link(fake))
                self.assertEqual(first["collections"]["cxl"]["status"], "indexed")
                space, info_path = workspace.index_info_files(
                    ws.datasets_dir / "cxl")[0]
                self.assertEqual(space, contracts.space_key(_link(fake)))
                vectors = info_path.parent / "vectors.npy"
                raw = vectors.read_bytes()
                vectors.write_bytes(raw[: len(raw) // 2])
                status = workspace.status()
                collection = status["collections"]["cxl"]
                self.assertEqual(collection["status"], "needs_reindex")
                self.assertTrue(collection["reasons"])
                requests_before = _embed_requests(fake)
                report = scan.scan(link=_link(fake))
                self.assertEqual(report["collections"]["cxl"]["status"],
                                 "needs_reindex")
                self.assertEqual(report["embed_calls"], 0)
                self.assertTrue(report["collections"]["cxl"]["reasons"])
                # Surfacing corruption must not burn embeddings or quietly
                # rebuild the generation behind a scan's back.
                self.assertEqual(_embed_requests(fake), requests_before)
                self.assertEqual(scan.scan()["collections"]["cxl"]["status"],
                                 "needs_reindex")


class CacheTests(unittest.TestCase):
    def test_extraction_cache_document_is_content_addressed(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf", pages=["Widget text describes how the widget is mounted on the frame."])
            with FakeModelServer(dim=8) as fake:
                scan.scan(link=_link(fake))
            pdf = ws.documents_dir / "cxl" / "a.pdf"
            sha = contracts.sha256_file(pdf)
            cache_file = ws.extracted_dir / (sha + ".json")
            self.assertTrue(cache_file.is_file())
            data = _read_json(cache_file)
            self.assertEqual(data["sha256"], sha)
            self.assertEqual(set(data["extractor"]), {"name", "version"})
            self.assertIsInstance(data["pages"], list)
            self.assertGreaterEqual(len(data["pages"]), 1)
            self.assertEqual(data["pages"][0]["page"], 1)


# --- prune (explicit data loss) -------------------------------------------


class PruneTests(unittest.TestCase):
    def test_missing_file_is_kept_until_pruned(self):
        with initialized_workspace() as ws:
            pdf = make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                           pages=["Torque wrench. Tighten the bolt."])
            with FakeModelServer(dim=8) as fake:
                scan.scan(link=_link(fake))
                pdf.unlink()
                without_prune = scan.scan(link=_link(fake))
                self.assertEqual(without_prune["counts"]["missing"], 1)
                kept = without_prune["collections"]["cxl"]
                self.assertGreater(kept["chunks"], 0)
                self.assertEqual(kept["embed_calls"], 0)
                manifest = contracts.validate_manifest(
                    _read_json(ws.manifest_file))
                self.assertEqual(manifest["documents"][0]["status"], "missing")
                # The deleted document's chunks still serve until the user says
                # otherwise, which is what a non-pruning scan guarantees.
                self.assertTrue(_searchable(ws, fake, _link(fake), "cxl",
                                            "torque wrench"))

                with_prune = scan.scan(link=_link(fake), prune=True)
                self.assertEqual(with_prune["counts"]["missing"], 1)
                collection = with_prune["collections"]["cxl"]
                self.assertEqual(collection["chunks"], 0)
                self.assertEqual(collection["embed_calls"], 0)
                self.assertEqual(collection["status"], "missing")
                manifest = contracts.validate_manifest(
                    _read_json(ws.manifest_file))
                self.assertEqual(manifest["documents"], [])
                dataset = contracts.validate_dataset(
                    _read_json(ws.datasets_dir / "cxl" / "dataset.json"))
                self.assertEqual(dataset["sources"], [])
                # A prune of an already-empty workspace is a no-op, not a crash.
                again = scan.scan(link=_link(fake), prune=True)
                self.assertEqual(again["counts"]["missing"], 0)


if __name__ == "__main__":
    unittest.main()
