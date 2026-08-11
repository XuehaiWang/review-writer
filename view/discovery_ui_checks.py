from __future__ import annotations

import unittest
from pathlib import Path


DISCOVERY_PAGE = Path(__file__).resolve().parent / "assets" / "dashboard" / "discovery.html"
DISCOVERY_SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "review-topic-paper-discovery" / "scripts" / "discover.py"


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

    def test_summary_uses_unique_paper_ids_and_labels_keyword_hits_separately(self) -> None:
        self.assertIn("function discoveryCounts", self.source)
        self.assertIn("uniqueLocal: new Set(localKeys).size", self.source)
        self.assertIn("['Selected papers', stats.uniqueLocal]", self.source)
        self.assertIn("['Candidate papers', stats.candidateLocal]", self.source)
        self.assertIn("['Keyword hits', stats.localHits]", self.source)
        self.assertNotIn("${localMatches} local matches", self.source)

    def test_paper_choice_is_explicit_and_shared_across_duplicate_keyword_hits(self) -> None:
        self.assertIn("Include in Matrix", self.source)
        self.assertIn("Remove from Matrix", self.source)
        self.assertIn("function candidateSelected", self.source)
        self.assertIn("row?.selected_for_matrix === true", self.source)
        self.assertNotIn("row?.selected_for_matrix === true && row?.keep !== false", self.source)
        self.assertIn("function setCandidateSelection", self.source)
        self.assertIn("if (row.paper_id === id)", self.source)
        self.assertIn("function setCandidateRole", self.source)
        self.assertIn("if (role === 'excluded') row.selected_for_matrix = false", self.source)
        self.assertIn('type="button" class="btn ${selectedForMatrix', self.source)
        self.assertIn('aria-pressed="${selectedForMatrix', self.source)

    def test_confirmation_verifies_matrix_membership_before_redirecting(self) -> None:
        self.assertIn("result.matrix_sync?.selection_current", self.source)
        self.assertIn("count !== expectedCount", self.source)
        self.assertIn("selection_fingerprint", self.source)

    def test_new_candidates_start_unselected_and_can_be_cleared_in_bulk(self) -> None:
        discovery_script = DISCOVERY_SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(discovery_script.count('"selected_for_matrix": False'), 3)
        self.assertIn("clearPaperSelection", self.source)
        self.assertIn("function clearCandidateSelection", self.source)

    def test_left_review_column_keeps_keyword_list_visible(self) -> None:
        self.assertIn('class="panel discovery-sidebar"', self.source)
        self.assertIn('class="new-discovery-shell"', self.source)
        self.assertNotIn('<details class="new-discovery-shell" open', self.source)
        self.assertIn(".discovery-sidebar .list { min-height: 170px;", self.source)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr));", self.source)
        self.assertIn("body.page-discovery", self.source)
        self.assertIn('class="sidebar-controls"', self.source)

    def test_generic_stage_action_is_not_duplicated_on_discovery(self) -> None:
        review_ui = (DISCOVERY_PAGE.parent / "review-ui.js").read_text(encoding="utf-8")
        self.assertIn('if (current.id === "discovery") return;', review_ui)

    def test_stage_columns_collapse_at_the_shared_narrow_screen_breakpoint(self) -> None:
        review_css = (DISCOVERY_PAGE.parent / "review-ui.css").read_text(encoding="utf-8")
        self.assertIn("body[class] .app { grid-template-columns: 1fr !important; }", review_css)


if __name__ == "__main__":
    unittest.main()
