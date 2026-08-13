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
        self.assertIn("uniqueLocal: new Set(candidateLocalKeys).size", self.source)
        self.assertIn("candidateHits: candidateLocalRows.length", self.source)
        self.assertIn("['Candidate papers', stats.candidateLocal]", self.source)
        self.assertIn("['Keyword hits', stats.candidateHits]", self.source)
        self.assertNotIn("['Keyword hits', stats.localHits]", self.source)
        self.assertIn("selectedLocal: new Set(selectedLocalKeys).size", self.source)
        self.assertIn("['Selected papers', stats.selectedLocal]", self.source)
        self.assertIn("const localRows = groups.flatMap(group => group.local_results || [])", self.source)
        self.assertIn("const selectedLocalRows = localRows.filter(candidateSelected)", self.source)
        self.assertNotIn("${localMatches} local matches", self.source)

    def test_ready_message_reports_candidate_pool_and_hits_not_selected_counts(self) -> None:
        self.assertIn("found ${stats.candidateLocal} candidate papers (${stats.candidateHits} keyword hits)", self.source)
        self.assertIn("Selected for Matrix: ${stats.selectedLocal}; include candidates to build the selection.", self.source)
        self.assertNotIn("${stats.uniqueLocal} unique local papers (${stats.localHits} keyword hits)", self.source)

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
        self.assertIn("const expectedCount = discoveryCounts(data).selectedLocal", self.source)
        self.assertIn("count !== expectedCount", self.source)
        self.assertIn("selection_fingerprint", self.source)

    def test_top_ranked_candidates_can_replace_the_local_matrix_selection(self) -> None:
        self.assertIn('id="topPaperCount"', self.source)
        self.assertIn('id="selectTopPapers"', self.source)
        self.assertIn("function rankedLocalCandidates", self.source)
        self.assertIn("function selectTopCandidates", self.source)
        self.assertIn("(b.score - a.score) || (a.sourceOrder - b.sourceOrder)", self.source)
        self.assertIn("const selectedIds = new Set(candidates.slice(0, count)", self.source)
        self.assertIn("row.selected_for_matrix = selectedForMatrix", self.source)
        self.assertIn("els.selectTopPapers.addEventListener('click', selectTopCandidates)", self.source)

    def test_middle_toolbar_groups_keyword_and_bulk_selection_actions(self) -> None:
        self.assertIn('class="toolbar keyword-result-toolbar"', self.source)
        self.assertIn('class="keyword-review-actions"', self.source)
        self.assertIn("grid-template-columns: max-content 72px minmax(0, 1fr);", self.source)
        toolbar_start = self.source.index('class="toolbar keyword-result-toolbar"')
        toolbar_end = self.source.index('<div id="results"', toolbar_start)
        toolbar = self.source[toolbar_start:toolbar_end]
        self.assertLess(toolbar.index('id="toggleKeywordBtn"'), toolbar.index('id="clearPaperSelection"'))
        self.assertLess(toolbar.index('id="clearPaperSelection"'), toolbar.index('class="top-selection-control"'))

    def test_new_candidates_start_unselected_and_can_be_cleared_in_bulk(self) -> None:
        discovery_script = DISCOVERY_SCRIPT.read_text(encoding="utf-8")
        self.assertGreaterEqual(discovery_script.count('"selected_for_matrix": False'), 3)
        self.assertIn("clearPaperSelection", self.source)
        self.assertIn("function clearCandidateSelection", self.source)

    def test_existing_project_can_change_topic_with_explicit_reset_confirmation(self) -> None:
        self.assertIn('id="restartDiscoveryBtn"', self.source)
        self.assertIn('id="restartNotice"', self.source)
        self.assertIn("function prepareDiscoveryRestart", self.source)
        self.assertIn("restart_existing: discoveryFormMode === 'restart'", self.source)
        self.assertIn("window.confirm(tr('Replace this project topic", self.source)
        self.assertIn("The current project is changed only after Discovery succeeds.", self.source)
        self.assertIn('id="cancelRestartBtn"', self.source)

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
