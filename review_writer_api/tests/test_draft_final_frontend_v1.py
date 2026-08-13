from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "view" / "assets" / "dashboard" / "draft.html"
FINAL = ROOT / "view" / "assets" / "dashboard" / "final.html"


class DraftFinalFrontendV1Tests(unittest.TestCase):
    def test_draft_uses_only_native_versioned_workflow_routes(self) -> None:
        source = DRAFT.read_text(encoding="utf-8")
        self.assertIn("/api/v1/projects/", source)
        self.assertIn("/draft/assemble", source)
        self.assertIn("/draft/evaluation-jobs", source)
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
            "quality.score",
            "issue.paragraph.text",
            "issue.paragraph.images",
            "rewrite_states",
            "review-language-change",
            "正在重写",
            "Rewrite complete",
        ):
            self.assertIn(token, source)

    def test_final_uses_native_jobs_and_editable_overview_text(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        for token in (
            "/api/v1/projects/",
            "/final/conclusion-jobs",
            "/final/overview-jobs",
            "/final/overview-text",
            "/final/build",
            "/final/export-jobs",
            "/api/v1/jobs/",
            "overview_text",
            "release_report_md",
            "release_current",
            "review-language-change",
            "currentJobId",
            "/cancel",
            "cancelCurrentJob",
            "runCancel",
            "if(currentJobId)return",
            "currentJobId===jobId",
        ):
            self.assertIn(token, source)
        self.assertNotIn("/api/project/", source)
        self.assertNotIn("/file?path", source)

    def test_native_errors_are_parsed_and_have_runtime_bilingual_copy(self) -> None:
        for path in (DRAFT, FINAL):
            source = path.read_text(encoding="utf-8")
            self.assertIn("data.error?.message", source)
            self.assertIn("data.error?.code", source)
            self.assertIn("STATE_CONFLICT", source)
            self.assertIn("工作流已发生变化，请刷新后重试。", source)
            self.assertNotIn("detail?.message||data.error||", source)
            self.assertIn("job.error_code", source)
            self.assertIn("cancel_requested", source)
            self.assertNotIn("'cancelling'", source)
            self.assertIn("JOB_EXECUTION_FAILED", source)

    def test_final_has_distinct_audit_and_release_views(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        self.assertIn('data-doc="audit"', source)
        self.assertIn('data-doc="release"', source)
        self.assertIn("payload.final_audit_report_md", source)
        self.assertIn("payload.release_report_md", source)


if __name__ == "__main__":
    unittest.main()
