from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneChecks(unittest.TestCase):
    def test_root_test_sources_are_not_hidden_by_blanket_ignore(self) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "tests/future_cleanup_check.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(1, result.returncode, result.stdout or result.stderr)

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
        for name in ("final.html",):
            text = (ROOT / "view" / "assets" / "dashboard" / name).read_text(encoding="utf-8")
            self.assertIsNone(assignments.search(text), name)

    def test_fastapi_serves_native_dashboard_without_legacy_transport(self) -> None:
        api = ROOT / "review_writer_api"
        app = (api / "app.py").read_text(encoding="utf-8")

        for path in (
            api / "legacy_adapter.py",
            api / "workflow_compat.py",
            api / "dashboard_executor.py",
            ROOT / "view" / "prefect_runtime.py",
            ROOT / "view" / "prefect_flows.py",
            ROOT / "view" / "provider_settings.py",
            ROOT / "view" / "workflow_store.py",
        ):
            self.assertFalse(path.exists(), path)
        self.assertNotIn("DashboardHandler", app)
        self.assertNotIn("serve_review_dashboard", app)
        self.assertNotIn("dispatch_legacy", app)
        self.assertIn("dashboard_page_paths", app)
        self.assertIn('app.mount(\n        "/assets"', app)

    def test_paragraph_edit_skill_uses_only_native_draft_artifacts(self) -> None:
        skill = (
            ROOT / "skills" / "review-paragraph-edit" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for retired in (
            "review-projects/",
            "first_draft.md",
            "citations.json",
            "figure_candidates.json",
            "paragraph_history.json",
            "versions/first_draft_",
            "paragraph_manifest_builder.py",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, skill)
        for required in (
            "/api/v1/projects/<id>/draft",
            "/api/v1/projects/<id>/draft/paragraphs/<pid>",
            "/api/v1/projects/<id>/draft/restore",
            "revision",
            "409",
            "immutable",
        ):
            with self.subTest(required=required):
                self.assertIn(required, skill)
        self.assertIn("not supported", skill.casefold())


if __name__ == "__main__":
    unittest.main()
