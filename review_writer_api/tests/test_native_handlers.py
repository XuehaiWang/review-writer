from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from review_writer_api.native_handlers import NativeWorkflowHandlers
from review_writer_api.workspaces import HostedWorkspaceManager


class _Context:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.job_id = str(uuid.uuid4())

    @staticmethod
    def cancellation_requested() -> bool:
        return False


class _RecordingRunner:
    def __init__(self):
        self.command: tuple[str, ...] = ()

    def run(self, command, **_kwargs):
        self.command = tuple(command)
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

    def run(self, command, **_kwargs):
        command = tuple(str(value) for value in command)
        self.commands.append(command)
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
            return
        if script == "propose_paragraph_rewrite.py":
            (first / "feedback_rewrite_candidates.json").write_text(
                json.dumps(
                    {
                        "entries": {
                            "p1": {
                                "paragraph_id": "p1",
                                "candidate_text": "Improved evidence comparison [1].",
                            }
                        }
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


class NativeWorkflowHandlerTests(unittest.TestCase):
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
            rewritten = handlers.draft_rewrite(
                context,
                {
                    **common,
                    "paragraph_id": "p1",
                    "paragraph_text": "Evidence paragraph.",
                    "quality": evaluated,
                    "issues": evaluated["issues"],
                },
            )

            self.assertEqual(81.5, evaluated["score"])
            self.assertEqual("PAR-001", evaluated["issues"][0]["issue_id"])
            self.assertEqual("Improved evidence comparison [1].", rewritten["candidate_text"])
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
                "draft.rewrite",
                "final.conclusion",
                "final.overview",
                "final.export",
            }
            self.assertTrue(expected.issubset(handlers.mapping()))


if __name__ == "__main__":
    unittest.main()
