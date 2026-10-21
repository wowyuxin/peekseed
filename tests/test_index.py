"""Tests for generation-atomic indexes, the probe gate and persisted jobs.

Covers audit §6.10 gates 4, 5 and 6 plus the info.json schema-2/probe contract:
a cancelled or failed build leaves the previous generation serving; a corrupt
index is reported as needing a rebuild; a chunker change makes a collection
stale; the probe detects an embedder that no longer matches the space.
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from peekseed import contracts, ingest
from tests.fixtures import FakeModelServer, make_pdf, temp_data_root


def embed_link(fake, model="test-embed", **extra):
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


def build_dataset(root, fake, pages=None, name="Docs"):
    from peekseed import config

    dataset = ingest.create_dataset(config.paths(), name)
    ingest.add_source(dataset, make_pdf(root / (name + ".pdf"),
                                        pages=pages or ["Widget text about buttons."]))
    return dataset


def chunk_pages_for_test(n=5):
    """Deterministic chunk rows with content-derived ids."""
    from peekseed import ingest as ingest_module

    pages = [{"page": index + 1, "text": "Paragraph number %d about widgets. " % index * 8}
             for index in range(n)]
    return ingest_module.chunk_pages(
        pages, target_chars=200, max_chars=400, dataset_id="docs",
        source="docs.pdf", source_sha256="a" * 64,
    )


class GenerationTests(unittest.TestCase):
    def test_generation_layout_and_current_flip(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake, pages=[
                "Installation. The widget is mounted with two screws. " * 30,
                "Operation. Press the green button to start the widget. " * 30,
            ])
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link,
                                      target_chars=200, max_chars=300)
            space_dir = Path(dataset["dir"]) / "index" / info["space"]
            self.assertTrue((space_dir / "CURRENT").is_file())
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             info["generation"])
            generation_dir = space_dir / "generations" / info["generation"]
            for name in ("vectors.npy", "meta.json", "info.json", "probe.npy"):
                self.assertTrue((generation_dir / name).is_file(), name)
            # The checkpoint is removed once a generation is complete.
            self.assertFalse((generation_dir / "progress.json").exists())

            loaded = ingest.load_index(dataset["dir"], info["space"])
            self.assertEqual(loaded["info"]["schema"], contracts.INFO_SCHEMA)
            self.assertEqual(loaded["vectors"].shape,
                             (info["n"], info["dim"]))
            self.assertEqual(len(loaded["meta"]), info["n"])
            self.assertTrue(contracts.has_probe(loaded["info"]))
            # The digest names exactly the inputs this index was built from.
            self.assertEqual(
                loaded["info"]["documents_digest"],
                contracts.documents_digest(loaded["info"]["sources"]),
            )

    def test_dangling_current_needs_reindex(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            space_dir = Path(dataset["dir"]) / "index" / info["space"]
            (space_dir / "CURRENT").write_text("missing-generation\n")
            with self.assertRaises(ingest.NeedsReindex):
                ingest.load_index(dataset["dir"], info["space"])
            state = ingest.index_state(dataset["dir"], info["space"])
            self.assertEqual(state["status"], "needs_reindex")
            self.assertTrue(state["reasons"])

    def test_legacy_flat_index_still_loads(self):
        with temp_data_root() as root, FakeModelServer(dim=4) as fake:
            from peekseed import config, ingest as ingest_module

            dataset = ingest_module.create_dataset(config.paths(), "Legacy")
            chunks = chunk_pages_for_test(3)
            ingest_module.write_chunks(Path(dataset["dir"]), chunks)
            link = embed_link(fake)
            space = ingest_module.embed_space_key(link)
            space_dir = Path(dataset["dir"]) / "index" / space
            space_dir.mkdir(parents=True)
            vectors = np.zeros((len(chunks), 4), dtype=np.float32)
            vectors[:, 0] = 1.0
            np.save(space_dir / "vectors.npy", vectors)
            (space_dir / "meta.json").write_text(
                json.dumps([{"id": chunk["id"], "doc": chunk["doc"],
                             "text": chunk["text"]} for chunk in chunks]))
            (space_dir / "info.json").write_text(json.dumps({
                "schema": 1, "dataset": "legacy", "name": "Legacy",
                "space": space, "link_id": "fake", "model": "test-embed",
                "protocol": "openai", "dim": 4, "n": len(chunks),
                "created_at": "2026-01-01T00:00:00Z",
                "chunker": {"target_chars": 1200, "max_chars": 2000, "overlap": 0},
                "sources": [{"name": "docs.pdf", "pages": 3, "bytes": 10}],
            }))
            loaded = ingest.load_index(dataset["dir"], space)
            self.assertEqual(loaded["info"]["schema"], contracts.INFO_SCHEMA)
            self.assertFalse(contracts.has_probe(loaded["info"]))
            state = ingest.index_state(dataset["dir"], space)
            self.assertEqual(state["status"], "stale")
            self.assertTrue(any("probe" in reason for reason in state["reasons"]))


class FailureIsolationTests(unittest.TestCase):
    def _dataset(self, root, fake):
        dataset = build_dataset(root, fake, pages=[
            "Alpha. The widget is mounted with two screws. " * 25,
            "Beta. Press the green button to start the widget. " * 25,
            "Gamma. The widget has a blue cover. " * 25,
        ])
        return dataset

    def test_cancelled_build_keeps_the_previous_generation_and_resumes(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = self._dataset(root, fake)
            link = embed_link(fake)
            first = ingest.build_index(dataset["dir"], link, batch_size=2,
                                       target_chars=200, max_chars=400)
            self.assertGreaterEqual(first["n"], 4)
            space_dir = Path(dataset["dir"]) / "index" / first["space"]
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             first["generation"])
            requests_start = len(fake.requests)

            # Cancel after the first batch: the new generation stays incomplete
            # and CURRENT keeps pointing at the successful one.
            state = {"calls": 0}

            def cancel():
                state["calls"] += 1
                return state["calls"] > 1

            with self.assertRaises(ingest.IndexCancelled):
                ingest.build_index(dataset["dir"], link, batch_size=2,
                                   target_chars=200, max_chars=400,
                                   cancel=cancel)
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             first["generation"])
            # The cancelled build only paid for its probe and one batch.
            self.assertEqual(len(fake.requests) - requests_start, 2)
            still_serving = ingest.load_index(dataset["dir"], first["space"])
            self.assertEqual(still_serving["info"]["generation"], first["generation"])
            self.assertEqual(still_serving["vectors"].shape[0], first["n"])
            incomplete = [path for path in (space_dir / "generations").iterdir()
                          if (path / "progress.json").exists()]
            self.assertEqual(len(incomplete), 1)
            progress = json.loads((incomplete[0] / "progress.json").read_text())
            self.assertGreater(progress["row"], 0)

            # A cancelled job record is persisted, not silently dropped.
            jobs = list((Path(root) / "jobs").glob("*.json"))
            states = [json.loads(path.read_text())["state"] for path in jobs]
            self.assertIn("cancelled", states)

            # Restart: the incomplete generation is resumed from its checkpoint
            # (no probe re-embed, only the remaining rows) and then committed.
            requests_before = len(fake.requests)
            resumed = ingest.build_index(dataset["dir"], link, batch_size=2,
                                         target_chars=200, max_chars=400)
            self.assertEqual(resumed["generation"], incomplete[0].name)
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             resumed["generation"])
            self.assertEqual(resumed["n"], first["n"])
            remaining = first["n"] - progress["row"]
            expected_calls = (remaining + 1) // 2
            self.assertEqual(len(fake.requests) - requests_before, expected_calls)
            ingest.load_index(dataset["dir"], first["space"])

    def test_corrupt_vectors_is_reported_as_needing_a_rebuild(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            generation_dir = (Path(dataset["dir"]) / "index" / info["space"]
                              / "generations" / info["generation"])
            # Truncate the vectors to something unreadable, like a crash would.
            (generation_dir / "vectors.npy").write_bytes(b"not a numpy file")
            with self.assertRaises(ingest.NeedsReindex):
                ingest.load_index(dataset["dir"], info["space"])
            state = ingest.index_state(dataset["dir"], info["space"])
            self.assertEqual(state["status"], "needs_reindex")
            self.assertTrue(state["reasons"])
            # The metadata is still readable, so status can explain the problem.
            self.assertTrue(contracts.has_probe(
                contracts.read_json(generation_dir / "info.json")))

            # A rebuild repairs the space without touching other spaces.
            repaired = ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertEqual(repaired["space"], info["space"])
            loaded = ingest.load_index(dataset["dir"], info["space"])
            self.assertEqual(loaded["vectors"].shape[0], repaired["n"])

    def test_failed_build_marks_the_job_failed_and_serves_the_old_generation(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = self._dataset(root, fake)
            link = embed_link(fake)
            first = ingest.build_index(dataset["dir"], link, batch_size=2)
            # A link that points nowhere makes the next build fail immediately.
            broken = dict(link, base_url="http://127.0.0.1:1")
            with self.assertRaises(ingest.IngestError):
                ingest.build_index(dataset["dir"], broken, batch_size=2)
            space_dir = Path(dataset["dir"]) / "index" / first["space"]
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             first["generation"])
            jobs = [json.loads(path.read_text())
                    for path in (Path(root) / "jobs").glob("*.json")]
            self.assertTrue(any(job["state"] == "failed" for job in jobs))


class StalenessTests(unittest.TestCase):
    def test_chunker_version_change_makes_the_index_stale(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            current = ingest.index_state(
                dataset["dir"], info["space"],
                chunker=contracts.chunker_identity(),
            )
            self.assertEqual(current["status"], "indexed")
            original = contracts.CHUNKER_VERSION
            contracts.CHUNKER_VERSION = original + 1
            try:
                stale = ingest.index_state(
                    dataset["dir"], info["space"],
                    chunker=contracts.chunker_identity(),
                )
                self.assertEqual(stale["status"], "stale")
                self.assertTrue(any("chunker changed" in reason
                                    for reason in stale["reasons"]))
            finally:
                contracts.CHUNKER_VERSION = original

    def test_reindex_dry_run_writes_nothing(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link)
            space_dir = Path(dataset["dir"]) / "index" / info["space"]
            before = (space_dir / "CURRENT").read_text()
            requests_before = len(fake.requests)
            report = ingest.reindex(dataset["dir"], link, dry_run=True)
            self.assertEqual(report["chunks"], info["n"])
            self.assertEqual(report["target_space"], info["space"])
            self.assertGreater(report["embed_calls"], 0)
            self.assertTrue(report["estimate"])
            self.assertEqual(len(fake.requests), requests_before)
            self.assertEqual((space_dir / "CURRENT").read_text(), before)
            self.assertEqual(
                sorted(path.name for path in (space_dir / "generations").iterdir()),
                [info["generation"]],
            )

    def test_reindex_gc_removes_older_generations(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            link = embed_link(fake)
            first = ingest.build_index(dataset["dir"], link)
            second = ingest.build_index(dataset["dir"], link)
            self.assertNotEqual(first["generation"], second["generation"])
            report = ingest.reindex(dataset["dir"], link, gc=True)
            # reindex rebuilds into a fresh generation and then drops every older
            # one, keeping only the committed generation.
            self.assertEqual(sorted(report["gc_removed"]),
                             sorted([first["generation"], second["generation"]]))
            space_dir = Path(dataset["dir"]) / "index" / second["space"]
            remaining = sorted(path.name for path in (space_dir / "generations").iterdir())
            self.assertEqual(len(remaining), 1)
            self.assertEqual((space_dir / "CURRENT").read_text().strip(), remaining[0])


class ProbeTests(unittest.TestCase):
    def test_probe_matches_the_server_that_built_the_index(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link)
            cosine = ingest.probe_cosine(link, dataset["dir"], info["space"])
            self.assertAlmostEqual(cosine, 1.0, places=5)
            ingest.verify_probe(link, dataset["dir"], info["space"])

    def test_probe_mismatch_is_actionable(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link)
            with FakeModelServer(dim=16) as other:
                wrong = embed_link(other)
                with self.assertRaises(ingest.ProbeMismatch) as raised:
                    ingest.verify_probe(wrong, dataset["dir"], info["space"])
                message = str(raised.exception)
                self.assertIn("embedding space", message)
                self.assertIn("reindex", message.lower())

    def test_generation_probe_check_matches_without_reloading_vectors(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link)
            generation = (Path(dataset["dir"]) / "index" / info["space"]
                          / "generations" / info["generation"])
            cosine = ingest.probe_cosine_for_generation(
                link, generation, info["space"])
            self.assertAlmostEqual(cosine, 1.0, places=5)
            ingest.verify_probe_for_generation(link, generation, info["space"])
            with FakeModelServer(dim=16) as other:
                wrong = embed_link(other)
                with self.assertRaises(ingest.ProbeMismatch) as raised:
                    ingest.verify_probe_for_generation(
                        wrong, generation, info["space"])
                self.assertIn("dimension", str(raised.exception))


class JobTests(unittest.TestCase):
    def test_job_record_lifecycle(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            job = {}
            info = ingest.build_index(dataset["dir"], embed_link(fake), job=job)
            # The in-memory API-style job is updated for the UI.
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["progress"], 1.0)
            # And a persisted contract record exists with the terminal state.
            jobs = [json.loads(path.read_text())
                    for path in (Path(root) / "jobs").glob("*.json")]
            self.assertEqual(len(jobs), 1)
            validated = contracts.validate_job(jobs[0])
            self.assertEqual(validated["state"], "succeeded")
            self.assertEqual(validated["collection"], dataset["id"])
            self.assertEqual(validated["progress"]["done"], info["n"])

    def test_interrupted_jobs_are_marked_not_failed(self):
        with temp_data_root() as root:
            jobs_dir = Path(root) / "jobs"
            jobs_dir.mkdir(parents=True)
            job = contracts.new_job("index", collection="docs", job_id="job-crash")
            job["state"] = "running"
            job["started_at"] = contracts.utc_now()
            contracts.write_json_atomic(jobs_dir / "job-crash.json", job)
            done = contracts.new_job("index", collection="docs", job_id="job-ok")
            done["state"] = "succeeded"
            contracts.write_json_atomic(jobs_dir / "job-ok.json", done)
            interrupted = ingest.mark_interrupted_jobs(jobs_dir)
            self.assertEqual(interrupted, ["job-crash"])
            after = json.loads((jobs_dir / "job-crash.json").read_text())
            self.assertEqual(after["state"], "interrupted")
            self.assertIsNotNone(after["finished_at"])
            self.assertEqual(json.loads((jobs_dir / "job-ok.json").read_text())["state"],
                             "succeeded")

    def test_retry_classifier(self):
        from peekseed.ingest import index as index_module

        retryable = [
            ingest.IngestError("The embedding link returned HTTP 429."),
            ingest.IngestError("The embedding link returned HTTP 503."),
            ingest.IngestError("Could not reach the embedding link (ConnectError)."),
        ]
        permanent = [
            ingest.IngestError("The embedding link returned HTTP 400."),
            ingest.IngestError("The embedding link returned invalid JSON: boom"),
        ]
        for error in retryable:
            self.assertTrue(index_module._retryable(error), str(error))
        for error in permanent:
            self.assertFalse(index_module._retryable(error), str(error))

    def test_transient_failures_are_retried(self):
        from peekseed.ingest import index as index_module

        calls = {"count": 0}
        original = index_module.embed_texts

        def flaky(link, texts, *, batch_size, timeout=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ingest.IngestError("The embedding link returned HTTP 503.")
            return [[1.0, 0.0] for _ in texts]

        index_module.embed_texts = flaky
        original_delay = index_module.RETRY_BASE_DELAY
        index_module.RETRY_BASE_DELAY = 0.0
        try:
            vectors = index_module._embed_with_retry(
                {"id": "x"}, ["hello"], batch_size=1
            )
        finally:
            index_module.embed_texts = original
            index_module.RETRY_BASE_DELAY = original_delay
        self.assertEqual(calls["count"], 2)
        self.assertEqual(len(vectors), 1)


if __name__ == "__main__":
    unittest.main()
