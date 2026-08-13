"""Production JobService handlers for Library and Discovery."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from review_writer_api.credentials import ProviderSettingsService
from review_writer_api.scientific_runner import ScientificRunner
from review_writer_api.security import Principal, Role
from review_writer_api.workspaces import HostedWorkspaceManager


SENSITIVE_KEY = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


class NativeWorkflowHandlers:
    def __init__(
        self,
        runner: ScientificRunner,
        workspaces: HostedWorkspaceManager,
        provider_settings: ProviderSettingsService | None,
    ):
        self.runner = runner
        self.workspaces = workspaces
        self.provider_settings = provider_settings
        self.root = Path(__file__).resolve().parents[1]

    def mapping(self) -> dict[str, Any]:
        return {
            "library.search": self.library_search,
            "library.download": self.library_download,
            "discovery.search": self.discovery_search,
            "sections.generate": self.sections_generate,
            "figures.redraw": self.figures_redraw,
        }

    def _environment(self, user_id: str) -> tuple[dict[str, str], dict[str, str]]:
        if self.provider_settings is None:
            return {}, {}
        principal = Principal(user_id, frozenset({Role.USER}))
        values = self.provider_settings.runtime_environment(principal)
        secrets = {key: value for key, value in values.items() if SENSITIVE_KEY.search(key)}
        normal = {key: value for key, value in values.items() if key not in secrets}
        return normal, secrets

    def _staging(self, user_id: str, job_id: str) -> Path:
        root = self.workspaces.user_root(user_id) / ".review-writer" / "job-staging"
        if root.is_symlink():
            raise RuntimeError("Native job staging is not trusted.")
        staging = root / job_id
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    @staticmethod
    def _result(staging: Path, filename: str) -> dict[str, Any]:
        payload = json.loads((staging / filename).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Scientific task result is not an object.")
        return payload

    def library_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        normal, secrets = self._environment(context.user_id)
        command = [
            sys.executable,
            "-m",
            "review_writer_api.scientific_tasks",
            "literature-search",
            "--review-root",
            str(self.workspaces.user_root(context.user_id)),
            "--output",
            str(staging / "search-result.json"),
            "--topic",
            str(payload["topic"]),
            "--limit",
            str(max(1, min(int(payload.get("limit") or 20), 50))),
        ]
        for key, flag in (("year_from", "--year-from"), ("year_to", "--year-to")):
            if payload.get(key) is not None:
                command.extend([flag, str(int(payload[key]))])
        if payload.get("email"):
            command.extend(["--mailto", str(payload["email"])])
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("search-result.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
        )
        return self._result(staging, "search-result.json")

    def library_download(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        task_workspace = staging / "library-workspace"
        if task_workspace.is_symlink():
            raise RuntimeError("Library download workspace is not trusted.")
        task_workspace.mkdir(exist_ok=True)
        (staging / "candidates.json").write_text(
            json.dumps(payload["candidates"], ensure_ascii=False), encoding="utf-8"
        )
        normal, secrets = self._environment(context.user_id)
        command = [
            sys.executable,
            "-m",
            "review_writer_api.scientific_tasks",
            "literature-download",
            "--review-root",
            str(task_workspace),
            "--input",
            str(staging / "candidates.json"),
            "--output",
            str(staging / "download-result.json"),
        ]
        if payload.get("email"):
            command.extend(["--email", str(payload["email"])])
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("download-result.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=35 * 60,
        )
        return self._result(staging, "download-result.json")

    def discovery_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        normal, secrets = self._environment(context.user_id)
        command = [
            sys.executable,
            str(
                self.root
                / "skills"
                / "review-topic-paper-discovery"
                / "scripts"
                / "discover.py"
            ),
            "--review-root",
            str(self.workspaces.user_root(context.user_id)),
            "--project-id",
            str(payload["project_id"]),
            "--topic",
            str(payload["topic"]),
            "--keywords",
            str(payload.get("keywords") or ""),
            "--output-project-dir",
            str(staging),
        ]
        if payload.get("web_search"):
            command.append("--web-search")
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("00_discovery/combined_results_by_keyword.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
        )
        return self._result(staging, "00_discovery/combined_results_by_keyword.json")

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
        normal, secrets = self._environment(context.user_id)
        relative_stage = Path("section-workspace") / "review-projects" / project_id / "02_section_drafting"
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
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "section_drafts.json").as_posix(),
                (relative_stage / "section_drafts.md").as_posix(),
                (relative_stage / "section_drafting_report.md").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
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
            "sections": drafts.get("sections") or [],
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

    def figures_redraw(self, context, payload):
        """Run one established figure redraw in an isolated user/job workspace."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / "figure-workspace"
        project_id = str(payload["project_id"])
        figure = dict(payload["figure"])
        figure_id = str(figure["figure_id"])
        source = Path(str(payload["source_path"])).resolve()
        user_root = self.workspaces.user_root(context.user_id)
        try:
            source.relative_to(user_root)
        except ValueError as exc:
            raise RuntimeError("Figure source escaped its user workspace.") from exc
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("Figure source is unavailable.")
        section_stage = (
            workspace / "review-projects" / project_id / "02_section_drafting"
        )
        source_directory = section_stage / "source"
        source_directory.mkdir(parents=True, exist_ok=True)
        copied_source = source_directory / f"{figure_id}{source.suffix or '.png'}"
        shutil.copy2(source, copied_source)
        figure["source_image_path"] = str(copied_source)
        (section_stage / "figure_candidates.json").write_text(
            json.dumps([figure], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        normal, secrets = self._environment(context.user_id)
        relative_manifest = (
            Path("figure-workspace")
            / "review-projects"
            / project_id
            / "03_figure_redraw"
            / "redrawn_figure_manifest.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-figure-style-redraw"
                    / "scripts"
                    / "redraw_figures.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--figure-id",
                figure_id,
                "--figure-type",
                str(payload.get("figure_type") or "auto"),
                "--require-redrawn",
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_manifest.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        manifest = json.loads(
            (staging / relative_manifest).read_text(encoding="utf-8")
        )
        row = next(
            (
                item
                for item in manifest.get("figures") or []
                if isinstance(item, dict)
                and str(item.get("figure_id") or "") == figure_id
            ),
            None,
        )
        if not isinstance(row, dict) or row.get("status") != "redrawn":
            raise RuntimeError("The figure redraw did not produce a usable output.")
        output = str(
            row.get("redrawn_image")
            or row.get("output_path")
            or row.get("output_image_path")
            or ""
        )
        if not output:
            raise RuntimeError("The figure redraw output path is missing.")
        return {**row, "output_path": output}
