from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneChecks(unittest.TestCase):
    def test_skill_docs_do_not_contain_machine_or_retired_paths(self) -> None:
        forbidden = re.compile(
            r"(?:[A-Za-z]:\\|/home/|/Users/|/mnt/|review-writer-main|"
            r"source-paper/Progargylic|review-library/(?:paper_pdf|mineru-outputs)|"
            r"allene_classification_rules|<review-root>/template|references/templates)"
        )
        matches: list[str] = []
        for path in (ROOT / "skills").rglob("SKILL.md"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if forbidden.search(line):
                    matches.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(matches, [])

    def test_legacy_library_dashboard_is_absent(self) -> None:
        self.assertFalse((ROOT / "review-library" / "dashboard").exists())
        self.assertTrue((ROOT / "view" / "assets" / "dashboard" / "library.html").is_file())

    def test_online_registration_uses_the_canonical_mineru_output(self) -> None:
        script = (
            ROOT / "skills" / "review-online-paper-discovery" / "scripts" / "discover.py"
        ).read_text(encoding="utf-8")
        self.assertIn('else review_root / "mineru-outputs"', script)
        self.assertNotIn('review_root / "review-library" / "mineru-outputs"', script)

    def test_runtime_secrets_are_not_stored_inside_skills(self) -> None:
        token_file = (
            ROOT
            / "skills"
            / "mineru-precise-parse-review-writer"
            / "config"
            / "mineru_api_token.txt"
        )
        parser = (
            ROOT
            / "skills"
            / "mineru-precise-parse-review-writer"
            / "scripts"
            / "parse_review_writer_pdfs.py"
        ).read_text(encoding="utf-8")
        self.assertFalse(token_file.exists())
        self.assertNotIn("mineru_api_token.txt", parser)

    def test_dashboard_pages_do_not_patch_core_render_functions_at_runtime(self) -> None:
        assignments = re.compile(
            r"(?m)^\s*(?:render|renderMain|renderList|renderSummary|matchRedrawn|"
            r"outputPath|rejectedPreviewPath|svgEditorMarkup|svgEditorUndo|"
            r"bindSvgEditorInteractions|saveSvgEditor|svgEditorDocument|"
            r"renderSvgEditorOperations|loadSvgEditorAudit)\s*=\s*(?:async\s+)?function"
        )
        for name in ("figures.html", "final.html"):
            text = (ROOT / "view" / "assets" / "dashboard" / name).read_text(encoding="utf-8")
            self.assertIsNone(assignments.search(text), name)

    def test_fastapi_shell_keeps_dashboard_transport_behind_one_boundary(self) -> None:
        api = ROOT / "review_writer_api"
        app = (api / "app.py").read_text(encoding="utf-8")
        gateway = (api / "workflow_compat.py").read_text(encoding="utf-8")
        executor = (api / "dashboard_executor.py").read_text(encoding="utf-8")

        self.assertFalse((api / "legacy_adapter.py").exists())
        self.assertNotIn("DashboardHandler", app)
        self.assertNotIn("serve_review_dashboard", app)
        self.assertNotIn("dispatch_legacy", app)
        self.assertIn("workflow_gateway.register_routes", app)
        self.assertIn('"Deprecation": "true"', gateway)
        self.assertIn("DashboardHandler", executor)


if __name__ == "__main__":
    unittest.main()
