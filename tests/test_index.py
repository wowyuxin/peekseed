"""Tests for generation-atomic indexes, the probe gate and persisted jobs.

Covers audit §6.10 gates 4, 5 and 6 plus the info.json schema-3/probe contract:
a cancelled or failed build leaves the previous generation serving; a corrupt
index is reported as needing a rebuild; a chunker change makes a collection
stale; the probe detects an embedder that no longer matches the space.

Indexes are built from fake peelfeed corpora (``tests.fixtures.make_peel``);
there is no in-process extraction path any more.
"""

import fcntl
import json
import os
import unittest
from pathlib import Path

import numpy as np

from peekseed import contracts, ingest
from tests.fixtures import (EMBED_CHUNK, FakeModelServer, make_pdf, make_peel,
                            temp_data_root, write_peel_build)


def embed_link(fake, model="test-embed", **extra):
    link = {
        "id": "fake",
        "role": "embed",
        "label": "Fake",
        "protocol": "openai",
        "base_url": fake.openai_base,
        "model": model,
        "api_key_env": "",
        "enabled": True,
        "chunk": dict(EMBED_CHUNK),
    }
    link.update(extra)
    return link


def build_dataset(root, fake, pages=None, name="Docs"):
    from peekseed import config

    dataset = ingest.create_dataset(config.paths(), name)
    ingest.add_source(dataset, make_pdf(root / (name + ".pdf"), pages=pages or
                                        ["Widget text about buttons."]))
    make_peel(dataset, pages=pages or ["Widget text about buttons."])
    return dataset


def chunk_rows(n=5, source_sha="a" * 64):
    """Deterministic chunk rows with content-derived ids (for legacy layout tests)."""
    rows = []
    for index in range(n):
        text = "Paragraph number %d about widgets. " % index * 8
        rows.append({
            "id": contracts.chunk_id(source_sha, index, index,
                                     contracts.PEEL_CHUNKER_VERSION),
            "dataset": "docs",
            "source": "docs.pdf",
            "source_sha256": source_sha,
            "page_start": index + 1,
            "page_end": index + 1,
            "section": "Section %d" % index,
            "chapter": "",
            "numbering": "",
            "parent": "",
            "path": "Section %d" % index,
            "doc": "Section %d\n\n%s" % (index, text),
            "text": text,
            "heading_only": False,
            "block_index": index,
            "chunk_index": index,
            "chunker_version": contracts.PEEL_CHUNKER_VERSION,
        })
    return rows


