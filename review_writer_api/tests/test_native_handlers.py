from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_writer_api.errors import LiteratureSearchFailed, WorkflowValidationError
from review_writer_api.native_handlers import NativeWorkflowHandlers
from review_writer_api.job_handlers.support import (
    bibliography_needs_bounded_agent as _bibliography_needs_bounded_agent,
    bibliography_source_names as _bibliography_source_names,
    matrix_live_payload as _matrix_live_payload,
    section_generation_timeout_seconds as _section_generation_timeout_seconds,
)
from review_writer_api.scientific_runner import ScientificRunFailed
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.draft_quality import full_draft_quality_provenance
from review_writer_core.paragraph_markers import parse_marked_paragraphs


class _Context:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.job_id = str(uuid.uuid4())
        self.progress_reports: list[tuple[int, int]] = []
        self.partial_results: list[dict] = []

    @staticmethod
    def cancellation_requested() -> bool:
        return False

    def report_progress(self, current: int, total: int):
        self.progress_reports.append((current, total))

    def report_partial_result(self, result: dict):
        self.partial_results.append(result)


class BibliographySourceSelectionTests(unittest.TestCase):
    def test_openalex_requires_server_api_key_for_batch_verification(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(("crossref",), _bibliography_source_names())
        with patch.dict("os.environ", {"OPENALEX_API_KEY": "configured"}, clear=True):
            self.assertEqual(
                ("crossref", "openalex"), _bibliography_source_names()
            )

    def test_bounded_agent_runs_only_for_unresolved_automatic_audits(self) -> None:
        self.assertTrue(
            _bibliography_needs_bounded_agent(
                {
                    "status": "not_found",
                    "automatic_resolution_missing_fields": ["authors"],
                }
            )
        )
        self.assertFalse(
            _bibliography_needs_bounded_agent(
                {
                    "status": "verified",
                    "automatic_resolution_missing_fields": [],
                }
            )
        )
        self.assertTrue(
            _bibliography_needs_bounded_agent(
                {
                    "status": "not_found",
                    "manual_review_status": "resolved",
                    "resolved_by": "automatic",
                    "automatic_resolution_missing_fields": ["authors"],
                }
            )
        )
        self.assertFalse(
            _bibliography_needs_bounded_agent(
                {
                    "status": "not_found",
                    "manual_review_status": "resolved",
                    "resolved_by": "human",
                    "automatic_resolution_missing_fields": ["authors"],
                }
            )
        )


class SectionGenerationTimeoutTests(unittest.TestCase):
    def test_full_chapter_run_scales_beyond_fixed_fifteen_minutes(self) -> None:
        tasks = [{"section_id": f"S{index:02d}"} for index in range(1, 10)]

        self.assertEqual(59 * 60, _section_generation_timeout_seconds(tasks))

    def test_resume_budget_counts_only_unfinished_chapters(self) -> None:
        tasks = [{"section_id": f"S{index:02d}"} for index in range(1, 10)]
        checkpoint = {
            "entries": {
                f"S{index:02d}": {"output": {}}
                for index in range(1, 6)
            }
        }

        self.assertEqual(
            29 * 60,
            _section_generation_timeout_seconds(tasks, checkpoint),
        )

    def test_empty_or_completed_run_keeps_a_bounded_minimum(self) -> None:
        tasks = [{"section_id": "S01"}]
        checkpoint = {"entries": {"S01": {"output": {}}}}

        self.assertEqual(
            15 * 60,
            _section_generation_timeout_seconds(tasks, checkpoint),
        )


class _RecordingRunner:
    def __init__(self):
        self.command: tuple[str, ...] = ()
        self.kwargs: dict = {}

    def run(self, command, **kwargs):
        self.command = tuple(command)
        self.kwargs = dict(kwargs)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "added_count": 0,
                    "already_present_count": 0,
                    "failed_count": 0,
                    "results": [],
                }
            ),
            encoding="utf-8",
        )


