from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from review_writer_api.errors import LiteratureSearchFailed, WorkflowValidationError
from review_writer_api.native_handlers import NativeWorkflowHandlers
from review_writer_api.scientific_runner import ScientificRunFailed
from review_writer_api.workspaces import HostedWorkspaceManager


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
                        "features": {"metal_categories": ["Cu", "Fe"]},
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
                        "paragraph_id": "p1",
                        "paragraph_text": "Evidence paragraph.",
                    },
                )

            self.assertIn("current Draft content", str(failed.exception))
            self.assertEqual([], runner.commands)

    def test_style_only_candidate_cannot_clear_manual_confirmation_route(self) -> None:
        evaluation = NativeWorkflowHandlers._retain_manual_confirmation_route(
            {
                "evaluation_scope": "single_paragraph",
                "paragraph_id": "p1",
                "paragraph_score": {
                    "paragraph_id": "p1",
                    "score": 94,
                    "severity": "none",
                    "route": "pass",
                    "failed_dimensions": [],
                },
            },
            {
                "requires_manual_confirmation": True,
                "diagnosis": "The displayed figure identity needs source confirmation.",
            },
        )

        self.assertEqual(79.0, evaluation["paragraph_score"]["score"])
        self.assertEqual("major", evaluation["paragraph_score"]["severity"])
        self.assertEqual(
            "human_confirmation", evaluation["paragraph_score"]["route"]
        )
        self.assertIn(
            "manual_source_confirmation",
            evaluation["paragraph_score"]["failed_dimensions"],
        )
        self.assertTrue(evaluation["requires_manual_confirmation"])

    def test_draft_rewrite_scores_only_the_selected_paragraph_before_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _WorkflowRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))

            result = handlers.draft_rewrite(
                context,
                {
                    "project_id": "project-1",
                    "draft_text": "Evidence paragraph.\n\n<!-- paragraph_id: p1 -->\n",
                    "paragraph_id": "p1",
                    "paragraph_text": "Evidence paragraph.",
                    "quality": {"score": 70, "goal": 90},
                    "issues": [
                        {
                            "issue_id": "PAR-001",
                            "paragraph_id": "p1",
                            "score": 70,
                            "route": "section_rewrite",
                        }
                    ],
                    "matrix": {"rows": []},
                    "section_index": {"sections": []},
                    "figure_manifest": {"figures": []},
                    "figure_artifact_paths": {},
                    "library_metadata": {},
                },
            )

            scripts = [Path(command[1]).name for command in runner.commands]
            self.assertEqual(
                [
                    "propose_paragraph_rewrite.py",
                    "evaluate_paragraph_candidate.py",
                ],
                scripts,
            )
            self.assertEqual(
                "stored_source_score",
                result["source_paragraph_evaluation"]["evaluation_mode"],
            )
            self.assertEqual(
                "accepted_candidate",
                result["candidate_evaluation"]["evaluation_mode"],
            )
            self.assertEqual("Improved evidence comparison [1].", result["candidate_text"])

    def test_draft_handlers_build_isolated_workspace_and_normalize_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspaces = HostedWorkspaceManager(Path(temporary) / "users")
            runner = _WorkflowRunner()
            handlers = NativeWorkflowHandlers(runner, workspaces, None)
            context = _Context(str(uuid.uuid4()))
            source = workspaces.user_root(context.user_id) / "paper.md"
            source.write_text("Original paper evidence.", encoding="utf-8")
            common = {
                "project_id": "project-1",
                "draft_text": "Evidence paragraph.\n\n<!-- paragraph_id: p1 -->\n",
                "matrix": {"rows": [{"paper_id": "P001"}]},
                "section_index": {
                    "sections": [
                        {
                            "section_id": "s1",
                            "paragraphs": [
                                {
                                    "paragraph_id": "p1",
                                    "text": "Evidence paragraph.",
                                    "cited_paper_ids": ["P001"],
                                }
                            ],
                        }
                    ]
                },
                "figure_manifest": {"figures": []},
                "figure_artifact_paths": {},
                "library_metadata": {
                    "P001": {"source_paths": {"markdown": str(source)}}
                },
            }

            evaluated = handlers.draft_evaluate(
                context, {**common, "goal": 90, "paragraphs": []}
            )
            optimized = handlers.draft_optimize(
                context,
                {
                    **common,
                    "goal": 92,
                    "paragraph_goal": 86,
                    "max_iterations": 4,
                    "min_case_words": 120,
                    "max_case_words": 260,
                },
            )
            rewritten = handlers.draft_rewrite(
                context,
                {
                    **common,
                    "paragraph_id": "p1",
                    "paragraph_text": "Evidence paragraph.",
                    "quality": {
                        **evaluated,
                        "issues": [
                            *evaluated["issues"],
                            {
                                "issue_id": "PAR-999",
                                "paragraph_id": "p2",
                                "message": "Unrelated paragraph issue.",
                            },
                        ],
                    },
                    "issues": evaluated["issues"],
                },
            )
            accepted_evaluation = handlers.draft_accept_rewrite(
                context,
                {
                    **common,
                    "paragraph_id": "p1",
                    "paragraph_text": "Evidence paragraph.",
                    "candidate_text": "Improved evidence comparison [1].",
                    "candidate_draft_text": common["draft_text"].replace(
                        "Evidence paragraph.",
                        "Improved evidence comparison [1].",
                    ),
                    "goal": 90,
                    "paragraph_goal": 85,
                    "min_case_words": 1,
                    "max_case_words": 280,
                    "word_range_applicable": False,
                },
            )

            self.assertEqual(81.5, evaluated["score"])
            self.assertEqual("PAR-001", evaluated["issues"][0]["issue_id"])
            self.assertEqual(70, evaluated["issues"][0]["score"])
            self.assertIn("Batch optimized evidence", optimized["draft_text"])
            self.assertEqual(1, optimized["feedback_status"]["rewrite_accepted"])
            optimize_command = next(
                command
                for command in runner.commands
                if Path(command[1]).name == "feedback_loop.py"
                and "--evaluate-only" not in command
            )
            self.assertEqual("92.0", optimize_command[optimize_command.index("--goal") + 1])
            self.assertEqual("86.0", optimize_command[optimize_command.index("--paragraph-goal") + 1])
            self.assertEqual("4", optimize_command[optimize_command.index("--max-iterations") + 1])
            optimize_index = runner.commands.index(optimize_command)
            self.assertGreaterEqual(
                runner.run_options[optimize_index]["timeout_seconds"],
                3 * 60 * 60,
            )
            self.assertEqual("Improved evidence comparison [1].", rewritten["candidate_text"])
            self.assertEqual(["PAR-001"], rewritten["resolved_issue_ids"])
            self.assertEqual(
                "single_paragraph",
                rewritten["candidate_evaluation"]["evaluation_scope"],
            )
            self.assertEqual(
                "accepted_candidate",
                rewritten["candidate_evaluation"]["evaluation_mode"],
            )
            self.assertEqual("single_paragraph", accepted_evaluation["evaluation_scope"])
            self.assertEqual(92, accepted_evaluation["paragraph_score"]["score"])
            workspace = (
                workspaces.user_root(context.user_id)
                / ".review-writer"
                / "job-staging"
                / context.job_id
                / "draft-workspace"
            )
            metadata = json.loads(
                (
                    workspace
                    / "review-library"
                    / "metadata"
                    / "papers"
                    / "P001.metadata.json"
                ).read_text(encoding="utf-8")
            )
            copied = Path(metadata["source_paths"]["markdown"])
            self.assertTrue(copied.is_file())
            self.assertTrue(copied.is_relative_to(workspace))

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
            self.assertEqual("project-1", overview["editable_text"]["title"])
            self.assertTrue(Path(exported["output_path"]).is_file())
            expected = {
                "draft.evaluate",
                "draft.optimize",
                "draft.rewrite",
                "draft.accept-rewrite",
                "final.conclusion",
                "final.overview",
                "final.export",
            }
            self.assertTrue(expected.issubset(handlers.mapping()))


if __name__ == "__main__":
    unittest.main()