class GenerationTests(unittest.TestCase):
    def test_generation_layout_and_current_flip(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake, pages=[
                "Installation. The widget is mounted with two screws. " * 30,
                "Operation. Press the green button to start the widget. " * 30,
            ])
            link = embed_link(fake)
            info = ingest.build_index(dataset["dir"], link)
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
            # The digests name exactly the inputs this index was built from.
            self.assertEqual(
                loaded["info"]["documents_digest"],
                contracts.documents_digest(loaded["info"]["sources"]),
            )
            self.assertEqual(
                loaded["info"]["corpus_digest"],
                contracts.corpus_digest(loaded["info"]["sources"]),
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
            chunks = chunk_rows(3)
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
        return build_dataset(root, fake, pages=[
            "Alpha. The widget is mounted with two screws. " * 25,
            "Beta. Press the green button to start the widget. " * 25,
            "Gamma. The widget has a blue cover. " * 25,
        ])

    def test_cancelled_build_keeps_the_previous_generation_and_resumes(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = self._dataset(root, fake)
            link = embed_link(fake)
            first = ingest.build_index(dataset["dir"], link, batch_size=2)
            self.assertGreaterEqual(first["n"], 3)
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
                                   force=True, cancel=cancel)
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
            self.assertEqual(progress["corpus_digest"],
                             first["corpus_digest"])

            # A cancelled job record is persisted, not silently dropped.
            jobs = list((Path(root) / "jobs").glob("*.json"))
            states = [json.loads(path.read_text())["state"] for path in jobs]
            self.assertIn("cancelled", states)

            # Restart: the incomplete generation is resumed from its checkpoint
            # (no probe re-embed, only the remaining rows) and then committed.
            requests_before = len(fake.requests)
            resumed = ingest.build_index(dataset["dir"], link, batch_size=2)
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
            # A link that points nowhere makes the next (forced) build fail.
            broken = dict(link, base_url="http://127.0.0.1:1")
            with self.assertRaises(ingest.IngestError):
                ingest.build_index(dataset["dir"], broken, batch_size=2,
                                   force=True)
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

    def test_a_rebuilt_peel_makes_the_index_stale(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            state = ingest.index_state(dataset["dir"], info["space"],
                                       chunker=contracts.chunker_identity())
            self.assertEqual(state["status"], "indexed")
            make_peel(dataset, conversion_salt="2")
            state = ingest.index_state(dataset["dir"], info["space"],
                                       chunker=contracts.chunker_identity())
            self.assertEqual(state["status"], "stale")
            self.assertTrue(any("peel corpus changed" in reason
                                for reason in state["reasons"]))

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
            second = ingest.build_index(dataset["dir"], link, force=True)
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


class ReaderLeaseTests(unittest.TestCase):
    """CP3 wire §2.8: an index build leases the current build with flock SH."""

    def test_a_build_holds_the_shared_reader_lease_and_releases_it(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed.ingest import index as index_module

            dataset = build_dataset(root, fake)
            peel = make_peel(dataset)
            lock_path = (Path(peel["peel_dir"]) / "extract" / "reader-locks"
                         / (peel["build_id"] + ".lock"))
            observed = {}

            def exclusive_probe():
                fd = os.open(str(lock_path), os.O_RDWR)
                try:
                    with self.assertRaises(OSError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)

            original = index_module.embed_query

            def probing_query(link, text, *, timeout=None):
                if not observed:
                    observed["exists"] = lock_path.is_file()
                    exclusive_probe()
                return original(link, text, timeout=timeout)

            index_module.embed_query = probing_query
            try:
                info = ingest.build_index(dataset["dir"], embed_link(fake))
            finally:
                index_module.embed_query = original
            self.assertTrue(observed.get("exists"), "no reader lock file created")
            # Released after the CURRENT flip: peelfeed cleanup can take EX.
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            self.assertEqual((Path(dataset["dir"]) / "index" / info["space"]
                              / "CURRENT").read_text().strip(),
                             info["generation"])

    def test_a_build_aborts_before_current_flip_when_the_peel_moved_on(self):
        """CP3 wire §2.8: a mid-build publish aborts the generation (no flip)."""
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed.ingest import index as index_module

            dataset = build_dataset(root, fake)
            peel = make_peel(dataset)
            link = embed_link(fake)
            first = ingest.build_index(dataset["dir"], link)
            space_dir = Path(dataset["dir"]) / "index" / first["space"]
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             first["generation"])
            new_conversion = "f" * 64
            original = index_module.embed_query

            def publishing_query(link_arg, text, *, timeout=None):
                vector = original(link_arg, text, timeout=timeout)
                write_peel_build(peel, conversion_id=new_conversion)
                return vector

            index_module.embed_query = publishing_query
            try:
                with self.assertRaises(ingest.NeedsReindex) as caught:
                    ingest.build_index(dataset["dir"], link, force=True)
            finally:
                index_module.embed_query = original
            message = str(caught.exception)
            self.assertIn("needs_reindex", message)
            self.assertIn("changed", message)
            # The previous generation keeps serving; CURRENT was never flipped.
            self.assertEqual((space_dir / "CURRENT").read_text().strip(),
                             first["generation"])
            still_serving = ingest.load_index(dataset["dir"], first["space"])
            self.assertEqual(still_serving["info"]["generation"],
                             first["generation"])
            # The failed job carries the readable reason.
            jobs = [json.loads(path.read_text())
                    for path in (Path(root) / "jobs").glob("*.json")]
            failed = [job for job in jobs if job["state"] == "failed"]
            self.assertTrue(failed, jobs)
            self.assertIn("needs_reindex", failed[-1]["error"])
            # The dataset extraction identity is reconciled to the new current
            # instead of keeping the lease-time snapshot.
            metadata = json.loads((Path(dataset["dir"]) / "dataset.json").read_text())
            self.assertEqual(
                metadata["sources"][0]["extraction"]["conversion_id"],
                new_conversion)

    def test_legacy_real_corpus_dir_resolves_to_the_cutover_build(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            dataset = build_dataset(root, fake)
            peel = make_peel(dataset, layout="legacy")
            build = ingest.resolve_current_build(Path(peel["peel_dir"]))
            self.assertTrue(build["legacy"])
            self.assertEqual(build["build_id"],
                             "cutover-" + peel["conversion_id"])
            info = ingest.build_index(dataset["dir"], embed_link(fake))
            self.assertGreaterEqual(info["n"], 2)
            lock_path = (Path(peel["peel_dir"]) / "extract" / "reader-locks"
                         / (build["build_id"] + ".lock"))
            self.assertTrue(lock_path.is_file())


class SharedDatasetRefreshTests(unittest.TestCase):
    """CP3 wire §2.7 / contract §5.3: every dataset referencing a peel follows."""

    def _two_datasets(self, root):
        from peekseed import config

        pdf = make_pdf(root / "shared.pdf",
                       pages=["Widget page one about buttons.",
                              "Widget page two about screws."])
        first = ingest.create_dataset(config.paths(), "First")
        ingest.add_source(first, pdf)
        second = ingest.create_dataset(config.paths(), "Second")
        ingest.add_source(second, pdf)
        peel = make_peel(first)
        make_peel(second)
        return first, second, peel

    def test_reconcile_refreshes_all_datasets_and_conversion_change_stalens(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config

            first, second, peel = self._two_datasets(root)
            link = embed_link(fake)
            info = ingest.build_index(first["dir"], link)
            state = ingest.index_state(first["dir"], info["space"],
                                       chunker=contracts.chunker_identity())
            self.assertEqual(state["status"], "indexed")
            new_conversion = "a" * 64
            write_peel_build(peel, conversion_id=new_conversion)
            result = ingest.reconcile_all_datasets(config.paths())
            self.assertEqual(set(result["updated"]),
                             {first["id"], second["id"]})
            for dataset in (first, second):
                metadata = json.loads(
                    (Path(dataset["dir"]) / "dataset.json").read_text())
                extraction = metadata["sources"][0]["extraction"]
                self.assertEqual(extraction["conversion_id"], new_conversion)
                self.assertEqual(extraction["state"], "extracted")
            stale = ingest.index_state(first["dir"], info["space"],
                                       chunker=contracts.chunker_identity())
            self.assertEqual(stale["status"], "stale")
            self.assertTrue(any("peel corpus changed" in reason
                                for reason in stale["reasons"]), stale)

    def test_chunk_set_only_publish_keeps_indexes_valid(self):
        """Same conversion_id + new build_id must not stale any index."""
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config

            first, second, peel = self._two_datasets(root)
            link = embed_link(fake)
            info = ingest.build_index(first["dir"], link)
            small = {"target_chars": 1500, "max_chars": 2000, "image_cap": 1}
            write_peel_build(peel, conversion_id=peel["conversion_id"],
                             build_id="b20261108T120000-chunkonly",
                             chunk_sets=(EMBED_CHUNK, small))
            result = ingest.reconcile_all_datasets(config.paths())
            self.assertEqual(result["updated"], {})
            for dataset in (first, second):
                metadata = json.loads(
                    (Path(dataset["dir"]) / "dataset.json").read_text())
                self.assertEqual(metadata["sources"][0]["extraction"]
                                 ["conversion_id"], peel["conversion_id"])
            state = ingest.index_state(first["dir"], info["space"],
                                       chunker=contracts.chunker_identity())
            self.assertEqual(state["status"], "indexed", state)

    def test_reconcile_is_fault_tolerant_for_a_broken_peel(self):
        with temp_data_root() as root, FakeModelServer(dim=8) as fake:
            from peekseed import config

            dataset = build_dataset(root, fake)
            directory = Path(dataset["dir"])
            metadata = json.loads((directory / "dataset.json").read_text())
            metadata["sources"][0]["extraction"]["peel_id"] = "missing-peel"
            contracts.write_json_atomic(directory / "dataset.json", metadata)
            result = ingest.reconcile_all_datasets(config.paths())
            self.assertIsInstance(result, dict)
            metadata = json.loads((directory / "dataset.json").read_text())
            self.assertEqual(metadata["sources"][0]["extraction"]["peel_id"],
                             "missing-peel")
            self.assertEqual(metadata["sources"][0]["extraction"]["state"],
                             "extracted")


if __name__ == "__main__":
    unittest.main()
