"""Final-stage native job handlers.

The host supplies the shared runner, workspace, gateway, and staging helpers.
Keeping the stage methods here makes the job registry readable without
duplicating runtime infrastructure.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from review_writer_api.errors import WorkflowValidationError
from review_writer_core.review_titles import build_publication_overview_text
from review_writer_core.pdf_diagnostics import pdf_character_diagnostics


class FinalJobHandlers:
    def final_conclusion(self, context, payload):
        staging, workspace, project = self._compatibility_workspace(
            context, payload, name="final-conclusion-workspace"
        )
        project_id = str(payload["project_id"])
        normal, secrets = self._text_gateway_environment(context)
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

    def final_front_matter(self, context, payload):
        """Generate only missing/stale machine-owned abstract and keywords."""

        staging = self._staging(context.user_id, context.job_id)
        input_path = staging / "final-front-matter-input.json"
        output_path = staging / "final-front-matter-output.json"
        self._write_json(input_path, payload)
        normal, secrets = self._text_gateway_environment(context)
        self.runner.run(
            [
                sys.executable,
                str(
                    self.root
                    / "skills"
                    / "review-final-audit-release"
                    / "scripts"
                    / "generate_front_matter.py"
                ),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ],
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("final-front-matter-output.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            timeout_seconds=10 * 60,
        )
        return self._result(staging, "final-front-matter-output.json")

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
        normal, secrets = self._image_gateway_environment(context)
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
        blueprint = payload.get("blueprint")
        blueprint = blueprint if isinstance(blueprint, dict) else {}
        raw_topic = str(
            blueprint.get("review_topic")
            or blueprint.get("topic")
            or blueprint.get("review_question")
            or features.get("review_title")
            or project_id
        )
        basis = blueprint.get("classification_basis")
        basis = basis if isinstance(basis, dict) else {}
        editable_text = build_publication_overview_text(
            raw_topic,
            manuscript_title=features.get("display_title") or features.get("manuscript_title"),
            group_by=features.get("group_by") or [],
            classification_rule=(
                features.get("classification_rule")
                or basis.get("description")
                or basis.get("primary_axis")
                or basis.get("overview_axis")
            ),
            has_chirality=bool(features.get("has_chirality")),
            has_reaction_focus=bool(features.get("has_reaction_focus")),
        )
        overview_contract = (
            features.get("overview_content_contract")
            if isinstance(features.get("overview_content_contract"), dict)
            else {}
        )
        editable_text["labels"] = list(
            dict.fromkeys(
                str(value).strip()
                for value in overview_contract.get("modules") or []
                if str(value or "").strip()
            )
        )
        editable_text["primary_axis"] = str(
            overview_contract.get("primary_axis") or ""
        )
        editable_text["evidence_bindings"] = dict(
            overview_contract.get("evidence_bindings") or {}
        )
        return {
            "output_path": str(output),
            "editable_text": editable_text,
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
            env={},
            secret_env={},
            cancel_requested=context.cancellation_requested,
            timeout_seconds=5 * 60,
        )
        return {"output_path": str(output), "download_name": "final_draft.docx"}

    def final_pdf(self, context, payload):
        """Render the released manuscript through the locked LuaLaTeX path."""

        diagnostics = pdf_character_diagnostics(str(payload.get("final_markdown") or ""))
        context.report_partial_result({"pdf_diagnostics": diagnostics})
        staging = self._staging(context.user_id, context.job_id)
        bundle = staging / "pdf-render-bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        input_path = staging / "pdf-render-input.json"
        self._write_json(
            input_path,
            {
                "final_markdown": str(payload.get("final_markdown") or ""),
                "artifact_paths": dict(payload.get("figure_artifact_paths") or {}),
                "language_profile": str(payload.get("language_profile") or "en"),
                "source_final_artifact_id": payload.get("source_final_artifact_id"),
                "source_release_artifact_id": payload.get("source_release_artifact_id"),
            },
        )
        renderer_url = str(
            os.environ.get("REVIEW_WRITER_PDF_RENDERER_URL") or ""
        ).strip()
        if renderer_url:
            self._render_pdf_remotely(context, payload, renderer_url, bundle)
        else:
            relative = Path("pdf-render-bundle")
            self.runner.run(
                [
                    sys.executable,
                    str(
                        self.root
                        / "skills"
                        / "review-final-audit-release"
                        / "scripts"
                        / "render_modern_survey_pdf.py"
                    ),
                    "--input-json",
                    str(input_path),
                    "--output-dir",
                    str(bundle),
                ],
                cwd=self.root,
                staging_directory=staging,
                expected_outputs=tuple(
                    (relative / name).as_posix() for name in self._pdf_output_names()
                ),
                cancel_requested=context.cancellation_requested,
                timeout_seconds=12 * 60,
            )
        profile = str(payload.get("language_profile") or "en")
        return {
            "output_path": str(bundle / "manuscript.pdf"),
            "tex_path": str(bundle / "manuscript.tex"),
            "compile_log_path": str(bundle / "compile.log"),
            "manuscript_state": self._read_bundle_json(bundle, "manuscript_state.json"),
            "render_manifest": self._read_bundle_json(bundle, "render_manifest.json"),
            "pdf_qa": self._read_bundle_json(bundle, "pdf_qa.json"),
            "pdf_diagnostics": diagnostics,
            "download_name": f"final_draft.{profile}.pdf",
        }

    @staticmethod
    def _pdf_output_names():
        return (
            "manuscript.pdf",
            "manuscript.tex",
            "manuscript_state.json",
            "render_manifest.json",
            "pdf_qa.json",
            "compile.log",
        )

    @staticmethod
    def _read_bundle_json(bundle, name):
        return json.loads((bundle / name).read_text(encoding="utf-8"))

    def _render_pdf_remotely(self, context, payload, renderer_url, bundle):
        user_root = self.workspaces.user_root(context.user_id)
        assets = []
        for artifact_id, raw_path in sorted(
            dict(payload.get("figure_artifact_paths") or {}).items()
        ):
            source = self._trusted_user_file(user_root, raw_path)
            if source is None:
                raise RuntimeError("A PDF asset is outside the trusted user workspace.")
            raw = source.read_bytes()
            assets.append(
                {
                    "artifact_id": str(artifact_id),
                    "filename": source.name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "data_base64": base64.b64encode(raw).decode("ascii"),
                }
            )
        request = urllib.request.Request(
            renderer_url,
            data=json.dumps(
                {
                    "final_markdown": str(payload.get("final_markdown") or ""),
                    "language_profile": str(payload.get("language_profile") or "en"),
                    "source_final_artifact_id": payload.get("source_final_artifact_id"),
                    "source_release_artifact_id": payload.get("source_release_artifact_id"),
                    "assets": assets,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer "
                + str(os.environ.get("REVIEW_WRITER_PDF_RENDERER_TOKEN") or ""),
                "Content-Type": "application/json",
                "Accept": "application/zip",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15 * 60) as response:
                archive_bytes = response.read(200 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            self._raise_pdf_renderer_http_error(exc)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"The isolated PDF renderer is unavailable: {exc}") from exc
        if len(archive_bytes) > 200 * 1024 * 1024:
            raise RuntimeError("The isolated PDF renderer response exceeded 200 MiB.")
        self._extract_pdf_bundle(archive_bytes, bundle)

    @staticmethod
    def _raise_pdf_renderer_http_error(exc):
        renderer_body = exc.read(32000).decode("utf-8", "replace")
        if exc.code != 422:
            raise RuntimeError(
                f"The isolated PDF renderer rejected the job (HTTP {exc.code})."
            ) from exc
        try:
            renderer_payload = json.loads(renderer_body)
        except json.JSONDecodeError:
            renderer_payload = {}
        renderer_detail = str(
            renderer_payload.get("detail")
            if isinstance(renderer_payload, dict)
            else ""
        )
        blocking_checks = sorted(
            set(
                re.findall(
                    r'"type"\s*:\s*"([a-z0-9_\-]+)"',
                    renderer_detail,
                    flags=re.IGNORECASE,
                )
            )
        )
        check_suffix = (
            " Blocking checks: " + ", ".join(blocking_checks) + "."
            if blocking_checks
            else ""
        )
        if "Text line contains an invalid character" in renderer_detail:
            message = "PDF compilation failed: an invalid character was rejected. See the source locations in this task's PDF diagnostics; no automatic character repair was applied."
        elif blocking_checks:
            message = "PDF publication checks failed." + check_suffix
        else:
            message = "PDF rendering failed. The renderer rejected an input or compilation failed; this does not necessarily require regenerating the Final manuscript."
        raise WorkflowValidationError(
            message,
            details={"renderer_status": 422, "blocking_checks": blocking_checks},
        ) from exc

    def _extract_pdf_bundle(self, archive_bytes, bundle):
        expected = set(self._pdf_output_names())
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                if set(archive.namelist()) != expected:
                    raise RuntimeError(
                        "The isolated PDF renderer returned an unexpected file set."
                    )
                if sum(archive.getinfo(name).file_size for name in expected) > 200 * 1024 * 1024:
                    raise RuntimeError("The PDF renderer output bundle exceeded 200 MiB.")
                for name in sorted(expected):
                    info = archive.getinfo(name)
                    if info.file_size > 150 * 1024 * 1024:
                        raise RuntimeError("A PDF renderer output exceeded 150 MiB.")
                    (bundle / name).write_bytes(archive.read(info))
        except zipfile.BadZipFile as exc:
            raise RuntimeError(
                "The isolated PDF renderer returned an invalid archive."
            ) from exc
