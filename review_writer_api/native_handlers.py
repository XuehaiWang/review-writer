"""Production JobService handlers for Library and Discovery."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from review_writer_api.credentials import ProviderSettingsService
from review_writer_api.errors import LiteratureSearchFailed, WorkflowValidationError
from review_writer_api.scientific_runner import ScientificRunFailed, ScientificRunner
from review_writer_api.security import Principal, Role
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.providers import DEFAULT_IMAGE_MODEL


SENSITIVE_KEY = re.compile(r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)
ARTIFACT_URL = re.compile(r"/api/v1/artifacts/([0-9a-fA-F-]{36})/content")
SAFE_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
EVALUATE_DRAFT_TIMEOUT_SECONDS = 30 * 60
OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS = 2 * 60 * 60
OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS = 45 * 60
OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS = 6 * 60 * 60


def _literature_search_failure_message(error: ScientificRunFailed) -> str:
    diagnostic = str((error.details or {}).get("stderr") or "")
    lowered = diagnostic.casefold()
    if "private destination is blocked" in lowered:
        return (
            "Crossref resolved through a private or transparent-proxy address that is not "
            "trusted by this deployment. Configure REVIEW_WRITER_TRUSTED_PROXY_NETWORKS "
            "and retry."
        )
    if any(marker in lowered for marker in ("[winerror 10060]", "timed out", "timeouterror")):
        return "Crossref did not respond before timeout. Check this server's outbound network or proxy, then retry."
    if any(marker in lowered for marker in ("getaddrinfo failed", "name or service not known", "temporary failure in name resolution", "nodename nor servname")):
        return "Crossref could not be resolved by DNS. Check this server's DNS and outbound network, then retry."
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return "Crossref TLS certificate verification failed. Check this server's certificate trust store or HTTPS proxy."
    if "http error 429" in lowered:
        return "Crossref rate-limited the literature search. Wait briefly and retry; providing a contact email may improve reliability."
    if "http error 403" in lowered:
        return "Crossref rejected the literature search request. Check the server network or proxy policy, then retry."
    if re.search(r"http error 5\d\d", lowered):
        return "Crossref is temporarily unavailable. Retry the literature search shortly."
    return "Crossref literature search failed before results were returned. Check outbound access to https://api.crossref.org and retry."


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
            "draft.evaluate": self.draft_evaluate,
            "draft.optimize": self.draft_optimize,
            "draft.rewrite": self.draft_rewrite,
            "draft.accept-rewrite": self.draft_accept_rewrite,
            "final.conclusion": self.final_conclusion,
            "final.overview": self.final_overview,
            "final.export": self.final_export,
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
        try:
            safe_job_id = str(uuid.UUID(str(job_id)))
        except ValueError as exc:
            raise RuntimeError("Native job identifier is invalid.") from exc
        return self.workspaces.trusted_user_directory(
            user_id, ".review-writer", "job-staging", safe_job_id
        )

    @staticmethod
    def _result(staging: Path, filename: str) -> dict[str, Any]:
        payload = json.loads((staging / filename).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Scientific task result is not an object.")
        return payload

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _assert_safe_project_id(project_id: str) -> str:
        if not SAFE_PROJECT_ID.fullmatch(project_id) or ".." in project_id:
            raise RuntimeError("Project identifier is unsafe for a scientific workspace.")
        return project_id

    @staticmethod
    def _trusted_user_file(user_root: Path, raw: Any) -> Path | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            candidate = Path(value).resolve(strict=True)
            relative = candidate.relative_to(user_root.resolve(strict=True))
        except (OSError, ValueError):
            return None
        current = user_root.resolve(strict=True)
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                return None
        return candidate if candidate.is_file() else None

    @staticmethod
    def _replace_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        normalized = {str(key): str(value) for key, value in (paths or {}).items()}

        def replacement(match: re.Match[str]) -> str:
            return normalized.get(match.group(1), match.group(0))

        return ARTIFACT_URL.sub(replacement, str(markdown or ""))

    def _compatibility_workspace(
        self,
        context,
        payload: dict[str, Any],
        *,
        name: str,
        markdown_key: str = "draft_text",
    ) -> tuple[Path, Path, Path]:
        """Materialize immutable PostgreSQL payloads for established scientific CLIs."""

        staging = self._staging(context.user_id, context.job_id)
        workspace = staging / name
        if workspace.is_symlink():
            raise RuntimeError("Scientific compatibility workspace is not trusted.")
        if workspace.exists():
            shutil.rmtree(workspace)
        project_id = self._assert_safe_project_id(str(payload["project_id"]))
        project = workspace / "review-projects" / project_id
        planning = project / "01_matrix_outline"
        sections = project / "02_section_drafting"
        figures = project / "03_figure_redraw"
        first = project / "04_first_draft"
        final = project / "05_final_audit"
        for directory in (planning, sections, figures, first, final):
            directory.mkdir(parents=True, exist_ok=True)

        matrix = dict(payload.get("matrix") or {})
        if not isinstance(matrix.get("papers"), list):
            matrix["papers"] = list(matrix.get("rows") or [])
        self._write_json(planning / "literature_matrix.json", matrix)
        self._write_json(
            planning / "section_blueprint.json", dict(payload.get("blueprint") or {})
        )
        section_index = dict(payload.get("section_index") or {})
        self._write_json(sections / "section_drafts.json", section_index)
        section_markdown = str(payload.get("section_drafts_md") or "").strip()
        if not section_markdown:
            section_markdown = str(payload.get(markdown_key) or "")
        (sections / "section_drafts.md").write_text(
            section_markdown.rstrip() + "\n", encoding="utf-8"
        )

        artifact_paths = dict(payload.get("figure_artifact_paths") or {})
        figure_manifest = json.loads(
            json.dumps(payload.get("figure_manifest") or {}, ensure_ascii=False)
        )
        for row in figure_manifest.get("figures") or []:
            if not isinstance(row, dict):
                continue
            artifact_id = str(row.get("output_artifact_id") or "")
            if artifact_id and artifact_id in artifact_paths:
                resolved = str(artifact_paths[artifact_id])
                row["redrawn_image"] = resolved
                row["output_path"] = resolved
                row["output_image_path"] = resolved
        self._write_json(figures / "redrawn_figure_manifest.json", figure_manifest)

        draft_text = self._replace_artifact_urls(
            str(payload.get(markdown_key) or ""), artifact_paths
        )
        (first / "first_draft.md").write_text(
            draft_text.rstrip() + "\n", encoding="utf-8"
        )
        rewrite_overlays = payload.get("rewrite_overlays")
        if isinstance(rewrite_overlays, dict) and rewrite_overlays:
            self._write_json(first / "feedback_loop_rewrites.json", rewrite_overlays)
        citations = []
        for index, row in enumerate(matrix.get("rows") or matrix.get("papers") or [], 1):
            if not isinstance(row, dict) or not row.get("paper_id"):
                continue
            citations.append(
                {"paper_id": str(row["paper_id"]), "callout": index}
            )
        self._write_json(first / "citations.json", {"entries": citations})

        metadata_directory = workspace / "review-library" / "metadata" / "papers"
        sources_directory = workspace / "review-library" / "sources"
        user_root = self.workspaces.user_root(context.user_id)
        for paper_id, raw_metadata in (payload.get("library_metadata") or {}).items():
            safe_paper_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(paper_id))[:200]
            metadata = json.loads(json.dumps(raw_metadata or {}, ensure_ascii=False))
            source_paths = metadata.get("source_paths")
            source_paths = dict(source_paths) if isinstance(source_paths, dict) else {}
            copied_paths: dict[str, str] = {}
            for source_kind in ("content_list", "markdown"):
                source = self._trusted_user_file(user_root, source_paths.get(source_kind))
                if source is None:
                    continue
                destination = sources_directory / safe_paper_id / (
                    f"{source_kind}{source.suffix or '.txt'}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied_paths[source_kind] = str(destination)
            metadata["source_paths"] = copied_paths
            self._write_json(
                metadata_directory / f"{safe_paper_id}.metadata.json", metadata
            )
        return staging, workspace, project

    def library_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
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
        try:
            self.runner.run(
                command,
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=("search-result.json",),
                env={},
                secret_env={},
                cancel_requested=context.cancellation_requested,
            )
        except ScientificRunFailed as exc:
            raise LiteratureSearchFailed(
                _literature_search_failure_message(exc),
                details={
                    "attempts": exc.attempts,
                    "category": str((exc.details or {}).get("category") or "unknown"),
                },
            ) from exc
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
            # OA resolution uses Crossref/Europe PMC/Semantic Scholar and the
            # optional email supplied in the request. It must not depend on
            # unrelated text, image, or MinerU provider settings.
            env={},
            secret_env={},
            cancel_requested=context.cancellation_requested,
            timeout_seconds=35 * 60,
        )
        return self._result(staging, "download-result.json")

    def discovery_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        normal, secrets = self._environment(context.user_id)
        report_progress = getattr(context, "report_progress", None)
        if callable(report_progress):
            report_progress(1, 4)
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
            "--auto-query-plan",
            "--output-project-dir",
            str(staging),
        ]
        if payload.get("web_search"):
            command.append("--web-search")
        if callable(report_progress):
            report_progress(2, 4)
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("00_discovery/combined_results_by_keyword.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=(
                (lambda: report_progress(2, 4))
                if callable(report_progress)
                else None
            ),
        )
        if callable(report_progress):
            report_progress(3, 4)
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
        requested_figure_type = str(payload.get("figure_type") or "auto")
        image_model = str(
            normal.get("IMAGE_OPENAI_MODEL") or DEFAULT_IMAGE_MODEL
        ).strip() or DEFAULT_IMAGE_MODEL
        relative_manifest = (
            Path("figure-workspace")
            / "review-projects"
            / project_id
            / "03_figure_redraw"
            / "redrawn_figure_manifest.json"
        )
        command = [
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
                "--model",
                image_model,
                "--figure-type",
                requested_figure_type,
                "--render-mode",
                "ai-edit",
                "--require-redrawn",
            ]
        if requested_figure_type.strip().casefold() != "auto":
            command.append("--force-standard-ai-edit")
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_manifest.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=20 * 60,
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

    @staticmethod
    def _restore_artifact_urls(markdown: str, paths: dict[str, Any]) -> str:
        restored = str(markdown or "")
        for artifact_id, raw_path in (paths or {}).items():
            path = str(raw_path or "")
            if path:
                restored = restored.replace(
                    path, f"/api/v1/artifacts/{artifact_id}/content"
                )
        return restored

    @staticmethod
    def _feedback_progress_callback(context, status_file: Path, *, optimize: bool):
        previous = ""

        def callback() -> None:
            nonlocal previous
            try:
                if not status_file.is_file():
                    return
                status = json.loads(status_file.read_text(encoding="utf-8"))
                if not isinstance(status, dict):
                    return
                fingerprint = json.dumps(status, ensure_ascii=False, sort_keys=True)
                if fingerprint == previous:
                    return
                previous = fingerprint
                if hasattr(context, "report_partial_result"):
                    context.report_partial_result({"feedback_status": status})
                if hasattr(context, "report_progress"):
                    phase = str(status.get("phase") or "")
                    if optimize:
                        current = {
                            "preflight": 1,
                            "source_checking": 2,
                            "scoring": 2,
                            "evaluated": 3,
                            "rewriting": 3,
                            "plateau": 4,
                            "rewrite_blocked": 4,
                            "iteration_limit": 4,
                            "released": 4,
                        }.get(phase, 1)
                        context.report_progress(current, 5)
                    else:
                        current = 2 if phase in {"scoring", "evaluated", "released"} else 1
                        context.report_progress(current, 3)
            except Exception:
                # Progress reporting is observational and must never invalidate
                # a scientifically valid result that is ready to publish.
                return

        return callback

    def _draft_feedback(self, context, payload, *, evaluate_only: bool):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        relative_first = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
        )
        normal, secrets = self._environment(context.user_id)
        command = [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "feedback_loop.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--goal",
                str(float(payload.get("goal") or 90)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or 85)),
                "--max-iterations",
                str(int(payload.get("max_iterations") or 3)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or 140)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or 280)),
            ]
        if evaluate_only:
            command.append("--evaluate-only")
        first = project / "04_first_draft"
        max_iterations = int(payload.get("max_iterations") or 3)
        timeout_seconds = (
            EVALUATE_DRAFT_TIMEOUT_SECONDS
            if evaluate_only
            else min(
                OPTIMIZE_DRAFT_MAX_TIMEOUT_SECONDS,
                max(
                    OPTIMIZE_DRAFT_MIN_TIMEOUT_SECONDS,
                    max_iterations * OPTIMIZE_DRAFT_TIMEOUT_PER_ITERATION_SECONDS,
                ),
            )
        )
        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_first / "rubric_evaluation.json").as_posix(),
                (relative_first / "reviewer_findings.json").as_posix(),
                (relative_first / "first_draft_gate_status.json").as_posix(),
                (relative_first / "first_draft_preflight.json").as_posix(),
                (relative_first / "original_source_check.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=self._feedback_progress_callback(
                context,
                first / "feedback_loop_status.json",
                optimize=not evaluate_only,
            ),
            timeout_seconds=timeout_seconds,
        )
        evaluation = json.loads(
            (first / "rubric_evaluation.json").read_text(encoding="utf-8")
        )
        findings = json.loads(
            (first / "reviewer_findings.json").read_text(encoding="utf-8")
        )
        gate = json.loads(
            (first / "first_draft_gate_status.json").read_text(encoding="utf-8")
        )
        preflight = json.loads(
            (first / "first_draft_preflight.json").read_text(encoding="utf-8")
        )
        source_check = json.loads(
            (first / "original_source_check.json").read_text(encoding="utf-8")
        )
        paragraph_scores = {
            str(item.get("paragraph_id") or ""): item
            for item in evaluation.get("paragraph_scores") or []
            if isinstance(item, dict)
        }
        issues = []
        for index, finding in enumerate(findings if isinstance(findings, list) else [], 1):
            if not isinstance(finding, dict):
                continue
            paragraph_id = str(finding.get("paragraph_id") or "")
            paragraph_score = paragraph_scores.get(paragraph_id, {})
            issues.append(
                {
                    **finding,
                    "score": paragraph_score.get("score"),
                    "route": str(
                        paragraph_score.get("route") or finding.get("route") or ""
                    ),
                    "failed_dimensions": list(
                        paragraph_score.get("failed_dimensions") or []
                    ),
                    "issue_id": str(finding.get("id") or f"PAR-{index:03d}"),
                    "message": str(
                        finding.get("diagnosis")
                        or finding.get("recommended_direction")
                        or "Review this paragraph."
                    ),
                }
            )
        status_file = first / "feedback_loop_status.json"
        feedback_status = (
            json.loads(status_file.read_text(encoding="utf-8"))
            if status_file.is_file()
            else {}
        )
        overlay_file = first / "feedback_loop_rewrites.json"
        rewrite_overlays = (
            json.loads(overlay_file.read_text(encoding="utf-8"))
            if overlay_file.is_file()
            else dict(payload.get("rewrite_overlays") or {})
        )
        batch_review_file = first / "batch_review_candidates.json"
        batch_review = (
            json.loads(batch_review_file.read_text(encoding="utf-8"))
            if batch_review_file.is_file()
            else {}
        )
        result = {
            **evaluation,
            "score": float(evaluation.get("total_score") or 0),
            "goal": float(
                evaluation.get("pass_threshold") or payload.get("goal") or 90
            ),
            "issues": issues,
            "hard_gate_failures": list(
                gate.get("hard_gate_failures")
                or evaluation.get("hard_gate_failures")
                or []
            ),
            "preflight": preflight,
            "source_check": source_check,
            "gate": gate,
            "feedback_status": feedback_status,
        }
        if not evaluate_only:
            result["draft_text"] = self._restore_artifact_urls(
                (first / "first_draft.md").read_text(encoding="utf-8"),
                dict(payload.get("figure_artifact_paths") or {}),
            )
            result["rewrite_overlays"] = rewrite_overlays
            if isinstance(batch_review, dict):
                result["review_candidate_draft_text"] = self._restore_artifact_urls(
                    str(batch_review.get("candidate_draft_text") or ""),
                    dict(payload.get("figure_artifact_paths") or {}),
                )
                result["review_candidate_score"] = batch_review.get(
                    "candidate_score"
                )
                result["review_changes"] = list(
                    batch_review.get("changes") or []
                )
                result["review_excluded"] = list(
                    batch_review.get("excluded") or []
                )
        return result

    def draft_evaluate(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=True)

    def draft_optimize(self, context, payload):
        return self._draft_feedback(context, payload, evaluate_only=False)

    @staticmethod
    def _retain_manual_confirmation_route(
        candidate_evaluation: dict[str, Any],
        generation_entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Prevent a style-only rewrite from clearing an evidence conflict."""

        if not bool(generation_entry.get("requires_manual_confirmation")):
            return candidate_evaluation
        result = dict(candidate_evaluation)
        paragraph_score = dict(result.get("paragraph_score") or {})
        paragraph_score["score"] = min(
            float(paragraph_score.get("score") or 0),
            79.0,
        )
        paragraph_score["severity"] = "major"
        paragraph_score["route"] = "human_confirmation"
        failed = [
            str(value)
            for value in paragraph_score.get("failed_dimensions") or []
            if str(value).strip()
        ]
        if "manual_source_confirmation" not in failed:
            failed.append("manual_source_confirmation")
        paragraph_score["failed_dimensions"] = failed
        paragraph_score["diagnosis"] = str(
            generation_entry.get("diagnosis")
            or paragraph_score.get("diagnosis")
            or "Manual source or figure-identity confirmation remains required."
        )
        result.update(
            {
                "paragraph_score": paragraph_score,
                "requires_manual_confirmation": True,
                "manual_confirmation_reason": paragraph_score["diagnosis"],
            }
        )
        return result

    def draft_rewrite(self, context, payload):
        """Generate one paragraph candidate, then score only that candidate."""

        if not str(payload.get("draft_text") or "").strip():
            raise WorkflowValidationError(
                "The rewrite task did not receive the current Draft content. "
                "Refresh the Draft and generate the rewrite candidate again."
            )
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="draft-workspace"
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        quality = dict(payload.get("quality") or {})
        goal = float(payload.get("goal") or quality.get("goal") or 90)
        paragraph_goal = float(
            payload.get("paragraph_goal")
            or quality.get("paragraph_pass_threshold")
            or quality.get("paragraph_goal")
            or 85
        )
        min_case_words = int(payload.get("min_case_words") or 140)
        max_case_words = int(payload.get("max_case_words") or 280)
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raw_issues = quality.get("issues") or []
        issues = [
            item
            for item in raw_issues
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ]
        normal, secrets = self._environment(context.user_id)
        paragraph_evaluation_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        report_progress = getattr(context, "report_progress", None)
        finding = next(
            (
                dict(item)
                for item in quality.get("paragraph_scores") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            {},
        )
        prior_issue = dict(issues[0]) if issues else {}
        if not finding:
            finding = dict(prior_issue)
        if not finding:
            raise RuntimeError(
                "The selected paragraph has no stored score or issue context. "
                "Evaluate the current Draft before generating a candidate."
            )
        finding = {
            **prior_issue,
            **finding,
            "paragraph_id": paragraph_id,
            "severity": str(
                finding.get("severity")
                or prior_issue.get("severity")
                or "minor"
            ),
            "route": str(
                finding.get("route")
                or prior_issue.get("route")
                or "section_rewrite"
            ),
            "diagnosis": str(
                finding.get("diagnosis")
                or prior_issue.get("diagnosis")
                or prior_issue.get("message")
                or "Polish this paragraph while preserving all protected facts and citations."
            ),
            "failed_dimensions": list(
                finding.get("failed_dimensions")
                or prior_issue.get("failed_dimensions")
                or []
            ),
        }
        source_paragraph_evaluation = {
            "evaluation_scope": "single_paragraph",
            "evaluation_mode": "stored_source_score",
            "paragraph_id": paragraph_id,
            "paragraph_score": finding,
        }

        evaluation = {
            **quality,
            "goal": goal,
            "paragraph_pass_threshold": paragraph_goal,
            "paragraph_scores": [finding],
            "paragraph_failures": [finding],
            "blocking_paragraph_failures": [finding],
        }
        self._write_json(first / "rubric_evaluation.json", evaluation)
        source_check = quality.get("source_check")
        source_check = source_check if isinstance(source_check, dict) else {}
        source_entry = next(
            (
                item
                for item in source_check.get("entries") or []
                if isinstance(item, dict)
                and str(item.get("paragraph_id") or "") == paragraph_id
            ),
            None,
        )
        self._write_json(
            first / "original_source_check.json",
            {"entries": [source_entry]} if isinstance(source_entry, dict) and source_entry else {"entries": []},
        )
        preflight = quality.get("preflight")
        self._write_json(
            first / "first_draft_preflight.json",
            preflight if isinstance(preflight, dict) else {"paragraph_checks": []},
        )
        draft_path = first / "first_draft.md"
        digest = hashlib.sha256(draft_path.read_bytes()).hexdigest()
        self._write_json(
            first / "feedback_loop_status.json",
            {
                "status": "completed",
                "phase": "evaluated",
                "goal": goal,
                "paragraph_goal": paragraph_goal,
                "min_case_words": min_case_words,
                "max_case_words": max_case_words,
                "source_draft_sha256": digest,
                "output_draft_sha256": digest,
            },
        )
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "feedback_rewrite_candidates.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "propose_paragraph_rewrite.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidates = json.loads(
            (first / "feedback_rewrite_candidates.json").read_text(encoding="utf-8")
        )
        entry = (candidates.get("entries") or {}).get(paragraph_id)
        if not isinstance(entry, dict) or not str(entry.get("candidate_text") or "").strip():
            raise RuntimeError("The paragraph rewrite produced no candidate.")
        candidate_text = str(entry["candidate_text"]).strip()
        if callable(report_progress):
            report_progress(2, 4)

        original_draft = draft_path.read_text(encoding="utf-8")
        requested_paragraph = str(payload["paragraph_text"]).strip()
        # The proposal script is the final reader of the materialized Markdown.
        # Use the exact source span it rewrote for both replacement and the
        # second integrity check.  Mixing this value with an API/UI paragraph
        # projection previously compared a figure-bearing candidate against a
        # prose-only source, causing false image/metadata/number failures and
        # even duplicating the adjacent figure in the temporary scoring draft.
        original_paragraph = str(
            entry.get("original_text") or requested_paragraph
        ).strip()
        normalize = lambda value: " ".join(str(value or "").split())
        if normalize(original_paragraph) != normalize(requested_paragraph):
            raise RuntimeError(
                "The paragraph rewrite source boundary did not match the selected "
                "paragraph; the candidate was discarded before scoring."
            )
        if original_paragraph not in original_draft:
            raise RuntimeError(
                "The selected paragraph changed before candidate scoring."
            )
        candidate_draft = original_draft.replace(
            original_paragraph, candidate_text, 1
        )
        draft_path.write_text(candidate_draft, encoding="utf-8")
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": original_paragraph,
                "candidate_text": candidate_text,
                "allowed_unsupported_claims": list(
                    finding.get("unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(goal),
                "--paragraph-goal",
                str(paragraph_goal),
                "--min-case-words",
                str(min_case_words),
                "--max-case-words",
                str(max_case_words),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(paragraph_evaluation_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        candidate_evaluation = json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        candidate_evaluation = self._retain_manual_confirmation_route(
            candidate_evaluation,
            entry,
        )
        if callable(report_progress):
            report_progress(3, 4)
        return {
            "candidate_text": candidate_text,
            "resolved_issue_ids": [
                str(item.get("issue_id") or item.get("id") or "")
                for item in issues
                if str(item.get("issue_id") or item.get("id") or "")
            ],
            "report": entry,
            "source_paragraph_evaluation": source_paragraph_evaluation,
            "candidate_evaluation": candidate_evaluation,
        }

    def draft_accept_rewrite(self, context, payload):
        """Evaluate only the accepted candidate paragraph in an isolated workspace."""

        staging, workspace, project = self._compatibility_workspace(
            context,
            payload,
            name="draft-workspace",
            markdown_key="candidate_draft_text",
        )
        project_id = str(payload["project_id"])
        paragraph_id = str(payload["paragraph_id"])
        first = project / "04_first_draft"
        self._write_json(
            first / "paragraph_candidate_evaluation_request.json",
            {
                "evaluation_mode": "accepted_candidate",
                "paragraph_id": paragraph_id,
                "original_text": str(payload["paragraph_text"]),
                "candidate_text": str(payload["candidate_text"]),
                "allowed_unsupported_claims": list(
                    payload.get("allowed_unsupported_claims") or []
                ),
                "word_range_applicable": bool(
                    payload.get("word_range_applicable", True)
                ),
            },
        )
        normal, secrets = self._environment(context.user_id)
        relative_output = (
            Path("draft-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
            / "paragraph_candidate_evaluation.json"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-first-draft-feedback-loop"
                    / "scripts"
                    / "evaluate_paragraph_candidate.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--paragraph-id",
                paragraph_id,
                "--goal",
                str(float(payload.get("goal") or 90)),
                "--paragraph-goal",
                str(float(payload.get("paragraph_goal") or 85)),
                "--min-case-words",
                str(int(payload.get("min_case_words") or 140)),
                "--max-case-words",
                str(int(payload.get("max_case_words") or 280)),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        return json.loads(
            (first / "paragraph_candidate_evaluation.json").read_text(
                encoding="utf-8"
            )
        )

    def final_conclusion(self, context, payload):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="final-conclusion-workspace"
        )
        project_id = str(payload["project_id"])
        normal, secrets = self._environment(context.user_id)
        relative_first = (
            Path("final-conclusion-workspace")
            / "review-projects"
            / project_id
            / "04_first_draft"
        )
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-conclusion-generator"
                    / "scripts"
                    / "generate_conclusion1.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--mode",
                "orchestrated",
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_first / "conclusion_generated.md").as_posix(),
                (relative_first / "conclusion_quality_report.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        first = project / "04_first_draft"
        report = json.loads(
            (first / "conclusion_quality_report.json").read_text(encoding="utf-8")
        )
        return {
            "markdown": (first / "conclusion_generated.md").read_text(encoding="utf-8"),
            "report": report,
        }

    def final_overview(self, context, payload):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="final-overview-workspace"
        )
        project_id = str(payload["project_id"])
        output = project / "03_figure_redraw" / "overview_figure.png"
        report_path = project / "03_figure_redraw" / "overview_template_match.json"
        relative_stage = (
            Path("final-overview-workspace")
            / "review-projects"
            / project_id
            / "03_figure_redraw"
        )
        normal, secrets = self._environment(context.user_id)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-figure-style-redraw"
                    / "scripts"
                    / "generate_overview_figure.py"
                ),
                "--review-root",
                str(workspace),
                "--project-id",
                project_id,
                "--output",
                str(output),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(
                (relative_stage / "overview_figure.png").as_posix(),
                (relative_stage / "overview_template_match.json").as_posix(),
            ),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=15 * 60,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        features = report.get("features") if isinstance(report, dict) else {}
        features = features if isinstance(features, dict) else {}
        labels: list[str] = []
        for key in ("metal_categories", "group_by", "time_window"):
            value = features.get(key)
            values = value if isinstance(value, list) else [value]
            labels.extend(str(item) for item in values if str(item or "").strip())
        template = report.get("template")
        template_name = str(report.get("selected_template_name") or "")
        if not template_name and isinstance(template, dict):
            template_name = str(template.get("name") or "")
        blueprint = payload.get("blueprint")
        blueprint = blueprint if isinstance(blueprint, dict) else {}
        title = str(
            blueprint.get("review_topic")
            or blueprint.get("topic")
            or blueprint.get("review_question")
            or project_id
        )
        return {
            "output_path": str(output),
            "editable_text": {
                "title": title,
                "subtitle": template_name,
                "labels": list(dict.fromkeys(labels)),
            },
            "report": report,
        }

    def final_export(self, context, payload):
        staging, _workspace, project = self._compatibility_workspace(
            context,
            payload,
            name="final-export-workspace",
            markdown_key="final_markdown",
        )
        markdown_path = project / "05_final_audit" / "final_draft.md"
        markdown_path.write_text(
            self._replace_artifact_urls(
                str(payload.get("final_markdown") or ""),
                dict(payload.get("figure_artifact_paths") or {}),
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        output = project / "05_final_audit" / "final_draft.docx"
        relative_output = (
            Path("final-export-workspace")
            / "review-projects"
            / str(payload["project_id"])
            / "05_final_audit"
            / "final_draft.docx"
        )
        normal, secrets = self._environment(context.user_id)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-export-docx"
                    / "scripts"
                    / "run_md2docx.py"
                ),
                "--input",
                str(markdown_path),
                "--output",
                str(output),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=(relative_output.as_posix(),),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        return {"output_path": str(output), "download_name": "final_draft.docx"}
