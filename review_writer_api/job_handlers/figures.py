"""Figure-stage native job handlers."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from review_writer_core.providers import DEFAULT_IMAGE_MODEL


class FigureJobHandlers:
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
        normal, secrets = self._image_gateway_environment(context)
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
