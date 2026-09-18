from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "frontend" / "src" / "features" / "draft" / "DraftPage.tsx"
DRAFT_STATUS = ROOT / "frontend" / "src" / "features" / "draft" / "SectionDialogue.tsx"
FINAL = ROOT / "frontend" / "src" / "features" / "final" / "FinalPage.tsx"
FINAL_STATUS = ROOT / "frontend" / "src" / "features" / "final" / "FinalJobStatus.tsx"
API_CLIENT = ROOT / "frontend" / "src" / "api" / "client.ts"


class DraftFinalFrontendV1Tests(unittest.TestCase):
    def test_draft_uses_only_native_versioned_workflow_routes(self) -> None:
        source = DRAFT.read_text(encoding="utf-8")
        self.assertIn("/api/v1/projects/", source)
        self.assertIn('"/assemble"', source)
        self.assertIn("/dialogue-batch", source)
        self.assertIn("/dialogue-candidates/", source)
        self.assertNotIn("/evaluation-jobs", source)
        self.assertNotIn("/optimization-jobs", source)
        self.assertIn('"/restore"', source)
        self.assertIn("data.versions", source)
        self.assertIn("/api/v1/jobs/", source)
        self.assertNotIn("/api/project/", source)
        self.assertNotIn("/file?path", source)

    def test_draft_renders_dialogue_comparisons_and_persisted_job_status(self) -> None:
        source = DRAFT.read_text(encoding="utf-8")
        for token in (
            "CandidateComparison",
            "ParagraphManualEditor",
            "section_task_states",
            "refetchInterval",
            "base_hashes",
            "useDraftScratch",
        ):
            self.assertIn(token, source + DRAFT_STATUS.read_text(encoding="utf-8") + (DRAFT.parent / "ChapterVersions.tsx").read_text(encoding="utf-8"))

    def test_final_uses_publication_jobs_and_draft_owns_creation(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        for token in (
            "/api/v1/projects/",
            "`/api/v1/projects/${encodeURIComponent(project!.project_id)}/final/${action}-jobs`",
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
        self.assertNotIn('startJob("conclusion")', source)
        self.assertNotIn('startJob("overview")', source)
        composition = (DRAFT.parent / "DraftCompositionPanel.tsx").read_text(encoding="utf-8")
        for token in (
            'open("abstract")', 'open("conclusion")', 'open("overview")',
            'run.mutate(kind)', '/synthesis/', '/overview/adopt',
        ):
            self.assertIn(token, composition)

    def test_native_errors_are_parsed_and_cancel_states_are_shared(self) -> None:
        client = API_CLIENT.read_text(encoding="utf-8")
        self.assertIn("detail.message", client)
        self.assertIn("detail.code", client)
        self.assertIn("error.message", client)
        self.assertIn("error.code", client)
        self.assertIn("throw new ApiError", client)
        for path in (FINAL_STATUS,):
            source = path.read_text(encoding="utf-8")
            self.assertIn('"cancel_requested"', source)
            self.assertIn('status === "failed"', source)

    def test_final_combines_audit_and_release_details_without_losing_reports(self) -> None:
        source = FINAL.read_text(encoding="utf-8")
        self.assertIn('["audit", text("检查详情", "Checks")]', source)
        self.assertNotIn('["release",', source)
        self.assertIn("payload.final_audit_report_md", source)
        self.assertIn("payload.release_report_md", source)


if __name__ == "__main__":
    unittest.main()
