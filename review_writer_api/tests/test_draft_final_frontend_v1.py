from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "frontend" / "src" / "features" / "draft" / "DraftPage.tsx"
DRAFT_STATUS = ROOT / "frontend" / "src" / "features" / "draft" / "DraftJobStatus.tsx"
FINAL = ROOT / "frontend" / "src" / "features" / "final" / "FinalPage.tsx"
FINAL_STATUS = ROOT / "frontend" / "src" / "features" / "final" / "FinalJobStatus.tsx"
API_CLIENT = ROOT / "frontend" / "src" / "api" / "client.ts"


class DraftFinalFrontendV1Tests(unittest.TestCase):
    def test_draft_uses_only_native_versioned_workflow_routes(self) -> None:
        source = DRAFT.read_text(encoding="utf-8")
        self.assertIn("/api/v1/projects/", source)
        self.assertIn("/draft/assemble", source)
        self.assertIn("/draft/evaluation-jobs", source)
        self.assertIn("/draft/optimization-jobs", source)
        self.assertIn("/rewrite-jobs", source)
        self.assertIn("/rewrite-candidates/", source)
        self.assertIn("/draft/restore", source)
        self.assertIn("payload.versions", source)
        self.assertIn("/api/v1/jobs/", source)
        self.assertNotIn("/api/project/", source)
        self.assertNotIn("/file?path", source)

    def test_draft_renders_live_score_issue_paragraph_images_and_job_status(self) -> None:
        source = DRAFT.read_text(encoding="utf-8")
        for token in (
            "payload.quality.score",
            "issue.paragraph?.text",
            "issue.paragraph?.images",
            "payload.rewrite_states",
            "rewriteActive",
            "DraftJobStatus",
            "Generating and scoring candidate",
            "Candidate generated and scored",
        ):
            self.assertIn(token, source + DRAFT_STATUS.read_text(encoding="utf-8"))

    def test_final_uses_native_jobs_and_editable_overview_text(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        for token in (
            "/api/v1/projects/",
            "/final/overview-text",
            "`/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/${action}-jobs`",
            'startJob("conclusion")',
            'startJob("overview")',
            'startJob("build")',
            'startJob("export")',
            "/api/v1/jobs/",
            "overview_text",
            "release_report_md",
            "release_current",
            "currentJobId",
            "/cancel",
            "cancel.mutate()",
            "FinalJobStatus",
        ):
            self.assertIn(token, source)
        self.assertNotIn("/api/project/", source)
        self.assertNotIn("/file?path", source)

    def test_native_errors_are_parsed_and_cancel_states_are_shared(self) -> None:
        client = API_CLIENT.read_text(encoding="utf-8")
        self.assertIn("detail.message", client)
        self.assertIn("detail.code", client)
        self.assertIn("error.message", client)
        self.assertIn("error.code", client)
        self.assertIn("throw new ApiError", client)
        for path in (DRAFT_STATUS, FINAL_STATUS):
            source = path.read_text(encoding="utf-8")
            self.assertIn('"cancel_requested"', source)
            self.assertIn('status === "failed"', source)

    def test_final_has_distinct_audit_and_release_views(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        self.assertIn('["audit", text("终稿审计", "Final audit")]', source)
        self.assertIn('["release", text("发布报告", "Release report")]', source)
        self.assertIn("payload.final_audit_report_md", source)
        self.assertIn("payload.release_report_md", source)


if __name__ == "__main__":
    unittest.main()
