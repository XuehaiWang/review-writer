import unittest
import json
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from view.serve_review_dashboard import (
    build_discovery_command,
    regenerate_section_blueprint,
    start_discovery,
    validate_discovery_start_payload,
    validate_new_project_id,
)


_blueprint_script = Path(__file__).parents[2] / "skills" / "review-section-blueprint" / "scripts" / "init_section_blueprint.py"
_blueprint_spec = importlib.util.spec_from_file_location("section_blueprint_initializer", _blueprint_script)
assert _blueprint_spec and _blueprint_spec.loader
section_blueprint_initializer = importlib.util.module_from_spec(_blueprint_spec)
_blueprint_spec.loader.exec_module(section_blueprint_initializer)


def completed_discovery(command: list[str], *, returncode: int = 0, stderr: str = ""):
    if returncode == 0:
        output_project = Path(command[command.index("--output-project-dir") + 1])
        discovery = output_project / "00_discovery"
        discovery.mkdir(parents=True, exist_ok=True)
        (discovery / "combined_results_by_keyword.json").write_text(
            json.dumps({"topic": command[command.index("--topic") + 1], "results": []}),
            encoding="utf-8",
        )

    class Completed:
        stdout = "discovery complete" if returncode == 0 else ""

    result = Completed()
    result.returncode = returncode
    result.stderr = stderr
    return result


