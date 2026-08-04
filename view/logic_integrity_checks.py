"""Regression checks for security, lineage reconciliation, and citation contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = ROOT / "view" / "serve_review_dashboard.py"
SPEC = importlib.util.spec_from_file_location("serve_review_dashboard_logic_integrity", DASHBOARD_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dashboard)

CHART_SCRIPT = ROOT / "skills" / "review-outline-summary-chart" / "scripts" / "generate_review_summary_chart.py"
STATUS_SCRIPT = ROOT / "skills" / "review-writing-orchestrator" / "scripts" / "project_status.py"
AUDIT_SCRIPT = ROOT / "skills" / "review-final-audit-release" / "scripts" / "final_audit_scan.py"


class _RouteValidator:
    validate_route_identifiers = dashboard.DashboardHandler.validate_route_identifiers

    def __init__(self) -> None:
        self.errors: list[object] = []

    def send_json(self, payload: object, status: object = None) -> None:
        self.errors.append((payload, status))


class LogicIntegrityChecks(unittest.TestCase):
    def test_clean_checkout_can_create_empty_runtime_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directories = dashboard.ensure_review_library_workspace(root)
            self.assertTrue(all(path.is_dir() for path in directories))
            self.assertTrue((root / "review-library" / "metadata" / "papers").is_dir())
            self.assertFalse(any((root / "review-library").rglob("*.metadata.json")))

    def test_route_validator_rejects_encoded_project_traversal(self) -> None:
        validator = _RouteValidator()

        self.assertTrue(validator.validate_route_identifiers("/api/project/demo/final"))
        self.assertFalse(
            validator.validate_route_identifiers(
                "/api/project/..%5Creview-projects%5Cdemo/final"
            )
        )
        self.assertFalse(
            validator.validate_route_identifiers(
                "/api/project/demo/figures/..%5C..%5C.env/redraw"
            )
        )
        self.assertEqual(len(validator.errors), 2)

    def test_file_access_blocks_workspace_secrets_but_keeps_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_file = root / "review-projects" / "demo" / "artifact.png"
            secret = root / ".env"
            external = root.parent / "registered-source.pdf"
            project_file.parent.mkdir(parents=True)
            project_file.write_bytes(b"png")
            secret.write_text("SECRET=value", encoding="utf-8")
            external.write_bytes(b"pdf")
            try:
                self.assertTrue(
                    dashboard.file_path_is_authorized(
                        project_file, root, frozenset(), frozenset()
                    )
                )
                self.assertFalse(
                    dashboard.file_path_is_authorized(
                        secret, root, frozenset(), frozenset()
                    )
                )
                self.assertTrue(
                    dashboard.file_path_is_authorized(
                        external, root, frozenset({external.resolve()}), frozenset()
                    )
                )
            finally:
                external.unlink(missing_ok=True)

    def test_current_citation_envelope_maps_callouts(self) -> None:
        module = runpy.run_path(str(CHART_SCRIPT))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "citations.json"
            path.write_text(
                json.dumps(
                    {
                        "project_id": "demo",
                        "entries": [
                            {"callout": 1, "paper_id": "P001"},
                            {"callout": 2, "paper_id": "P002"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                module["load_citation_map"](path),
                {"1": "P001", "2": "P002"},
            )

    def test_final_audit_checks_single_paper_id_entries(self) -> None:
        module = runpy.run_path(str(AUDIT_SCRIPT))
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            matrix = project / "01_matrix_outline"
            draft = project / "04_first_draft"
            final = project / "05_final_audit"
            matrix.mkdir(parents=True)
            draft.mkdir(parents=True)
            final.mkdir(parents=True)
            (matrix / "literature_matrix.json").write_text(
                json.dumps({"rows": [{"paper_id": "P001"}]}), encoding="utf-8"
            )
            (draft / "citations.json").write_text(
                json.dumps({"entries": [{"callout": 1, "paper_id": "P999"}]}),
                encoding="utf-8",
            )
            (final / "final_draft.md").write_text(
                "# Review\n\nBody [1].\n\n## References\n\n[1] Reference.\n",
                encoding="utf-8",
            )
            (project / "03_figure_redraw").mkdir(parents=True)
            (project / "03_figure_redraw" / "skip_reason.md").write_text(
                "User approved a text-only review.", encoding="utf-8"
            )

            scan = module["scan_draft"](project)

            self.assertEqual(scan["unknown_cited_papers"], ["P999"])
            self.assertIn("citations_reference_unknown_papers", scan["issues"])

    def test_full_scope_overview_chart_satisfies_project_status(self) -> None:
        module = runpy.run_path(str(STATUS_SCRIPT))
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            stage = project / "05_final_audit"
            stage.mkdir(parents=True)
            draft = stage / "final_draft.md"
            html = stage / "review_summary_chart.html"
            image = stage / "review_summary_chart.png"
            draft.write_text("# Review\n\n## Introduction\n\nText.\n", encoding="utf-8")
            html.write_text("<html>chart</html>", encoding="utf-8")
            Image.new("RGB", (16, 16), "white").save(image)
            chart = {
                "stats": {
                    "draft_source": str(draft.resolve()),
                    "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
                    "generation_scope": "full",
                    "html_sha256": hashlib.sha256(html.read_bytes()).hexdigest(),
                    "image_manifest": {
                        "full": {
                            "path": image.name,
                            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                        },
                        "sections": [],
                    },
                }
            }
            (stage / "review_summary_chart.json").write_text(
                json.dumps(chart), encoding="utf-8"
            )

            self.assertEqual(module["summary_chart_semantic_issues"](project), [])

    def test_semantic_reconciliation_restores_current_later_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "review-projects" / "demo"
            (project / "04_first_draft").mkdir(parents=True)
            (project / "05_final_audit").mkdir(parents=True)
            (project / "04_first_draft" / "first_draft.md").write_text(
                "# Draft\n", encoding="utf-8"
            )
            (project / "04_first_draft" / "conclusion_generated.md").write_text(
                "## Conclusion\n", encoding="utf-8"
            )
            (project / "05_final_audit" / "overview_figure.png").write_bytes(b"png")
            (project / "05_final_audit" / "final_draft.md").write_text(
                "# Final\n", encoding="utf-8"
            )
            store = dashboard.workflow_store(root)
            for stage_id in (
                "figures",
                "draft",
                "final-conclusion",
                "final-overview-figure",
                "final",
            ):
                store.set_stage_state("demo", stage_id, "stale")

            with patch.object(
                dashboard,
                "project_blueprint_payload",
                return_value={"freshness": {"versioned": True, "stale": False}},
            ), patch.object(
                dashboard,
                "project_sections_payload",
                return_value={"handoff": {"schema_version": 2, "drafts_stale": False}},
            ), patch.object(
                dashboard,
                "project_figure_review_payload",
                return_value={"freshness": {"stale": False}},
            ), patch.object(
                dashboard,
                "project_figures_payload",
                return_value={"freshness": {"stale": False, "selected_count": 1}},
            ), patch.object(
                dashboard,
                "project_draft_payload",
                return_value={"freshness": {"stale": False}},
            ), patch.object(
                dashboard,
                "project_final_payload",
                return_value={
                    "freshness": {"stale": False},
                    "conclusion_current": True,
                    "overview_figure_exists": True,
                    "overview_figure_current": True,
                },
            ):
                dashboard.reconcile_project_semantic_states(root, "demo")

            states = {
                row["stage_id"]: row["status"]
                for row in store.workflow_snapshot("demo")["stage_state"]
            }
            for stage_id in (
                "figures",
                "draft",
                "final-conclusion",
                "final-overview-figure",
                "final",
            ):
                self.assertEqual(states[stage_id], "completed")


if __name__ == "__main__":
    unittest.main()
