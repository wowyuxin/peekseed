"""Tests for the versioned data contracts (peekseed/contracts.py)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from peekseed import contracts
from peekseed.links import embed_space_key

SHA_A = "a" * 64
SHA_B = "b" * 64


def sample_info(**overrides):
    data = {
        "schema": 2,
        "dataset": "cxl",
        "space": "embed-local__qwen3-vl-embedding-2b-8bit-mlx",
        "generation": "0f9c2a1e",
        "n": 2,
        "dim": 8,
        "link_id": "embed-local",
        "model": "m",
        "protocol": "custom",
        "created_at": "2026-10-20T20:00:00Z",
        "embedding": {
            "link_id": "embed-local",
            "model": "m",
            "protocol": "custom",
            "dim": 8,
            "normalized": True,
            "doc_prefix": "",
            "query_prefix": "",
            "probe": {"text_sha256": contracts.probe_sha256(), "file": "probe.npy"},
        },
        "chunker": {"name": "headings", "version": 1, "target_chars": 1200, "max_chars": 2000},
        "extractor": {"name": "pypdf", "version": "6.21.1"},
        "documents_digest": contracts.documents_digest(
            [{"path": "documents/cxl/a.pdf", "sha256": SHA_A}]
        ),
        "sources": [
            {"name": "a.pdf", "path": "documents/cxl/a.pdf", "sha256": SHA_A,
             "bytes": 10, "pages": 2},
        ],
    }
    data.update(overrides)
    return data


def sample_dataset(**overrides):
    data = {
        "schema": 2,
        "id": "cxl",
        "name": "CXL",
        "created_at": "2026-10-20T20:00:00Z",
        "notes": "",
        "sources": [
            {"name": "a.pdf", "path": "documents/cxl/a.pdf", "sha256": SHA_A,
             "bytes": 10, "pages": 2, "extractor": "pypdf@6.21.1", "status": "indexed"},
        ],
    }
    data.update(overrides)
    return data


def sample_manifest(**overrides):
    data = contracts.new_manifest("2026-10-20T20:00:00Z")
    data["last_scan"] = "2026-10-20T20:05:00Z"
    data["extractor"] = "pypdf@6.21.1"
    data["documents"] = [
        {"path": "documents/cxl/a.pdf", "collection": "cxl", "sha256": SHA_A,
         "bytes": 10, "pages": 2, "status": "indexed",
         "seen_at": "2026-10-20T20:05:00Z"},
    ]
    data.update(overrides)
    return data


def sample_chunk(**overrides):
    source_sha = SHA_A
    chunk_id = contracts.chunk_id(source_sha, 3, 7)
    data = {
        "id": chunk_id,
        "ordinal": 7,
        "dataset": "cxl",
        "source": "a.pdf",
        "source_sha256": source_sha,
        "block_index": 3,
        "chunk_index": 7,
        "chunker_version": contracts.CHUNKER_VERSION,
        "page_start": 2,
        "page_end": 3,
        "pdf_y": 0,
        "section": "1 Overview",
        "chapter": "Chapter 1",
        "numbering": "1",
        "parent": "",
        "path": "Chapter 1 / 1 Overview",
        "doc": "Chapter 1 / 1 Overview\n\nbody",
        "text": "body",
        "heading_only": False,
    }
    data.update(overrides)
    return data


class IdentifierTests(unittest.TestCase):
    def test_chunk_id_is_deterministic_and_content_derived(self):
        first = contracts.chunk_id(SHA_A, 3, 7)
        self.assertEqual(first, contracts.chunk_id(SHA_A, 3, 7))
        self.assertEqual(len(first), 16)
        # Every input changes the id: source, block position, chunk position, chunker.
        self.assertNotEqual(first, contracts.chunk_id(SHA_B, 3, 7))
        self.assertNotEqual(first, contracts.chunk_id(SHA_A, 4, 7))
        self.assertNotEqual(first, contracts.chunk_id(SHA_A, 3, 8))
        self.assertNotEqual(first, contracts.chunk_id(SHA_A, 3, 7, chunker_version=2))
        # A different file in the same collection cannot renumber this chunk.
        self.assertEqual(first, contracts.chunk_id(SHA_A, 3, 7, contracts.CHUNKER_VERSION))

    def test_chunk_id_rejects_a_malformed_source_hash(self):
        with self.assertRaises(contracts.ContractError):
            contracts.chunk_id("not-a-hash", 0, 0)
        with self.assertRaises(contracts.ContractError):
            contracts.chunk_id("A" * 63, 0, 0)

    def test_documents_digest_ignores_order_but_not_content_or_path(self):
        one = {"path": "documents/cxl/a.pdf", "sha256": SHA_A}
        two = {"path": "documents/cxl/b.pdf", "sha256": SHA_B}
        self.assertEqual(
            contracts.documents_digest([one, two]),
            contracts.documents_digest([two, one]),
        )
        self.assertNotEqual(
            contracts.documents_digest([one, two]),
            contracts.documents_digest([one]),
        )
        moved = {"path": "documents/other/a.pdf", "sha256": SHA_A}
        self.assertNotEqual(
            contracts.documents_digest([one]), contracts.documents_digest([moved])
        )
        with self.assertRaises(contracts.ContractError):
            contracts.documents_digest([{"path": "documents/cxl/a.pdf", "sha256": ""}])

    def test_space_key_matches_the_link_store(self):
        link = {"id": "Embed Local", "model": "Qwen3-VL-Embedding-2B-8bit-mlx"}
        self.assertEqual(contracts.space_key(link), embed_space_key(link))
        self.assertEqual(contracts.space_key(link), "embed-local__qwen3-vl-embedding-2b-8bit-mlx")
        self.assertEqual(contracts.space_key({"id": "x", "model": ""}), "x__default")

    def test_space_identity_records_the_full_space(self):
        link = {"id": "embed-local", "model": "m", "protocol": "custom",
                "doc_prefix": "passage: ", "query_prefix": "query: "}
        identity = contracts.space_identity(link, dim=2048)
        self.assertEqual(identity["dim"], 2048)
        self.assertEqual(identity["protocol"], "custom")
        self.assertEqual(identity["doc_prefix"], "passage: ")
        self.assertEqual(identity["query_prefix"], "query: ")
        self.assertTrue(identity["normalized"])

    def test_probe_is_fixed_and_recorded(self):
        self.assertEqual(contracts.probe_sha256(), contracts.probe_sha256())
        self.assertEqual(len(contracts.probe_sha256()), 64)
        self.assertTrue(contracts.has_probe(sample_info()))
        self.assertFalse(contracts.has_probe(sample_info(embedding={
            "link_id": "x", "model": "m", "protocol": "custom", "dim": 8,
            "normalized": True, "doc_prefix": "", "query_prefix": "",
            "probe": {"text_sha256": "", "file": ""},
        })))


class ManifestTests(unittest.TestCase):
    def test_new_manifest_is_valid(self):
        manifest = contracts.new_manifest("2026-10-20T20:00:00Z")
        self.assertEqual(contracts.validate_manifest(manifest)["schema"], 1)
        self.assertEqual(manifest["documents"], [])

    def test_manifest_round_trips_and_normalises(self):
        validated = contracts.validate_manifest(sample_manifest())
        self.assertEqual(validated["documents"][0]["status"], "indexed")
        self.assertEqual(validated["documents"][0]["path"], "documents/cxl/a.pdf")

    def test_manifest_rejects_duplicate_paths(self):
        data = sample_manifest()
        data["documents"].append(dict(data["documents"][0]))
        with self.assertRaises(contracts.ContractError):
            contracts.validate_manifest(data)

    def test_manifest_rejects_unknown_status(self):
        data = sample_manifest()
        data["documents"][0]["status"] = "wat"
        with self.assertRaises(contracts.ContractError):
            contracts.validate_manifest(data)

    def test_manifest_refuses_a_newer_schema(self):
        data = sample_manifest()
        data["schema"] = 2
        with self.assertRaises(contracts.SchemaTooNew):
            contracts.validate_manifest(data)


class DatasetTests(unittest.TestCase):
    def test_dataset_validates(self):
        validated = contracts.validate_dataset(sample_dataset())
        self.assertEqual(validated["schema"], 2)
        self.assertEqual(validated["sources"][0]["sha256"], SHA_A)

    def test_dataset_refuses_a_newer_schema(self):
        with self.assertRaises(contracts.SchemaTooNew):
            contracts.validate_dataset(sample_dataset(schema=3))

    def test_dataset_upgrade_from_version_1(self):
        legacy = {
            "version": 1,
            "id": "docs",
            "name": "Docs",
            "created_at": "2026-01-01T00:00:00Z",
            "sources": [{"name": "a.pdf", "path": "original/a.pdf", "bytes": 5, "pages": 2}],
            "notes": "n",
        }
        upgraded = contracts.upgrade_dataset(legacy, sha256_of=lambda path: SHA_A)
        self.assertEqual(upgraded["schema"], 2)
        self.assertEqual(upgraded["sources"][0]["sha256"], SHA_A)
        self.assertEqual(upgraded["sources"][0]["status"], "indexed")
        # A missing file keeps no hash and is marked new rather than guessed.
        upgraded = contracts.upgrade_dataset(legacy, sha256_of=lambda path: None)
        self.assertEqual(upgraded["sources"][0]["sha256"], "")
        self.assertEqual(upgraded["sources"][0]["status"], "new")

    def test_dataset_upgrade_is_idempotent(self):
        once = contracts.upgrade_dataset(sample_dataset())
        self.assertEqual(contracts.upgrade_dataset(once), once)


class InfoTests(unittest.TestCase):
    def test_info_validates(self):
        validated = contracts.validate_info(sample_info())
        self.assertEqual(validated["embedding"]["dim"], 8)
        self.assertTrue(contracts.has_probe(validated))

    def test_info_refuses_a_newer_schema(self):
        with self.assertRaises(contracts.SchemaTooNew):
            contracts.validate_info(sample_info(schema=3))

    def test_info_rejects_disagreeing_mirrors(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_info(sample_info(dim=4))
        with self.assertRaises(contracts.ContractError):
            contracts.validate_info(sample_info(link_id="other"))

    def test_info_upgrade_from_schema_1(self):
        legacy = {
            "schema": 1, "dataset": "docs", "name": "Docs", "space": "x__default",
            "link_id": "x", "model": "", "protocol": "openai", "dim": 4, "n": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "chunker": {"target_chars": 1200, "max_chars": 2000, "overlap": 0},
            "sources": [{"name": "a.pdf", "pages": 2, "bytes": 5}],
        }
        upgraded = contracts.upgrade_info(legacy)
        self.assertEqual(upgraded["schema"], 2)
        self.assertFalse(contracts.has_probe(upgraded))
        self.assertEqual(upgraded["embedding"]["dim"], 4)
        self.assertEqual(upgraded["chunker"]["version"], contracts.CHUNKER_VERSION)

    def test_stale_reasons(self):
        sources = [{"path": "documents/cxl/a.pdf", "sha256": SHA_A}]
        info = sample_info()
        self.assertEqual(
            contracts.stale_reasons(
                info, sources,
                chunker=contracts.chunker_identity(),
                extractor=contracts.extractor_identity("pypdf", "6.21.1"),
            ),
            [],
        )
        changed = [{"path": "documents/cxl/a.pdf", "sha256": SHA_B}]
        reasons = contracts.stale_reasons(info, changed, chunker=contracts.chunker_identity())
        self.assertTrue(any("source documents changed" in reason for reason in reasons))
        reasons = contracts.stale_reasons(
            info, sources,
            chunker={"name": contracts.CHUNKER_NAME, "version": 2},
        )
        self.assertTrue(any("chunker changed" in reason for reason in reasons))
        reasons = contracts.stale_reasons(
            info, sources, extractor=contracts.extractor_identity("pypdf", "7.0.0")
        )
        self.assertTrue(any("extractor changed" in reason for reason in reasons))


class JobTests(unittest.TestCase):
    def test_job_round_trips(self):
        job = contracts.new_job("index", collection="cxl", job_id="job-abc")
        job["state"] = "running"
        job["progress"] = {"phase": "embed", "done": 2, "total": 4, "fraction": 0.5}
        validated = contracts.validate_job(job)
        self.assertEqual(validated["id"], "job-abc")
        self.assertEqual(validated["state"], "running")
        self.assertEqual(validated["progress"]["fraction"], 0.5)

    def test_job_rejects_unknown_kind_and_state(self):
        with self.assertRaises(contracts.ContractError):
            contracts.new_job("explode")
        job = contracts.new_job("scan")
        job["state"] = "finished"
        with self.assertRaises(contracts.ContractError):
            contracts.validate_job(job)

    def test_job_refuses_a_newer_schema(self):
        job = contracts.new_job("scan", job_id="job-x")
        job["schema"] = 2
        with self.assertRaises(contracts.SchemaTooNew):
            contracts.validate_job(job)


class ChunkRowTests(unittest.TestCase):
    def test_chunk_row_validates_and_derives_its_id(self):
        validated = contracts.validate_chunk(sample_chunk(), derive_id=True)
        self.assertEqual(validated["source_sha256"], SHA_A)
        self.assertEqual(validated["id"], contracts.chunk_id(SHA_A, 3, 7))

    def test_chunk_row_refuses_a_wrong_id(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_chunk(sample_chunk(id="deadbeefdeadbeef"), derive_id=True)

    def test_chunk_row_accepts_imported_legacy_ids_by_default(self):
        validated = contracts.validate_chunk(
            sample_chunk(id="cxl-unit-000001", ordinal=1), derive_id=False
        )
        self.assertEqual(validated["id"], "cxl-unit-000001")

    def test_chunk_row_rejects_bad_page_range(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_chunk(sample_chunk(page_start=5, page_end=4))


class JsonIOTests(unittest.TestCase):
    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "nested" / "manifest.json"
            contracts.write_json_atomic(path, {"schema": 1, "a": 1})
            self.assertEqual(contracts.read_json(path), {"schema": 1, "a": 1})
            self.assertIsNone(contracts.read_json(Path(root) / "missing.json"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_read_reports_malformed_json(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(contracts.ContractError):
                contracts.read_json(path)


if __name__ == "__main__":
    unittest.main()
