import unittest
from pathlib import Path


class LibraryStageNavigationTests(unittest.TestCase):
    def test_library_handoff_enters_discovery_without_requiring_a_project(self) -> None:
        dashboard = Path(__file__).parents[1] / "assets" / "dashboard"
        script = (dashboard / "review-ui.js").read_text(encoding="utf-8")
        library = (dashboard / "library.html").read_text(encoding="utf-8")
        discovery = (dashboard / "discovery.html").read_text(encoding="utf-8")

        library_branch = script.index('if (current.id === "library")')
        project_gate = script.index("const projectId = projectForAction();", library_branch)
        self.assertLess(library_branch, project_gate)
        self.assertIn('window.location.assign("/discovery?create=1")', script)
        self.assertNotIn('id="globalProjectSelect"', library)
        self.assertNotIn("loadProjects()", library)
        self.assertIn('id="newDiscoveryShell"', discovery)
        self.assertIn("createRequested", discovery)

    def test_library_search_covers_metadata_fields_and_uses_all_query_terms(self) -> None:
        dashboard = Path(__file__).parents[1] / "assets" / "dashboard"
        library = (dashboard / "library.html").read_text(encoding="utf-8")

        self.assertIn("function paperSearchDocument", library)
        self.assertIn("paper.title", library)
        self.assertIn("searchableValues(paper.authors)", library)
        self.assertIn("searchableValues(paper.keywords)", library)
        self.assertIn("paper.abstract", library)
        self.assertIn("searchableValues(paper.structured_tags)", library)
        self.assertIn("terms.every(term => documentText.includes(term))", library)
        self.assertIn('class="search-empty" role="status"', library)


if __name__ == "__main__":
    unittest.main()
