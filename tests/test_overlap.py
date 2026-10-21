"""The overlap harness used for the P4 acceptance record (offline tests)."""

import unittest

from peekseed import overlap


def rows(*ids):
    return [{"id": value, "text": "…"} for value in ids]


class OverlapTests(unittest.TestCase):
    def test_top_k_overlap_uses_the_union_not_the_candidate_size(self):
        baseline = rows("a", "b", "c", "d")
        candidate = rows("b", "a", "z")
        result = overlap.overlap(baseline, candidate, topk=4)
        self.assertEqual(result["overlap"], 2)
        self.assertEqual(result["union"], 5)       # {a,b,c,d,z}
        self.assertEqual(result["fraction"], 0.4)
        self.assertEqual(result["baseline_top"], ["a", "b", "c", "d"])
        self.assertEqual(result["candidate_top"], ["b", "a", "z"])

    def test_topk_truncates_both_lists_and_an_empty_list_is_zero(self):
        result = overlap.overlap(rows("a", "b", "c"), rows("a", "b"), topk=2)
        self.assertEqual(result["overlap"], 2)
        self.assertEqual(result["fraction"], 1.0)
        empty = overlap.overlap([], rows("a"), topk=2)
        self.assertEqual(empty["fraction"], 0.0)
        self.assertEqual(empty["union"], 1)

    def test_compare_records_each_query_and_survives_a_failing_provider(self):
        def baseline(query):
            return rows("a", "b") if query != "boom" else 1 / 0

        def candidate(query):
            return rows("a", "c")

        report = overlap.compare(["q1", "boom", "q2"], baseline, candidate, topk=2)
        self.assertEqual(report["measured"], 2)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["mean_overlap"], 0.3333)  # {a} / {a,b,c} twice
        self.assertEqual(report["min_overlap"], 0.3333)
        self.assertIn("baseline", report["queries"][1]["error"])

    def test_a_custom_id_key_is_supported(self):
        baseline = [{"chunk": "x"}, {"chunk": "y"}]
        candidate = [{"chunk": "y"}]
        result = overlap.overlap(baseline, candidate, key="chunk", topk=2)
        self.assertEqual(result["overlap"], 1)
        self.assertEqual(result["fraction"], 0.5)


if __name__ == "__main__":
    unittest.main()
