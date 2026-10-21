import json
import unittest
from pathlib import Path

import numpy as np

from tests.fixtures import FakeModelServer, make_pdf, temp_data_root


class DatasetIndexTests(unittest.TestCase):
    def _index(self, meta=None, vectors=None):
        from peekseed.engine import DatasetIndex
        server = FakeModelServer(dim=64)
        meta = meta or [
            {"id": "c1", "section": "Installation", "chapter": "Using it",
             "numbering": "2.1", "parent": "", "path": "", "page_start": 1,
             "page_end": 1, "doc": "Using it\n\nThe widget is mounted with two screws.",
             "text": "The widget is mounted with two screws."},
            {"id": "c2", "section": "Operation", "chapter": "Using it",
             "numbering": "2.2", "parent": "", "path": "", "page_start": 2,
             "page_end": 2, "doc": "Using it\n\nPress the green button to start the widget.",
             "text": "Press the green button to start the widget."},
            {"id": "c3", "section": "Shutdown", "chapter": "Using it",
             "numbering": "2.3", "parent": "", "path": "", "page_start": 3,
             "page_end": 3, "doc": "Using it\n\nHold the red button for two seconds.",
             "text": "Hold the red button for two seconds."},
        ]
        vectors = vectors if vectors is not None else np.asarray(
            [server.vector(item["doc"]) for item in meta], dtype=np.float32)
        return DatasetIndex("docs", "space", info={"dim": 64}, vectors=vectors,
                            meta=meta), server

    def test_search_returns_order_scores_and_timings(self):
        index, server = self._index()
        query = "green button"
        order, reranked, scores, timings = index.search(
            server.vector(query), query, top=3)
        self.assertFalse(reranked)
        self.assertEqual(len(order), 3)
        self.assertEqual(len(scores), len(order))
        self.assertEqual(order[0], 1)  # the operation chunk is the query's chunk
        self.assertIn("retrieve_sec", timings)

    def test_heading_only_chunks_are_not_searchable(self):
        meta = [
            {"id": "head", "section": "Installation", "chapter": "", "numbering": "",
             "parent": "", "path": "", "page_start": 1, "page_end": 1,
             "doc": "Installation\n\n", "text": "", "heading_only": True},
            {"id": "body", "section": "Installation", "chapter": "", "numbering": "",
             "parent": "", "path": "", "page_start": 1, "page_end": 1,
             "doc": "Installation\n\nMount it.", "text": "Mount it.",
             "heading_only": False},
        ]
        index, server = self._index(meta=meta)
        order, _reranked, _scores, _timings = index.search(
            server.vector("mount"), "mount", top=2)
        self.assertNotIn(0, order)

    def test_rerank_hook_changes_the_order(self):
        index, server = self._index()
        calls = []

        def rerank(query, docs):
            calls.append((query, len(docs)))
            # Score by content, so the expected order is independent of the
            # fusion order the reranker is handed.
            return [30.0 if "green" in doc else
                    20.0 if "screws" in doc else 10.0 for doc in docs]

        query = "green button"
        order, reranked, scores, timings = index.search(
            server.vector(query), query, top=3, rerank_pool=3, rerank_batch=3,
            rerank_fn=rerank)
        self.assertTrue(reranked)
        self.assertEqual(calls, [(query, 3)])
        self.assertEqual(order, [1, 0, 2])
        self.assertEqual(scores, [30.0, 20.0, 10.0])
        self.assertIsNotNone(timings["rerank_sec"])

    def test_bad_rerank_falls_back_to_fusion(self):
        index, server = self._index()

        def broken(query, docs):
            return None

        order, reranked, scores, timings = index.search(
            server.vector("widget"), "widget", top=3, rerank_pool=3,
            rerank_fn=broken)
        self.assertFalse(reranked)
        self.assertEqual(len(order), 3)
        self.assertIsNone(timings["rerank_sec"])

    def test_dimension_mismatch_is_a_value_error(self):
        index, _server = self._index()
        with self.assertRaises(ValueError):
            index.search(np.zeros(3, dtype=np.float32), "x", top=1)


class IndexStoreTests(unittest.TestCase):
    def test_load_finds_indexes_and_skips_broken_dirs(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config, ingest, links
            dataset = ingest.create_dataset(config.paths(), "Docs")
            ingest.add_source(dataset, make_pdf(root / "doc.pdf",
                                                pages=["Widget text about buttons."]))
            link = {"id": "fake", "role": "embed", "label": "Fake",
                    "protocol": "openai", "base_url": fake.openai_base,
                    "model": "test-embed", "api_key": "", "enabled": True}
            info = ingest.build_index(dataset["dir"], link)

            from peekseed.engine import IndexStore
            store = IndexStore(config.paths().datasets_dir)
            store.load()
            self.assertIsNotNone(store.get(dataset["id"], info["space"]))
            self.assertEqual(len(store.for_dataset(dataset["id"])), 1)
            self.assertIsNotNone(store.find(dataset["id"], "fake", "test-embed"))

            # a broken index beside a good one must be skipped, not fatal
            broken = Path(dataset["dir"]) / "index" / "broken"
            broken.mkdir(parents=True)
            (broken / "info.json").write_text("{not json")
            store.load()
            self.assertEqual(len(store.all()), 1)

    def _synthetic_generation(self, datasets_dir):
        """Write one minimal generation-atomic index without using the ingest path."""
        space = Path(datasets_dir) / "cxl" / "index" / "link__model"
        generation = space / "generations" / "abc123"
        generation.mkdir(parents=True)
        meta = [{"id": "chunk-a", "doc": "widget text", "text": "widget text",
                 "page_start": 1, "page_end": 1, "heading_only": False}]
        (generation / "info.json").write_text(json.dumps(
            {"schema": 2, "dim": 2, "space": "link__model",
             "embedding": {"dim": 2}, "n": 1}))
        (generation / "meta.json").write_text(json.dumps(meta))
        np.save(space / "generations" / "abc123" / "vectors.npy",
                np.array([[1.0, 0.0]], dtype=np.float32))
        (space / "CURRENT").write_text("abc123\n")
        return space

    def test_current_generation_is_loaded(self):
        import tempfile
        from peekseed.engine import IndexStore
        with tempfile.TemporaryDirectory() as tmp:
            datasets_dir = Path(tmp) / "datasets"
            space = self._synthetic_generation(datasets_dir)
            store = IndexStore(str(datasets_dir))
            store.load()
            index = store.get("cxl", "link__model")
            self.assertIsNotNone(index)
            self.assertEqual(index.info["n"], 1)

            # A CURRENT that names a missing generation is unusable: the space
            # must be reported as needing a rebuild, never served half-written.
            (space / "CURRENT").write_text("gone\n")
            store.load()
            self.assertEqual(store.all(), {})

    def test_legacy_space_without_current_still_loads(self):
        import tempfile
        from peekseed.engine import IndexStore
        with tempfile.TemporaryDirectory() as tmp:
            datasets_dir = Path(tmp) / "datasets"
            space = self._synthetic_generation(datasets_dir)
            generation = space / "generations" / "abc123"
            for name in ("info.json", "meta.json", "vectors.npy"):
                source = generation / name
                source.replace(space / name)
            (space / "CURRENT").unlink()
            store = IndexStore(str(datasets_dir))
            store.load()
            self.assertIsNotNone(store.get("cxl", "link__model"))


if __name__ == "__main__":
    unittest.main()
