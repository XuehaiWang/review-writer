"""Regression checks for the optional first-draft feedback loop."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import runpy
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from review_writer_core.paragraph_markers import (
    build_paragraph_manifest,
    ensure_prose_paragraph_markers,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "review-first-draft-feedback-loop" / "scripts" / "feedback_loop.py"
FINAL_PAGE = ROOT / "view" / "assets" / "dashboard" / "final.html"
DRAFT_PAGE = ROOT / "view" / "assets" / "dashboard" / "draft.html"
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
    def test_single_paragraph_ai_rewrite_is_a_non_mutating_human_review_candidate(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
            draft_hash = hashlib.sha256(draft.read_bytes()).hexdigest()
            write_json(
                project / "04_first_draft" / "feedback_loop_status.json",
                {
                    "status": "needs_human_review",
                    "output_draft_sha256": draft_hash,
                    "paragraph_goal": 85,
                },
            )
            write_json(
                project / "04_first_draft" / "rubric_evaluation.json",
                {
                    "paragraph_failures": [
                        {
                            "paragraph_id": "sec1-p1",
                            "score": 60,
                            "route": "section_rewrite",
                            "diagnosis": "Clarify the comparison.",
                        }
                    ]
                },
            )
            write_json(
                project / "04_first_draft" / "first_draft_preflight.json",
                {"paragraph_checks": [{"paragraph_id": "sec1-p1", "word_range_applicable": True}]},
            )
            before = draft.read_bytes()
            feedback_module = types.ModuleType("feedback_loop")
            feedback_module.__dict__.update(loop)
            candidate_spec = importlib.util.spec_from_file_location(
                "propose_paragraph_rewrite_check",
                ROOT
                / "skills"
                / "review-first-draft-feedback-loop"
                / "scripts"
                / "propose_paragraph_rewrite.py",
            )
            candidate_module = importlib.util.module_from_spec(candidate_spec)
            assert candidate_spec.loader is not None
            with patch.dict("sys.modules", {"feedback_loop": feedback_module}):
                candidate_spec.loader.exec_module(candidate_module)
            proposed = "This reported study provides a clearer bounded comparison with the reported result [1]."
            with patch.object(candidate_module.loop, "call_json_model", return_value={"text": proposed}):
                entry = candidate_module.propose(
                    argparse.Namespace(
                        review_root=str(root),
                        project_id="demo",
                        paragraph_id="sec1-p1",
                        min_case_words=1,
                        max_case_words=80,
                    )
                )
            self.assertEqual(draft.read_bytes(), before)
            self.assertEqual(entry["status"], "pending_human_review")
            self.assertEqual(entry["candidate_text"], proposed)
            stored = json.loads(
                (project / "04_first_draft" / "feedback_rewrite_candidates.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(stored["entries"]["sec1-p1"]["draft_sha256"], draft_hash)

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

        self.assertEqual(result["next_stage"], "draft")
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

    def test_host_rejects_an_overall_goal_below_the_rubric_threshold(self) -> None:
        with self.assertRaisesRegex(ValueError, "rubric threshold"):
            dashboard.run_first_draft_feedback_loop(ROOT, "demo", goal=89)

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

    def test_feedback_issue_payload_exposes_current_manuscript_paragraph(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _project, draft = create_project(root, paragraph)
            figure = draft.parent / "figures" / "scheme.png"
            figure.parent.mkdir(parents=True)
            figure.write_bytes(b"png")
            marker = "<!-- paragraph_id: sec1-p1 -->"
            markdown = draft.read_text(encoding="utf-8").replace(
                marker,
                marker
                + '\n\n<!-- inserted_figure: {"figure_id":"P001-F01",'
                + '"target_paragraph_id":"sec1-p1"} -->\n\n'
                + "![Reaction scheme](figures/scheme.png)",
            )
            draft.write_text(markdown, encoding="utf-8")

            contents = dashboard.feedback_paragraph_contents(draft)

            self.assertEqual(contents["sec1-p1"]["heading"], "Results")
            self.assertEqual(contents["sec1-p1"]["text"], paragraph)
            self.assertEqual(contents["sec1-p1"]["images"][0]["alt"], "Reaction scheme")
            self.assertEqual(
                contents["sec1-p1"]["images"][0]["path"], str(figure.resolve())
            )
            self.assertNotIn("References", contents["sec1-p1"]["text"])

    def test_rewrite_protects_citations_numbers_and_stereochemistry(self) -> None:
        original = "The reaction delivered 92% yield and 95% ee for the R product [1]."
        candidate = "The reaction delivered 90% yield and 95% ee for the S product [2]."

        errors = loop["validate_rewrite"](original, candidate, 5, 30)

        self.assertIn("protected_callouts_changed", errors)
        self.assertIn("protected_numbers_changed", errors)
        self.assertIn("protected_stereo_changed", errors)

    def test_rewrite_cannot_change_figure_path_or_anchor_metadata(self) -> None:
        original = (
            '<!-- inserted_figure: {"figure_id":"P001-F01",'
            '"target_paragraph_id":"sec1-p1"} -->\n\n'
            "![Reaction scheme](figures/scheme.png)\n\n"
            "The scheme summarizes the reported transformation [1]."
        )
        changed_path = original.replace("figures/scheme.png", "figures/other.png")
        removed_anchor = re.sub(r"<!--.*?-->\s*", "", original, count=1, flags=re.S)

        path_errors = loop["validate_rewrite"](original, changed_path, 1, 100)
        anchor_errors = loop["validate_rewrite"](original, removed_anchor, 1, 100)

        self.assertIn("protected_images_changed", path_errors)
        self.assertIn("protected_figure_metadata_changed", anchor_errors)

    def test_rewrite_protects_chemical_identities_labels_and_citation_order(self) -> None:
        original = (
            "CuI and DIPEA form allene intermediate int-I [1], whereas "
            "PdCl2 gives alkyne product A [2]."
        )
        changed_chemistry = (
            "PdCl2 and triethylamine form alkyne intermediate int-X [1], whereas "
            "CuI gives allene product B [2]."
        )
        swapped_support = (
            "CuI and DIPEA form allene intermediate int-I [2], whereas "
            "PdCl2 gives alkyne product A [1]."
        )

        chemistry_errors = loop["validate_rewrite"](original, changed_chemistry, 5, 40)
        citation_errors = loop["validate_rewrite"](original, swapped_support, 5, 40)

        self.assertIn("protected_chemical_identities_changed", chemistry_errors)
        self.assertIn("protected_required_labels_changed", chemistry_errors)
        self.assertIn("protected_callouts_changed", citation_errors)

    def test_supporting_transition_is_not_forced_into_case_word_range(self) -> None:
        paragraph = "The reported study provides a bounded comparison supported by the source [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _project, draft = create_project(root, paragraph)
            markdown = draft.read_text(encoding="utf-8").replace(
                "## References",
                "This short transition introduces the next comparison.\n\n"
                "<!-- paragraph_id: bridge-p1 -->\n\n## References",
            )
            draft.write_text(markdown, encoding="utf-8")

            report = loop["deterministic_preflight"](
                root,
                "demo",
                min_words=20,
                max_words=80,
            )
            checks = {
                item["paragraph_id"]: item for item in report["paragraph_checks"]
            }
            self.assertTrue(checks["sec1-p1"]["word_range_applicable"])
            self.assertIn("P01", checks["sec1-p1"]["issues"])
            self.assertFalse(checks["bridge-p1"]["word_range_applicable"])
            self.assertNotIn("P01", checks["bridge-p1"]["issues"])

    def test_optimization_retrieves_page_anchored_mineru_original_text(self) -> None:
        paragraph = (
            "Palladium oxidative addition of propargylic carbonate forms an "
            "allenylpalladium intermediate before allene insertion [1]."
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _draft = create_project(root, paragraph)
            content_list = root / "mineru-outputs" / "p001_content_list.json"
            write_json(
                content_list,
                [
                    {
                        "type": "text",
                        "page_idx": 0,
                        "text": "A general introduction discusses transition-metal catalysis.",
                    },
                    {
                        "type": "text",
                        "page_idx": 4,
                        "text": (
                            "Oxidative addition of propargylic carbonates with Pd(0) affords "
                            "allenylpalladium(II) species, followed by insertion with allenes."
                        ),
                    },
                ],
            )
            metadata_path = root / "review-library" / "metadata" / "papers" / "P001.metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source_paths"]["content_list"] = str(content_list)
            write_json(metadata_path, metadata)
            parsed = loop["parse_marked_paragraphs"](
                (project / "04_first_draft" / "first_draft.md").read_text(encoding="utf-8")
            )[0]

            evidence = loop["source_evidence"](
                root,
                project,
                parsed,
                {"paper_id": "P001", "cited_paper_ids": ["P001"]},
                {"P001": {"paper_id": "P001", "title": "Source"}},
                {},
            )

            self.assertEqual(evidence["evidence_scope"], "retrieved_original_full_text")
            self.assertTrue(evidence["original_source_ready"])
            source = evidence["evidence"][0]
            self.assertEqual(source["source_kind"], "mineru_content_list")
            self.assertEqual(source["original_passages"][0]["page"], 5)
            self.assertEqual(source["original_passages"][0]["ref"], "P001:p5:b2")
            self.assertIn("allenylpalladium", source["original_passages"][0]["text"])

    def test_figure_caption_reference_number_does_not_override_paper_identity(self) -> None:
        paragraph = (
            '<!-- inserted_figure: {"figure_id":"P011-F01"} -->\n\n'
            "![Scheme](figure.png)\n\nScheme 15. Source-paper caption.[7]"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, _draft = create_project(root, paragraph)
            write_json(
                project / "04_first_draft" / "citations.json",
                {"entries": [{"callout": 7, "paper_id": "P014"}]},
            )
            source = root / "review-library" / "sources" / "P011.md"
            source.write_text("P011 source describes Scheme 15.", encoding="utf-8")
            write_json(
                root / "review-library" / "metadata" / "papers" / "P011.metadata.json",
                {"paper_id": "P011", "source_paths": {"markdown": str(source)}},
            )
            parsed = loop["parse_marked_paragraphs"](
                (project / "04_first_draft" / "first_draft.md").read_text(encoding="utf-8")
            )[0]
            parsed["paragraph_id"] = "p011-effects-p1"

            evidence = loop["source_evidence"](
                root,
                project,
                parsed,
                {},
                {"P011": {"paper_id": "P011"}, "P014": {"paper_id": "P014"}},
                {},
            )

            self.assertEqual(evidence["paper_ids"], ["P011"])

    def test_cross_language_retrieval_finds_chinese_chemistry_passages(self) -> None:
        document = {
            "blocks": [
                {"page": 1, "text": "Abstract: reactions of allenols are reviewed."},
                {
                    "page": 8,
                    "text": "在金催化条件下发生环化反应，并观察到轴手性向中心手性转移。",
                },
                {
                    "page": 12,
                    "text": "零价钯催化烯丙基溴化物发生反应，生成二氢呋喃化合物。",
                },
            ]
        }

        passages = loop["retrieve_original_passages"](
            "P029",
            "Gold-catalyzed cyclization enabled axial-to-central chirality transfer; "
            "Pd(0) allylic bromide chemistry furnished dihydrofurans.",
            document,
        )

        pages = {item["page"] for item in passages}
        self.assertIn(8, pages)
        self.assertIn(12, pages)

    def test_source_recheck_cleanup_is_automatic_but_missing_source_is_not(self) -> None:
        finding = {
            "score": 76,
            "route": "local_source_recheck",
            "source_check_status": "partially_supported",
            "unsupported_claims": ["The exact catalyst loading was not supported."],
        }
        available = {
            "paper_ids": ["P001"],
            "evidence": [{"original_text_available": True}],
        }
        unavailable = {
            "paper_ids": ["P001"],
            "evidence": [{"original_text_available": False}],
        }

        self.assertEqual(
            loop["automatic_rewrite_mode"](finding, available, paragraph_goal=85),
            "source_recheck_cleanup",
        )
        self.assertEqual(
            loop["automatic_rewrite_mode"](finding, unavailable, paragraph_goal=85),
            "",
        )

    def test_source_cleanup_may_delete_only_values_in_unsupported_claims(self) -> None:
        original = "The reaction used CuI at 20 °C and gave 90% yield [1]."
        safe = "The reaction used CuI and gave 90% yield [1]."
        unsafe = "The reaction used PdCl2 and gave 90% yield [1]."

        safe_errors = loop["validate_rewrite"](
            original,
            safe,
            5,
            30,
            allowed_unsupported_claims=["The reported temperature was 20 °C."],
        )
        unsafe_errors = loop["validate_rewrite"](
            original,
            unsafe,
            5,
            30,
            allowed_unsupported_claims=["The reported temperature was 20 °C."],
        )

        self.assertNotIn("protected_numbers_changed", safe_errors)
        self.assertIn("protected_chemical_identities_changed", unsafe_errors)

    def test_rejected_rewrite_gets_one_fact_safe_repair_attempt(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
            rubric = json.loads(
                (
                    ROOT
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "references"
                    / "unified_rubric.json"
                ).read_text(encoding="utf-8")
            )
            calls = 0

            def fake_model(prompt: str, *, label: str) -> dict[str, object]:
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
                    self.assertEqual(label, "Paragraph rewrite sec1-p1")
                    return {"text": "This candidate changes its support [2]."}
                if calls == 3:
                    self.assertEqual(label, "Paragraph rewrite repair sec1-p1")
                    self.assertIn("protected_callouts_changed", prompt)
                    return {
                        "text": "This study provides a clearer bounded comparison with the reported result [1]."
                    }
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
                        max_iterations=1,
                        min_improvement=1.0,
                        min_case_words=5,
                        max_case_words=30,
                        evaluate_only=False,
                    )
                )
            finally:
                feedback_globals["call_json_model"] = original_model

            self.assertEqual(result["status"], "released")
            self.assertEqual(calls, 4)
            self.assertIn("clearer bounded comparison", draft.read_text(encoding="utf-8"))
            overlay = json.loads(
                (project / "04_first_draft" / "feedback_loop_rewrites.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("sec1-p1", overlay["entries"])
            status = json.loads(
                (project / "04_first_draft" / "feedback_loop_status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(status["rewrite_items"][0]["paragraph_id"], "sec1-p1")
            self.assertEqual(status["rewrite_items"][0]["status"], "completed")

    def test_unnumbered_intro_paragraphs_receive_stable_markers(self) -> None:
        markdown = (
            "# Review\n\n## Introduction\n\n"
            "The first synthesis paragraph defines the review scope.\n\n"
            "The second synthesis paragraph states the organizing comparison.\n\n"
            "## References\n\n[1] Source.\n"
        )

        updated, report = ensure_prose_paragraph_markers(markdown)
        manifest = build_paragraph_manifest(updated, "demo")

        self.assertEqual(report["prose_paragraph_count"], 2)
        self.assertEqual(len(report["inserted"]), 2)
        self.assertEqual(manifest["paragraph_count"], 2)
        self.assertIn("<!-- paragraph_id: intro-p1 -->", updated)
        self.assertIn("<!-- paragraph_id: intro-p2 -->", updated)

    def test_normalizer_rejects_an_empty_paragraph_set(self) -> None:
        rubric = json.loads(
            (ROOT / "skills" / "review-first-draft-feedback-loop" / "references" / "unified_rubric.json").read_text(
                encoding="utf-8"
            )
        )
        raw = {
            "dimension_scores": [
                {"id": item["id"], "level": 4, "evidence": "Satisfied."}
                for item in rubric["dimensions"]
            ],
            "paragraph_scores": [],
        }

        with self.assertRaisesRegex(RuntimeError, "no marked prose paragraphs"):
            loop["normalize_evaluation"](
                raw,
                rubric,
                [],
                {"hard_regressions": [], "paragraph_findings": []},
                90,
                85,
            )

    def test_critical_final_polish_finding_cannot_release_gate(self) -> None:
        rubric = json.loads(
            (ROOT / "skills" / "review-first-draft-feedback-loop" / "references" / "unified_rubric.json").read_text(
                encoding="utf-8"
            )
        )
        raw = {
            "dimension_scores": [
                {"id": item["id"], "level": 4, "evidence": "Satisfied."}
                for item in rubric["dimensions"]
            ],
            "paragraph_scores": [
                {
                    "paragraph_id": "sec1-p1",
                    "score": 95,
                    "severity": "critical",
                    "route": "final_polish",
                    "failed_dimensions": ["G07"],
                    "diagnosis": "Protected-fact wording is unresolved.",
                }
            ],
        }
        evaluation = loop["normalize_evaluation"](
            raw,
            rubric,
            [{"paragraph_id": "sec1-p1"}],
            {"hard_regressions": [], "paragraph_findings": []},
            90,
            85,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            gate = loop["queue_artifacts"](Path(temp_dir), evaluation, {"hard_regressions": []})

        self.assertEqual(evaluation["decision"], "REGENERATE_SECTIONS")
        self.assertNotEqual(gate["gate_decision"], "GATE_RELEASE")

    def test_final_rewrite_round_is_rescored_before_release(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
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
                        max_iterations=1,
                        min_improvement=1.0,
                        min_case_words=5,
                        max_case_words=30,
                        evaluate_only=False,
                    )
                )
            finally:
                feedback_globals["call_json_model"] = original_model

            preflight = json.loads(
                (project / "04_first_draft" / "first_draft_preflight.json").read_text(encoding="utf-8")
            )
            evaluation = json.loads(
                (project / "04_first_draft" / "rubric_evaluation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "released")
            self.assertEqual(calls, 3)
            self.assertEqual(preflight["draft_sha256"], hashlib.sha256(draft.read_bytes()).hexdigest())
            self.assertEqual(evaluation["total_score"], 100.0)

    def test_iteration_limit_restores_the_highest_scored_draft(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        first_rewrite = "This study establishes a clearer bounded comparison with the reported result [1]."
        second_rewrite = "This study establishes the clearest bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
            rubric = json.loads(
                (ROOT / "skills" / "review-first-draft-feedback-loop" / "references" / "unified_rubric.json").read_text(
                    encoding="utf-8"
                )
            )
            calls = 0

            def evaluation(level: float, dimension: str = "P03") -> dict[str, object]:
                return {
                    "dimension_scores": [
                        {"id": item["id"], "level": level, "evidence": "Needs work."}
                        for item in rubric["dimensions"]
                    ],
                    "paragraph_scores": [
                        {
                            "paragraph_id": "sec1-p1",
                            "score": 60,
                            "failed_dimensions": [dimension],
                            "severity": "major",
                            "diagnosis": "Improve flow.",
                            "route": "section_rewrite",
                        }
                    ],
                }

            def fake_model(_prompt: str, *, label: str) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return evaluation(3.2)
                if calls == 2:
                    return {"text": first_rewrite}
                if calls == 3:
                    return evaluation(2.8, "G08")
                if calls == 4:
                    return {"text": second_rewrite}
                self.assertEqual(label, "First-draft rubric evaluation")
                return evaluation(2.4)

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
                        max_iterations=2,
                        min_improvement=1.0,
                        min_case_words=5,
                        max_case_words=30,
                        evaluate_only=False,
                    )
                )
            finally:
                feedback_globals["call_json_model"] = original_model

            status = json.loads(
                (project / "04_first_draft" / "feedback_loop_status.json").read_text(encoding="utf-8")
            )
            saved_evaluation = json.loads(
                (project / "04_first_draft" / "rubric_evaluation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["status"], "needs_human_review")
            self.assertTrue(result["best_score_restored"])
            self.assertEqual(result["score"], 80.0)
            self.assertEqual(saved_evaluation["total_score"], 80.0)
            self.assertIn(paragraph, draft.read_text(encoding="utf-8"))
            self.assertTrue(status["best_score_restored"])

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

    def test_quality_workspace_moves_to_draft_and_final_starts_at_preparation(self) -> None:
        draft_page = DRAFT_PAGE.read_text(encoding="utf-8")
        final_page = FINAL_PAGE.read_text(encoding="utf-8")

        self.assertIn('data-tab="quality"', draft_page)
        self.assertIn('data-tab="approval"', draft_page)
        self.assertIn('data-feedback-action="improve"', draft_page)
        self.assertIn('data-feedback-action="evaluate"', draft_page)
        self.assertIn('data-feedback-action="stop"', draft_page)
        self.assertIn('data-issue-toggle=', draft_page)
        self.assertIn('feedback_paragraphs', draft_page)
        self.assertIn('decorateFeedbackIssueImages', draft_page)
        self.assertIn('feedback_rewrite_candidates', draft_page)
        self.assertIn('reviewDraftApproveForHandoff', draft_page)
        self.assertNotIn('data-doc="quality"', final_page)
        self.assertIn('class="tab active" data-doc="preparation"', final_page)
        self.assertIn('data-final-stage="final-conclusion"', final_page)
        self.assertIn('data-final-stage="final-overview-figure"', final_page)
        self.assertIn('data-final-stage="final"', final_page)

    def test_word_range_uses_two_validated_number_inputs(self) -> None:
        page = DRAFT_PAGE.read_text(encoding="utf-8")

        self.assertIn('id="feedbackMinWords" type="number"', page)
        self.assertIn('id="feedbackMaxWords" type="number"', page)
        self.assertNotIn('id="feedbackWordRange"', page)
        self.assertIn("maximum < minimum", page)

    def test_draft_approval_is_hash_bound_and_low_score_requires_override(self) -> None:
        paragraph = "This study establishes a bounded comparison with the reported result [1]."
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project, draft = create_project(root, paragraph)
            digest = hashlib.sha256(draft.read_bytes()).hexdigest()
            write_json(
                draft.parent / "feedback_loop_status.json",
                {
                    "status": "completed",
                    "phase": "evaluated",
                    "score": 88,
                    "goal": 90,
                    "output_draft_sha256": digest,
                },
            )
            write_json(
                draft.parent / "first_draft_gate_status.json",
                {"hard_gate_failures": []},
            )

            with patch.object(
                dashboard,
                "project_draft_payload",
                return_value={"freshness": {"upstream_stale": False}},
            ):
                held = dashboard.approve_current_draft(root, "demo")
                approved = dashboard.approve_current_draft(
                    root,
                    "demo",
                    override_low_score=True,
                    override_reason="human reviewed",
                )

            self.assertTrue(held["requires_override"])
            self.assertTrue(approved["current"])
            draft.write_text(draft.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8")
            self.assertFalse(dashboard.draft_approval_state(project)["current"])
            self.assertFalse(dashboard.draft_quality_state(project)["feedback_loop_current"])


if __name__ == "__main__":
    unittest.main()
