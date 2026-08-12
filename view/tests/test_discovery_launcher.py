import unittest
import json
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

from view.serve_review_dashboard import (
    build_discovery_command,
    generate_project_blueprint,
    generate_project_matrix,
    initialize_figure_candidates,
    initialize_first_draft,
    initialize_overview_schemes,
    project_draft_payload,
    initialize_section_tasks,
    save_selected_outline,
    analyze_outline_text,
    start_discovery,
    validate_discovery_start_payload,
    validate_new_project_id,
)


_blueprint_script = Path(__file__).parents[2] / "skills" / "review-section-blueprint" / "scripts" / "init_section_blueprint.py"
_blueprint_spec = importlib.util.spec_from_file_location("section_blueprint_initializer", _blueprint_script)
assert _blueprint_spec and _blueprint_spec.loader
section_blueprint_initializer = importlib.util.module_from_spec(_blueprint_spec)
_blueprint_spec.loader.exec_module(section_blueprint_initializer)
_inventory_script = Path(__file__).parents[2] / "skills" / "review-section-drafting-figure-picking" / "scripts" / "build_paper_figure_inventory.py"
_inventory_spec = importlib.util.spec_from_file_location("figure_inventory_builder", _inventory_script)
assert _inventory_spec and _inventory_spec.loader
figure_inventory_builder = importlib.util.module_from_spec(_inventory_spec)
_inventory_spec.loader.exec_module(figure_inventory_builder)


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
        self.assertEqual(value, {"project_id": "allene-review", "topic": "axial chiral allenes", "web_search": True})

    def test_start_discovery_creates_project_and_returns_runner_output(self) -> None:
        class Completed:
            returncode = 0
            stdout = "discovery complete"
            stderr = ""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = start_discovery(
                root,
                {"project_id": "allene-review", "topic": "axial chiral allenes", "web_search": False},
                lambda command: Completed(),
            )
            self.assertEqual(result, {"ok": True, "project_id": "allene-review", "output": "discovery complete"})
            self.assertTrue((root / "review-projects" / "allene-review").is_dir())

    def test_generate_project_matrix_requires_confirmed_discovery(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            discovery = project / "00_discovery"
            discovery.mkdir(parents=True)
            (discovery / "human_check_state.json").write_text('{"status":"pending"}', encoding="utf-8")
            result = generate_project_matrix(root, "demo")
            self.assertEqual(result, {"ok": False, "error": "Confirm Discovery before generating Matrix."})
            self.assertFalse((project / "01_matrix_outline").exists())

    def test_generate_project_matrix_writes_required_artifacts_for_confirmed_local_paper(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            discovery = project / "00_discovery"
            metadata_dir = root / "review-library" / "metadata" / "papers"
            discovery.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
            (discovery / "human_check_state.json").write_text('{"status":"confirmed"}', encoding="utf-8")
            (discovery / "selected_discovery_results.json").write_text(json.dumps({"local_papers":[{"paper_id":"P001","keep":True,"role":"core_candidate"}]}), encoding="utf-8")
            (metadata_dir / "P001.metadata.json").write_text(json.dumps({
                "paper_id":"P001", "title":{"value":"Allene synthesis"}, "authors":{"value":["A. Author"]},
                "abstract":{"value":"Evidence from the original paper."},
                "structured_tags":{"value":{"substrate":"terminal alkynes", "product":"axial-chiral allenes"}},
                "source_paths":{"markdown":"paper.md"}
            }), encoding="utf-8")
            result = generate_project_matrix(root, "demo")
            stage = project / "01_matrix_outline"
            self.assertEqual(result["ok"], True)
            self.assertEqual(result["paper_count"], 1)
            self.assertTrue(all((stage / name).exists() for name in [
                "paper_reading_notes.json", "literature_matrix.json", "literature_matrix.csv", "outline_options.md", "matrix_outline_report.md"
            ]))
            matrix = json.loads((stage / "literature_matrix.json").read_text(encoding="utf-8"))
            self.assertEqual(matrix[0]["paper_id"], "P001")

    def test_matrix_page_syncs_project_dropdown_when_loading_requested_project(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "matrix.html").read_text(encoding="utf-8")
        self.assertIn("$('projectSelect').value=id;", page)

    def test_stage_navigation_carries_the_selected_project_query(self) -> None:
        script = (Path(__file__).parents[1] / "assets" / "dashboard" / "review-ui.js").read_text(encoding="utf-8")
        self.assertIn("params.set(\"project\", projectId)", script)

    def test_every_downstream_stage_reads_the_project_query(self) -> None:
        dashboard = Path(__file__).parents[1] / "assets" / "dashboard"
        for name in ["matrix.html", "blueprint.html", "sections.html", "figures.html", "overview_schemes.html", "draft.html", "final.html"]:
            self.assertIn("requestedProject", (dashboard / name).read_text(encoding="utf-8"), name)

    def test_save_selected_outline_requires_generated_outline_options(self) -> None:
        with TemporaryDirectory() as tmp:
            result = save_selected_outline(Path(tmp), "demo")
            self.assertEqual(result, {"ok": False, "error": "Generate Matrix outline options before selecting one."})

    def test_save_selected_outline_writes_chosen_outline(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "review-projects" / "demo" / "01_matrix_outline"
            stage.mkdir(parents=True)
            (stage / "outline_options.md").write_text("# Option 1\n\nSubstrate-classified", encoding="utf-8")
            result = save_selected_outline(root, "demo")
            self.assertEqual(result, {"ok": True, "project_id": "demo"})
            self.assertEqual((stage / "selected_outline.md").read_text(encoding="utf-8"), "# Option 1\n\nSubstrate-classified\n")

    def test_analyze_outline_text_extracts_heading_framework(self) -> None:
        analysis = analyze_outline_text("# Introduction\n## Terminal alkynes\n## Propargylic alcohols\n# Outlook")
        self.assertEqual(analysis["framework"], "entry-classified")
        self.assertEqual(analysis["headings"], ["Introduction", "Terminal alkynes", "Propargylic alcohols", "Outlook"])

    def test_generate_project_blueprint_runs_initializer_after_outline_is_saved(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "review-projects" / "demo" / "01_matrix_outline"
            stage.mkdir(parents=True)
            (stage / "selected_outline.md").write_text("# Review\n\n## 1. Terminal alkynes\n", encoding="utf-8")
            (stage / "literature_matrix.json").write_text("[]", encoding="utf-8")
            seen: list[list[str]] = []
            class Completed: returncode = 0; stdout = "blueprint ready"; stderr = ""
            result = generate_project_blueprint(root, "demo", lambda command: seen.append(command) or Completed())
            self.assertTrue(result["ok"])
            self.assertEqual(result["project_id"], "demo")
            self.assertIn("init_section_blueprint.py", " ".join(seen[0]))

    def test_blueprint_initializer_accepts_markdown_numbered_outline_headings(self) -> None:
        sections = section_blueprint_initializer.parse_outline_sections("# Review\n\n## 1. Terminal alkynes\n\n## 2. Propargylic alcohols\n")
        self.assertEqual([section["title"] for section in sections], ["Terminal alkynes", "Propargylic alcohols"])

    def test_blueprint_page_offers_a_generate_blueprint_action(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "blueprint.html").read_text(encoding="utf-8")
        self.assertIn('id="generateBlueprintBtn"', page)
        self.assertIn('/generate-blueprint', page)

    def test_initialize_section_tasks_copies_blueprint_sections_into_sections_stage(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            matrix_stage = root / "review-projects" / "demo" / "01_matrix_outline"
            matrix_stage.mkdir(parents=True)
            (matrix_stage / "section_blueprint.json").write_text(json.dumps({"sections": [{
                "section_id": "sec1", "title": "Terminal alkynes", "section_thesis": "Compare terminal alkyne routes.",
                "major_papers": ["P001"], "review_claims": [{"claim": "Copper routes are useful."}],
                "figure_or_table_needs": [{"type": "scheme", "purpose": "Show the key reaction."}], "avoid_patterns": ["paper-by-paper list"],
            }]}), encoding="utf-8")
            result = initialize_section_tasks(root, "demo")
            tasks = json.loads((root / "review-projects" / "demo" / "02_section_drafting" / "section_tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(result, {"ok": True, "project_id": "demo", "task_count": 1})
            self.assertEqual(tasks[0]["allowed_papers"], ["P001"])

    def test_initialize_section_tasks_distributes_matrix_papers_when_blueprint_has_no_assignments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "review-projects" / "demo" / "01_matrix_outline"
            stage.mkdir(parents=True)
            (stage / "section_blueprint.json").write_text(json.dumps({"sections": [
                {"section_id": "sec1", "title": "First", "section_thesis": "", "major_papers": [], "review_claims": [], "figure_or_table_needs": [], "avoid_patterns": []},
                {"section_id": "sec2", "title": "Second", "section_thesis": "", "major_papers": [], "review_claims": [], "figure_or_table_needs": [], "avoid_patterns": []},
            ]}), encoding="utf-8")
            (stage / "literature_matrix.json").write_text(json.dumps([{"paper_id": "P001"}, {"paper_id": "P002"}]), encoding="utf-8")
            initialize_section_tasks(root, "demo")
            tasks = json.loads((root / "review-projects" / "demo" / "02_section_drafting" / "section_tasks.json").read_text(encoding="utf-8"))
            self.assertEqual([task["allowed_papers"] for task in tasks], [["P001"], ["P002"]])

    def test_sections_page_uses_imported_tasks_when_drafts_are_not_written(self) -> None:
        page = (Path(__file__).parents[1] / "assets" / "dashboard" / "sections.html").read_text(encoding="utf-8")
        self.assertIn("function entries()", page)
        self.assertIn("Blueprint task", page)

    def test_initialize_figure_candidates_creates_unresolved_records_from_section_tasks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "review-projects" / "demo" / "02_section_drafting"
            stage.mkdir(parents=True)
            (stage / "section_tasks.json").write_text(json.dumps([{
                "section_id": "sec1", "heading": "Terminal alkynes", "allowed_papers": ["P001"]
            }]), encoding="utf-8")
            result = initialize_figure_candidates(root, "demo")
            candidates = json.loads((stage / "figure_candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(result, {"ok": True, "project_id": "demo", "candidate_count": 1})
            self.assertEqual(candidates[0]["resolution_status"], "needs_source_review")
            self.assertFalse(candidates[0]["manuscript_selected"])

    def test_initialize_overview_schemes_covers_every_matrix_paper_and_keeps_missing_images_for_review(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            matrix_stage = project / "01_matrix_outline"
            figures_stage = project / "02_section_drafting"
            matrix_stage.mkdir(parents=True)
            figures_stage.mkdir(parents=True)
            distractor = root / "P001-distractor.png"
            scheme = root / "P001-main-scheme.png"
            distractor.write_bytes(b"image")
            scheme.write_bytes(b"image")
            (matrix_stage / "literature_matrix.json").write_text(json.dumps([
                {"paper_id": "P001", "title": "Paper one"},
                {"paper_id": "P002", "title": "Paper two"},
            ]), encoding="utf-8")
            (figures_stage / "figure_candidates.json").write_text(json.dumps([
                {"paper_id": "P001", "source_label": "Embedded image p1-1", "source_image_path": str(distractor), "source_caption_text": "Embedded source-paper image extracted automatically; human review must verify its chemical relevance."},
            ]), encoding="utf-8")
            (figures_stage / "paper_figure_inventory.json").write_text(json.dumps({"papers": [{
                "paper_id": "P001", "title": "Paper one", "top_candidates": [
                    {"source_label": "Embedded image p1-1", "source_image_path": str(distractor), "source_caption_text": "Embedded source-paper image extracted automatically; human review must verify its chemical relevance."},
                    {"source_label": "Scheme 1", "source_image_path": str(scheme), "source_caption_text": "Scheme 1. Enantioselective conversion of propargylic carbonate substrates to axially chiral allenes."},
                ],
            }]}), encoding="utf-8")

            result = initialize_overview_schemes(root, "demo")
            manifest = json.loads((project / "03a_paper_overview_schemes" / "main_reaction_bw" / "selection_manifest.json").read_text(encoding="utf-8"))
            rows = {row["paper_id"]: row for row in manifest["figures"]}

            self.assertEqual(result["paper_count"], 2)
            self.assertEqual(set(rows), {"P001", "P002"})
            self.assertEqual(rows["P001"]["review_status"], "pending")
            self.assertTrue(rows["P001"]["needs_human_check"])
            self.assertEqual(rows["P001"]["selected_source_figure"]["source_image_path"], str(scheme))
            self.assertEqual(rows["P002"]["review_status"], "needs_source_review")

    def test_initialize_overview_schemes_does_not_auto_select_an_uncaptioned_embedded_image(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            matrix_stage = project / "01_matrix_outline"
            figures_stage = project / "02_section_drafting"
            matrix_stage.mkdir(parents=True)
            figures_stage.mkdir(parents=True)
            image = root / "embedded.png"
            image.write_bytes(b"image")
            (matrix_stage / "literature_matrix.json").write_text(json.dumps([{"paper_id": "P001", "title": "Paper one"}]), encoding="utf-8")
            (figures_stage / "paper_figure_inventory.json").write_text(json.dumps({"papers": [{
                "paper_id": "P001", "top_candidates": [{
                    "source_label": "Embedded image p1-1", "source_image_path": str(image),
                    "source_caption_text": "Embedded source-paper image extracted automatically; human review must verify its chemical relevance.",
                }],
            }]}), encoding="utf-8")

            initialize_overview_schemes(root, "demo")
            manifest = json.loads((project / "03a_paper_overview_schemes" / "main_reaction_bw" / "selection_manifest.json").read_text(encoding="utf-8"))
            row = manifest["figures"][0]

            self.assertIsNone(row["selected_source_figure"])
            self.assertEqual(row["review_status"], "needs_source_review")
            self.assertEqual(row["selection_status"], "insufficient_evidence")

    def test_initialize_overview_schemes_marks_no_image_placeholder_as_missing_source_image(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            matrix_stage = project / "01_matrix_outline"
            figures_stage = project / "02_section_drafting"
            matrix_stage.mkdir(parents=True)
            figures_stage.mkdir(parents=True)
            (matrix_stage / "literature_matrix.json").write_text(json.dumps([{"paper_id": "P001", "title": "Paper one"}]), encoding="utf-8")
            (figures_stage / "paper_figure_candidates.json").write_text(json.dumps([{
                "paper_id": "P001", "status": "no_useful_figure", "no_useful_figure_reason": "No image candidates were found.",
            }]), encoding="utf-8")

            initialize_overview_schemes(root, "demo")
            manifest = json.loads((project / "03a_paper_overview_schemes" / "main_reaction_bw" / "selection_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["figures"][0]["selection_status"], "missing_source_image")

    def test_initialize_first_draft_requires_written_section_files(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(initialize_first_draft(Path(tmp), "demo"), {
                "ok": False, "error": "Write at least one section draft before initializing Draft."
            })

    def test_initialize_first_draft_requires_an_approved_materialized_overview_scheme(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            section_dir = root / "review-projects" / "demo" / "02_section_drafting" / "sections"
            section_dir.mkdir(parents=True)
            (section_dir / "sec1.md").write_text("## Section\n\nText.", encoding="utf-8")

            result = initialize_first_draft(root, "demo")

            self.assertEqual(result, {
                "ok": False,
                "error": "Approve at least one Overview Scheme with a generated black-and-white image before initializing Draft.",
            })

    def test_draft_payload_exposes_only_approved_materialized_overview_schemes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "review-projects" / "demo"
            stage = project / "03a_paper_overview_schemes" / "main_reaction_bw"
            stage.mkdir(parents=True)
            approved_image = root / "approved.png"
            approved_image.write_bytes(b"image")
            (stage / "selection_manifest.json").write_text(json.dumps({"figures": [
                {"paper_id": "P001", "review_status": "approved", "generated_image": str(approved_image)},
                {"paper_id": "P002", "review_status": "pending", "generated_image": str(approved_image)},
            ]}), encoding="utf-8")

            payload = project_draft_payload(root, "demo")

            self.assertEqual([row["paper_id"] for row in payload["redrawn_figures"]], ["P001"])

    def test_downstream_pages_offer_project_scoped_handoff_actions(self) -> None:
        dashboard = Path(__file__).parents[1] / "assets" / "dashboard"
        self.assertIn('/initialize-figures', (dashboard / "sections.html").read_text(encoding="utf-8"))
        self.assertIn('/initialize-draft', (dashboard / "draft.html").read_text(encoding="utf-8"))
        self.assertIn('/initialize-overview-schemes', (dashboard / "figures.html").read_text(encoding="utf-8"))

    def test_figure_inventory_resolves_a_local_pdf_by_legacy_filename(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_pdf = root / "paper.pdf"
            local_pdf.write_bytes(b"%PDF-test")
            self.assertEqual(figure_inventory_builder.resolve_local_pdf("/home/ps/old/paper.pdf", root), local_pdf)


if __name__ == "__main__":
    unittest.main()
