"""Regression checks for the optional first-draft feedback loop."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "review-first-draft-feedback-loop" / "scripts" / "feedback_loop.py"
FINAL_PAGE = ROOT / "view" / "assets" / "dashboard" / "final.html"
loop = runpy.run_path(str(SCRIPT))
DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "serve_review_dashboard_feedback_loop",
    ROOT / "view" / "serve_review_dashboard.py",
)
dashboard = importlib.util.module_from_spec(DASHBOARD_SPEC)
assert DASHBOARD_SPEC.loader is not None
DASHBOARD_SPEC.loader.exec_module(dashboard)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def create_project(root: Path, paragraph: str) -> tuple[Path, Path]:
    project = root / "review-projects" / "demo"
    draft = project / "04_first_draft" / "first_draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(
        "# Review\n\n## Results\n\n"
        + paragraph
        + "\n\n<!-- paragraph_id: sec1-p1 -->\n\n## References\n\n[1] Source.\n",
        encoding="utf-8",
    )
    write_json(
        project / "02_section_drafting" / "section_drafts.json",
        {
            "project_id": "demo",
            "sections": [
                {
                    "paragraphs": [
                        {
                            "paragraph_id": "sec1-p1",
                            "paper_id": "P001",
                            "cited_paper_ids": ["P001"],
                            "text": paragraph,
                        }
                    ]
                }
            ],
        },
    )
    write_json(
        project / "01_matrix_outline" / "literature_matrix.json",
        {"rows": [{"paper_id": "P001", "title": "Source"}]},
    )
    write_json(
        project / "04_first_draft" / "citations.json",
        {"entries": [{"callout": 1, "paper_id": "P001"}]},
    )
    source = root / "review-library" / "sources" / "P001.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    write_json(
        root / "review-library" / "metadata" / "papers" / "P001.metadata.json",
        {"paper_id": "P001", "source_paths": {"pdf": "review-library/sources/P001.pdf"}},
    )
    return project, draft


class FeedbackLoopChecks(unittest.TestCase):
    def test_release_path_scores_every_paragraph_without_mutating_stage5(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _draft = create_project(root, paragraph)
            section_path = project / "02_section_drafting" / "section_drafts.json"
            section_before = section_path.read_bytes()
            rubric = json.loads(
                (ROOT / "skills" / "review-first-draft-feedback-loop" / "references" / "unified_rubric.json").read_text(
                    encoding="utf-8"
                )
            )

            def fake_model(_prompt: str, *, label: str) -> dict[str, object]:
                self.assertEqual(label, "First-draft rubric evaluation")
                return {
                    "dimension_scores": [
                        {"id": item["id"], "level": 4, "evidence": "Satisfied."}
                        for item in rubric["dimensions"]
                    ],
                    "paragraph_scores": [
                        {
                            "paragraph_id": "sec1-p1",
                            "score": 96,
                            "failed_dimensions": [],
                            "severity": "none",
                            "diagnosis": "Ready.",
                            "route": "pass",
                        }
                    ],
                }

            feedback_globals = loop["run_feedback_loop"].__globals__
            original_model = feedback_globals["call_json_model"]
            feedback_globals["call_json_model"] = fake_model
            try:
                result = loop["run_feedback_loop"](
                    argparse.Namespace(
                        review_root=str(root),
                        project_id="demo",
                        goal=90.0,
                        paragraph_goal=85.0,
                        max_iterations=3,
                        min_improvement=1.0,
                        min_case_words=5,
                        max_case_words=30,
                        evaluate_only=False,
                    )
                )
            finally:
                feedback_globals["call_json_model"] = original_model

            self.assertEqual(result["status"], "released")
            self.assertEqual(section_path.read_bytes(), section_before)
            self.assertFalse(
                (project / "04_first_draft" / "feedback_loop_rewrites.json").exists()
            )
            status = json.loads(
                (project / "04_first_draft" / "feedback_loop_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["phase"], "released")
            self.assertEqual(status["paragraph_scores"][0]["paragraph_id"], "sec1-p1")

    def test_fatal_retry_failure_restores_draft_and_overlay_transactionally(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
            original_draft = draft.read_bytes()
            rubric = json.loads(
                (ROOT / "skills" / "review-first-draft-feedback-loop" / "references" / "unified_rubric.json").read_text(
                    encoding="utf-8"
                )
            )
            calls = 0

            def fake_model(_prompt: str, *, label: str) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {
                        "dimension_scores": [
                            {"id": item["id"], "level": 2, "evidence": "Needs work."}
                            for item in rubric["dimensions"]
                        ],
                        "paragraph_scores": [
                            {
                                "paragraph_id": "sec1-p1",
                                "score": 60,
                                "failed_dimensions": ["P03"],
                                "severity": "major",
                                "diagnosis": "Improve flow.",
                                "route": "section_rewrite",
                            }
                        ],
                    }
                if calls == 2:
                    self.assertIn("Paragraph rewrite", label)
                    return {
                        "text": "This study provides a clearer bounded comparison using the reported result [1]."
                    }
                raise RuntimeError("simulated provider outage")

            feedback_globals = loop["run_feedback_loop"].__globals__
            original_model = feedback_globals["call_json_model"]
            feedback_globals["call_json_model"] = fake_model
            try:
                with self.assertRaisesRegex(RuntimeError, "simulated provider outage"):
                    loop["run_feedback_loop"](
                        argparse.Namespace(
                            review_root=str(root),
                            project_id="demo",
                            goal=90.0,
                            paragraph_goal=85.0,
                            max_iterations=3,
                            min_improvement=1.0,
                            min_case_words=5,
                            max_case_words=30,
                            evaluate_only=False,
                        )
                    )
            finally:
                feedback_globals["call_json_model"] = original_model

            self.assertEqual(draft.read_bytes(), original_draft)
            self.assertFalse(
                (project / "04_first_draft" / "feedback_loop_rewrites.json").exists()
            )
            status = json.loads(
                (project / "04_first_draft" / "feedback_loop_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["status"], "failed")
            self.assertEqual(
                status["output_draft_sha256"], hashlib.sha256(original_draft).hexdigest()
            )

    def test_stage_options_reach_the_feedback_loop_without_gating_final(self) -> None:
        captured: dict[str, object] = {}

        def fake_loop(review_root: Path, project_id: str, **options: object) -> dict[str, object]:
            captured.update(options)
            return {"status": {"status": "completed"}}

        with patch.object(dashboard, "run_first_draft_feedback_loop", side_effect=fake_loop):
            result = dashboard.execute_dashboard_stage(
                ROOT,
                "demo",
                "draft-feedback-loop",
                {
                    "goal": 92,
                    "paragraph_goal": 86,
                    "max_iterations": 4,
                    "min_case_words": 130,
                    "max_case_words": 260,
                    "evaluate_only": True,
                },
            )

        self.assertEqual(result["next_stage"], "final")
        self.assertEqual(captured["goal"], 92.0)
        self.assertEqual(captured["paragraph_goal"], 86.0)
        self.assertEqual(captured["max_iterations"], 4)
        self.assertTrue(captured["evaluate_only"])
        stage_specs = __import__(dashboard.WorkflowStore.__module__).STAGE_SPECS
        self.assertNotIn(
            "draft-feedback-loop", stage_specs["final"]["optional_depends_on"]
        )
        self.assertNotIn(
            "draft-feedback-loop",
            stage_specs["final-conclusion"]["optional_depends_on"],
        )

    def test_provider_endpoint_accepts_base_or_complete_route(self) -> None:
        self.assertEqual(
            loop["provider_endpoint"]("https://provider.example", "chat-completions"),
            "https://provider.example/v1/chat/completions",
        )
        self.assertEqual(
            loop["provider_endpoint"](
                "https://provider.example/v1/chat/completions", "chat-completions"
            ),
            "https://provider.example/v1/chat/completions",
        )
        self.assertEqual(
            loop["provider_endpoint"]("https://provider.example/v1", "responses"),
            "https://provider.example/v1/responses",
        )

    def test_preflight_accepts_current_envelopes_and_relative_source_paths(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            create_project(root, paragraph)

            report = loop["deterministic_preflight"](
                root, "demo", min_words=5, max_words=30
            )

            self.assertEqual(report["hard_regressions"], [])
            self.assertEqual(report["checks"]["citation_callouts"], [1])
            self.assertTrue(report["paragraph_checks"][0]["local_source_available"])

    def test_rewrite_protects_citations_numbers_and_stereochemistry(self) -> None:
        original = "The reaction delivered 92% yield and 95% ee for the R product [1]."
        candidate = "The reaction delivered 90% yield and 95% ee for the S product [2]."

        errors = loop["validate_rewrite"](original, candidate, 5, 30)

        self.assertIn("protected_callouts_changed", errors)
        self.assertIn("protected_numbers_changed", errors)
        self.assertIn("protected_stereo_changed", errors)

    def test_multiple_iterations_keep_original_overlay_hash(self) -> None:
        original = "Original evidence-bound statement [1]."
        first = "First clearer evidence-bound statement [1]."
        second = "Second clearer evidence-bound statement [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, original)

            loop["record_rewrite_overlay"](project, "sec1-p1", original, first)
            loop["record_rewrite_overlay"](project, "sec1-p1", first, second)
            overlay = json.loads(
                (project / "04_first_draft" / "feedback_loop_rewrites.json").read_text(
                    encoding="utf-8"
                )
            )["entries"]["sec1-p1"]

            self.assertEqual(
                overlay["source_text_sha256"],
                hashlib.sha256(loop["clean_text"](original).encode("utf-8")).hexdigest(),
            )
            result = loop["apply_rewrite_overlays"](project)
            self.assertEqual(result["applied"], ["sec1-p1"])
            self.assertIn(second, draft.read_text(encoding="utf-8"))

    def test_overlay_conflict_never_overwrites_new_upstream_text(self) -> None:
        original = "Original evidence-bound statement [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, original)
            loop["record_rewrite_overlay"](
                project, "sec1-p1", original, "Accepted rewritten statement [1]."
            )
            newer = draft.read_text(encoding="utf-8").replace(original, "New upstream statement [1].")
            draft.write_text(newer, encoding="utf-8")

            result = loop["apply_rewrite_overlays"](project)

            self.assertEqual(result["applied"], [])
            self.assertEqual(result["conflicts"], ["sec1-p1"])
            self.assertIn("New upstream statement [1].", draft.read_text(encoding="utf-8"))

    def test_final_page_keeps_old_actions_independent_and_adds_optional_loop(self) -> None:
        page = FINAL_PAGE.read_text(encoding="utf-8")

        self.assertIn('data-doc="quality"', page)
        self.assertIn('data-feedback-action="improve"', page)
        self.assertIn('data-feedback-action="evaluate"', page)
        self.assertIn('data-feedback-action="stop"', page)
        self.assertIn('data-final-stage="final-conclusion"', page)
        self.assertIn('data-final-stage="final-overview-figure"', page)
        self.assertIn('data-final-stage="final"', page)
        self.assertIn("This optional step does not block the existing Final actions.", page)


if __name__ == "__main__":
    unittest.main()
