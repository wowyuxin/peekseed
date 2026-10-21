"""Corpus import contract: manifest schema, idempotency, zero re-embed and probe.

These tests run the real importer against a temporary workspace and the fake
model server: a source dataset is built once, then imported under a different,
deterministic collection id. The assertions that matter are the contract's:
the second import writes nothing and embeds no chunks, the copied index serves
without re-embedding, and an embedder that no longer matches the stored probe
aborts the import instead of producing a silently wrong space.
"""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import FakeModelServer, make_pdf


def _reset():
    from peekseed import app as app_module
    from peekseed import config, links

    config.reset_cache()
    links.reset_cache()
    app_module.reset_state()


@contextlib.contextmanager
def temp_workspace():
    """Point PEEKSEED_WORKSPACE at a throwaway tree (no PEEKSEED_DATA)."""
    previous = {key: os.environ.get(key)
                for key in ("PEEKSEED_WORKSPACE", "PEEKSEED_DATA")}
    root = Path(tempfile.mkdtemp(prefix="peekseed-import-test-"))
    os.environ["PEEKSEED_WORKSPACE"] = str(root)
    os.environ.pop("PEEKSEED_DATA", None)
    _reset()
    try:
        yield root
    finally:
        _reset()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(str(root), ignore_errors=True)


class ImportTestBase(unittest.TestCase):
    def _link(self, fake):
        from peekseed import config, links

        config.ensure()
        return links.create({
            "role": "embed", "label": "Fake embed", "protocol": "openai",
            "base_url": fake.openai_base, "model": "test-embed",
            "enabled": True,
        })

    def _manifest(self, root, collections, schema=1):
        path = Path(root) / "corpus.json"
        path.write_text(json.dumps({"schema": schema,
                                    "collections": collections}), encoding="utf-8")
        return path

    def _pdf(self, root, name="manual.pdf", pages=None):
        return make_pdf(Path(root) / name,
                        pages=pages or ["Widget installation and operation."] * 3)

    def _source_dataset(self, fake, pages=None):
        from peekseed import config, ingest, links

        dataset = ingest.create_dataset(config.paths(), "Bundle Source")
        pdf = self._pdf(config.paths().workspace_root, pages=pages)
        ingest.add_source(dataset, pdf)
        info = ingest.build_index(Path(dataset["dir"]), links.active("embed"))
        return dataset, info


class ManifestTests(ImportTestBase):
    def test_schema_newer_is_refused_with_an_actionable_message(self):
        from peekseed import importer

        with temp_workspace() as root:
            path = self._manifest(root, [{"name": "cxl", "documents": []}],
                                  schema=2)
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.load_manifest(path)
            self.assertIn("newer", str(raised.exception))
            self.assertIn("schema 2", str(raised.exception))

    def test_invalid_manifests_are_rejected_before_any_write(self):
        from peekseed import importer

        with temp_workspace() as root:
            cases = [
                {"schema": 1, "collections": []},
                {"schema": 1, "collections": [{"name": ""}]},
                {"schema": 1, "collections": [{"name": "a"}]},
                {"schema": 1, "collections": [
                    {"name": "a", "documents": ["x.pdf"]},
                    {"name": "A", "documents": ["y.pdf"]}]},
                {"schema": 1, "collections": [
                    {"name": "a", "dataset": "does-not-exist"}]},
            ]
            for payload in cases:
                path = Path(root) / "bad.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(importer.CorpusImportError, msg=payload):
                    importer.load_manifest(path)
            self.assertEqual(list((root / ".peekseed" / "datasets").glob("*")), [])

    def test_collection_ids_are_deterministic_slugs(self):
        from peekseed import importer

        with temp_workspace() as root:
            path = self._manifest(root, [
                {"name": "CXL", "documents": ["a.pdf"]},
                {"name": "PCIe 5.0", "documents": ["b.pdf"]},
            ])
            manifest = importer.load_manifest(path)
            ids = [row["id"] for row in manifest["collections"]]
            self.assertEqual(ids, ["cxl", "pcie-5.0"])


