import unittest

from tests.fixtures import temp_data_root


class HistoryTests(unittest.TestCase):
    def _result(self):
        return {
            "dataset": "docs",
            "query": "green button",
            "model": "test-embed",
            "timings": {"embed_sec": 0.01, "total_sec": 0.02},
            "reranked": False,
            "results": [{"rank": 1, "id": "docs-000001", "label": "Operation",
                         "section": "2.2 Operation", "page_start": 2,
                         "page_end": 2, "snippet": "Press the green button."}],
        }

    def test_record_list_get_and_clear(self):
        with temp_data_root() as root:
            from peekseed.history import HistoryStore
            store = HistoryStore(root / "history")
            entry_id = store.record_search(self._result())
            self.assertTrue(entry_id)
            metas = store.list_meta()
            self.assertEqual(len(metas), 1)
            self.assertEqual(metas[0]["dataset"], "docs")
            entry = store.get(entry_id)
            self.assertEqual(entry["dataset"], "docs")
            self.assertEqual(entry["search"]["query"], "green button")
            self.assertEqual(store.clear()["cleared"], 1)
            self.assertEqual(store.list_meta(), [])

    def test_begin_run_records_answer_and_dataset(self):
        with temp_data_root() as root:
            from peekseed.history import HistoryStore
            store = HistoryStore(root / "history")
            entry_id = store.record_search(self._result())
            rec = store.begin_run(
                entry_id, mode="ask", question="How do I start it?",
                model="test-answer", dataset="docs",
                seed={"refs": [1], "auto": False,
                      "excerpts": [{"n": 1, "label": "Operation", "text": "..."}]})
            self.assertTrue(rec.persisted)
            rec.observe({"type": "context", "dataset": "docs"})
            rec.observe({"type": "text", "text": "Press the green button [1]."})
            rec.observe({"type": "done", "cited": [1], "unknown": [],
                         "ledger": [{"n": 1, "label": "Operation"}],
                         "chars": 27})
            rec.finish(status="done")
            entry = store.get(entry_id)
            self.assertEqual(entry["kind"], "llm")
            self.assertEqual(len(entry["runs"]), 1)
            run = entry["runs"][0]
            self.assertEqual(run["dataset"], "docs")
            self.assertIn("green button", run["answer"])
            self.assertEqual(run["cited"], [1])
            self.assertEqual(run["status"], "done")

    def test_get_unknown_returns_none(self):
        with temp_data_root() as root:
            from peekseed.history import HistoryStore
            store = HistoryStore(root / "history")
            self.assertIsNone(store.get("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
