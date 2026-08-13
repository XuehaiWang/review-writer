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
        self.assertIn("redraw-all", page)


if __name__ == "__main__":
    unittest.main()
