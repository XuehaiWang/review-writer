from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class DiscoveryV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        database_url = f"sqlite+pysqlite:///{(root / 'discovery.sqlite3').as_posix()}"
        self.engine = create_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            first = User(email="first@example.com", display_name="First", password_hash="hash")
            second = User(email="second@example.com", display_name="Second", password_hash="hash")
            session.add_all([first, second])
            session.flush()
            project = Project(user_id=first.id, slug="copper", topic="Copper")
            other_project = Project(user_id=second.id, slug="hidden", topic="Hidden")
            catalog = [
                LibraryPaper(
                    user_id=first.id,
                    paper_id=paper_id,
                    content_sha256=f"{paper_id.lower():0<64}"[:64],
                    original_filename=f"{paper_id}.pdf",
                    title=f"Catalog {paper_id}",
                    authors_json=[f"Catalog author {paper_id}"],
                    keywords_json=[f"catalog-keyword-{paper_id}"],
                    tags_json={"reaction_type": "catalog"},
                    metadata_json={
                        "paper_id": paper_id,
                        "title": {"value": f"Catalog {paper_id}"},
                        "authors": {"value": [f"Catalog author {paper_id}"]},
                        "keywords": {"value": [f"catalog-keyword-{paper_id}"]},
                        "abstract": {"value": f"Catalog abstract {paper_id}"},
                        "year": {"value": 2024},
                        "journal": {"value": "Catalog Journal"},
                        "doi": {"value": f"10.9/{paper_id.lower()}"},
                        "structured_tags": {
                            "value": {
                                "catalyst_or_method": "copper catalysis",
                                "reaction_type": "allenation",
                            }
                        },
                    },
                    pdf_relative_path=f"review-library/uploads/{paper_id}.pdf",
                    markdown_relative_path=f"review-library/markdown/{paper_id}.md",
                )
                for paper_id in ("P001", "P002", "P003")
            ]
            session.add_all([project, other_project, *catalog])
            session.flush()
            self.project_id = str(project.id)
            self.other_project_id = str(other_project.id)
            self.first = Principal(str(first.id), frozenset({Role.USER}), first.email)
            self.second = Principal(str(second.id), frozenset({Role.USER}), second.email)
        self.current = self.first
        settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url=database_url,
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=root / "users",
        )

        def discovery_builder(_context, payload):
            if payload["topic"] == "forced failure":
                raise RuntimeError("provider unavailable")
            return {
                "project_id": payload["project_id"],
                "topic": payload["topic"],
                "selection_mode": "explicit",
                "results": [
                    {
                        "keyword": "copper",
                        "category": "catalyst_or_method",
                        "keep": True,
                        "local_results": [
                            {
                                "paper_id": "P001",
                                "title": "High score",
                                "score": 99,
                                "role": "core_candidate",
                                "selected_for_matrix": False,
                                "base_tags": {
                                    "catalyst_or_method": "copper catalysis",
                                    "reaction_type": "allenation",
                                },
                                "project_tag_assessment": {
                                    "topic_fingerprint": "topic-hash",
                                    "suggested_tags": {
                                        "catalyst_or_method": ["copper catalysis"],
                                        "reaction_type": ["allenation"],
                                    },
                                    "evidence": [],
                                },
                                "confirmed_project_tags": {},
                                "tag_review_status": "pending",
                            },
                            {
                                "paper_id": "P002",
                                "title": "Second",
                                "score": 80,
                                "role": "uncertain",
                                "selected_for_matrix": False,
                            },
                        ],
                        "web_results": [
                            {
                                "candidate_id": "crossref:10.1/example",
                                "doi": "10.1/example",
                                "title": "External",
                                "source": "crossref",
                                "selected_for_matrix": False,
                            }
                        ],
                    },
                    {
                        "keyword": "allenation",
                        "category": "reaction_type",
                        "keep": True,
                        "local_results": [
                            {
                                "paper_id": "P001",
                                "title": "High score",
                                "score": 90,
                                "role": "core_candidate",
                                "selected_for_matrix": False,
                                "base_tags": {
                                    "catalyst_or_method": "copper catalysis",
                                    "reaction_type": "allenation",
                                },
                                "project_tag_assessment": {
                                    "topic_fingerprint": "topic-hash",
                                    "suggested_tags": {
                                        "catalyst_or_method": ["copper catalysis"],
                                        "reaction_type": ["allenation"],
                                    },
                                    "evidence": [],
                                },
                                "confirmed_project_tags": {},
                                "tag_review_status": "pending",
                            },
                            {
                                "paper_id": "P003",
                                "title": "Third",
                                "score": 70,
                                "role": "background",
                                "selected_for_matrix": False,
                            },
                        ],
                        "web_results": [],
                    },
                ],
            }

        self.app = create_app(
            settings,
            principal_provider=lambda: self.current,
            session_factory_override=self.sessions,
            native_workflow_overrides={"discovery.search": discovery_builder},
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str = "request") -> dict[str, str]:
        return {"Origin": "http://testserver", "Idempotency-Key": key}

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return payload
            time.sleep(0.02)
        self.fail("Job did not finish.")

    def discover(self, client: TestClient, topic: str = "Copper allenation") -> dict:
        response = client.post(
            f"/api/v1/projects/{self.project_id}/discovery/jobs",
            json={"topic": topic, "keywords": "copper, allenation", "web_search": True},
            headers=self.headers(f"discovery:{topic}"),
        )
        self.assertEqual(202, response.status_code, response.text)
        return self.wait_job(client, response.json()["id"])

    def test_new_topic_builds_candidate_pool(self) -> None:
        with TestClient(self.app) as client:
            job = self.discover(client)
            review = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            project = client.get("/api/v1/projects").json()["items"][0]
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(4, job["progress_current"])
        self.assertEqual(4, job["progress_total"])
        self.assertEqual(3, review["statistics"]["candidate_count"])
        self.assertEqual(4, review["statistics"]["keyword_hit_count"])
        self.assertEqual(0, review["statistics"]["selected_count"])
        self.assertEqual("review", project["discovery_status"])

    def test_failed_restart_preserves_current_project(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 1},
                headers=self.headers(),
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers(),
            ).json()
            before = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            failed = self.discover(client, "forced failure")
            after = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
        self.assertEqual("failed", failed["status"])
        self.assertEqual(before["artifact_id"], after["artifact_id"])
        self.assertEqual("Copper allenation", after["topic"])
        self.assertEqual(
            confirmed["matrix_artifact_id"],
            self.app.state.workflow_repository.get_current_artifact(
                self.first.user_id, self.project_id, "matrix/literature_matrix.json"
            ).id,
        )
        self.assertEqual(
            "review",
            self.app.state.workflow_repository.get_stage_state(
                self.first.user_id, self.project_id, "matrix"
            ).status,
        )

    def test_successful_restart_preserves_downstream_current_artifacts_and_states(self) -> None:
        service = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        published: dict[str, str] = {}
        with TestClient(self.app) as client:
            self.discover(client)
            for stage, logical_name in (
                ("matrix", "matrix/literature_matrix.json"),
                ("blueprint", "blueprint/outline.json"),
            ):
                artifact, run = service._write_json_artifact(
                    self.first,
                    self.project_id,
                    stage_id=stage,
                    logical_name=logical_name,
                    payload={"stage": stage},
                )
                repository.compare_and_set_stage(
                    self.first.user_id,
                    self.project_id,
                    stage,
                    0,
                    status="approved",
                    current_run_id=run.id,
                )
                self.assertEqual(
                    artifact.id,
                    repository.get_current_artifact(self.first.user_id, self.project_id, logical_name).id,
                )
                published[logical_name] = artifact.id
            self.discover(client, "Replacement topic")

        self.assertEqual(
            "Copper",
            repository.get_owned_project(
                self.first.user_id, self.project_id
            ).topic,
        )

        for stage, logical_name in (
            ("matrix", "matrix/literature_matrix.json"),
            ("blueprint", "blueprint/outline.json"),
        ):
            self.assertEqual(
                published[logical_name],
                repository.get_current_artifact(
                    self.first.user_id, self.project_id, logical_name
                ).id,
            )
            self.assertEqual(
                "approved",
                repository.get_stage_state(self.first.user_id, self.project_id, stage).status,
            )

    def test_confirming_unchanged_inputs_reuses_matrix_and_keeps_downstream_current(self) -> None:
        service = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 1},
                headers=self.headers("initial-selection"),
            ).json()
            initial = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers("initial-confirm"),
            ).json()
            blueprint, blueprint_run = service._write_json_artifact(
                self.first,
                self.project_id,
                stage_id="blueprint",
                logical_name="blueprint/outline.json",
                payload={"source_matrix_artifact_id": initial["matrix_artifact_id"]},
            )
            repository.compare_and_set_stage(
                self.first.user_id,
                self.project_id,
                "blueprint",
                0,
                status="approved",
                current_run_id=blueprint_run.id,
            )
            current_payload, _artifact = service._read_current(
                self.first, self.project_id, "discovery/review.json"
            )
            restarted = service.replace_from_job(
                self.first,
                self.project_id,
                {"topic": "Copper allenation"},
                current_payload,
            )
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": restarted["revision"]},
                headers=self.headers("unchanged-confirm"),
            ).json()

        self.assertTrue(confirmed["matrix_reused"])
        self.assertFalse(confirmed["downstream_stale"])
        self.assertEqual(initial["matrix_artifact_id"], confirmed["matrix_artifact_id"])
        self.assertEqual(
            blueprint.id,
            repository.get_current_artifact(
                self.first.user_id, self.project_id, "blueprint/outline.json"
            ).id,
        )
        self.assertEqual(
            "approved",
            repository.get_stage_state(
                self.first.user_id, self.project_id, "blueprint"
            ).status,
        )

    def test_confirming_changed_inputs_marks_downstream_stale_without_hiding_it(self) -> None:
        service = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 1},
                headers=self.headers("changed-initial-selection"),
            ).json()
            initial = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers("changed-initial-confirm"),
            ).json()
            retained_note = "Retained full-reading evidence. " * 14
            edited_matrix = client.put(
                f"/api/v1/projects/{self.project_id}/planning/matrix/P001",
                json={
                    "revision": initial["matrix_revision"],
                    "main_content": retained_note,
                    "most_relevant_figure": None,
                    "mark_complete": True,
                },
                headers=self.headers("changed-matrix-note"),
            )
            self.assertEqual(200, edited_matrix.status_code, edited_matrix.text)
            blueprint, blueprint_run = service._write_json_artifact(
                self.first,
                self.project_id,
                stage_id="blueprint",
                logical_name="blueprint/outline.json",
                payload={"source_matrix_artifact_id": initial["matrix_artifact_id"]},
            )
            repository.compare_and_set_stage(
                self.first.user_id,
                self.project_id,
                "blueprint",
                0,
                status="approved",
                current_run_id=blueprint_run.id,
            )
            self.discover(client, "Replacement topic")
            changed_selection = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 2},
                headers=self.headers("changed-selection"),
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": changed_selection["revision"]},
                headers=self.headers("changed-confirm"),
            ).json()

        self.assertFalse(confirmed["matrix_reused"])
        self.assertTrue(confirmed["downstream_stale"])
        self.assertNotEqual(initial["matrix_artifact_id"], confirmed["matrix_artifact_id"])
        retained_row = next(
            row for row in confirmed["matrix"]["rows"] if row["paper_id"] == "P001"
        )
        self.assertEqual(retained_note.strip(), retained_row["main_content"])
        self.assertEqual("full_reading_complete", retained_row["matrix_status"])
        self.assertEqual(
            blueprint.id,
            repository.get_current_artifact(
                self.first.user_id, self.project_id, "blueprint/outline.json"
            ).id,
        )
        blueprint_state = repository.get_stage_state(
            self.first.user_id, self.project_id, "blueprint"
        )
        self.assertEqual("stale", blueprint_state.status)
        self.assertEqual(blueprint_run.id, blueprint_state.current_run_id)
        self.assertEqual(
            "Replacement topic",
            repository.get_owned_project(
                self.first.user_id, self.project_id
            ).topic,
        )

    def test_failed_atomic_restart_keeps_previous_discovery_and_downstream_current(self) -> None:
        service = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.discover(client)
            matrix_artifact, matrix_run = service._write_json_artifact(
                self.first,
                self.project_id,
                stage_id="matrix",
                logical_name="matrix/literature_matrix.json",
                payload={"rows": [{"paper_id": "P001"}]},
            )
            repository.compare_and_set_stage(
                self.first.user_id,
                self.project_id,
                "matrix",
                0,
                status="approved",
                current_run_id=matrix_run.id,
            )
            before = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            with patch.object(
                repository,
                "replace_discovery_atomically",
                side_effect=RuntimeError("injected atomic replacement failure"),
            ):
                failed = self.discover(client, "Replacement must roll back")
            after = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()

        self.assertEqual("failed", failed["status"])
        self.assertEqual(before["artifact_id"], after["artifact_id"])
        self.assertEqual("Copper allenation", after["topic"])
        self.assertEqual(
            matrix_artifact.id,
            repository.get_current_artifact(
                self.first.user_id,
                self.project_id,
                "matrix/literature_matrix.json",
            ).id,
        )
        self.assertEqual(
            "approved",
            repository.get_stage_state(
                self.first.user_id, self.project_id, "matrix"
            ).status,
        )

    def test_identical_restart_reuses_content_without_losing_atomic_transition(self) -> None:
        service = self.app.state.discovery_service
        with TestClient(self.app) as client:
            self.discover(client)
            before = client.get(
                f"/api/v1/projects/{self.project_id}/discovery"
            ).json()
            current_payload, _artifact = service._read_current(
                self.first, self.project_id, "discovery/review.json"
            )
            restarted = service.replace_from_job(
                self.first,
                self.project_id,
                {"topic": before["topic"]},
                current_payload,
            )
            after = client.get(
                f"/api/v1/projects/{self.project_id}/discovery"
            ).json()

        self.assertEqual(before["artifact_id"], restarted["artifact_id"])
        self.assertEqual(before["artifact_id"], after["artifact_id"])
        self.assertEqual(before["revision"] + 1, after["revision"])

    def test_viewing_candidate_does_not_select_it(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            first = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            second = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
        self.assertEqual(0, first["statistics"]["selected_count"])
        self.assertEqual(first["revision"], second["revision"])

    def test_explicit_selection_updates_duplicate_hits(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            response = client.put(
                f"/api/v1/projects/{self.project_id}/discovery/selection/P001",
                json={"selected": True},
                headers=self.headers(),
            )
            review = response.json()
        hits = [
            row
            for group in review["results"]
            for row in group["local_results"]
            if row["paper_id"] == "P001"
        ]
        self.assertEqual(2, len(hits))
        self.assertTrue(all(row["selected_for_matrix"] for row in hits))
        self.assertEqual(1, review["statistics"]["selected_count"])

    def test_discovery_payloads_reject_ambiguous_json_types(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            ambiguous_boolean = client.put(
                f"/api/v1/projects/{self.project_id}/discovery/selection/P001",
                json={"selected": "false"},
                headers=self.headers(),
            )
            invalid_revision = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": "not-an-integer"},
                headers=self.headers(),
            )
        self.assertEqual(422, ambiguous_boolean.status_code)
        self.assertEqual(422, invalid_revision.status_code)

    def test_application_container_exposes_native_task7_services(self) -> None:
        self.assertIs(self.app.state.library_service, self.app.state.container.library_service)
        self.assertIs(self.app.state.discovery_service, self.app.state.container.discovery_service)

    def test_top_n_selects_ranked_unique_papers_and_clear_keeps_pool(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 2},
                headers=self.headers(),
            ).json()
            cleared_response = client.delete(
                f"/api/v1/projects/{self.project_id}/discovery/selection",
                headers=self.headers(),
            )
            self.assertEqual(200, cleared_response.status_code, cleared_response.text)
            cleared = cleared_response.json()
        self.assertEqual(["P001", "P002"], selected["selected_paper_ids"])
        self.assertEqual(0, cleared["statistics"]["selected_count"])
        self.assertEqual(3, cleared["statistics"]["candidate_count"])

    def test_keyword_and_role_review_persists_and_save_versions(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            current = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            current["results"][0]["keep"] = False
            current["results"][1]["local_results"][1]["role"] = "supporting_candidate"
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/discovery",
                json={"revision": current["revision"], "results": current["results"]},
                headers=self.headers(),
            ).json()
            reloaded = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
        self.assertNotEqual(current["artifact_id"], saved["artifact_id"])
        self.assertFalse(reloaded["results"][0]["keep"])
        self.assertEqual(
            "supporting_candidate",
            reloaded["results"][1]["local_results"][1]["role"],
        )

    def test_confirmed_project_tags_are_project_scoped_and_enter_matrix(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            current = client.get(
                f"/api/v1/projects/{self.project_id}/discovery"
            ).json()
            for group in current["results"]:
                for row in group["local_results"]:
                    if row["paper_id"] != "P001":
                        continue
                    row["selected_for_matrix"] = True
                    row["confirmed_project_tags"] = {
                        "catalyst_or_method": ["project copper catalyst"],
                        "reaction_type": ["project-specific allenation"],
                    }
                    row["tag_review_status"] = "confirmed"
                    # These fields are immutable evidence and must not be
                    # replaceable through the review endpoint.
                    row["base_tags"] = {"reaction_type": "tampered"}
                    row["project_tag_assessment"]["suggested_tags"] = {
                        "reaction_type": ["tampered suggestion"]
                    }
            saved_response = client.put(
                f"/api/v1/projects/{self.project_id}/discovery",
                json={
                    "revision": current["revision"],
                    "results": current["results"],
                },
                headers=self.headers(),
            )
            self.assertEqual(200, saved_response.status_code, saved_response.text)
            saved = saved_response.json()
            confirmed_response = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": saved["revision"]},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed_response.status_code, confirmed_response.text)
            confirmed = confirmed_response.json()

        p001_hits = [
            row
            for group in saved["results"]
            for row in group["local_results"]
            if row["paper_id"] == "P001"
        ]
        self.assertEqual(1, saved["statistics"]["tag_reviewed_candidate_count"])
        self.assertTrue(all(row["tag_review_status"] == "confirmed" for row in p001_hits))
        self.assertTrue(
            all(row["base_tags"]["reaction_type"] == "allenation" for row in p001_hits)
        )
        self.assertTrue(
            all(
                row["project_tag_assessment"]["suggested_tags"]["reaction_type"]
                == ["allenation"]
                for row in p001_hits
            )
        )
        matrix_row = confirmed["matrix"]["rows"][0]
        self.assertEqual("confirmed", matrix_row["project_tag_review_status"])
        self.assertEqual(
            ["project-specific allenation"],
            matrix_row["project_tags"]["reaction_type"],
        )
        self.assertEqual("allenation", matrix_row["base_tags"]["reaction_type"])

    def test_project_tag_suggestions_enter_matrix_without_manual_confirmation(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.put(
                f"/api/v1/projects/{self.project_id}/discovery/selection/P001",
                json={"selected": True},
                headers=self.headers(),
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers(),
            ).json()
        matrix_row = confirmed["matrix"]["rows"][0]
        self.assertEqual("automatic", matrix_row["project_tag_review_status"])
        self.assertEqual(
            {
                "catalyst_or_method": ["copper catalysis"],
                "reaction_type": ["allenation"],
            },
            matrix_row["project_tags"],
        )

    def test_failed_atomic_save_keeps_previous_discovery_pointer_and_revision(self) -> None:
        repository = self.app.state.workflow_repository
        with TestClient(self.app) as client:
            self.discover(client)
            before = client.get(
                f"/api/v1/projects/{self.project_id}/discovery"
            ).json()
            changed = before["results"]
            changed[0]["keep"] = False
            with patch.object(
                repository,
                "save_discovery_atomically",
                side_effect=RuntimeError("injected atomic save failure"),
            ):
                with self.assertRaises(RuntimeError):
                    client.put(
                        f"/api/v1/projects/{self.project_id}/discovery",
                        json={
                            "revision": before["revision"],
                            "results": changed,
                        },
                        headers=self.headers(),
                    )
            after = client.get(
                f"/api/v1/projects/{self.project_id}/discovery"
            ).json()

        self.assertEqual(before["artifact_id"], after["artifact_id"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertTrue(after["results"][0]["keep"])

    def test_confirm_synchronizes_exact_selection(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/selection/top",
                json={"count": 2},
                headers=self.headers(),
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers(),
            ).json()
        self.assertEqual(2, confirmed["matrix_sync"]["selected_paper_count"])
        self.assertEqual(2, confirmed["matrix_sync"]["synchronized_paper_count"])
        self.assertEqual(["P001", "P002"], confirmed["matrix_sync"]["selected_paper_ids"])
        self.assertNotIn("selection_fingerprint", confirmed["matrix_sync"])
        self.assertTrue(confirmed["matrix_sync"]["selection_current"])
        self.assertEqual(["P001", "P002"], [row["paper_id"] for row in confirmed["matrix"]["rows"]])
        self.assertEqual("Catalog P001", confirmed["matrix"]["rows"][0]["title"])
        self.assertEqual(["Catalog author P001"], confirmed["matrix"]["rows"][0]["authors"])

    def test_confirmation_rejects_selected_paper_absent_from_owners_catalog(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.put(
                f"/api/v1/projects/{self.project_id}/discovery/selection/P003",
                json={"selected": True},
                headers=self.headers(),
            ).json()
            with self.sessions.begin() as session:
                paper = session.scalar(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == uuid.UUID(self.first.user_id),
                        LibraryPaper.paper_id == "P003",
                    )
                )
                session.delete(paper)
            rejected = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": selected["revision"]},
                headers=self.headers(),
            )
        self.assertEqual(422, rejected.status_code)
        self.assertEqual("DISCOVERY_SELECTION_NOT_IN_LIBRARY", rejected.json()["error"]["code"])

    def test_failed_atomic_confirmation_keeps_old_matrix_and_discovery_state_current(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            selected = client.post(f"/api/v1/projects/{self.project_id}/discovery/selection/top", json={"count": 1}, headers=self.headers()).json()
            before = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            with patch.object(self.app.state.workflow_repository, "confirm_discovery_atomically", side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    client.post(f"/api/v1/projects/{self.project_id}/discovery/confirm", json={"revision": selected["revision"]}, headers=self.headers())
            after = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
        self.assertEqual(before["artifact_id"], after["artifact_id"])
        self.assertIsNone(self.app.state.workflow_repository.get_current_artifact(self.first.user_id, self.project_id, "matrix/literature_matrix.json"))

    def test_external_results_preserve_source_identity_and_project_isolation(self) -> None:
        with TestClient(self.app) as client:
            self.discover(client)
            review = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            external = review["results"][0]["web_results"][0]
            self.assertEqual("crossref:10.1/example", external["candidate_id"])
            self.assertEqual("crossref", external["source"])

            self.current = self.second
            hidden = client.get(f"/api/v1/projects/{self.project_id}/discovery")
            self.assertEqual(404, hidden.status_code)


if __name__ == "__main__":
    unittest.main()
