from __future__ import annotations

import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

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
            substrates = (
                "aromatic substrates",
                "small-molecule substrates",
                "biomolecular substrates",
            )
            catalysts = (
                "transition-metal catalysis",
                "organocatalysis",
                "photochemical methods",
            )
            reactions = (
                "cross-coupling",
                "addition reactions",
                "cyclization and annulation",
            )
            for index in range(1, 36):
                paper_id = f"P{index:03d}"
                structured_tags = {
                    "substrate": substrates[(index - 1) % len(substrates)],
                    "catalyst_or_method": catalysts[(index - 1) % len(catalysts)],
                    "reaction_type": reactions[(index - 1) % len(reactions)],
                }
                papers.append(
                    LibraryPaper(
                        user_id=first.id,
                        paper_id=paper_id,
                        content_sha256=f"{index:064x}",
                        original_filename=f"{paper_id}.pdf",
                        title=f"Paper {index}",
                        authors_json=[f"Author {index}"],
                        keywords_json=["allenation", "copper"],
                        tags_json=structured_tags,
                        metadata_json={
                            "paper_id": paper_id,
                            "title": {"value": f"Paper {index}"},
                            "authors": {"value": [f"Author {index}"]},
                            "keywords": {"value": ["allenation", "copper"]},
                            "abstract": {"value": f"Evidence for {paper_id}."},
                            "year": {"value": 2024},
                            "structured_tags": {"value": structured_tags},
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

    @staticmethod
    def isolated_reference_analysis(
        _principal,
        _project_id,
        *,
        candidate_id,
        safe_name,
        raw,
        matrix,
    ) -> dict:
        del safe_name, raw
        paper_ids = [row["paper_id"] for row in matrix["rows"]]
        representatives = paper_ids[:6]
        outline = (
            "# Selected Outline\n\n"
            "Scientific content source: current literature Matrix only.\n\n"
            "## Introduction and scope\n"
            f"Assigned papers: {', '.join(representatives)}.\n"
            "Purpose: define the current review scope.\n\n"
            "## 1. Copper allenation evidence\n"
            f"Assigned papers: {', '.join(paper_ids)}.\n"
            "Purpose: compare evidence from the current Matrix.\n\n"
            "## Conclusion and outlook\n"
            f"Assigned papers: {', '.join(representatives)}.\n"
            "Purpose: synthesize limitations and future directions.\n"
        )
        return {
            "candidate_id": candidate_id,
            "analysis_mode": "ai_style_only_transfer_v2",
            "content_source": "current_matrix_only",
            "reference_content_reused": False,
            "content_firewall": {
                "transfer_received_reference_text": False,
                "all_heading_levels_content_source": "current_matrix_only",
            },
            "reference_structure_metrics": {"heading_count": 3},
            "writing_style": {"organization_pattern": "progressive comparison"},
            "outline_md": outline,
        }

    def test_matrix_contains_entire_confirmed_selection(self) -> None:
        with TestClient(self.app) as client:
            payload = self.planning(client)
        self.assertEqual(35, len(payload["literature_matrix"]["rows"]))
        self.assertEqual(35, payload["matrix_sync"]["selected_paper_count"])
        self.assertNotIn("selection_fingerprint", payload["discovery_selection"])

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
            previous_blueprint = (
                self.app.state.workflow_repository.get_current_artifact(
                    self.first.user_id,
                    self.project_id,
                    "blueprint/section_blueprint.json",
                )
            )
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
        self.assertEqual(
            previous_blueprint.id,
            self.app.state.workflow_repository.get_current_artifact(
                self.first.user_id, self.project_id, "blueprint/section_blueprint.json"
            ).id,
        )
        self.assertFalse(planning["outline_current"])
        self.assertFalse(planning["blueprint_current"])
        self.assertEqual(
            "stale",
            self.app.state.workflow_repository.get_stage_state(
                self.first.user_id, self.project_id, "blueprint"
            ).status,
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
        self.assertIn("## Introduction\nSection role: introduction", selected["selected_outline_md"])
        self.assertIn(
            "## Cross-category comparison and conclusion\nSection role: conclusion",
            selected["selected_outline_md"],
        )
        self.assertIn("## 1. cross-coupling", selected["selected_outline_md"])
        self.assertIn("## 2. addition reactions", selected["selected_outline_md"])
        self.assertIn("## 3. cyclization and annulation", selected["selected_outline_md"])
        self.assertTrue(selected["outline_complete"])
        self.assertEqual("reaction", selected["outline_style"])

    def test_reselecting_current_outline_is_idempotent(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "reaction")
            repeated = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={
                    "revision": selected["matrix_revision"],
                    "outline_style": "reaction",
                },
                headers=self.headers(),
            )
        self.assertEqual(200, repeated.status_code, repeated.text)
        payload = repeated.json()
        self.assertTrue(payload["unchanged"])
        self.assertEqual(selected["matrix_revision"], payload["matrix_revision"])
        self.assertEqual(selected["outline_artifact_id"], payload["outline_artifact_id"])

    def test_builtin_outline_styles_use_distinct_metadata_axes(self) -> None:
        with TestClient(self.app) as client:
            substrate = self.choose_outline(client, "substrate")["selected_outline_md"]
            catalyst = self.choose_outline(client, "catalyst")["selected_outline_md"]
            reaction = self.choose_outline(client, "reaction")["selected_outline_md"]
        self.assertIn("## 1. aromatic substrates", substrate)
        self.assertIn("## 1. transition-metal catalysis", catalyst)
        self.assertIn("## 1. cross-coupling", reaction)
        self.assertNotEqual(substrate, catalyst)
        self.assertNotEqual(catalyst, reaction)

    def test_outline_sources_prefer_confirmed_or_automatic_project_tags(self) -> None:
        service = self.app.state.planning_service
        confirmed_tags, _ = service._outline_sources(
            self.first,
            [
                {
                    "paper_id": "P001",
                    "project_tag_review_status": "confirmed",
                    "project_tags": {
                        "reaction_type": ["project-specific transformation"]
                    },
                }
            ],
        )
        pending_tags, _ = service._outline_sources(
            self.first,
            [
                {
                    "paper_id": "P001",
                    "project_tag_review_status": "pending",
                    "project_tags": {"reaction_type": ["unreviewed suggestion"]},
                }
            ],
        )
        automatic_tags, _ = service._outline_sources(
            self.first,
            [
                {
                    "paper_id": "P001",
                    "project_tag_review_status": "automatic",
                    "project_tags": {
                        "reaction_type": ["automatically assessed transformation"]
                    },
                }
            ],
        )
        self.assertEqual(
            ["project-specific transformation"],
            confirmed_tags["P001"]["reaction_type"],
        )
        self.assertEqual("cross-coupling", pending_tags["P001"]["reaction_type"])
        self.assertEqual(
            ["automatically assessed transformation"],
            automatic_tags["P001"]["reaction_type"],
        )

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
        with patch.object(
            self.app.state.planning_service,
            "_analyze_reference_document",
            side_effect=self.isolated_reference_analysis,
        ):
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
                candidate_id = response.json()["candidate"]["candidate_id"]
                selected_response = client.put(
                    f"/api/v1/projects/{self.project_id}/planning/outline",
                    json={
                        "revision": payload["matrix_revision"],
                        "outline_style": f"reference:{candidate_id}",
                    },
                    headers=self.headers(),
                )
                self.assertEqual(200, selected_response.status_code, selected_response.text)
        candidate = response.json()["candidate"]
        self.assertTrue(candidate["source_artifact_id"])
        self.assertEqual("current_matrix_only", candidate["content_source"])
        self.assertFalse(candidate["reference_content_reused"])
        self.assertNotIn("Mechanisms", candidate["outline_md"])
        self.assertIn(candidate["candidate_id"], {item["candidate_id"] for item in payload["reference_outline_candidates"]})
        self.assertNotIn("Mechanisms", selected_response.json()["selected_outline_md"])

    def test_legacy_reference_candidate_fails_content_isolation(self) -> None:
        service = self.app.state.planning_service
        self.assertFalse(
            service._reference_candidate_is_isolated(
                {
                    "analysis_mode": "heading_extraction",
                    "outline_md": "## Source heading",
                }
            )
        )

    def test_reference_docx_content_is_not_used_as_candidate_headings(self) -> None:
        document = Document()
        document.add_heading("1. Mechanistic organization", level=1)
        document.add_paragraph("Reference discussion.")
        stream = BytesIO()
        document.save(stream)
        with patch.object(
            self.app.state.planning_service,
            "_analyze_reference_document",
            side_effect=self.isolated_reference_analysis,
        ):
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
        self.assertEqual("ai_style_only_transfer_v2", candidate["analysis_mode"])
        self.assertNotIn("Mechanistic organization", candidate["outline_md"])
        self.assertIn("Copper allenation evidence", candidate["outline_md"])

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
        primary_occurrences = [
            paper_id
            for section in blueprint["sections"]
            for paper_id in section["primary_papers"]
        ]
        self.assertEqual(len(primary_occurrences), len(set(primary_occurrences)))
        introduction = next(
            section
            for section in blueprint["sections"]
            if section["section_role"] == "introduction"
        )
        conclusion = next(
            section
            for section in blueprint["sections"]
            if section["section_role"] == "conclusion"
        )
        self.assertEqual([], introduction["major_papers"])
        self.assertEqual([], conclusion["major_papers"])
        self.assertTrue(introduction["supporting_papers"])
        self.assertTrue(conclusion["supporting_papers"])
        self.assertTrue(
            blueprint["paper_assignment_policy"][
                "introduction_and_conclusion_are_synthesis_only"
            ]
        )
        self.assertEqual(selected["outline_artifact_id"], blueprint["source_outline_artifact_id"])

    def test_duplicate_body_assignment_becomes_supporting_cross_reference(self) -> None:
        with TestClient(self.app) as client:
            selected = self.choose_outline(client, "custom")
            outline = (
                "# Review\n\n"
                "## Introduction\n"
                "Section role: introduction\n"
                "Purpose: define scope.\n\n"
                "## 1. First evidence theme\n"
                "Section role: body\n"
                "Assigned papers: P001, P002.\n\n"
                "## 2. Cross-cutting theme\n"
                "Section role: body\n"
                "Assigned papers: P001, P003.\n\n"
                "## Conclusion\n"
                "Section role: conclusion\n"
                "Purpose: synthesize findings.\n"
            )
            saved = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={
                    "revision": selected["matrix_revision"],
                    "outline_style": "custom",
                    "outline_md": outline,
                },
                headers=self.headers(),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            generated = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(200, generated.status_code, generated.text)
        sections = generated.json()["section_blueprint"]["sections"]
        first = next(section for section in sections if section["title"] == "First evidence theme")
        second = next(section for section in sections if section["title"] == "Cross-cutting theme")
        self.assertEqual(["P001", "P002"], first["primary_papers"])
        self.assertEqual(["P003"], second["primary_papers"])
        self.assertEqual(["P001"], second["supporting_papers"])

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
