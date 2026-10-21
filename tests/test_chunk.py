"""Tests for content-derived chunk ids (audit: incremental embedding safety).

The legacy ``"<dataset>-<NNNNNN>"`` ids are kept for callers that concatenate
sources with ``id_start``; the workspace scan passes the source's sha so a chunk
id depends only on its own file and never on what else is in the collection.
"""

import hashlib
import unittest

from peekseed import contracts
from peekseed.ingest.chunk import chunk_pages


def _pages():
    return [
        {"page": 1, "text": "Widget installation. The widget is mounted with "
                            "two screws. Torque is 4 Nm."},
        {"page": 2, "text": "Widget operation. Press the green button to start "
                            "the widget. The status light turns blue."},
    ]


def _sha(data=b"widget manual"):
    return hashlib.sha256(data).hexdigest()


class ContentChunkIdTests(unittest.TestCase):
    def test_ids_are_content_derived_and_validated(self):
        sha = _sha()
        chunks = chunk_pages(_pages(), dataset_id="cxl", source="manual.pdf",
                             source_sha256=sha, target_chars=120, max_chars=200)
        self.assertGreater(len(chunks), 0)
        for index, chunk in enumerate(chunks):
            self.assertEqual(chunk["source_sha256"], sha)
            self.assertEqual(chunk["chunk_index"], index)
            self.assertEqual(chunk["ordinal"], index + 1)
            self.assertEqual(chunk["chunker_version"], contracts.CHUNKER_VERSION)
            self.assertGreaterEqual(chunk["block_index"], 0)
            self.assertEqual(
                chunk["id"],
                contracts.chunk_id(sha, chunk["block_index"], chunk["chunk_index"]),
            )
            # The validator re-derives the id, so a drifting chunker is caught.
            validated = contracts.validate_chunk(chunk, derive_id=True)
            self.assertEqual(validated["id"], chunk["id"])
            self.assertEqual(validated["chunk_index"], index)

    def test_ids_are_stable_across_runs(self):
        sha = _sha()
        first = chunk_pages(_pages(), dataset_id="cxl", source="manual.pdf",
                            source_sha256=sha, target_chars=120, max_chars=200)
        second = chunk_pages(_pages(), dataset_id="cxl", source="manual.pdf",
                             source_sha256=sha, target_chars=120, max_chars=200)
        self.assertEqual([chunk["id"] for chunk in first],
                         [chunk["id"] for chunk in second])

    def test_adding_another_file_does_not_renumber(self):
        sha_a = _sha(b"file a")
        sha_b = _sha(b"file b")
        alone = chunk_pages(_pages(), dataset_id="cxl", source="a.pdf",
                            source_sha256=sha_a, target_chars=120, max_chars=200)
        # File B joins the same collection (its own source hash and own
        # chunk_index sequence); file A's ids must be untouched.
        other = chunk_pages(_pages(), dataset_id="cxl", source="b.pdf",
                            source_sha256=sha_b, target_chars=120, max_chars=200)
        again = chunk_pages(_pages(), dataset_id="cxl", source="a.pdf",
                            source_sha256=sha_a, target_chars=120, max_chars=200)
        self.assertEqual([chunk["id"] for chunk in alone],
                         [chunk["id"] for chunk in again])
        self.assertNotEqual([chunk["id"] for chunk in alone][0],
                            [chunk["id"] for chunk in other][0])

    def test_block_index_points_at_first_constituent_block(self):
        sha = _sha()
        # Two clearly separate blocks: a heading and one body paragraph.
        pages = [{"page": 1, "text": "Installation\n\nMount the widget."}]
        chunks = chunk_pages(pages, dataset_id="cxl", source="a.pdf",
                             source_sha256=sha, target_chars=2000, max_chars=2000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["block_index"], 0)

    def test_malformed_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            chunk_pages(_pages(), source_sha256="not-a-hash")

    def test_legacy_ids_are_preserved_without_a_hash(self):
        chunks = chunk_pages(_pages(), dataset_id="docs", source="manual.pdf",
                             target_chars=120, max_chars=200)
        self.assertGreater(len(chunks), 0)
        self.assertEqual([chunk["id"] for chunk in chunks][:1], ["docs-000001"])
        for chunk in chunks:
            self.assertRegex(chunk["id"], r"^docs-\d{6}$")
            # The legacy record shape is unchanged for existing callers.
            self.assertNotIn("source_sha256", chunk)
            self.assertNotIn("chunk_index", chunk)
        # id_start is still honoured when several PDFs share one dataset.
        chunks = chunk_pages(_pages(), dataset_id="docs", id_start=7,
                             target_chars=120, max_chars=200)
        self.assertEqual(chunks[0]["id"], "docs-000007")


if __name__ == "__main__":
    unittest.main()
