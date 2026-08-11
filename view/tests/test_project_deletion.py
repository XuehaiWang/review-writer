import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from view import serve_review_dashboard as dashboard


REVIEW_UI_PATH = Path(__file__).parents[1] / "assets" / "dashboard" / "review-ui.js"


class ProjectDeletionTests(unittest.TestCase):
    def test_delete_review_project_removes_only_named_child(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "review-projects" / "unused"
            target.mkdir(parents=True)
            (target / "artifact.txt").write_text("delete me", encoding="utf-8")
            sibling = root / "review-projects" / "keep"
            sibling.mkdir(parents=True)

            result = dashboard.delete_review_project(root, "unused")

            self.assertEqual(result, {"deleted_project_id": "unused"})
            self.assertFalse(target.exists())
            self.assertTrue(sibling.exists())

    def test_delete_review_project_rejects_path_traversal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaises(ValueError):
                dashboard.delete_review_project(root, "../sentinel.txt")

            self.assertTrue(sentinel.exists())

    def test_delete_review_project_reports_missing_project(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                dashboard.delete_review_project(Path(tmp), "missing")


class ProjectDeletionUiTests(unittest.TestCase):
    def test_shared_dashboard_ui_requires_typed_project_id_before_delete(self) -> None:
        script = REVIEW_UI_PATH.read_text(encoding="utf-8")

        self.assertIn("delete-project", script)
        self.assertIn("window.prompt", script)
        self.assertIn('method: "DELETE"', script)


if __name__ == "__main__":
    unittest.main()
