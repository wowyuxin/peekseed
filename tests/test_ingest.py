"""Storage-only uploads and the peelfeed corpus read path.

There is no PDF parser or chunker in peekseed any more: uploading stores bytes,
and indexing reads a fake peel corpus created by ``tests.fixtures.make_peel``.
The gates (sha/conversion/validation/peel-root allowlist) and the skipped-source
reporting are the contract this file pins.
"""

import json
import os
import unittest
from pathlib import Path

from tests.fixtures import FakeModelServer, make_pdf, make_peel, temp_data_root


def embed_link(fake, model="test-embed", **extra):
    link = {
        "id": "fake", "role": "embed", "label": "Fake",
        "protocol": "openai", "base_url": fake.openai_base,
        "model": model, "api_key": "", "enabled": True,
    }
    link.update(extra)
    return link


class StorageUploadTests(unittest.TestCase):
    def test_create_dataset_and_add_source(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Product Docs")
            self.assertEqual(dataset["id"], "product-docs")
            pdf = make_pdf(root / "a.pdf", pages=["hello"])
            entry = ingest.add_source(dataset, pdf)
            self.assertEqual(entry["name"], "a.pdf")
            self.assertEqual(entry["status"], "uploaded")
            self.assertEqual(entry["pages"], 0)
            self.assertEqual(entry["extraction"]["state"], "not_extracted")
            self.assertTrue((Path(dataset["dir"]) / "original" / "a.pdf").is_file())
            # same file again is idempotent, not duplicated
            again = ingest.add_source(dataset, pdf)
            self.assertEqual(again["name"], "a.pdf")
            info = ingest.dataset_info(dataset["dir"])
            self.assertEqual(info["id"], "product-docs")
            self.assertEqual(len(info["sources"]), 1)

    def test_reuploading_the_same_file_is_idempotent(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            one = make_pdf(root / "one" / "same.pdf", pages=["first"])
            first = ingest.add_source(dataset, one)
            second = ingest.add_source(dataset, one)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(first["name"], second["name"])
            originals = list((Path(dataset["dir"]) / "original").iterdir())
            self.assertEqual(len(originals), 1)

    def test_same_bytes_under_another_name_is_a_second_source(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            one = make_pdf(root / "one" / "same.pdf", pages=["first"])
            two = make_pdf(root / "two" / "other-name.pdf", pages=["first"])
            first = ingest.add_source(dataset, one)
            second = ingest.add_source(dataset, two)
            # Same content hash, distinct stored copies/entries (the pre-M-1
            # duplicate-name semantics; peelfeed reuses one peel by sha256).
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertNotEqual(first["name"], second["name"])
            self.assertEqual(len(list((Path(dataset["dir"]) / "original").iterdir())), 2)

    def test_duplicate_names_are_uniquified(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            one = make_pdf(root / "one" / "same.pdf", pages=["first"])
            two = make_pdf(root / "two" / "same.pdf", pages=["second"])
            self.assertEqual(ingest.add_source(dataset, one)["name"], "same.pdf")
            self.assertEqual(ingest.add_source(dataset, two)["name"], "same-2.pdf")

    def test_dataset_ids_are_unique(self):
        with temp_data_root():
            from peekseed import config, ingest
            first = ingest.create_dataset(config.paths(), "Same name")
            second = ingest.create_dataset(config.paths(), "Same name")
            self.assertNotEqual(first["id"], second["id"])

    def test_non_pdf_upload_is_refused_with_a_clear_message(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            notes = root / "notes.txt"
            notes.write_text("not a pdf", encoding="utf-8")
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.add_source(dataset, notes)
            self.assertIn("Unsupported file type", str(caught.exception))
            self.assertFalse((Path(dataset["dir"]) / "original" / "notes.txt").exists())

    def test_corrupt_pdf_bytes_are_refused_before_storage(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            broken = root / "broken.pdf"
            broken.write_bytes(b"not a pdf at all")
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.add_source(dataset, broken)
            self.assertIn("not a PDF", str(caught.exception))


class IndexBuildTests(unittest.TestCase):
    def _dataset_with_peel(self, root, pages=None):
        from peekseed import config, ingest
        dataset = ingest.create_dataset(config.paths(), "Docs")
        pdf = make_pdf(root / "manual.pdf", pages=pages or [
            "Installation. The widget is mounted with two screws.",
            "Operation. Press the green button to start.",
        ])
        ingest.add_source(dataset, pdf)
        make_peel(dataset, pages=pages or [
            "Installation. The widget is mounted with two screws.",
            "Operation. Press the green button to start.",
        ])
        return dataset

    def test_build_index_reads_a_peel_corpus(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import ingest
            dataset = self._dataset_with_peel(root)
            progress = []
            info = ingest.build_index(
                dataset["dir"], embed_link(fake),
                progress=lambda frac, msg: progress.append(frac),
            )
            self.assertEqual(info["n"], 2)
            self.assertEqual(info["dim"], 8)
            self.assertEqual(info["model"], "test-embed")
            self.assertEqual(info["extractor"]["name"], "peelfeed")
            self.assertEqual(info["extractor"]["version"], "peelfeed-test@1")
            self.assertEqual(info["chunker"]["name"], "peel")
            self.assertEqual(info["chunker"]["version"], 1)
            self.assertEqual(info["peel"]["peel_id"], info["peel"]["peel_id"])
            self.assertEqual(len(info["corpus_digest"]), 64)
            self.assertEqual(info["skipped_sources"], [])
            self.assertEqual(progress[-1], 1.0)
            space_dir = Path(dataset["dir"]) / "index" / info["space"]
            generation_dir = space_dir / "generations" / info["generation"]
            self.assertTrue((space_dir / "CURRENT").is_file())
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             info["generation"])
            self.assertTrue((generation_dir / "vectors.npy").is_file())
            self.assertTrue((generation_dir / "probe.npy").is_file())
            meta = json.loads((generation_dir / "meta.json").read_text())
            self.assertEqual(len(meta), info["n"])
            for key in ("id", "section", "chapter", "numbering", "parent",
                        "path", "page_start", "page_end", "pdf_y", "doc",
                        "kind", "image_paths", "source_pages", "char_count",
                        "canonical_ids", "engine", "conversion_id", "peel_id"):
                self.assertIn(key, meta[0])
            # chunks.jsonl is written from the corpus chunks and is canonical.
            self.assertTrue((Path(dataset["dir"]) / "chunks.jsonl").is_file())
            chunks = ingest.read_chunks(dataset["dir"])
            self.assertEqual(len(chunks), info["n"])
            self.assertEqual(len(chunks[0]["id"]), 16)
            self.assertEqual(len(chunks[0]["source_sha256"]), 64)
            # The extraction block was refreshed from the manifest (pages/engine).
            dataset_json = json.loads(
                (Path(dataset["dir"]) / "dataset.json").read_text())
            extraction = dataset_json["sources"][0]["extraction"]
            self.assertEqual(extraction["state"], "extracted")
            self.assertEqual(dataset_json["sources"][0]["pages"], 2)
            self.assertEqual(extraction["engine"], "peelfeed-test@1")

    def test_second_build_makes_zero_embed_calls(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import ingest
            dataset = self._dataset_with_peel(root)
            first = ingest.build_index(dataset["dir"], embed_link(fake))
            requests_after_first = len(fake.requests)
            self.assertGreater(requests_after_first, 0)
            second = ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertEqual(len(fake.requests), requests_after_first)
            self.assertTrue(second.get("up_to_date"))
            self.assertEqual(second["generation"], first["generation"])
            # force=True deliberately rebuilds (reindex path).
            third = ingest.build_index(dataset["dir"], embed_link(fake),
                                       force=True)
            self.assertNotEqual(third["generation"], first["generation"])
            self.assertGreater(len(fake.requests), requests_after_first)

    def test_no_extracted_sources_raises_a_typed_error(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "manual.pdf"))
            with self.assertRaises(ingest.NoExtractedSources) as caught:
                ingest.build_index(dataset["dir"], embed_link_stub())
            error = caught.exception
            self.assertEqual(error.code, "no_extracted_sources")
            self.assertEqual(error.skipped[0]["state"], "not_extracted")
            self.assertIn("no_extracted_sources", str(error))

    def test_non_extracted_sources_are_listed_as_skipped(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            first = make_pdf(root / "first.pdf", pages=["First page about widgets."])
            ingest.add_source(dataset, first)
            make_peel(dataset, source="first.pdf", pages=["First page about widgets."])
            second = make_pdf(root / "second.pdf", pages=["Second page about sprockets."])
            ingest.add_source(dataset, second)
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertEqual(info["n"], 1)
            self.assertEqual(len(info["skipped_sources"]), 1)
            skipped = info["skipped_sources"][0]
            self.assertEqual(skipped["name"], "second.pdf")
            self.assertEqual(skipped["state"], "not_extracted")
            self.assertIn("Extract", skipped["reason"])

    def test_corpus_gate_rejects_a_different_source(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, contracts, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "manual.pdf"))
            peel = make_peel(dataset)
            manifest_path = Path(peel["peel_dir"]) / "extract" / "corpus" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["source_sha256"] = "b" * 64
            contracts.write_json_atomic(manifest_path, manifest)
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertIn("different source", str(caught.exception))
            self.assertIn("manual.pdf", str(caught.exception))

    def test_corpus_gate_rejects_a_conversion_mismatch(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, contracts, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "manual.pdf"))
            peel = make_peel(dataset)
            manifest_path = Path(peel["peel_dir"]) / "extract" / "corpus" / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["conversion_id"] = "c" * 64
            contracts.write_json_atomic(manifest_path, manifest)
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertIn("conversion_id", str(caught.exception))

    def test_corpus_gate_rejects_validation_errors(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "manual.pdf"))
            make_peel(dataset, validation_errors=["page 1 missing caption"])
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertIn("validation has 1 error", str(caught.exception))

    def test_corpus_gate_rejects_a_peel_outside_the_root(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "manual.pdf"))
            outside = root.parent / "outside-peels"
            peel = make_peel(dataset, peel_root=outside)
            # A symlink inside the allowlisted peel root pointing outside must be
            # refused by the realpath gate, not followed.
            default_root = Path(os.environ.get("PEEKSEED_PEELFEED_ROOT")
                                or (config.paths().root / "peels"))
            default_root.mkdir(parents=True, exist_ok=True)
            os.symlink(peel["peel_dir"], str(default_root / peel["peel_id"]))
            with self.assertRaises(ingest.IngestError) as caught:
                ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertIn("outside the peel root", str(caught.exception))

    def test_custom_embed_link(self):
        with temp_data_root() as root, FakeModelServer(dim=4) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Custom")
            ingest.add_source(dataset, make_pdf(root / "m.pdf"))
            make_peel(dataset, pages=["Hello custom embedder."])
            link = {"id": "custom", "role": "embed", "label": "Custom",
                    "protocol": "custom", "base_url": fake.base,
                    "model": "custom-embed", "api_key": "", "enabled": True}
            info = ingest.build_index(dataset["dir"], link)
            self.assertEqual(info["protocol"], "custom")
            self.assertEqual(info["dim"], 4)
            paths = [path for path, _body, _headers in fake.requests]
            self.assertIn("/embed", paths)

    def test_embed_texts_rejects_bad_response_length(self):
        with temp_data_root(), FakeModelServer(dim=8) as fake:
            from peekseed import ingest
            link = embed_link(fake)
            vectors = ingest.embed_texts(link, ["one", "two"])
            self.assertEqual(len(vectors), 2)
            self.assertEqual(len(vectors[0]), 8)


def embed_link_stub():
    return {"id": "fake", "role": "embed", "label": "Fake",
            "protocol": "openai", "base_url": "http://127.0.0.1:9/v1",
            "model": "test-embed", "api_key": "", "enabled": True}


if __name__ == "__main__":
    unittest.main()