class PrebuiltImportTests(ImportTestBase):
    def test_prebuilt_index_is_copied_without_reembedding_then_skipped(self):
        from peekseed import importer, ingest

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            source, info = self._source_dataset(fake)
            calls_before = len(fake.requests)
            manifest = self._manifest(root, [{
                "name": "CXL", "title": "CXL",
                "dataset": source["dir"],
            }])
            report = importer.import_corpus(manifest)
            target = root / ".peekseed" / "datasets" / "cxl"
            self.assertTrue((target / "dataset.json").is_file())
            row = report["collections"][0]
            self.assertEqual(row["status"], "imported")
            self.assertEqual(row["kind"], "prebuilt")
            self.assertEqual(row["id"], "cxl")
            self.assertEqual(row["space"], info["space"])
            self.assertGreaterEqual(row["probe_cosine"], 0.999)
            # Exactly one new embed call: the probe canary. No chunk was embedded.
            self.assertEqual(len(fake.requests) - calls_before, 1)

            loaded = ingest.load_index(target, info["space"])
            self.assertEqual(loaded["info"]["dataset"], "cxl")
            self.assertEqual(len(loaded["meta"]), len(ingest.read_chunks(target)))

            calls_before = len(fake.requests)
            second = importer.import_corpus(manifest)
            self.assertEqual(second["collections"][0]["status"], "skipped")
            self.assertEqual(len(fake.requests) - calls_before, 1)

    def test_embedder_swap_aborts_the_import_and_leaves_no_target(self):
        from peekseed import importer, links

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            source, _info = self._source_dataset(fake)
            with FakeModelServer(dim=16) as swapped:
                # Same link id/model, different server: only the probe can tell.
                first = links.active("embed")
                links.update(first["id"], {"base_url": swapped.openai_base})
                manifest = self._manifest(root, [{
                    "name": "CXL", "dataset": source["dir"],
                }])
                with self.assertRaises(importer.CorpusImportError) as raised:
                    importer.import_corpus(manifest)
                self.assertIn("embedding space", str(raised.exception).lower())
                self.assertFalse((root / ".peekseed" / "datasets" / "cxl").exists())

    def test_explicit_matching_documents_import_cleanly(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            source, info = self._source_dataset(fake)
            source_pdf = Path(source["dir"]) / "original" / "manual.pdf"
            manifest = self._manifest(root, [{
                "name": "CXL", "dataset": source["dir"],
                "documents": [str(source_pdf)],
            }])
            report = importer.import_corpus(manifest)
            self.assertEqual(report["collections"][0]["status"], "imported")
            self.assertEqual(report["collections"][0]["space"], info["space"])

    def test_explicit_document_that_differs_from_the_bundle_is_rejected(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            source, _info = self._source_dataset(fake)
            other_dir = root / "different"
            other_dir.mkdir()
            other = self._pdf(other_dir, pages=["A completely different document."] * 3)
            manifest = self._manifest(root, [{
                "name": "CXL", "dataset": source["dir"],
                "documents": [str(other)],
            }])
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.import_corpus(manifest)
            self.assertIn("different file", str(raised.exception))
            self.assertFalse((root / ".peekseed" / "datasets" / "cxl").exists())

    def test_prebuilt_without_chunks_jsonl_is_rejected(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            source, _info = self._source_dataset(fake)
            (Path(source["dir"]) / "chunks.jsonl").unlink()
            manifest = self._manifest(root, [{
                "name": "CXL", "dataset": source["dir"],
            }])
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.import_corpus(manifest)
            self.assertIn("chunks.jsonl", str(raised.exception))


class DocumentsImportTests(ImportTestBase):
    def test_documents_import_builds_an_index_and_a_second_run_skips(self):
        from peekseed import importer, ingest

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root, pages=["Widget installation. " * 40] * 4)
            manifest = self._manifest(root, [{
                "name": "Docs", "documents": [str(pdf)],
            }])
            calls_before = len(fake.requests)
            report = importer.import_corpus(manifest)
            row = report["collections"][0]
            self.assertEqual(row["status"], "imported")
            self.assertEqual(row["kind"], "documents")
            self.assertEqual(row["id"], "docs")
            self.assertEqual(row["documents"], 1)
            self.assertGreater(row["chunks"], 0)
            self.assertGreater(len(fake.requests) - calls_before, 1)
            target = root / ".peekseed" / "datasets" / "docs"
            self.assertTrue((target / "dataset.json").is_file())
            self.assertTrue(ingest.read_chunks(target))
            self.assertEqual(len(ingest.dataset_indexes(target)), 1)

            calls_before = len(fake.requests)
            second = importer.import_corpus(manifest)
            self.assertEqual(second["collections"][0]["status"], "skipped")
            self.assertEqual(len(fake.requests) - calls_before, 1)

    def test_dry_run_writes_nothing_and_embeds_nothing(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root)
            manifest = self._manifest(root, [{
                "name": "Docs", "documents": [str(pdf)],
            }])
            calls_before = len(fake.requests)
            report = importer.import_corpus(manifest, dry_run=True)
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["collections"][0]["documents_exist"], True)
            self.assertEqual(len(fake.requests), calls_before)
            self.assertFalse((root / ".peekseed" / "datasets" / "docs").exists())

    def test_wrong_sha256_is_rejected_before_the_index_is_built(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root)
            manifest = self._manifest(root, [{
                "name": "Docs", "documents": [str(pdf)],
                "sha256": {"manual.pdf": "0" * 64},
            }])
            calls_before = len(fake.requests)
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.import_corpus(manifest)
            self.assertIn("sha256", str(raised.exception))
            self.assertFalse((root / ".peekseed" / "datasets" / "docs").exists())
            self.assertEqual(len(fake.requests), calls_before)

    def test_cli_import_reports_and_exits_zero(self):
        from peekseed.__main__ import main

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root)
            manifest = self._manifest(root, [{
                "name": "Docs", "documents": [str(pdf)],
            }])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["import", "--manifest", str(manifest),
                             "--workspace", str(root)])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("imported into", out.getvalue())
            self.assertIn("docs", out.getvalue())
            self.assertTrue((root / ".peekseed" / "datasets" / "docs").is_dir())

