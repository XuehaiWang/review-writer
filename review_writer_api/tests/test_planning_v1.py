from __future__ import annotations

import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from docx import Document

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class PlanningV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        database_url = f"sqlite+pysqlite:///{(root / 'planning.sqlite3').as_posix()}"
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
            project = Project(user_id=first.id, slug="planning", topic="Copper allenation")
            hidden = Project(user_id=second.id, slug="hidden", topic="Hidden")
            session.add_all([project, hidden])
            session.flush()
            papers = []
            for index in range(1, 36):
                paper_id = f"P{index:03d}"
                papers.append(
                    LibraryPaper(
                        user_id=first.id,
                        paper_id=paper_id,
                        content_sha256=f"{index:064x}",
                        original_filename=f"{paper_id}.pdf",
                        title=f"Paper {index}",
                        authors_json=[f"Author {index}"],
                        keywords_json=["allenation", "copper"],
                        tags_json={"reaction_type": "allenation"},
                        metadata_json={
                            "paper_id": paper_id,
                            "title": {"value": f"Paper {index}"},
                            "authors": {"value": [f"Author {index}"]},
                            "keywords": {"value": ["allenation", "copper"]},
                            "abstract": {"value": f"Evidence for {paper_id}."},
                            "year": {"value": 2024},
                        },
                        pdf_relative_path=f"review-library/uploads/{paper_id}.pdf",
                        markdown_relative_path=f"review-library/markdown/{paper_id}.md",
                    )
                )
            session.add_all(papers)
            self.project_id = str(project.id)
            self.hidden_project_id = str(hidden.id)
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
        self.app = create_app(
            settings,
            principal_provider=lambda: self.current,
            session_factory_override=self.sessions,
        )
        self._seed_discovery(range(1, 36))

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def headers() -> dict[str, str]:
        return {"Origin": "http://testserver"}

    def _review(self, selected: set[int]) -> dict:
        return {
            "project_id": self.project_id,
            "topic": "Copper allenation",
            "selection_mode": "explicit",
            "results": [
                {
                    "keyword": "allenation",
                    "keep": True,
                    "local_results": [
                        {
                            "paper_id": f"P{index:03d}",
                            "title": f"Paper {index}",
                            "score": 100 - index,
                            "role": "core_candidate",
                            "selected_for_matrix": index in selected,
                        }
                        for index in range(1, 36)
                    ],
                    "web_results": [],
                }
            ],
        }

    def _seed_discovery(self, selected) -> None:
        service = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        artifact, run = service._write_json_artifact(
            self.first,
            self.project_id,
            stage_id="discovery",
            logical_name="discovery/review.json",
            payload=self._review(set(selected)),
            make_current=False,
        )
        repository.save_discovery_atomically(
            self.first.user_id,
            self.project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=0,
            status="review",
        )
        with TestClient(self.app) as client:
            response = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": 1},
                headers=self.headers(),
            )
        self.assertEqual(200, response.status_code, response.text)

    def planning(self, client: TestClient) -> dict:
        response = client.get(f"/api/v1/projects/{self.project_id}/planning")
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def choose_outline(self, client: TestClient, style: str = "substrate") -> dict:
        current = self.planning(client)
        response = client.put(
            f"/api/v1/projects/{self.project_id}/planning/outline",
            json={"revision": current["matrix_revision"], "outline_style": style},
            headers=self.headers(),
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_matrix_contains_entire_confirmed_selection(self) -> None:
        with TestClient(self.app) as client:
            payload = self.planning(client)
        self.assertEqual(35, len(payload["literature_matrix"]["rows"]))
        self.assertEqual(35, payload["matrix_sync"]["selected_paper_count"])

    def test_reconfirmation_replaces_matrix_selection(self) -> None:
        with TestClient(self.app) as client:
            self.choose_outline(client)
            blueprint_revision = self.app.state.workflow_repository.get_stage_state(
                self.first.user_id, self.project_id, "blueprint"
            )
            if blueprint_revision is None:
                response = client.post(
                    f"/api/v1/projects/{self.project_id}/planning/blueprint",
                    json={"revision": 0},
                    headers=self.headers(),
                )
                self.assertEqual(200, response.status_code, response.text)
            review = client.get(f"/api/v1/projects/{self.project_id}/discovery").json()
            for row in review["results"][0]["local_results"]:
                row["selected_for_matrix"] = row["paper_id"] in {"P001", "P035"}
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/discovery",
                json={"revision": review["revision"], "results": review["results"]},
                headers=self.headers(),
            ).json()
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": saved["revision"]},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            planning = self.planning(client)
        self.assertEqual(["P001", "P035"], [row["paper_id"] for row in planning["literature_matrix"]["rows"]])
        self.assertIsNone(
            self.app.state.workflow_repository.get_current_artifact(
                self.first.user_id, self.project_id, "blueprint/section_blueprint.json"
            )
        )

    def test_matrix_row_edit_uses_revision(self) -> None:
        with TestClient(self.app) as client:
            current = self.planning(client)
            response = client.put(
                f"/api/v1/projects/{self.project_id}/planning/matrix/P001",
                json={
                    "revision": current["matrix_revision"],
                    "main_content": "A" * 320,
                    "mark_complete": True,
                },
                headers=self.headers(),
            )
            self.assertEqual(200, response.status_code, response.text)
            stale = client.put(
                f"/api/v1/projects/{self.project_id}/planning/matrix/P002",
                json={"revision": current["matrix_revision"], "main_content": "changed"},
                headers=self.headers(),
            )
            reloaded = self.planning(client)
        self.assertEqual(409, stale.status_code, stale.text)
        rows = {row["paper_id"]: row for row in reloaded["literature_matrix"]["rows"]}
        self.assertEqual("full_reading_complete", rows["P001"]["matrix_status"])
        self.assertEqual("", rows["P002"]["main_content"])

    def test_builtin_outline_loads_editable_content(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "reaction")
        self.assertIn("##", selected["selected_outline_md"])
        self.assertTrue(selected["outline_complete"])
        self.assertEqual("reaction", selected["outline_style"])

    def test_custom_outline_starts_blank(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "custom")
        self.assertEqual("", selected["selected_outline_md"])
        self.assertFalse(selected["outline_complete"])

    def test_manual_outline_save_versions_content(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "custom")
            outline = "# Review\n\n## 1. Introduction\nAssigned papers: P001, P002.\nPurpose: scope.\n"
            response = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={
                    "revision": selected["matrix_revision"],
                    "outline_style": "custom",
                    "outline_md": outline,
                },
                headers=self.headers(),
            )
            self.assertEqual(200, response.status_code, response.text)
            saved = response.json()
            stale = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={
                    "revision": selected["matrix_revision"],
                    "outline_style": "custom",
                    "outline_md": outline.replace("scope", "stale"),
                },
                headers=self.headers(),
            )
            reloaded = self.planning(client)
        self.assertEqual(409, stale.status_code)
        self.assertEqual(saved["outline_artifact_id"], reloaded["outline_selection"]["artifact_id"])
        self.assertIn("Purpose: scope.", reloaded["selected_outline_md"])

    def test_saved_outline_appears_in_comparison(self) -> None:
        with TestClient(self.app) as client:
            self.choose_outline(client, "catalyst")
            payload = self.planning(client)
        candidates = {item["candidate_id"]: item for item in payload["outline_candidates"]}
        self.assertIn("saved-current", candidates)
        self.assertEqual(payload["selected_outline_md"], candidates["saved-current"]["outline_md"])

    def test_reference_outline_is_registered(self) -> None:
        raw = "# Reference\n\n## 1. Mechanisms\nAssigned papers: P001.\nPurpose: compare.\n".encode()
        with TestClient(self.app) as client:
            current = self.planning(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/planning/reference-outlines",
                json={
                    "revision": current["matrix_revision"],
                    "filename": "reference.md",
                    "content_base64": base64.b64encode(raw).decode(),
                },
                headers=self.headers(),
            )
            self.assertEqual(201, response.status_code, response.text)
            payload = self.planning(client)
        candidate = response.json()["candidate"]
        self.assertTrue(candidate["source_artifact_id"])
        self.assertIn(candidate["candidate_id"], {item["candidate_id"] for item in payload["reference_outline_candidates"]})

    def test_reference_docx_headings_become_an_editable_candidate(self) -> None:
        document = Document()
        document.add_heading("1. Mechanistic organization", level=1)
        document.add_paragraph("Reference discussion.")
        stream = BytesIO()
        document.save(stream)
        with TestClient(self.app) as client:
            current = self.planning(client)
            response = client.post(
                f"/api/v1/projects/{self.project_id}/planning/reference-outlines",
                json={
                    "revision": current["matrix_revision"],
                    "filename": "reference.docx",
                    "content_base64": base64.b64encode(stream.getvalue()).decode(),
                },
                headers=self.headers(),
            )
        self.assertEqual(201, response.status_code, response.text)
        candidate = response.json()["candidate"]
        self.assertEqual("heading_extraction", candidate["analysis_mode"])
        self.assertIn("## 1. Mechanistic organization", candidate["outline_md"])

    def test_blueprint_uses_current_matrix_and_outline(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "substrate")
            response = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(200, response.status_code, response.text)
            blueprint = response.json()["section_blueprint"]
        matrix_ids = {f"P{index:03d}" for index in range(1, 36)}
        assigned = {paper_id for section in blueprint["sections"] for paper_id in section["major_papers"]}
        self.assertTrue(assigned)
        self.assertLessEqual(assigned, matrix_ids)
        self.assertEqual(selected["outline_artifact_id"], blueprint["source_outline_artifact_id"])

    def test_blueprint_confirmation_advances_to_sections(self) -> None:
        with TestClient(self.app) as client:
            self.choose_outline(client, "reaction")
            generated = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(200, generated.status_code, generated.text)
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint/confirm",
                json={"revision": generated.json()["blueprint_revision"]},
                headers=self.headers(),
            )
            project = client.get(f"/api/v1/projects/{self.project_id}").json()
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual("sections", project["current_stage"])

    def test_planning_contract_exposes_composite_tabs(self) -> None:
        with TestClient(self.app) as client:
            payload = self.planning(client)
        self.assertEqual(["matrix", "blueprint"], [tab["id"] for tab in payload["workspace"]["tabs"]])
        self.assertEqual("文献矩阵", payload["workspace"]["tabs"][0]["labels"]["zh"])
        self.assertEqual("Blueprint", payload["workspace"]["tabs"][1]["labels"]["en"])

    def test_planning_api_and_container_are_user_isolated(self) -> None:
        self.assertIs(
            self.app.state.planning_service,
            self.app.state.container.planning_service,
        )
        self.current = self.second
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/planning")
        self.assertEqual(404, response.status_code, response.text)


if __name__ == "__main__":
    unittest.main()
