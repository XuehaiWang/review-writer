from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.security import Principal, Role


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class NativeFigureApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        database_url = f"sqlite+pysqlite:///{(self.root / 'figures.sqlite3').as_posix()}"
        self.engine = create_engine(
            database_url, connect_args={"check_same_thread": False}
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            first = User(
                email="first@example.com", display_name="First", password_hash="hash"
            )
            second = User(
                email="second@example.com", display_name="Second", password_hash="hash"
            )
            session.add_all([first, second])
            session.flush()
            project = Project(user_id=first.id, slug="figures", topic="Copper chemistry")
            other = Project(user_id=second.id, slug="other", topic="Hidden")
            session.add_all([project, other])
            session.flush()
            self.project_id = str(project.id)
            self.other_project_id = str(other.id)
            self.first = Principal(str(first.id), frozenset({Role.USER}), first.email)
            self.second = Principal(str(second.id), frozenset({Role.USER}), second.email)
        self.current = self.first
        self.output_size = (20, 10)
        self.integrity_status = "pass"
        self.include_chemistry_integrity = True
        self.redraw_calls = 0
        self.redraw_error = ""
        self.redraw_errors_by_figure: dict[str, str] = {}
        self.block_redraw = False
        self.block_figure_id = ""
        self.redraw_started = threading.Event()

        def redraw(_context, payload):
            self.redraw_calls += 1
            figure = payload["figure"]
            if self.block_redraw or self.block_figure_id == str(
                figure.get("figure_id") or ""
            ):
                self.redraw_started.set()
                while True:
                    _context.checkpoint()
                    time.sleep(0.01)
            error = self.redraw_errors_by_figure.get(
                str(figure.get("figure_id") or ""), self.redraw_error
            )
            if error:
                raise RuntimeError(error)
            output = (
                self.root
                / "users"
                / self.first.user_id
                / ".review-writer"
                / "test-redraws"
                / f"redraw-{self.redraw_calls}.png"
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new(
                "RGB",
                self.output_size,
                (240, max(0, 240 - self.redraw_calls), 240),
            ).save(output)
            result = {
                "figure_id": figure["figure_id"],
                "status": "redrawn",
                "output_path": str(output),
                "render_mode": "ai-edit",
            }
            if self.include_chemistry_integrity:
                result["chemistry_integrity"] = {
                    "status": self.integrity_status,
                    "failures": (
                        []
                        if self.integrity_status == "pass"
                        else ["chemical labels require human verification"]
                    ),
                }
            return result

        settings = ApiSettings(
            review_root=self.root,
            deployment_mode="hosted",
            database_url=database_url,
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=self.root / "users",
        )
        self.app = create_app(
            settings,
            principal_provider=lambda: self.current,
            session_factory_override=self.sessions,
            native_workflow_overrides={"figures.redraw": redraw},
        )
        self._seed_sections()

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str = "figures") -> dict[str, str]:
        return {"Origin": "http://testserver", "Idempotency-Key": key}

    def _publish(
        self,
        run_id: str,
        logical_name: str,
        filename: str,
        content: bytes,
        artifact_type: str,
    ):
        artifacts = self.app.state.artifact_service
        staging = artifacts.stage_run_directory(
            self.first.user_id, self.project_id, run_id
        )
        (staging / filename).write_bytes(content)
        return artifacts.publish(
            self.first.user_id,
            self.project_id,
            run_id,
            filename,
            logical_name=logical_name,
            artifact_type=artifact_type,
            producer_stage="sections",
            make_current=False,
        )

    def _seed_sections(self) -> None:
        repository = self.app.state.workflow_repository
        run = repository.create_stage_run(
            self.first.user_id, self.project_id, "sections", status="succeeded"
        )
        source_file = self.root / "source.png"
        Image.new("RGB", (20, 10), "white").save(source_file)
        source = self._publish(
            run.id,
            "sections/source-images/P001/0.png",
            "source.png",
            source_file.read_bytes(),
            "png",
        )
        table_file = self.root / "table.png"
        Image.new("RGB", (20, 10), (250, 250, 245)).save(table_file)
        table = self._publish(
            run.id,
            "sections/source-images/P001/1.png",
            "table.png",
            table_file.read_bytes(),
            "png",
        )
        missing_anchor_file = self.root / "missing-anchor.png"
        Image.new("RGB", (20, 10), (245, 250, 250)).save(missing_anchor_file)
        missing_anchor = self._publish(
            run.id,
            "sections/source-images/P001/2.png",
            "missing-anchor.png",
            missing_anchor_file.read_bytes(),
            "png",
        )
        candidates = [
            {
                "figure_id": "P001-F01",
                "paper_id": "P001",
                "candidate_index": 0,
                "source_label": "Scheme 1",
                "source_type": "image",
                "source_image_artifact_id": source.id,
                "score": 8,
                "section_id": "S1",
                "section_heading": "Methods",
                "target_paragraph_id": "S1-p1",
                "manuscript_selected": True,
            },
            {
                "figure_id": "P001-F02",
                "paper_id": "P001",
                "candidate_index": 1,
                "source_label": "Table 1",
                "source_type": "table",
                "source_image_artifact_id": table.id,
                "score": 10,
                "section_id": "S1",
                "section_heading": "Methods",
                "target_paragraph_id": "S1-p1",
                "manuscript_selected": True,
            },
            {
                "figure_id": "P001-F03",
                "paper_id": "P001",
                "candidate_index": 2,
                "source_label": "Figure without anchor",
                "source_type": "image",
                "source_image_artifact_id": missing_anchor.id,
                "score": 20,
                "section_id": "S1",
                "section_heading": "Methods",
                "target_paragraph_id": "",
                "manuscript_selected": True,
            },
        ]
        paper_candidates = {
            "project_id": self.project_id,
            "papers": [
                {
                    "paper_id": "P001",
                    "title": "Copper paper",
                    "candidates": candidates,
                    "selected_candidate_index": 1,
                },
                {
                    "paper_id": "P002",
                    "title": "Paper without a usable source figure",
                    "candidates": [],
                    "selected_candidate_index": None,
                    "status": "no_useful_figure",
                    "no_useful_figure_reason": "No image candidates were found.",
                },
            ],
        }
        defaults = {
            "source": "automatic_top_score",
            "papers": {
                "P001": {
                    "selected_candidate_index": 1,
                    "selection_source": "automatic_top_score",
                    "review_note": "Highest-scoring anchored source.",
                    "reviewed_at": "2026-08-13T00:00:00+00:00",
                }
            },
        }
        section_index = {
            "project_id": self.project_id,
            "sections": [
                {
                    "section_id": "S1",
                    "heading": "Methods",
                    "paragraphs": [
                        {
                            "paragraph_id": "S1-p1",
                            "paper_id": "P001",
                            "cited_paper_ids": ["P001"],
                            "text": "Grounded paragraph.",
                        },
                        {
                            "paragraph_id": "S1-p2",
                            "paper_id": "P002",
                            "cited_paper_ids": ["P002"],
                            "text": "A cited paper has no usable source figure.",
                        },
                    ],
                    "draft_md": "## Methods\n\nGrounded paragraph.\n",
                    "logical_name": "sections/S1.md",
                }
            ],
        }
        files = {
            "sections/section_drafts.json": (section_index, "index.json"),
            "sections/figure_candidates.json": (candidates, "figure-candidates.json"),
            "sections/paper_figure_candidates.json": (
                paper_candidates,
                "paper-candidates.json",
            ),
            "sections/default_figure_reviews.json": (defaults, "defaults.json"),
        }
        promoted = {
            source.logical_name: source.id,
            table.logical_name: table.id,
            missing_anchor.logical_name: missing_anchor.id,
        }
        for logical_name, (payload, filename) in files.items():
            artifact = self._publish(
                run.id,
                logical_name,
                filename,
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
                "json",
            )
            promoted[logical_name] = artifact.id
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "sections",
            artifact_ids=promoted,
            run_id=run.id,
            expected_revision=0,
            status="approved",
            invalidate_stages=("figure-review", "figures", "draft", "final"),
        )

    def confirm_review(self, client: TestClient) -> dict:
        review = client.get(
            f"/api/v1/projects/{self.project_id}/figures/review"
        ).json()
        response = client.post(
            f"/api/v1/projects/{self.project_id}/figures/review/confirm",
            json={"revision": review["revision"]},
            headers=self.headers("confirm-review"),
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def add_second_confirmed_figure(self) -> None:
        repository = self.app.state.workflow_repository
        artifacts = self.app.state.artifact_service
        candidate_artifact = repository.get_current_artifact(
            self.first.user_id, self.project_id, "sections/figure_candidates.json"
        )
        input_artifact = repository.get_current_artifact(
            self.first.user_id, self.project_id, "figure-review/selected_figures.json"
        )
        self.assertIsNotNone(candidate_artifact)
        self.assertIsNotNone(input_artifact)
        all_candidates = json.loads(
            artifacts.resolve_owned_artifact(
                self.first.user_id, candidate_artifact.id
            ).path.read_text(encoding="utf-8")
        )
        selected = json.loads(
            artifacts.resolve_owned_artifact(
                self.first.user_id, input_artifact.id
            ).path.read_text(encoding="utf-8")
        )
        second = next(row for row in all_candidates if row["figure_id"] == "P001-F01")
        selected["figures"].append(second)
        run = repository.create_stage_run(
            self.first.user_id, self.project_id, "figure-review", status="succeeded"
        )
        staging = artifacts.stage_run_directory(
            self.first.user_id, self.project_id, run.id
        )
        (staging / "selected.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifact = artifacts.publish(
            self.first.user_id,
            self.project_id,
            run.id,
            "selected.json",
            logical_name="figure-review/selected_figures.json",
            artifact_type="json",
            producer_stage="figure-review",
            make_current=False,
        )
        state = repository.get_stage_state(
            self.first.user_id, self.project_id, "figure-review"
        )
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "figure-review",
            artifact_ids={artifact.logical_name: artifact.id},
            run_id=run.id,
            expected_revision=state.revision,
            status="approved",
            invalidate_stages=("figures", "draft", "final"),
        )

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return payload
            time.sleep(0.02)
        self.fail("Figure job did not finish.")

    def start_redraw(self, client: TestClient, key: str = "redraw") -> dict:
        response = client.post(
            f"/api/v1/projects/{self.project_id}/figures/jobs",
            json={},
            headers=self.headers(key),
        )
        self.assertEqual(202, response.status_code, response.text)
        return self.wait_job(client, response.json()["id"])
