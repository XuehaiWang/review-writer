from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.domain_services.planning import BLUEPRINT_LOGICAL_NAME


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class SectionsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        database_url = f"sqlite+pysqlite:///{(root / 'sections.sqlite3').as_posix()}"
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
            project = Project(user_id=first.id, slug="sections", topic="Copper allenation")
            other = Project(user_id=second.id, slug="other", topic="Hidden")
            session.add_all([project, other])
            session.flush()
            session.add_all(
                [
                    LibraryPaper(
                        user_id=first.id,
                        paper_id=paper_id,
                        content_sha256=f"{index:064x}",
                        original_filename=f"{paper_id}.pdf",
                        title=f"Paper {index}",
                        authors_json=[f"Author {index}"],
                        keywords_json=["copper"],
                        tags_json={},
                        metadata_json={
                            "paper_id": paper_id,
                            "title": {"value": f"Paper {index}"},
                            "abstract": {"value": f"Grounded evidence for {paper_id}."},
                        },
                        pdf_relative_path=f"review-library/uploads/{paper_id}.pdf",
                        markdown_relative_path=f"review-library/markdown/{paper_id}.md",
                    )
                    for index, paper_id in enumerate(("P001", "P002", "P003"), start=1)
                ]
            )
            self.project_id = str(project.id)
            self.other_project_id = str(other.id)
            self.first = Principal(str(first.id), frozenset({Role.USER}), first.email)
            self.second = Principal(str(second.id), frozenset({Role.USER}), second.email)
        self.current = self.first
        self.writer_calls = 0
        self.transient_failures = 0

        def writer(context, payload):
            self.writer_calls += 1
            if self.transient_failures > 0:
                self.transient_failures -= 1
                raise RuntimeError("Section-writing model request failed with HTTP 503")
            sections = []
            for task in payload["tasks"]:
                paragraphs = [
                    {
                        "paragraph_id": f"{task['section_id']}-p{index}",
                        "paper_id": paper_id,
                        "cited_paper_ids": [paper_id],
                        "text": f"Grounded synthesis for {paper_id} [{index}].",
                    }
                    for index, paper_id in enumerate(task["allowed_papers"], start=1)
                ]
                markdown = "\n\n".join(
                    [f"## {task['heading']}"] + [paragraph["text"] for paragraph in paragraphs]
                ) + "\n"
                sections.append(
                    {
                        "section_id": task["section_id"],
                        "heading": task["heading"],
                        "overview": task["core_argument"],
                        "paragraphs": paragraphs,
                        "draft_md": markdown,
                    }
                )
            return {
                "sections": sections,
                "section_drafts_md": "\n".join(section["draft_md"] for section in sections),
                "report_md": f"# Section Drafting Report\n\nGenerated {len(sections)} sections.\n",
                "attempts": 1,
            }

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
            native_workflow_overrides={"sections.generate": writer},
        )
        self._seed_planning()

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str = "sections") -> dict[str, str]:
        return {"Origin": "http://testserver", "Idempotency-Key": key}

    def _seed_planning(self) -> None:
        discovery = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        review = {
            "project_id": self.project_id,
            "topic": "Copper allenation",
            "selection_mode": "explicit",
            "results": [
                {
                    "keyword": "copper",
                    "keep": True,
                    "local_results": [
                        {
                            "paper_id": paper_id,
                            "title": paper_id,
                            "score": 10 - index,
                            "role": "core_candidate",
                            "selected_for_matrix": True,
                        }
                        for index, paper_id in enumerate(("P001", "P002", "P003"))
                    ],
                    "web_results": [],
                }
            ],
        }
        artifact, run = discovery._write_json_artifact(
            self.first,
            self.project_id,
            stage_id="discovery",
            logical_name="discovery/review.json",
            payload=review,
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
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": 1},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            planning = client.get(f"/api/v1/projects/{self.project_id}/planning").json()
            selected = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={"revision": planning["matrix_revision"], "outline_style": "reaction"},
                headers=self.headers(),
            )
            self.assertEqual(200, selected.status_code, selected.text)
            blueprint = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(200, blueprint.status_code, blueprint.text)
            confirmed_blueprint = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint/confirm",
                json={"revision": blueprint.json()["blueprint_revision"]},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed_blueprint.status_code, confirmed_blueprint.text)

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return payload
            time.sleep(0.02)
        self.fail("Job did not finish.")

    def start(self, client: TestClient, key: str = "sections") -> dict:
        response = client.post(
            f"/api/v1/projects/{self.project_id}/sections/jobs",
            json={},
            headers=self.headers(key),
        )
        self.assertEqual(202, response.status_code, response.text)
        return self.wait_job(client, response.json()["id"])

    def test_payload_resolves_blueprint_papers(self) -> None:
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/sections")
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload["section_tasks"])
        self.assertEqual(
            {"P001", "P002", "P003"},
            {paper["paper_id"] for paper in payload["papers"]},
        )
        self.assertEqual(len(payload["section_blueprint"]["sections"]), len(payload["section_tasks"]))

    def test_missing_blueprint_paper_blocks_generation(self) -> None:
        service = self.app.state.planning_service
        repository = self.app.state.workflow_repository
        blueprint, _artifact = service._read_json(
            self.first, self.project_id, BLUEPRINT_LOGICAL_NAME
        )
        changed = deepcopy(blueprint)
        changed["sections"][0]["major_papers"] = ["P999"]
        published, run = service._publish_files(
            self.first,
            self.project_id,
            stage_id="blueprint",
            files={
                BLUEPRINT_LOGICAL_NAME: (
                    (json.dumps(changed, ensure_ascii=False) + "\n").encode(),
                    "json",
                )
            },
        )
        state = repository.get_stage_state(self.first.user_id, self.project_id, "blueprint")
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "blueprint",
            artifact_ids={BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id},
            run_id=run.id,
            expected_revision=state.revision,
            status="approved",
            invalidate_stages=("sections", "figure-review", "figures", "draft", "final"),
        )
        with TestClient(self.app) as client:
            response = client.post(
                f"/api/v1/projects/{self.project_id}/sections/jobs",
                json={},
                headers=self.headers("missing"),
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("BLUEPRINT_PAPERS_MISSING", response.json()["error"]["code"])

    def test_generation_persists_section_progress(self) -> None:
        with TestClient(self.app) as client:
            job = self.start(client, "progress")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(len(payload["section_tasks"]), job["progress_total"])
        self.assertEqual(job["progress_total"], job["progress_current"])

    def test_transient_provider_failure_retries_three_times(self) -> None:
        self.transient_failures = 2
        with TestClient(self.app) as client:
            job = self.start(client, "retry-503")
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(3, self.writer_calls)
        self.assertEqual(3, job["result"]["attempts"])

    def test_successful_task_publishes_section_artifact(self) -> None:
        with TestClient(self.app) as client:
            job = self.start(client, "publish")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        self.assertEqual("succeeded", job["status"])
        self.assertTrue(payload["section_files"])
        self.assertIn("Grounded synthesis", payload["section_files"][0]["content"])
        artifact_id = payload["section_files"][0]["artifact_id"]
        self.assertEqual(
            artifact_id,
            self.app.state.workflow_repository.get_current_artifact(
                self.first.user_id,
                self.project_id,
                f"sections/{payload['section_files'][0]['section_id']}.md",
            ).id,
        )

    def test_report_uses_current_jobs(self) -> None:
        with TestClient(self.app) as client:
            first = self.start(client, "report-1")
            second = self.start(client, "report-2")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        job_ids = [job["id"] for job in payload["report"]["jobs"]]
        self.assertIn(first["id"], job_ids)
        self.assertIn(second["id"], job_ids)
        self.assertEqual(payload["section_tasks"].__len__(), payload["report"]["current_task_count"])

    def test_handoff_requires_current_section_outputs(self) -> None:
        with TestClient(self.app) as client:
            before = client.post(
                f"/api/v1/projects/{self.project_id}/sections/confirm",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(409, before.status_code, before.text)
            self.start(client, "handoff")
            sections = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
            after = client.post(
                f"/api/v1/projects/{self.project_id}/sections/confirm",
                json={"revision": sections["revision"]},
                headers=self.headers(),
            )
        self.assertEqual(200, after.status_code, after.text)
        self.assertEqual("images", after.json()["next_stage"])

    def test_sections_api_and_container_are_user_isolated(self) -> None:
        self.assertIs(
            self.app.state.sections_service,
            self.app.state.container.sections_service,
        )
        self.current = self.second
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/sections")
        self.assertEqual(404, response.status_code, response.text)


if __name__ == "__main__":
    unittest.main()
