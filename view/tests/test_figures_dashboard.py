import unittest
from pathlib import Path


class FiguresDashboardTests(unittest.TestCase):
    def test_redrawn_output_uses_manifest_redrawn_image(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "figures.html").read_text(encoding="utf-8")
        self.assertIn("row.redrawn_image", page)
        self.assertIn("在线全图 SVG 编辑", page)
        self.assertIn("manual-arrow-edit", page)
        self.assertIn("rejected_preview_image", page)
        self.assertIn("全部 AI 重绘", page)
        self.assertIn("/figures/jobs", page)
        self.assertIn("/api/v1/jobs/${encodeURIComponent(jobId)}/cancel", page)
        self.assertIn("/figures/approve-successful", page)
        self.assertIn("/api/v1/jobs/${encodeURIComponent(previous.job_id)}/retry", page)
        self.assertIn("retry_of_job_id:previous.job_id", page)
        self.assertIn("Saving batch human-review decisions", page)
        self.assertIn("The human-review record was saved", page)
        self.assertIn("reviewI18n?.getLanguage", page)
        self.assertIn("previous.origin==='single'", page)
        self.assertIn("review-language-change", page)
        self.assertIn("bulkApprovalNoticeState", page)
        self.assertIn("singleApprovalNoticeState", page)


if __name__ == "__main__":
    unittest.main()
