"""Review cluster-list layout regression (served HTML/CSS contract).

The Review modal's left cluster list is a scroll container (``.rr-clusters`` has
``max-height`` + ``overflow-y:auto``). Each row is a ``<button class="rr-cl">``
inside that column flex container. Without ``flex:none`` the rows shrink to
their padding+border inside the ``max-height`` container, so the text of every
row overflows and overlaps the next one (reported 2026-11-11 on the deployed
CXL review page: 51 clusters, all rows 18 px tall with overlapping text).
The global ``button`` rule also centers children, which centered each cluster
row; the row must be left-aligned like the rest of the report.

This test guards the two CSS properties that prevent the regression. It is a
served-file contract check (no browser); the visual before/after evidence lives
in ``development/reports/2026-11-review-ui-cluster-list/``.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "peekseed" / "web" / "index.html"


def _rule_body(text, selector):
    match = re.search(re.escape(selector) + r"\{([^}]*)\}", text)
    return match.group(1) if match else ""


class ReviewClusterListLayoutTests(unittest.TestCase):
    def setUp(self):
        self.text = UI_PATH.read_text(encoding="utf-8")

    def test_rows_keep_their_content_height_inside_the_scroll_list(self):
        """Rows must not flex-shrink below content (text overlap regression)."""
        body = _rule_body(self.text, ".rr-cl")
        self.assertTrue(body, ".rr-cl rule is missing")
        self.assertIn("flex:none", body)
        self.assertNotIn("min-height:0", body)

    def test_rows_are_left_aligned_not_centered_by_the_button_rule(self):
        """The global button centering must be reset for a left-aligned row."""
        body = _rule_body(self.text, ".rr-cl")
        self.assertIn("align-items:stretch", body)
        self.assertIn("justify-content:flex-start", body)

    def test_cluster_list_stays_scrollable(self):
        """The list keeps its own scroll area instead of squeezing rows."""
        body = _rule_body(self.text, ".rr-clusters")
        self.assertIn("overflow-y:auto", body)
        self.assertIn("max-height:64vh", body)


if __name__ == "__main__":
    unittest.main()
