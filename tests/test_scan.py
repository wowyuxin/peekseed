"""Tests for the storage-only workspace scan.

The scan is discovery/reconciliation now: it hashes files, diffs the manifest,
writes ``dataset.json`` sources (``uploaded`` + ``not_extracted``) and reports the
existing index state read-only. It never extracts, chunks, builds an index or
calls an embedding link. Extraction is a peelfeed job (``make_peel`` in tests).
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import EMBED_CHUNK, FakeModelServer, make_pdf, make_peel

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
        "chunk": dict(EMBED_CHUNK),
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


def _sources(dataset_dir):
    dataset = contracts.validate_dataset(
        _read_json(Path(dataset_dir) / "dataset.json"))
    return dataset["sources"]


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
            dataset_dir = ws.datasets_dir / "cxl"
            self.assertTrue((dataset_dir / "dataset.json").is_file())
            # Storage only: no chunks, no index, no embed calls.
            self.assertFalse((dataset_dir / "chunks.jsonl").exists())
            self.assertFalse((dataset_dir / "index").exists())
            self.assertEqual(report["embed_calls"], 0)
            self.assertEqual(_embed_requests(fake), 0)
            sources = _sources(dataset_dir)
            self.assertEqual([source["name"] for source in sources],
                             ["CXL_spec.pdf"])
            self.assertEqual(sources[0]["status"], "uploaded")
            self.assertEqual(sources[0]["extraction"]["state"], "not_extracted")
            self.assertEqual(sources[0]["pages"], 0)
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
            self.assertEqual(report["collections"]["cxl"]["status"], "uploaded")

    def test_scan_requires_an_initialized_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, clean_peekseed_env():
            with working_directory(tmp):
                with self.assertRaises(RuntimeError) as caught:
                    scan.scan()
                self.assertIn("peekseed init", str(caught.exception))


# --- gate 2: a second scan writes nothing and embeds nothing ---------------


class IncrementalScanTests(unittest.TestCase):
    def test_second_scan_is_a_no_op(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                     pages=["CXL overview. The CXL protocol is coherent and "
                            "device-attached."])
            with FakeModelServer(dim=8) as fake:
                first = scan.scan(link=_link(fake))
                self.assertEqual(first["counts"]["new"], 1)
                self.assertEqual(first["collections"]["cxl"]["status"], "uploaded")
                dataset_path = ws.datasets_dir / "cxl" / "dataset.json"
                before_dataset = dataset_path.read_bytes()
                requests_after_first = _embed_requests(fake)
                self.assertEqual(requests_after_first, 0)

                second = scan.scan(link=_link(fake))
                self.assertEqual(second["counts"], {
                    "new": 0, "changed": 0, "unchanged": 1, "missing": 0,
                    "errors": 0,
                })
                self.assertEqual(second["embed_calls"], 0)
                self.assertEqual(_embed_requests(fake), requests_after_first)
                # Unchanged sources are not rewritten (mtime/content stable).
                self.assertEqual(dataset_path.read_bytes(), before_dataset)


# --- discovery of added/moved files ---------------------------------------


class DiscoveryTests(unittest.TestCase):
    def test_added_file_is_discovered(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                     pages=["CXL overview. The CXL protocol is coherent."])
            scan.scan()
            make_pdf(ws.documents_dir / "cxl" / "b.pdf",
                     pages=["Torque wrench. Tighten the bolt to four newton metres."])
            report = scan.scan()
            self.assertEqual(report["counts"]["new"], 1)
            self.assertEqual(report["counts"]["changed"], 0)
            names = sorted(source["name"] for source in
                           _sources(ws.datasets_dir / "cxl"))
            self.assertEqual(names, ["a.pdf", "b.pdf"])
            for source in _sources(ws.datasets_dir / "cxl"):
                self.assertEqual(source["status"], "uploaded")
                self.assertTrue(source["sha256"])

    def test_rename_is_a_path_update_that_keeps_the_extraction(self):
        with initialized_workspace() as ws:
            first = ws.documents_dir / "cxl" / "a.pdf"
            make_pdf(first, pages=["Torque wrench. Tighten the bolt."])
            scan.scan()
            dataset_dir = ws.datasets_dir / "cxl"
            make_peel("cxl", pages=["Torque wrench. Tighten the bolt."],
                      source="a.pdf")
            renamed = ws.documents_dir / "cxl" / "renamed.pdf"
            os.rename(str(first), str(renamed))
            report = scan.scan()
            self.assertEqual(report["counts"]["new"], 0)
            self.assertEqual(report["counts"]["changed"], 0)
            self.assertEqual(report["counts"]["unchanged"], 1)
            moved = next(
                item for item in report["files"]
                if item["path"] == "cxl/renamed.pdf"
            )
            self.assertEqual(moved["status"], "unchanged")
            self.assertEqual(moved["moved_from"], "cxl/a.pdf")
            sources = _sources(dataset_dir)
            self.assertEqual([source["name"] for source in sources],
                             ["renamed.pdf"])
            # The extraction block survives a rename: same content, same peel.
            self.assertEqual(sources[0]["extraction"]["state"], "extracted")
            self.assertTrue(sources[0]["extraction"]["peel_id"])

    def test_changed_content_resets_the_extraction(self):
        with initialized_workspace() as ws:
            pdf = make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                           pages=["Torque wrench. Tighten the bolt."])
            scan.scan()
            make_peel("cxl", pages=["Torque wrench. Tighten the bolt."])
            make_pdf(pdf, pages=["A completely different page about sprockets."])
            report = scan.scan()
            self.assertEqual(report["counts"]["changed"], 1)
            source = _sources(ws.datasets_dir / "cxl")[0]
            self.assertEqual(source["status"], "uploaded")
            self.assertEqual(source["extraction"]["state"], "not_extracted")
            self.assertEqual(source["extraction"]["peel_id"], "")


# --- failure isolation -----------------------------------------------------


class FailureIsolationTests(unittest.TestCase):
    def test_scan_does_not_parse_files(self):
        """A corrupt PDF is still stored: extraction owns format validation."""
        with initialized_workspace() as ws:
            (ws.documents_dir / "cxl").mkdir(parents=True, exist_ok=True)
            broken = ws.documents_dir / "cxl" / "broken.pdf"
            broken.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
            report = scan.scan()
            self.assertEqual(report["counts"]["errors"], 0)
            self.assertEqual(report["counts"]["new"], 1)
            source = _sources(ws.datasets_dir / "cxl")[0]
            self.assertEqual(source["status"], "uploaded")
            self.assertEqual(len(source["sha256"]), 64)

    def test_non_pdf_is_discovered_without_a_format_decision(self):
        with initialized_workspace() as ws:
            (ws.documents_dir / "cxl").mkdir(parents=True, exist_ok=True)
            (ws.documents_dir / "cxl" / "notes.xyz").write_text("not a document")
            report = scan.scan()
            self.assertEqual(report["counts"]["errors"], 0)
            skipped = next(
                item for item in report["files"]
                if item["path"] == "cxl/notes.xyz"
            )
            self.assertEqual(skipped["status"], "new")
            # peekseed stores it; peelfeed's profile decides whether it can be
            # extracted (the file is not silently dropped from the manifest).
            names = [source["name"] for source in
                     _sources(ws.datasets_dir / "cxl")]
            self.assertEqual(names, ["notes.xyz"])

    def test_symlink_escape_is_rejected_without_reading(self):
        with initialized_workspace() as ws:
            make_pdf(ws.documents_dir / "cxl" / "good.pdf",
                     pages=["Widget text describes how the widget is mounted."])
            outside = Path(ws.root).parent / "outside-secret.txt"
            outside.write_text("a secret outside the workspace")
            os.symlink(str(outside), str(ws.documents_dir / "cxl" / "escape.pdf"))
            report = scan.scan()
            escaped = next(
                item for item in report["files"]
                if item["path"] == "cxl/escape.pdf"
            )
            self.assertEqual(escaped["status"], "error")
            self.assertEqual(escaped["reason"], "symlink escapes the workspace")
            # The healthy document next to it is still reconciled.
            names = [source["name"] for source in
                     _sources(ws.datasets_dir / "cxl")]
            self.assertEqual(names, ["good.pdf"])


class CorruptIndexTests(unittest.TestCase):
    def test_corrupt_vectors_are_needs_reindex_not_indexed(self):
        """Gate 5: a damaged vectors.npy is surfaced, never silently served."""
        with initialized_workspace() as ws:
            from peekseed import ingest
            make_pdf(ws.documents_dir / "cxl" / "good.pdf",
                     pages=["Widget operation. Press the green button to start "
                            "the widget."])
            scan.scan()
            dataset_dir = ws.datasets_dir / "cxl"
            make_peel("cxl", pages=["Widget operation. Press the green button."],
                      source="good.pdf")
            with FakeModelServer(dim=8) as fake:
                link = _link(fake)
                ingest.build_index(dataset_dir, link)
                space, info_path = workspace.index_info_files(dataset_dir)[0]
                self.assertEqual(space, contracts.space_key(link))
                vectors = info_path.parent / "vectors.npy"
                raw = vectors.read_bytes()
                vectors.write_bytes(raw[: len(raw) // 2])
                status = workspace.status()
                collection = status["collections"]["cxl"]
                self.assertEqual(collection["status"], "needs_reindex")
                self.assertTrue(collection["reasons"])
                requests_before = _embed_requests(fake)
                report = scan.scan()
                self.assertEqual(report["collections"]["cxl"]["status"],
                                 "needs_reindex")
                self.assertEqual(report["embed_calls"], 0)
                self.assertTrue(report["collections"]["cxl"]["reasons"])
                # Surfacing corruption must not burn embeddings or quietly
                # rebuild the generation behind a scan's back.
                self.assertEqual(_embed_requests(fake), requests_before)


# --- prune (explicit data loss) -------------------------------------------


class PruneTests(unittest.TestCase):
    def test_missing_file_is_kept_until_pruned(self):
        with initialized_workspace() as ws:
            pdf = make_pdf(ws.documents_dir / "cxl" / "a.pdf",
                           pages=["Torque wrench. Tighten the bolt."])
            scan.scan()
            make_peel("cxl", pages=["Torque wrench. Tighten the bolt."])
            pdf.unlink()
            without_prune = scan.scan()
            self.assertEqual(without_prune["counts"]["missing"], 1)
            kept = _sources(ws.datasets_dir / "cxl")
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["status"], "missing")
            # The extraction block is kept: the peel still serves until pruned.
            self.assertEqual(kept[0]["extraction"]["state"], "extracted")
            manifest = contracts.validate_manifest(_read_json(ws.manifest_file))
            self.assertEqual(manifest["documents"][0]["status"], "missing")

            with_prune = scan.scan(prune=True)
            self.assertEqual(with_prune["counts"]["missing"], 1)
            self.assertEqual(_sources(ws.datasets_dir / "cxl"), [])
            manifest = contracts.validate_manifest(_read_json(ws.manifest_file))
            self.assertEqual(manifest["documents"], [])
            # A prune of an already-empty workspace is a no-op, not a crash.
            again = scan.scan(prune=True)
            self.assertEqual(again["counts"]["missing"], 0)


if __name__ == "__main__":
    unittest.main()
