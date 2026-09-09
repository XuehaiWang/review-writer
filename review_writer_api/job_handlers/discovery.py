"""Discovery-stage native job handlers."""

from __future__ import annotations

import json
import os
import sys


class DiscoveryJobHandlers:
    def discovery_search(self, context, payload):
        staging = self._staging(context.user_id, context.job_id)
        project_slug = self.workspaces.project_path(
            context.user_id, str(payload["project_id"])
        ).name
        screening_cache_dir = self.workspaces.trusted_user_directory(
            context.user_id,
            ".review-writer",
            "cache",
            "discovery-screening",
        )
        query_plan_cache_file = screening_cache_dir / f"{project_slug}.query-plan.json"
        normal, secrets = self._text_gateway_environment(context)
        source_normal, source_secrets = self._paper_source_environment()
        normal.update(source_normal)
        secrets.update(source_secrets)
        source_status_file = staging / "source-search-status.json"
        report_progress = getattr(context, "report_progress", None)
        if callable(report_progress):
            report_progress(1, 6)
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
            "--query-plan-cache",
            str(query_plan_cache_file),
            "--output-project-dir",
            str(staging),
            "--source-status-file",
            str(source_status_file),
        ]
        if payload.get("taxonomy_profile"):
            command.extend(["--taxonomy-profile", str(payload["taxonomy_profile"])])
        if payload.get("web_search"):
            command.append("--web-search")
            if str(
                os.environ.get("REVIEW_DISCOVERY_MULTI_SOURCE_ENABLED", "true")
            ).strip().casefold() in {"0", "false", "no", "off"}:
                command.extend(["--sources", "crossref"])
        if callable(report_progress):
            report_progress(2, 6)
        previous_source_progress = ""

        def publish_discovery_progress() -> None:
            nonlocal previous_source_progress
            if callable(report_progress):
                report_progress(2, 6)
            if not source_status_file.is_file():
                return
            try:
                source_progress = json.loads(source_status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return
            if isinstance(source_progress, dict):
                fingerprint = json.dumps(
                    source_progress, ensure_ascii=False, sort_keys=True
                )
                if fingerprint == previous_source_progress:
                    return
                previous_source_progress = fingerprint
                context.report_partial_result({"source_progress": source_progress})

        self.runner.run(
            command,
            cwd=self.root,
            staging_directory=staging,
            expected_outputs=("00_discovery/combined_results_by_keyword.json",),
            env=normal,
            secret_env=secrets,
            cancel_requested=context.cancellation_requested,
            progress_callback=(
                publish_discovery_progress
                if callable(report_progress)
                else None
            ),
        )
        if callable(report_progress):
            report_progress(3, 6)
        return self._result(staging, "00_discovery/combined_results_by_keyword.json")