class _WorkflowRunner:
    def __init__(self):
        self.commands: list[tuple[str, ...]] = []
        self.run_options: list[dict] = []

    def run(self, command, **kwargs):
        command = tuple(str(value) for value in command)
        self.commands.append(command)
        self.run_options.append(dict(kwargs))
        script = Path(command[1]).name
        if script == "run_md2docx.py":
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"PK\x03\x04docx")
            return
        review_root = Path(command[command.index("--review-root") + 1])
        project_id = command[command.index("--project-id") + 1]
        project = review_root / "review-projects" / project_id
        first = project / "04_first_draft"
        first.mkdir(parents=True, exist_ok=True)
        if script == "feedback_loop.py":
            evaluation = {
                "total_score": 81.5,
                "pass_threshold": 90,
                "decision": "REVISE",
                "dimension_scores": [{"id": "evidence", "score": 81.5}],
                "paragraph_scores": [
                    {
                        "paragraph_id": "p1",
                        "score": 70,
                        "severity": "major",
                        "route": "section_rewrite",
                    }
                ],
                "paragraph_failures": [
                    {
                        "paragraph_id": "p1",
                        "score": 70,
                        "severity": "major",
                        "route": "section_rewrite",
                        "diagnosis": "Add a direct comparison.",
                    }
                ],
                "hard_gate_failures": [],
            }
            (first / "rubric_evaluation.json").write_text(
                json.dumps(evaluation), encoding="utf-8"
            )
            (first / "reviewer_findings.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "PAR-001",
                            "paragraph_id": "p1",
                            "severity": "major",
                            "diagnosis": "Add a direct comparison.",
                            "route": "section_rewrite",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (first / "first_draft_gate_status.json").write_text(
                json.dumps({"hard_gate_failures": []}), encoding="utf-8"
            )
            (first / "first_draft_preflight.json").write_text(
                json.dumps({"paragraph_checks": []}), encoding="utf-8"
            )
            (first / "original_source_check.json").write_text(
                json.dumps({"entries": []}), encoding="utf-8"
            )
            if "--evaluate-only" not in command:
                draft_path = first / "first_draft.md"
                draft_path.write_text(
                    draft_path.read_text(encoding="utf-8").replace(
                        "Evidence paragraph.", "Batch optimized evidence paragraph [1].", 1
                    ),
                    encoding="utf-8",
                )
                (first / "feedback_loop_rewrites.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "entries": {
                                "p1": {
                                    "paragraph_id": "p1",
                                    "source_text_sha256": "source-hash",
                                    "rewritten_text": "Batch optimized evidence paragraph [1].",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            (first / "feedback_loop_status.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "phase": "evaluated" if "--evaluate-only" in command else "released",
                        "iteration": 1,
                        "max_iterations": int(command[command.index("--max-iterations") + 1]),
                        "rewrite_accepted": 0 if "--evaluate-only" in command else 1,
                    }
                ),
                encoding="utf-8",
            )
            callback = kwargs.get("progress_callback")
            if callback:
                callback()
            return
        if script == "propose_paragraph_rewrite.py":
            (first / "feedback_rewrite_candidates.json").write_text(
                json.dumps(
                    {
                        "entries": {
                            "p1": {
                                "paragraph_id": "p1",
                                "original_text": "Evidence paragraph.",
                                "candidate_text": "Improved evidence comparison [1].",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            return
        if script == "evaluate_paragraph_candidate.py":
            request = json.loads(
                (first / "paragraph_candidate_evaluation_request.json").read_text(
                    encoding="utf-8"
                )
            )
            current_paragraph = (
                request.get("evaluation_mode") == "current_paragraph"
            )
            (first / "paragraph_candidate_evaluation.json").write_text(
                json.dumps(
                    {
                        "evaluation_scope": "single_paragraph",
                        "evaluation_mode": request.get("evaluation_mode")
                        or "accepted_candidate",
                        "paragraph_id": request["paragraph_id"],
                        "paragraph_score": {
                            "paragraph_id": request["paragraph_id"],
                            "score": 70 if current_paragraph else 92,
                            "severity": "major" if current_paragraph else "none",
                            "route": "section_rewrite" if current_paragraph else "pass",
                        },
                        "local_dimension_scores": [],
                        "local_hard_gate_failures": [],
                        "local_preflight": {
                            "paragraph_checks": [],
                            "paragraph_findings": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            return
        if script == "generate_conclusion1.py":
            (first / "conclusion_generated.md").write_text(
                "## Conclusion\n\nA bounded conclusion.\n", encoding="utf-8"
            )
            (first / "conclusion_quality_report.json").write_text(
                json.dumps({"validation": {"passes_validation": True}}),
                encoding="utf-8",
            )
            return
        if script == "generate_overview_figure.py":
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"test-image")
            (project / "03_figure_redraw" / "overview_template_match.json").write_text(
                json.dumps(
                    {
                        "template": {"name": "Mechanism overview"},
                        "features": {
                            "metal_categories": ["Cu", "Fe"],
                            "overview_content_contract": {
                                "primary_axis": "substrate",
                                "modules": ["Alcohol substrates", "Terminal alkynes"],
                                "evidence_bindings": {
                                    "Alcohol substrates": {"section_id": "S02"}
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            return
        raise AssertionError(f"Unexpected command: {command}")


class _FigureRedrawRunner:
    def __init__(self):
        self.command: tuple[str, ...] = ()

    def run(self, command, **_kwargs):
        self.command = tuple(str(value) for value in command)
        review_root = Path(self.command[self.command.index("--review-root") + 1])
        project_id = self.command[self.command.index("--project-id") + 1]
        figure_id = self.command[self.command.index("--figure-id") + 1]
        redraw = review_root / "review-projects" / project_id / "03_figure_redraw"
        output = redraw / "redrawn" / f"{figure_id}.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated-image")
        (redraw / "redrawn_figure_manifest.json").write_text(
            json.dumps(
                {
                    "figures": [
                        {
                            "figure_id": figure_id,
                            "status": "redrawn",
                            "render_mode": "ai-edit",
                            "redrawn_image": str(output),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )


class NativeWorkflowHandlerTests(unittest.TestCase):
    def test_workspace_projects_only_a_verified_baseline_for_image_paths(self):
        import hashlib
        from copy import deepcopy
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                handlers = NativeWorkflowHandlers(None, HostedWorkspaceManager(Path(temporary) / "users"), None)
                aid = str(uuid.uuid4())
                original = f"# Review\n\nEvidence paragraph.\n\n<!-- paragraph_id: p1 -->\n\n![Figure](/api/v1/artifacts/{aid}/content)\n"
                quality = full_draft_quality_provenance(original, parse_marked_paragraphs(original))
                payload = {"project_id": "project-1", "draft_text": original,
                           "baseline_quality": quality, "figure_artifact_paths": {aid: "/app/figures/source.png"}}
                if changed:
                    payload["draft_text"] = original.replace("Evidence paragraph.", "A changed scientific result.")
                before = deepcopy(payload)
                _, _, project = handlers._compatibility_workspace(_Context(str(uuid.uuid4())), payload, name="draft-workspace")
                first = project / "04_first_draft"
                baseline = json.loads((first / "baseline_quality.json").read_text())
                digest = hashlib.sha256((first / "first_draft.md").read_bytes()).hexdigest()
                self.assertEqual(before, payload)
                if changed:
                    self.assertNotEqual(digest, baseline["evaluation_input_sha256"])
                else:
                    self.assertEqual(digest, baseline["evaluation_input_sha256"])
                    self.assertEqual(quality["evaluation_input_sha256"], baseline["source_evaluation_input_sha256"])

    def test_paper_source_environment_is_forwarded_without_exposing_keys(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "CROSSREF_MAILTO": "researcher@example.test",
                "REVIEW_DISCOVERY_SOURCES": "crossref,openalex",
                "REVIEW_DISCOVERY_MULTI_SOURCE_ENABLED": "true",
                "OPENALEX_API_KEY": "openalex-secret",
                "SEMANTIC_SCHOLAR_API_KEY": "semantic-secret",
            },
            clear=True,
        ):
            normal, secrets = NativeWorkflowHandlers._paper_source_environment()

        self.assertEqual("researcher@example.test", normal["CROSSREF_MAILTO"])
        self.assertEqual("crossref,openalex", normal["REVIEW_DISCOVERY_SOURCES"])
        self.assertEqual("true", normal["REVIEW_DISCOVERY_MULTI_SOURCE_ENABLED"])
        self.assertNotIn("OPENALEX_API_KEY", normal)
        self.assertEqual("openalex-secret", secrets["OPENALEX_API_KEY"])
        self.assertEqual("semantic-secret", secrets["SEMANTIC_SCHOLAR_API_KEY"])

    def test_text_environment_contains_only_gateway_task_credentials(self) -> None:
        class ProviderSettings:
            @staticmethod
            def runtime_environment(_principal):
                return {
                    "OPENAI_API_KEY": "text-secret",
                    "OPENAI_BASE_URL": "https://provider.example/v1",
                    "REVIEW_WRITING_MODEL": "provider-model",
                    "IMAGE_OPENAI_API_KEY": "image-secret",
                    "MINERU_API_TOKEN": "mineru-secret",
                }

        class Gateway:
            @staticmethod
            def environment_for_job(_context):
                return (
                    {"REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1:8770/internal"},
                    {"REVIEW_WRITER_TASK_TOKEN": "task-token"},
                )

        with tempfile.TemporaryDirectory() as temporary:
            handlers = NativeWorkflowHandlers(
                _WorkflowRunner(),
                HostedWorkspaceManager(Path(temporary) / "users"),
                ProviderSettings(),
                Gateway(),
            )
            normal, secrets = handlers._text_gateway_environment(_Context(str(uuid.uuid4())))

        self.assertEqual(
            {"REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1:8770/internal"},
            normal,
        )
        self.assertEqual({"REVIEW_WRITER_TASK_TOKEN": "task-token"}, secrets)

    def test_image_environment_removes_direct_provider_credentials(self) -> None:
        class ProviderSettings:
            @staticmethod
            def runtime_environment(_principal):
                return {
                    "IMAGE_OPENAI_API_KEY": "image-secret",
                    "IMAGE_OPENAI_BASE_URL": "https://provider.example/v1",
                    "IMAGE_OPENAI_MODEL": "provider-image-model",
                }

        class Gateway:
            @staticmethod
            def environment_for_job(_context):
                return (
                    {
                        "REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1:8770/text",
                        "REVIEW_WRITER_IMAGE_GATEWAY_URL": "http://127.0.0.1:8770/image",
                    },
                    {"REVIEW_WRITER_TASK_TOKEN": "task-token"},
                )

        with tempfile.TemporaryDirectory() as temporary:
            handlers = NativeWorkflowHandlers(
                _WorkflowRunner(),
                HostedWorkspaceManager(Path(temporary) / "users"),
                ProviderSettings(),
                Gateway(),
            )
            normal, secrets = handlers._image_gateway_environment(
                _Context(str(uuid.uuid4()))
            )

        self.assertNotIn("IMAGE_OPENAI_API_KEY", secrets)
        self.assertNotIn("IMAGE_OPENAI_BASE_URL", normal)
        self.assertEqual("http://127.0.0.1:8770/image", normal["REVIEW_WRITER_IMAGE_GATEWAY_URL"])
        self.assertEqual({"REVIEW_WRITER_TASK_TOKEN": "task-token"}, secrets)

    def test_figure_redraw_always_invokes_the_ai_edit_route(self) -> None:
        class ImageProviderSettings:
            @staticmethod
            def runtime_environment(_principal):
                return {
                    "IMAGE_OPENAI_MODEL": "configured-image-model",
                    "IMAGE_OPENAI_API_KEY": "secret",
                }

        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _FigureRedrawRunner()
            handlers = NativeWorkflowHandlers(
                runner, workspaces, ImageProviderSettings()
            )
            context = _Context(str(uuid.uuid4()))
            source = workspaces.user_root(context.user_id) / "library" / "source.png"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"source-image")

            result = handlers.figures_redraw(
                context,
                {
                    "project_id": "project-1",
                    "figure": {"figure_id": "P001-F01"},
                    "source_path": str(source),
                    "figure_type": "simple-scheme",
                },
            )

            render_mode_index = runner.command.index("--render-mode")
            model_index = runner.command.index("--model")
            self.assertEqual("ai-edit", runner.command[render_mode_index + 1])
            self.assertEqual("configured-image-model", runner.command[model_index + 1])
            self.assertIn("--force-standard-ai-edit", runner.command)
            self.assertNotIn("source-faithful-bw", runner.command)
            self.assertEqual("ai-edit", result["render_mode"])

    def test_library_search_does_not_depend_on_configured_model_providers(self) -> None:
        class SearchRunner:
            def run(self, command, **_kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps({"candidates": [], "candidate_count": 0}),
                    encoding="utf-8",
                )

        class BrokenProviderSettings:
            def runtime_environment(self, _principal):
                raise RuntimeError("unrelated model provider is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            handlers = NativeWorkflowHandlers(
                SearchRunner(), workspaces, BrokenProviderSettings()
            )
            context = _Context(str(uuid.uuid4()))

            result = handlers.library_search(
                context, {"topic": "axially chiral allenes", "limit": 20}
            )

            self.assertEqual([], result["candidates"])

    def test_library_search_turns_crossref_timeout_into_actionable_error(self) -> None:
        class TimeoutRunner:
            def run(self, _command, **_kwargs):
                raise ScientificRunFailed(
                    "Scientific task failed.",
                    attempts=3,
                    retryable=True,
                    details={
                        "category": "transient_timeout",
                        "stderr": "urllib.error.URLError: <urlopen error [WinError 10060] timed out>",
                    },
                )

        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            handlers = NativeWorkflowHandlers(TimeoutRunner(), workspaces, None)
            context = _Context(str(uuid.uuid4()))

            with self.assertRaises(LiteratureSearchFailed) as raised:
                handlers.library_search(context, {"topic": "axially chiral allenes", "limit": 20})

            self.assertIn("Crossref did not respond before timeout", str(raised.exception))
            self.assertEqual(3, raised.exception.details["attempts"])

    def test_library_search_reports_blocked_transparent_proxy_before_timeout_words(self) -> None:
        class BlockedProxyRunner:
            def run(self, _command, **_kwargs):
                raise ScientificRunFailed(
                    "Scientific task failed.",
                    attempts=1,
                    retryable=False,
                    details={
                        "category": "network_policy",
                        "stderr": (
                            "instrumented(target, timeout=25)\n"
                            "urllib.error.URLError: <urlopen error Provider connection "
                            "to a private destination is blocked.>"
                        ),
                    },
                )

        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            handlers = NativeWorkflowHandlers(BlockedProxyRunner(), workspaces, None)
            context = _Context(str(uuid.uuid4()))

            with self.assertRaises(LiteratureSearchFailed) as raised:
                handlers.library_search(
                    context, {"topic": "axially chiral allenes", "limit": 20}
                )

            self.assertIn("transparent-proxy", str(raised.exception))
            self.assertNotIn("before timeout", str(raised.exception))

    def test_download_scientific_process_writes_only_to_job_isolated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _RecordingRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))

            result = handlers.library_download(
                context,
                {"candidates": [{"candidate_id": "crossref:1"}]},
            )

            task_root = Path(
                runner.command[runner.command.index("--review-root") + 1]
            )
            user_root = workspaces.user_root(context.user_id)
            self.assertEqual(
                user_root
                / ".review-writer"
                / "job-staging"
                / context.job_id
                / "library-workspace",
                task_root,
            )
            self.assertNotEqual(user_root, task_root)
            self.assertEqual([], result["results"])

    def test_library_download_does_not_depend_on_model_provider_settings(self) -> None:
        class BrokenProviderSettings:
            def runtime_environment(self, _principal):
                raise RuntimeError("unrelated model provider is unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _RecordingRunner()
            handlers = NativeWorkflowHandlers(
                runner, workspaces, BrokenProviderSettings()
            )
            context = _Context(str(uuid.uuid4()))

            result = handlers.library_download(
                context,
                {"candidates": [{"candidate_id": "crossref:1"}], "email": ""},
            )

            self.assertEqual([], result["results"])
            self.assertEqual({}, runner.kwargs["env"])
            self.assertEqual({}, runner.kwargs["secret_env"])

    def test_draft_rewrite_rejects_a_missing_current_draft_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _WorkflowRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))

            with self.assertRaises(WorkflowValidationError) as failed:
                handlers.draft_rewrite(
                    context,
                    {
                        "project_id": "project-1",
                        "revision_mode": "dialogue",
                        "paragraph_id": "p1",
                        "paragraph_text": "Evidence paragraph.",
                    },
                )

            self.assertIn("current Draft content", str(failed.exception))
            self.assertEqual([], runner.commands)

    def test_dialogue_uses_one_revision_script_without_score_arguments(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "draft-workspace" / "review-projects" / "project-1"
            first = project / "04_first_draft"
            first.mkdir(parents=True)
            handlers = NativeWorkflowHandlers(_WorkflowRunner(), HostedWorkspaceManager(root / "users"), None)
            context = _Context(str(uuid.uuid4()))
            def run(command, **kwargs):
                self.assertEqual("revise_paragraph.py", Path(command[1]).name)
                self.assertNotIn("--goal", command)
                self.assertEqual(1, kwargs["max_attempts"])
                (first / "paragraph_revision_result.json").write_text(
                    json.dumps({"reply": "Clarified", "candidate_text": "Revised text."}), encoding="utf-8")
            with patch.object(handlers, "_compatibility_workspace", return_value=(root, root, project)), \
                 patch.object(handlers.runner, "run", side_effect=run):
                result = handlers.draft_rewrite(context, {"revision_mode": "dialogue",
                    "draft_text": "Saved text.", "dialogue": {"message": "Clarify"}})
            self.assertEqual("Revised text.", result["candidate_text"])
            self.assertNotIn("draft.evaluate", handlers.mapping())
            self.assertNotIn("draft.accept-rewrite", handlers.mapping())
            self.assertNotIn("draft.optimize", handlers.mapping())

    def test_section_progress_callback_publishes_each_completed_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status_file = Path(temporary) / "generation_progress.json"
            status_file.write_text(
                json.dumps(
                    {
                        "phase": "generating",
                        "current": 1,
                        "total": 10,
                        "current_heading": "Catalyst classes",
                        "completed_sections": [
                            {"section_id": "S01", "heading": "Introduction"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            context = _Context(str(uuid.uuid4()))
            callback = NativeWorkflowHandlers._section_progress_callback(
                context, status_file
            )

            callback()
            callback()

            self.assertEqual([(1, 10)], context.progress_reports)
            self.assertEqual(
                "Introduction",
                context.partial_results[0]["section_progress"]["completed_sections"][0]["heading"],
            )

    def test_section_progress_persists_repair_budget_without_progress_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status_file = Path(temporary) / "progress.json"
            checkpoint_file = Path(temporary) / "checkpoint.json"
            status_file.write_text(json.dumps({"phase": "reviewing", "current": 0, "total": 1}), encoding="utf-8")
            checkpoint_file.write_text(json.dumps({"entries": {}}), encoding="utf-8")
            context = _Context(str(uuid.uuid4()))
            callback = NativeWorkflowHandlers._section_progress_callback(context, status_file, checkpoint_file)
            callback()
            checkpoint_file.write_text(json.dumps({"entries": {}, "authoring_states": {
                "S01": {"state": {"repair_attempted": True}}}}), encoding="utf-8")
            callback()
            callback()
            self.assertEqual(2, len(context.partial_results))
            self.assertTrue(context.partial_results[-1]["section_checkpoint"]["authoring_states"]["S01"]["state"]["repair_attempted"])

    def test_matrix_progress_callback_publishes_live_fact_previews(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status_file = Path(temporary) / "matrix-progress.json"
            checkpoint_file = Path(temporary) / "matrix-checkpoint.json"
            status_file.write_text(
                json.dumps(
                    {
                        "phase": "extracting",
                        "current": 1,
                        "total": 2,
                        "current_paper_id": "P002",
                        "completed_papers": ["P001"],
                    }
                ),
                encoding="utf-8",
            )
            checkpoint_file.write_text(
                json.dumps(
                    {
                        "entries": {
                            "P001": {
                                "result": {
                                    "status": "complete",
                                    "facts": [
                                        {
                                            "fact_id": "F1",
                                            "field_id": "reaction_type",
                                            "value": "A phosphine-catalyzed cycloaddition was reported.",
                                            "support_level": "direct",
                                        }
                                    ],
                                    "evidence_backed_tags": {
                                        "reaction_type": [{"partition_id": "cycloaddition"}]
                                    },
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            context = _Context(str(uuid.uuid4()))
            callback = NativeWorkflowHandlers._matrix_progress_callback(
                context, status_file, checkpoint_file
            )

            callback()
            callback()

            self.assertEqual([(1, 2)], context.progress_reports)
            live = context.partial_results[0]["matrix_enrichment_live"]
            self.assertEqual("P002", live["current_paper_id"])
            self.assertEqual(1, live["items"][0]["fact_count"])
            self.assertEqual(
                "A phosphine-catalyzed cycloaddition was reported.",
                live["items"][0]["facts_preview"][0]["value"],
            )
            self.assertIn(
                "matrix_enrichment_checkpoint", context.partial_results[0]
            )

    def test_matrix_resume_replays_independent_questions_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = NativeWorkflowHandlers.__new__(NativeWorkflowHandlers)
            handler.root = root
            handler._staging = lambda *_: root
            handler._text_gateway_environment = lambda _: ({}, {})
            calls = []
            questions = [{"field_id": "scope", "query": "cohort"}, {"field_id": "method_conditions", "query": "annealing"}]
            def retrieve(principal, project, payload, request):
                question = request["questions"][0]
                calls.append(question)
                if question == questions[0]:
                    raise TimeoutError("temporary index outage")
                return []
            handler.planning_service = SimpleNamespace(retrieve_matrix_fact_evidence=retrieve)
            handler.runner = SimpleNamespace(run=lambda *a, **k: (root / "matrix-enrichment-output.json").write_text('{"papers": []}', encoding="utf-8"))
            context = SimpleNamespace(user_id=str(uuid.uuid4()), project_id="project", job_id=str(uuid.uuid4()),
                checkpoint=lambda: None, cancellation_requested=lambda: False)
            checkpoint = {"entries": {"paper": {"source_fingerprint": "current", "agent_state": {
                "retrieval_requests": [{"paper_id": "paper", "questions": questions}]}}}}
            payload = {"papers": [{"paper_id": "paper", "source_fingerprint": "current"}], "resume_checkpoint": checkpoint}
            result = handler.matrix_enrich(context, payload)
            self.assertEqual(questions, calls)
            self.assertEqual([questions[0]], result["matrix_enrichment_checkpoint"]["entries"]["paper"]["agent_state"]["pending_requests"])
            handler.planning_service.retrieve_matrix_fact_evidence = lambda *a: (_ for _ in ()).throw(WorkflowValidationError("Source changed"))
            with self.assertRaises(WorkflowValidationError):
                handler.matrix_enrich(context, payload)

    def test_matrix_concurrency_respects_gateway_slots_and_existing_agent_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = NativeWorkflowHandlers.__new__(NativeWorkflowHandlers)
            handler.root = root
            handler._staging = lambda *_: root
            handler._text_gateway_environment = lambda _: ({}, {})
            handler.runner = SimpleNamespace(run=lambda *a, **k: (root / "matrix-enrichment-output.json").write_text(
                '{"papers": []}', encoding="utf-8"))
            context = SimpleNamespace(user_id=str(uuid.uuid4()), project_id="project", job_id=str(uuid.uuid4()),
                                      checkpoint=lambda: None, cancellation_requested=lambda: False)
            for global_slots, user_slots, requested, expected in [(4, 3, 8, 3), (4, 1, 3, 1), (2, 3, 3, 2), (4, 3, 1, 1)]:
                with self.subTest(global_slots=global_slots, user_slots=user_slots, requested=requested):
                    # Split workers obtain these two settings through Compose.
                    with patch.dict("os.environ", {"REVIEW_WRITER_MODEL_GATEWAY_CONCURRENCY": str(global_slots),
                                                   "REVIEW_WRITER_MODEL_GATEWAY_USER_CONCURRENCY": str(user_slots)}):
                        handler.matrix_enrich(context, {"papers": [], "fact_agent_limits": {
                            "paper_concurrency": requested, "max_model_calls": 5, "max_supplement_rounds": 1}})
                    saved = json.loads((root / "matrix-enrichment-input.json").read_text(encoding="utf-8"))
                    self.assertEqual({"paper_concurrency": expected, "max_model_calls": 5, "max_supplement_rounds": 1},
                                     saved["fact_agent_limits"])

    def test_model_delegation_is_scoped_to_standalone_matrix_not_embedded_blueprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = NativeWorkflowHandlers.__new__(NativeWorkflowHandlers)
            handler.root = root
            handler.model_gateway = object()
            handler.planning_service = None
            handler._staging = lambda *_: root
            handler._text_gateway_environment = lambda _: ({"REVIEW_WRITER_MODEL_GATEWAY_URL": "internal"}, {})
            observed = []
            def run(*_args, **kwargs):
                observed.append(dict(kwargs["env"]))
                (root / "matrix-enrichment-output.json").write_text('{"papers": []}', encoding="utf-8")
            handler.runner = SimpleNamespace(run=run)
            context = SimpleNamespace(user_id=str(uuid.uuid4()), project_id="project", job_id=str(uuid.uuid4()),
                job_type="matrix.enrich", checkpoint=lambda: None, cancellation_requested=lambda: False)
            with patch.dict("os.environ", {"REVIEW_WRITER_DELEGATED_MODEL_ENABLED": "1"}):
                handler.matrix_enrich(context, {"papers": []})
                context.job_type = "planning.blueprint"
                handler.matrix_enrich(context, {"papers": []})
            self.assertEqual("1", observed[0]["REVIEW_WRITER_DELEGATE_MODEL_CALLS"])
            self.assertNotIn("REVIEW_WRITER_DELEGATE_MODEL_CALLS", observed[1])

    def test_matrix_live_payload_bounds_long_fact_preview(self) -> None:
        live = _matrix_live_payload(
            {
                "phase": "targeted_recheck",
                "current": 0,
                "total": 1,
                "current_paper_id": "P001",
                "completed_papers": ["P001"],
            },
            {
                "entries": {
                    "P001": {
                        "result": {
                            "status": "complete",
                            "facts": [{"field_id": "scope", "value": "x" * 500}],
                        }
                    }
                }
            },
        )

        self.assertLessEqual(len(live["items"][0]["facts_preview"][0]["value"]), 261)

    def test_overview_retry_preserves_only_user_scoped_step_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            handlers = NativeWorkflowHandlers(_WorkflowRunner(), workspaces, None)
            first = _Context(str(uuid.uuid4()))
            common = {"project_id": "project-1", "draft_text": "# Review\n\nBody.",
                      "matrix": {"rows": []}, "section_index": {"sections": []},
                      "figure_manifest": {"figures": []}, "figure_artifact_paths": {}, "library_metadata": {}}
            cache = {"entries": {"fixture": {"product_smiles": "CCO"}}}
            handlers.final_overview(first, {**common, "overview_generation_cache": cache})
            retry = _Context(first.user_id)
            retry.retry_of_job_id = first.job_id
            handlers.final_overview(retry, common)
            path = (handlers._staging(retry.user_id, retry.job_id) / "final-overview-workspace"
                    / "review-projects/project-1/03_figure_redraw/overview_generation_cache.json")
            self.assertEqual(cache, json.loads(path.read_text(encoding="utf-8")))
            other = _Context(str(uuid.uuid4()))
            other.retry_of_job_id = first.job_id
            handlers.final_overview(other, common)
            other_path = (handlers._staging(other.user_id, other.job_id) / "final-overview-workspace"
                          / "review-projects/project-1/03_figure_redraw/overview_generation_cache.json")
            self.assertFalse(other_path.exists())

    def test_final_handlers_are_registered_and_return_publishable_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _WorkflowRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))
            common = {
                "project_id": "project-1",
                "draft_text": "# Review\n\nBody.\n\n<!-- paragraph_id: p1 -->\n",
                "matrix": {"rows": []},
                "section_index": {"sections": []},
                "figure_manifest": {"figures": []},
                "figure_artifact_paths": {},
                "library_metadata": {},
            }

            conclusion = handlers.final_conclusion(context, common)
            overview = handlers.final_overview(context, common)
            exported = handlers.final_export(
                context, {**common, "final_markdown": common["draft_text"]}
            )

            self.assertIn("Conclusion", conclusion["markdown"])
            self.assertTrue(Path(overview["output_path"]).is_file())
            self.assertEqual(
                "Project-1",
                overview["editable_text"]["title"],
            )
            self.assertNotIn(
                "Mechanism overview", overview["editable_text"]["subtitle"]
            )
            self.assertEqual(
                ["Alcohol substrates", "Terminal alkynes"],
                overview["editable_text"]["labels"],
            )
            self.assertEqual("substrate", overview["editable_text"]["primary_axis"])
            self.assertTrue(Path(exported["output_path"]).is_file())
            expected = {
                "draft.rewrite",
                "final.conclusion",
                "final.overview",
                "final.export",
            }
            self.assertTrue(expected.issubset(handlers.mapping()))


if __name__ == "__main__":
    unittest.main()
