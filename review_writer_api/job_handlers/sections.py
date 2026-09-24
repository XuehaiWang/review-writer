"""Section-generation native job handlers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from review_writer_api.job_handlers.support import (
    section_generation_timeout_seconds as _section_generation_timeout_seconds,
)
from review_writer_core.stages.sections.coverage import reusable_section_entries
from review_writer_api.job_service import JobYieldRequested
from review_writer_api.model_concurrency import text_parallelism


class SectionJobHandlers:
    @staticmethod
    def _section_progress_callback(
        context, status_file: Path, checkpoint_file: Path | None = None
    ):
        """Publish each completed chapter without exposing mutable artifacts."""

        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                current = max(0, int(status.get("current") or 0))
                total = max(0, int(status.get("total") or 0))
                if total:
                    current = min(current, total)
                checkpoint_revision = None
                if checkpoint_file is not None and checkpoint_file.is_file():
                    info = checkpoint_file.stat()
                    checkpoint_revision = (info.st_mtime_ns, info.st_size)
                fingerprint = json.dumps([status, checkpoint_revision], ensure_ascii=False, sort_keys=True)
                if fingerprint == previous:
                    return
                if hasattr(context, "report_progress"):
                    context.report_progress(current, total)
                if hasattr(context, "report_partial_result"):
                    result = {"section_progress": status}
                    if checkpoint_file is not None and checkpoint_file.is_file():
                        checkpoint = json.loads(
                            checkpoint_file.read_text(encoding="utf-8")
                        )
                        if isinstance(checkpoint, dict):
                            result["section_checkpoint"] = checkpoint
                            drafted = set()
                            for sid, entry in (checkpoint.get("authoring_states") or {}).items():
                                paragraphs = ((entry.get("state") or {}).get("proposed") or {}).get("paragraphs") or []
                                if any(isinstance(p, dict) and (p.get("text") or any(
                                    isinstance(c, dict) and c.get("text") for c in p.get("claims") or [])) for p in paragraphs):
                                    drafted.add(sid)
                            for sid, entry in (checkpoint.get("entries") or {}).items():
                                if (entry.get("writing") or {}).get("paragraphs"):
                                    drafted.add(sid)
                            status["drafted_section_ids"] = sorted(drafted)
                    context.report_partial_result(result)
                previous = fingerprint
            except Exception:
                # Progress reporting is observational and must never invalidate
                # a scientifically valid result that is ready to publish.
                return

        return callback

    def sections_generate(self, context, payload):
        """Run the established section writer in an isolated compatibility workspace."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / "section-workspace"
        project_id = str(payload["project_id"])
        project = workspace / "review-projects" / project_id
        planning = project / "01_matrix_outline"
        section_stage = project / "02_section_drafting"
        planning.mkdir(parents=True, exist_ok=True)
        section_stage.mkdir(parents=True, exist_ok=True)
        (planning / "literature_matrix.json").write_text(
            json.dumps(payload["matrix"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (planning / "section_blueprint.json").write_text(
            json.dumps(payload["blueprint"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (planning / "selected_outline.md").write_text(
            str(payload.get("outline_md") or ""), encoding="utf-8"
        )
        (section_stage / "section_tasks.json").write_text(
            json.dumps(payload["tasks"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (section_stage / "section_evidence.json").write_text(
            json.dumps(
                payload.get("evidence_package") or {"schema_version": 1, "sections": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata_directory = workspace / "review-library" / "metadata" / "papers"
        metadata_directory.mkdir(parents=True, exist_ok=True)
        for paper_id, metadata in (payload.get("library_metadata") or {}).items():
            (metadata_directory / f"{paper_id}.metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        discovery_stage = project / "00_discovery"
        discovery_stage.mkdir(parents=True, exist_ok=True)
        selected_ids = list(
            dict.fromkeys(
                str(paper_id)
                for task in payload.get("tasks") or []
                for paper_id in task.get("allowed_papers") or []
            )
        )
        (discovery_stage / "selected_discovery_results.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "local_papers": [
                        {"paper_id": paper_id, "keep": True}
                        for paper_id in selected_ids
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        normal, secrets = self._text_gateway_environment(context)
        normal["REVIEW_SECTION_AUDIT_MODE"] = os.environ.get("REVIEW_SECTION_AUDIT_MODE", "full")
        if self.model_gateway is not None and os.environ.get("REVIEW_WRITER_DELEGATED_MODEL_ENABLED") == "1":
            normal["REVIEW_WRITER_DELEGATE_MODEL_CALLS"] = "1"
        relative_stage = Path("section-workspace") / "review-projects" / project_id / "02_section_drafting"
        progress_file = section_stage / "generation_progress.json"
        checkpoint_file = section_stage / "section_checkpoints.json"
        progress_file.unlink(missing_ok=True)
        current_job = context.repository.get_job(context.user_id, context.job_id)
        resume_checkpoint = (
            (current_job.result or {}).get("section_checkpoint")
            if current_job is not None else None
        ) or payload.get("resume_checkpoint")
        if isinstance(resume_checkpoint, dict):
            if context.retry_of_job_id:
                resume_checkpoint = {**resume_checkpoint, "failed_sections": []}
            entries, rejected = reusable_section_entries(
                resume_checkpoint.get("entries") or {}, payload.get("tasks") or [],
                {row["section_id"]: row for row in (payload.get("evidence_package") or {}).get("sections") or []},
            )
            resume_checkpoint = {
                **resume_checkpoint,
                "entries": entries,
                "rejected_entries": rejected,
                "resume_validated": True,
            }
            checkpoint_file.write_text(
                json.dumps(resume_checkpoint, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            checkpoint_file.unlink(missing_ok=True)
        section_timeout_seconds = _section_generation_timeout_seconds(
            payload.get("tasks"), resume_checkpoint
        )
        gateway = self.model_gateway
        parallelism = text_parallelism(
            getattr(gateway, "settings", None), getattr(gateway, "session_factory", None)
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-section-drafting-figure-picking"
                    / "scripts"
                    / "generate_section_drafts.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--section-concurrency",
                str(parallelism),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=((relative_stage / "generation_progress.json").as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._section_progress_callback(
                context, progress_file, checkpoint_file
            ),
            max_attempts=1,
            timeout_seconds=section_timeout_seconds,
        )
        self._section_progress_callback(context, progress_file, checkpoint_file)()
        task_ids = {
            str(task.get("section_id") or "")
            for task in payload.get("tasks") or []
            if str(task.get("section_role") or "").casefold() != "conclusion"
        }
        latest_checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        if task_ids.difference((latest_checkpoint.get("entries") or {}).keys()):
            raise JobYieldRequested()
        # The last section publishes the same complete outputs as the old
        # batch path; partial checkpoints never become current drafts.
        for filename in (
            "section_drafts.json", "section_drafts.md", "section_drafting_report.md",
            "synthesis_state.json", "writing_plan.json",
        ):
            if not (section_stage / filename).is_file():
                raise RuntimeError(f"Completed section output is missing: {filename}")
        drafts = json.loads(
            (section_stage / "section_drafts.json").read_text(encoding="utf-8")
        )
        scripts = (
            self.root
            / "skills"
            / "review-section-drafting-figure-picking"
            / "scripts"
        )
        self.runner.run(
            [
                sys.executable,
                str(scripts / "build_paper_figure_inventory.py"),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=((relative_stage / "paper_figure_inventory.json").as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        self.runner.run(
            [
                sys.executable,
                str(scripts / "select_initial_figure_candidates.py"),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "paper_figure_candidates.json").as_posix(),
                (relative_stage / "figure_candidates.json").as_posix(),
                (relative_stage / "human_figure_review.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        return {
            # Fact-gap repair can replace the task/evidence contract after the
            # job starts.  Publication must validate against this exact
            # hydrated snapshot rather than the queued job's stale payload.
            # The stage handler consumes this private value; it is never
            # persisted as a workflow artifact.
            "_publication_payload": payload,
            "sections": drafts.get("sections") or [],
            "synthesis_state": json.loads(
                (section_stage / "synthesis_state.json").read_text(encoding="utf-8")
            ),
            "writing_plan": json.loads(
                (section_stage / "writing_plan.json").read_text(encoding="utf-8")
            ),
            "section_drafts_md": (section_stage / "section_drafts.md").read_text(
                encoding="utf-8"
            ),
            "report_md": (
                section_stage / "section_drafting_report.md"
            ).read_text(encoding="utf-8"),
            "paper_figure_candidates": json.loads(
                (section_stage / "paper_figure_candidates.json").read_text(
                    encoding="utf-8"
                )
            ),
            "figure_candidates": json.loads(
                (section_stage / "figure_candidates.json").read_text(
                    encoding="utf-8"
                )
            ),
            "default_figure_reviews": json.loads(
                (section_stage / "human_figure_review.json").read_text(
                    encoding="utf-8"
                )
            ),
        }
