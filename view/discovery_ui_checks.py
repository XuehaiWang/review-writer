from __future__ import annotations

import unittest
from pathlib import Path


DISCOVERY_PAGE = Path(__file__).resolve().parent / "assets" / "dashboard" / "discovery.html"


class DiscoveryResultInteractionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = DISCOVERY_PAGE.read_text(encoding="utf-8")

    def test_result_card_selects_the_paper_without_a_view_button(self) -> None:
        self.assertIn('data-result-kind=', self.source)
        self.assertIn("e.target.closest('[data-result-kind]')", self.source)
        self.assertNotIn('data-action="view"', self.source)

    def test_selected_result_has_a_persistent_visual_marker(self) -> None:
        self.assertIn(".result.active", self.source)
        self.assertIn("selected-indicator", self.source)
        self.assertIn("aria-current", self.source)

    def test_detail_requests_cannot_overwrite_a_newer_selection(self) -> None:
        self.assertIn("detailRequestToken", self.source)
        self.assertIn("requestToken !== detailRequestToken", self.source)


if __name__ == "__main__":
    unittest.main()
