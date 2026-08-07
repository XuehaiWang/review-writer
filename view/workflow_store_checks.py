"""Focused regression checks for the durable workflow foundation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflow_store import WorkflowStore


class WorkflowStoreChecks(unittest.TestCase):
    def project(self, root: Path, project_id: str = "demo") -> Path:
        project = root / "review-projects" / project_id
        project.mkdir(parents=True)
        return project

    def test_artifact_versions_are_immutable_and_current_pointer_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            source = project / "00_discovery" / "selected_discovery_results.json"
            output = project / "01_matrix_outline" / "literature_matrix.json"
            source.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            source.write_text('{"papers":[1]}', encoding="utf-8")
            output.write_text('{"rows":[1]}', encoding="utf-8")
            store = WorkflowStore(root)

            source_version = store.register_artifact("demo", source, producer_stage="discovery")
            first = store.register_artifact(
                "demo",
                output,
                producer_stage="matrix",
                dependencies=[str(source_version["artifact_version_id"])],
            )
            output.write_text('{"rows":[1,2]}', encoding="utf-8")
            second = store.register_artifact("demo", output, producer_stage="matrix")

            self.assertNotEqual(first["artifact_version_id"], second["artifact_version_id"])
            snapshot = store.workflow_snapshot("demo")
            current = next(
                row for row in snapshot["current_artifacts"]
                if row["logical_name"] == "01_matrix_outline/literature_matrix.json"
            )
            self.assertEqual(current["artifact_version_id"], second["artifact_version_id"])

    def test_versioned_handoff_uses_hashes_instead_of_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            source = project / "01_matrix_outline" / "section_blueprint.json"
            output = project / "02_section_drafting" / "section_drafts.json"
            handoff = project / "02_section_drafting" / "section_handoff.json"
            source.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            source.write_text('{"sections":[1]}', encoding="utf-8")
            store = WorkflowStore(root)
            store.write_handoff("demo", handoff, "blueprint", [source])
            handoff_payload = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual(
                handoff_payload["source_artifacts"],
                ["01_matrix_outline/section_blueprint.json"],
            )
            output.write_text('{"drafts":[1]}', encoding="utf-8")
            store.complete_handoff("demo", handoff, [output], producer_stage="sections")

            self.assertFalse(store.handoff_freshness("demo", handoff, [output])["stale"])
            output.write_text('{"drafts":[1]}', encoding="utf-8")
            self.assertFalse(store.handoff_freshness("demo", handoff, [output])["stale"])
            source.write_text('{"sections":[2]}', encoding="utf-8")
            state = store.handoff_freshness("demo", handoff, [output])
            self.assertTrue(state["stale"])
            self.assertEqual(state["outdated_sources"], [str(source.resolve())])

    def test_complete_handoff_upgrades_legacy_named_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            source = project / "01_matrix_outline" / "section_blueprint.json"
            output = project / "02_section_drafting" / "section_drafts.json"
            handoff = project / "02_section_drafting" / "section_handoff.json"
            source.parent.mkdir(parents=True)
            output.parent.mkdir(parents=True)
            source.write_text('{"sections":[1]}', encoding="utf-8")
            output.write_text('{"drafts":[1]}', encoding="utf-8")
            handoff.write_text(
                json.dumps(
                    {
                        "source_stage": "blueprint",
                        "source_blueprint": str(source.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            store = WorkflowStore(root)

            upgraded = store.complete_handoff(
                "demo",
                handoff,
                [output],
                producer_stage="sections",
            )

            self.assertEqual(upgraded["schema_version"], 2)
            self.assertEqual(len(upgraded["source_versions"]), 1)
            self.assertFalse(store.handoff_freshness("demo", handoff, [output])["stale"])

    def test_stage_runs_and_jobs_survive_a_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            blueprint = project / "01_matrix_outline" / "section_blueprint.json"
            tasks = project / "02_section_drafting" / "section_tasks.json"
            blueprint.parent.mkdir(parents=True)
            tasks.parent.mkdir(parents=True)
            blueprint.write_text('{"sections":[1]}', encoding="utf-8")
            tasks.write_text("[]", encoding="utf-8")
            store = WorkflowStore(root)

            run_id = store.start_stage_run("demo", "sections")
            store.finish_stage_run(run_id, "completed", metadata={"section_count": 1})
            saved_job = store.save_job(
                "demo",
                "figure-redraw-all",
                {"status": "running", "completed": 3, "total": 30},
            )

            reloaded = WorkflowStore(root)
            snapshot = reloaded.workflow_snapshot("demo")
            section_state = next(row for row in snapshot["stage_state"] if row["stage_id"] == "sections")
            self.assertEqual(section_state["status"], "completed")
            self.assertEqual(snapshot["recent_stage_runs"][0]["run_id"], run_id)
            self.assertEqual(snapshot["recent_stage_runs"][0]["status"], "completed")
            self.assertEqual(
                reloaded.load_job("demo", "figure-redraw-all")["job_id"],
                saved_job["job_id"],
            )
            self.assertTrue(
                any(
                    row["stage_id"] == "draft"
                    and row["depends_on_stage_id"] == "figures"
                    and row["dependency_kind"] == "required"
                    for row in snapshot["stage_dependencies"]
                )
            )

    def test_changed_artifact_invalidates_transitive_downstream_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            source = project / "02_section_drafting" / "section_drafts.json"
            source.parent.mkdir(parents=True)
            source.write_text('{"sections":[1]}', encoding="utf-8")
            store = WorkflowStore(root)
            store.register_artifact("demo", source, producer_stage="sections")
            for stage_id in ("figures", "draft", "final"):
                store.set_stage_state("demo", stage_id, "completed")

            source.write_text('{"sections":[2]}', encoding="utf-8")
            store.register_artifact("demo", source, producer_stage="sections")
            snapshot = store.workflow_snapshot("demo")
            states = {row["stage_id"]: row["status"] for row in snapshot["stage_state"]}

            self.assertEqual(states["figures"], "stale")
            self.assertEqual(states["draft"], "stale")
            self.assertEqual(states["final"], "stale")

    def test_bootstrap_marks_existing_outputs_as_legacy_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self.project(root)
            output = project / "01_matrix_outline" / "literature_matrix.json"
            output.parent.mkdir(parents=True)
            output.write_text('{"rows":[1]}', encoding="utf-8")
            store = WorkflowStore(root)

            store.bootstrap_project("demo")
            snapshot = store.workflow_snapshot("demo")
            matrix = next(row for row in snapshot["stage_state"] if row["stage_id"] == "matrix")

            self.assertEqual(matrix["status"], "legacy_unverified")


if __name__ == "__main__":
    unittest.main()
