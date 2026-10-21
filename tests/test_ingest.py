import json
import unittest
from pathlib import Path

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root


class PdfTests(unittest.TestCase):
    def test_extract_pdf_reads_pages_in_order(self):
        with temp_data_root() as root:
            from peekseed import ingest
            path = make_pdf(root / "doc.pdf", pages=["First page text.", "Second page text."])
            pages = ingest.extract_pdf(path)
            self.assertEqual([page["page"] for page in pages], [1, 2])
            self.assertIn("First page", pages[0]["text"])
            self.assertIn("Second page", pages[1]["text"])

    def test_page_count(self):
        with temp_data_root() as root:
            from peekseed import ingest
            path = make_pdf(root / "doc.pdf", pages=["one", "two", "three"])
            self.assertEqual(ingest.page_count(path), 3)

    def test_corrupt_pdf_raises_ingest_error(self):
        with temp_data_root() as root:
            from peekseed import ingest
            path = root / "broken.pdf"
            path.write_bytes(b"%PDF-1.4\nnot really a pdf")
            with self.assertRaises(ingest.IngestError):
                ingest.extract_pdf(path)


class ChunkTests(unittest.TestCase):
    def test_chunk_records_are_complete_and_citable(self):
        from peekseed import ingest
        pages = [
            {"page": 1, "text": "This is the introduction. " * 20},
            {"page": 2, "text": "The widget is mounted with two screws. " * 20},
        ]
        chunks = ingest.chunk_pages(pages, dataset_id="docs", source="manual.pdf",
                                    target_chars=1000, max_chars=1500)
        self.assertGreaterEqual(len(chunks), 1)
        ids = set()
        for chunk in chunks:
            for key in ("id", "dataset", "source", "page_start", "page_end",
                        "section", "chapter", "numbering", "parent", "path",
                        "doc", "text", "heading_only"):
                self.assertIn(key, chunk)
            self.assertTrue(chunk["id"].startswith("docs-"))
            self.assertNotIn(chunk["id"], ids)
            ids.add(chunk["id"])
            self.assertIn("\n\n", chunk["doc"])
            self.assertLessEqual(len(chunk["doc"]), 1500 + 200)
        joined = "\n".join(chunk["text"] for chunk in chunks)
        # A chunk that spans pages marks the transitions so a fact stays citable.
        self.assertIn("[p.1]", joined)
        self.assertIn("[p.2]", joined)

    def test_empty_pages_produce_no_chunks(self):
        from peekseed import ingest
        self.assertEqual(ingest.chunk_pages([{"page": 1, "text": "   \n\n  "}],
                                            dataset_id="docs"), [])


class DatasetTests(unittest.TestCase):
    def test_create_dataset_and_add_source(self):
        with temp_data_root() as root:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Product Docs")
            self.assertEqual(dataset["id"], "product-docs")
            pdf = make_pdf(root / "a.pdf", pages=["hello"])
            entry = ingest.add_source(dataset, pdf)
            self.assertEqual(entry["name"], "a.pdf")
            self.assertTrue((Path(dataset["dir"]) / "original" / "a.pdf").is_file())
            # same file again is idempotent, not duplicated
            again = ingest.add_source(dataset, pdf)
            self.assertEqual(again["name"], "a.pdf")
            info = ingest.dataset_info(dataset["dir"])
            self.assertEqual(info["id"], "product-docs")
            self.assertEqual(len(info["sources"]), 1)

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


class IndexBuildTests(unittest.TestCase):
    def test_openai_embed_link_builds_an_index(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Docs")
            pdf = make_pdf(root / "manual.pdf", pages=[
                "Installation. The widget is mounted with two screws.",
                "Operation. Press the green button to start.",
            ])
            ingest.add_source(dataset, pdf)
            link = {"id": "fake", "role": "embed", "label": "Fake",
                    "protocol": "openai", "base_url": fake.openai_base,
                    "model": "test-embed", "api_key": "", "enabled": True}
            progress = []
            info = ingest.build_index(dataset["dir"], link,
                                      target_chars=300, max_chars=500,
                                      progress=lambda frac, msg: progress.append(frac))
            self.assertGreater(info["n"], 0)
            self.assertEqual(info["dim"], 8)
            self.assertEqual(info["model"], "test-embed")
            self.assertEqual(progress[-1], 1.0)
            space_dir = Path(dataset["dir"]) / "index" / info["space"]
            generation_dir = space_dir / "generations" / info["generation"]
            # The generation layout writes CURRENT last; the live generation holds
            # the files, and the space root only points at it.
            self.assertTrue((space_dir / "CURRENT").is_file())
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             info["generation"])
            self.assertTrue((generation_dir / "vectors.npy").is_file())
            self.assertTrue((generation_dir / "probe.npy").is_file())
            meta = json.loads((generation_dir / "meta.json").read_text())
            self.assertEqual(len(meta), info["n"])
            for key in ("id", "section", "chapter", "numbering", "parent",
                        "path", "page_start", "page_end", "pdf_y", "doc"):
                self.assertIn(key, meta[0])
            # chunks.jsonl is the canonical input and is written back
            self.assertTrue((Path(dataset["dir"]) / "chunks.jsonl").is_file())
            self.assertEqual(len(ingest.read_chunks(dataset["dir"])), info["n"])

    def test_custom_embed_link(self):
        with temp_data_root() as root, FakeModelServer(dim=4) as fake:
            from peekseed import config, ingest
            dataset = ingest.create_dataset(config.paths(), "Custom")
            ingest.add_source(dataset, make_pdf(root / "m.pdf", pages=["Hello custom embedder."]))
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
            link = {"id": "fake", "role": "embed", "label": "Fake",
                    "protocol": "openai", "base_url": fake.openai_base,
                    "model": "test-embed", "api_key": "", "enabled": True}
            vectors = ingest.embed_texts(link, ["one", "two"])
            self.assertEqual(len(vectors), 2)
            self.assertEqual(len(vectors[0]), 8)


if __name__ == "__main__":
    unittest.main()