class DiscoveryLauncherTests(unittest.TestCase):
    def test_validate_new_project_id_accepts_safe_slug(self) -> None:
        self.assertEqual(validate_new_project_id("allene-review-2026"), ("allene-review-2026", None))

    def test_validate_new_project_id_rejects_path_and_uppercase_values(self) -> None:
        self.assertIsNone(validate_new_project_id("../escape")[0])
        self.assertIsNone(validate_new_project_id("Allenes")[0])

    def test_build_discovery_command_is_local_only_by_default(self) -> None:
        command = build_discovery_command(Path("D:/review-root"), "allene-review", "axial chiral allenes", False)
        self.assertNotIn("--web-search", command)
        self.assertEqual(command[command.index("--review-root") + 1], "D:\\review-root")

    def test_build_discovery_command_enables_web_search_only_when_requested(self) -> None:
        command = build_discovery_command(Path("D:/review-root"), "allene-review", "axial chiral allenes", True)
        self.assertIn("--web-search", command)

    def test_build_discovery_command_can_write_to_a_staged_project(self) -> None:
        command = build_discovery_command(
            Path("D:/review-root"),
            "allene-review",
            "axial chiral allenes",
            False,
            output_project_dir=Path("D:/staging/project"),
            taxonomy_profile="allenation",
        )
        self.assertEqual(command[command.index("--output-project-dir") + 1], "D:\\staging\\project")
        self.assertEqual(command[command.index("--taxonomy-profile") + 1], "allenation")

    def test_validate_start_payload_rejects_blank_topic(self) -> None:
        value, error = validate_discovery_start_payload(
            {"project_id": "allene-review", "topic": "   ", "web_search": False}, lambda _: False
        )
        self.assertIsNone(value)
        self.assertEqual(error, "Topic is required.")

    def test_validate_start_payload_rejects_existing_project(self) -> None:
        value, error = validate_discovery_start_payload(
            {"project_id": "existing-review", "topic": "allenes", "web_search": False}, lambda _: True
        )
        self.assertIsNone(value)
        self.assertEqual(error, "A project with this ID already exists.")

    def test_validate_start_payload_normalizes_values(self) -> None:
        value, error = validate_discovery_start_payload(
            {"project_id": "allene-review", "topic": "  axial chiral allenes  ", "web_search": 1}, lambda _: False
        )
        self.assertIsNone(error)
        self.assertEqual(value, {"project_id": "allene-review", "topic": "axial chiral allenes", "web_search": True, "restart_existing": False})

    def test_validate_start_payload_allows_an_explicit_existing_discovery_restart(self) -> None:
        value, error = validate_discovery_start_payload(
            {
                "project_id": "existing-review",
                "topic": "new chemistry topic",
                "restart_existing": True,
            },
            lambda _: True,
        )
        self.assertIsNone(error)
        self.assertTrue(value["restart_existing"])

    def test_validate_start_payload_rejects_restart_without_existing_discovery(self) -> None:
        value, error = validate_discovery_start_payload(
            {"project_id": "missing-review", "topic": "new topic", "restart_existing": True},
            lambda _: False,
        )
        self.assertIsNone(value)
        self.assertEqual(error, "The project does not have Discovery results to restart.")

    def test_start_discovery_creates_project_and_returns_runner_output(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = start_discovery(
                root,
                {"project_id": "allene-review", "topic": "axial chiral allenes", "web_search": False},
                completed_discovery,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["project_id"], "allene-review")
            self.assertEqual(result["output"], "discovery complete")
            self.assertTrue(Path(result["query_plan_path"]).is_file())
            self.assertTrue((root / "review-projects" / "allene-review").is_dir())

    def test_restart_replaces_downstream_outputs_but_keeps_library_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            discovery = project / "00_discovery"
            matrix = project / "01_matrix_outline"
            discovery.mkdir(parents=True)
            matrix.mkdir()
            (discovery / "combined_results_by_keyword.json").write_text(
                json.dumps({"topic": "old topic", "results": [{"keyword": "old"}]}),
                encoding="utf-8",
            )
            (matrix / "literature_matrix.json").write_text("old matrix", encoding="utf-8")
            library_pdf = root / "review-library" / "uploads" / "paper.pdf"
            library_pdf.parent.mkdir(parents=True)
            library_pdf.write_bytes(b"pdf")

            result = start_discovery(
                root,
                {"project_id": "demo", "topic": "axial chiral allene synthesis", "restart_existing": True},
                completed_discovery,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["restarted"])
            self.assertFalse(matrix.exists())
            self.assertEqual(library_pdf.read_bytes(), b"pdf")
            refreshed = json.loads(
                (project / "00_discovery" / "combined_results_by_keyword.json").read_text(encoding="utf-8")
            )
            self.assertEqual(refreshed["topic"], "axial chiral allene synthesis")

    def test_failed_restart_keeps_the_existing_project_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            discovery = project / "00_discovery"
            matrix = project / "01_matrix_outline"
            discovery.mkdir(parents=True)
            matrix.mkdir()
            old_discovery = '{"topic":"old topic","results":[]}'
            (discovery / "combined_results_by_keyword.json").write_text(old_discovery, encoding="utf-8")
            (matrix / "literature_matrix.json").write_text("old matrix", encoding="utf-8")

            result = start_discovery(
                root,
                {"project_id": "demo", "topic": "axial chiral allene synthesis", "restart_existing": True},
                lambda command: completed_discovery(command, returncode=1, stderr="provider unavailable"),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                (discovery / "combined_results_by_keyword.json").read_text(encoding="utf-8"),
                old_discovery,
            )
            self.assertEqual((matrix / "literature_matrix.json").read_text(encoding="utf-8"), "old matrix")

    def test_matrix_page_syncs_project_dropdown_when_loading_requested_project(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "matrix.html").read_text(encoding="utf-8")
        self.assertIn("window.reviewUi?.initialProject(projects)", page)
        self.assertIn("$('projectSelect').value=initial.project_id", page)

    def test_stage_navigation_carries_the_selected_project_query(self) -> None:
        script = (Path(__file__).parents[1] / "assets" / "dashboard" / "review-ui.js").read_text(encoding="utf-8")
        self.assertIn("params.set(\"project\", projectId)", script)

    def test_every_downstream_stage_reads_the_project_query(self) -> None:
        dashboard = Path(__file__).parents[1] / "assets" / "dashboard"
        for name in ["matrix.html", "blueprint.html", "sections.html", "figure-review.html", "figures.html", "draft.html", "final.html"]:
            self.assertIn("initialProject", (dashboard / name).read_text(encoding="utf-8"), name)

    def test_regenerate_section_blueprint_runs_initializer_after_outline_is_saved(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "review-projects" / "demo" / "01_matrix_outline"
            stage.mkdir(parents=True)
            (stage / "selected_outline.md").write_text("# Review\n\n## 1. Terminal alkynes\n", encoding="utf-8")
            (stage / "literature_matrix.json").write_text("[]", encoding="utf-8")
            seen: list[list[str]] = []
            class Completed: returncode = 0; stdout = "blueprint ready"; stderr = ""
            with mock.patch(
                "view.serve_review_dashboard.subprocess.run",
                side_effect=lambda command, **_kwargs: seen.append(command) or Completed(),
            ):
                result = regenerate_section_blueprint(root, "demo")
            self.assertEqual(result["status"], "generated")
            self.assertIn("init_section_blueprint.py", " ".join(seen[0]))

    def test_blueprint_initializer_accepts_markdown_numbered_outline_headings(self) -> None:
        sections = section_blueprint_initializer.parse_outline_sections("# Review\n\n## 1. Terminal alkynes\n\n## 2. Propargylic alcohols\n")
        self.assertEqual([section["title"] for section in sections], ["Terminal alkynes", "Propargylic alcohols"])

    def test_blueprint_page_renders_generated_section_blueprint(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "blueprint.html").read_text(encoding="utf-8")
        self.assertIn("payload?.section_blueprint?.sections", page)
        self.assertIn("Writing Plan", page)

    def test_sections_page_uses_imported_tasks_when_drafts_are_not_written(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "sections.html").read_text(encoding="utf-8")
        self.assertIn("taskOnlyMode", page)
        self.assertIn("Writing Requirements", page)

if __name__ == "__main__":
    unittest.main()
