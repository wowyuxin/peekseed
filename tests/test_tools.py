import unittest

from tests.fixtures import make_dataset, sample_chunks, temp_data_root, write_chunks


class ToolsTests(unittest.TestCase):
    def _setup(self):
        from peekseed import config
        from peekseed.tools import DatasetTools
        dataset = make_dataset(name="Docs")
        write_chunks(dataset, sample_chunks(dataset["id"]))
        tools = DatasetTools(config.paths().datasets_dir)
        return dataset, tools

    def test_list_datasets_reports_chunks_and_name(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows = tools.list_datasets()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dataset"], dataset["id"])
            self.assertEqual(rows[0]["name"], "Docs")
            self.assertEqual(rows[0]["chunks"], 3)

    def test_grep_finds_an_exact_token(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows, images, note = tools.run(
                "grep", {"pattern": "green button"}, default_dataset=dataset["id"])
            self.assertEqual(images, [])
            self.assertTrue(rows)
            self.assertTrue(any("green" in (row.get("text") or "").lower()
                                for row in rows))
            self.assertIn("match", note or "")

    def test_read_chunk_returns_the_full_text(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows, _images, _note = tools.run(
                "read_chunk", {"chunk_id": dataset["id"] + "-000002"},
                default_dataset=dataset["id"])
            self.assertEqual(len(rows), 1)
            self.assertIn("green button", rows[0]["text"])

    def test_read_section_by_number(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows, _images, _note = tools.run(
                "read_section", {"section": "2.2"}, default_dataset=dataset["id"])
            self.assertEqual(len(rows), 1)
            self.assertIn("green button", rows[0]["text"])

    def test_outline_lists_the_chapter(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows, _images, _note = tools.run(
                "outline", {}, default_dataset=dataset["id"])
            self.assertTrue(rows)
            self.assertTrue(any("Using the widget" in (row.get("label") or "")
                                for row in rows))

    def test_keyword_search_falls_back_without_an_embedder(self):
        with temp_data_root():
            dataset, tools = self._setup()
            rows, _images, note = tools.run(
                "search", {"query": "green button", "k": 3},
                default_dataset=dataset["id"])
            self.assertTrue(rows)
            self.assertIn("keyword", note)

    def test_injected_semantic_search_is_used(self):
        with temp_data_root():
            dataset, tools = self._setup()
            seen = []

            def semantic(dataset_id, query, k):
                seen.append((dataset_id, query, k))
                return [(dataset_id + "-000002", 0.99)]

            view = tools.with_semantic(semantic)
            rows, _images, note = view.run(
                "search", {"query": "anything", "k": 2},
                default_dataset=dataset["id"])
            self.assertEqual(seen, [(dataset["id"], "anything", 2)])
            self.assertIn("semantic", note)
            self.assertIn("green button", rows[0]["text"])

    def test_unknown_dataset_is_a_tool_error(self):
        with temp_data_root():
            _dataset, tools = self._setup()
            from peekseed.tools import ToolError
            with self.assertRaises(ToolError):
                tools.dataset("does-not-exist")


if __name__ == "__main__":
    unittest.main()