class ChromaImportTests(ImportTestBase):
    def _export(self, root, pdf, texts, dim=8, fake=None):
        from peekseed import contracts
        sha = contracts.sha256_file(pdf)
        rows = []
        for index, text in enumerate(texts):
            rows.append({
                "id": "chroma-%d" % index,
                "document": text,
                "metadata": {
                    "source": Path(pdf).name,
                    "source_sha256": sha,
                    "page_start": index + 1,
                    "page_end": index + 1,
                    "section": "Widgets",
                    "doc": "Widgets\n\n" + text,
                },
                "embedding": fake.vector(text) if fake else [0.0] * dim,
            })
        path = Path(root) / "chroma-export.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                        encoding="utf-8")
        return path

    def test_chroma_export_imports_without_reembedding_and_is_idempotent(self):
        from peekseed import importer, ingest

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root)
            texts = ["Widget installation and operation.",
                     "Press the green button to start the widget.",
                     "Service intervals and spare parts."]
            export = self._export(root, pdf, texts, fake=fake)
            manifest = self._manifest(root, [{
                "name": "DDR5", "documents": [str(pdf)],
                "chroma_export": str(export),
            }])
            calls_before = len(fake.requests)
            report = importer.import_corpus(manifest)
            row = report["collections"][0]
            self.assertEqual(row["status"], "imported")
            self.assertEqual(row["kind"], "chroma")
            self.assertEqual(row["chunks"], len(texts))
            self.assertGreaterEqual(row["probe_cosine"], 0.999)
            target = root / ".peekseed" / "datasets" / "ddr5"
            loaded = ingest.load_index(target, row["space"])
            self.assertEqual(len(loaded["meta"]), len(texts))
            self.assertEqual([meta["text"] for meta in loaded["meta"]], texts)
            # Only the probe is embedded (once to write it, once to verify it).
            self.assertEqual(len(fake.requests) - calls_before, 2)

            calls_before = len(fake.requests)
            second = importer.import_corpus(manifest)
            self.assertEqual(second["collections"][0]["status"], "skipped")
            self.assertEqual(len(fake.requests) - calls_before, 1)

    def test_chroma_export_with_mixed_dimensions_is_rejected(self):
        from peekseed import importer

        with temp_workspace() as root, FakeModelServer(dim=8) as fake:
            self._link(fake)
            pdf = self._pdf(root)
            export = self._export(root, pdf, ["one", "two"], fake=fake)
            with export.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "document": "three",
                    "metadata": {"source": "manual.pdf"},
                    "embedding": [0.0] * 9,
                }) + "\n")
            manifest = self._manifest(root, [{
                "name": "DDR5", "documents": [str(pdf)],
                "chroma_export": str(export),
            }])
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.import_corpus(manifest)
            self.assertIn("mixed dimensions", str(raised.exception))
            self.assertFalse((root / ".peekseed" / "datasets" / "ddr5").exists())

    def test_chroma_export_without_documents_is_rejected(self):
        from peekseed import importer

        with temp_workspace() as root:
            path = Path(root) / "chroma.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            manifest = self._manifest(root, [{
                "name": "DDR5", "chroma_export": str(path),
            }])
            with self.assertRaises(importer.CorpusImportError) as raised:
                importer.load_manifest(manifest)
            self.assertIn("documents", str(raised.exception))
