from __future__ import annotations

import importlib
import unittest
import uuid

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from review_writer_api.database import Base, Project, User


def workflow_api():
    try:
        contracts = importlib.import_module("review_writer_api.workflow_contracts")
        models = importlib.import_module("review_writer_api.workflow_models")
    except ModuleNotFoundError as exc:
        raise AssertionError("PostgreSQL workflow models and contracts are missing.") from exc
    return contracts, models


def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


class WorkflowContractTests(unittest.TestCase):
    def test_internal_stages_map_to_seven_user_stages(self) -> None:
        contracts, _ = workflow_api()

        self.assertEqual("discovery", contracts.composite_stage("discovery"))
        self.assertEqual("planning", contracts.composite_stage("matrix"))
        self.assertEqual("planning", contracts.composite_stage("blueprint"))
        self.assertEqual("images", contracts.composite_stage("figure-review"))
        self.assertEqual("images", contracts.composite_stage("figures"))
        self.assertEqual("final", contracts.composite_stage("final"))
        self.assertEqual(
            {"succeeded", "failed", "cancelled", "interrupted"},
            contracts.TERMINAL_JOB_STATUSES,
        )

    def test_current_user_stage_returns_first_incomplete_composite_stage(self) -> None:
        contracts, _ = workflow_api()

        self.assertEqual("discovery", contracts.current_user_stage({}))
        self.assertEqual(
            "planning",
            contracts.current_user_stage({"discovery": "completed", "matrix": "pending"}),
        )
        self.assertEqual(
            "sections",
            contracts.current_user_stage(
                {"discovery": "completed", "matrix": "completed", "blueprint": "approved"}
            ),
        )
        self.assertEqual(
            "images",
            contracts.current_user_stage(
                {
                    "discovery": "completed",
                    "matrix": "completed",
                    "blueprint": "approved",
                    "sections": "succeeded",
                }
            ),
        )


class WorkflowModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contracts, self.models = workflow_api()
        self.sessions, self.engine = session_factory()

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _user_project(self, session, email: str, slug: str = "same-slug"):
        user = User(email=email, display_name=email, password_hash="hash")
        session.add(user)
        session.flush()
        project = Project(user_id=user.id, slug=slug, topic="topic")
        session.add(project)
        session.flush()
        return user, project

    def test_same_stage_is_independent_for_two_users_with_same_slug(self) -> None:
        with self.sessions.begin() as session:
            _user_a, project_a = self._user_project(session, "a@example.com")
            _user_b, project_b = self._user_project(session, "b@example.com")
            session.add_all(
                [
                    self.models.WorkflowStageState(
                        project_id=project_a.id, stage_id="discovery", status="completed"
                    ),
                    self.models.WorkflowStageState(
                        project_id=project_b.id, stage_id="discovery", status="pending"
                    ),
                ]
            )

        with self.sessions() as session:
            states = session.scalars(
                select(self.models.WorkflowStageState).order_by(
                    self.models.WorkflowStageState.status
                )
            ).all()
            self.assertEqual(["completed", "pending"], [state.status for state in states])
            self.assertNotEqual(states[0].project_id, states[1].project_id)

    def test_project_stage_id_pair_is_unique(self) -> None:
        with self.sessions.begin() as session:
            _user, project = self._user_project(session, "unique@example.com")
            project_id = project.id
            session.add(
                self.models.WorkflowStageState(
                    project_id=project_id, stage_id="matrix", status="pending"
                )
            )

        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                session.add(
                    self.models.WorkflowStageState(
                        project_id=project_id, stage_id="matrix", status="running"
                    )
                )

    def test_stage_run_preserves_json_snapshots_and_progress(self) -> None:
        with self.sessions.begin() as session:
            user, project = self._user_project(session, "run@example.com")
            run = self.models.WorkflowStageRun(
                project_id=project.id,
                stage_id="sections",
                requested_by_user_id=user.id,
                status="running",
                attempt=2,
                progress_current=3,
                progress_total=7,
                input_snapshot=[{"logical_name": "blueprint", "sha256": "a" * 64}],
                output_snapshot={"section_ids": ["sec1", "sec2"]},
                metadata_json={"provider": "openai-compatible"},
            )
            session.add(run)
            session.flush()
            run_id = run.id

        with self.sessions() as session:
            stored = session.get(self.models.WorkflowStageRun, run_id)
            self.assertEqual(2, stored.attempt)
            self.assertEqual((3, 7), (stored.progress_current, stored.progress_total))
            self.assertEqual("blueprint", stored.input_snapshot[0]["logical_name"])
            self.assertEqual(["sec1", "sec2"], stored.output_snapshot["section_ids"])
            self.assertEqual("openai-compatible", stored.metadata_json["provider"])

    def test_deleting_project_cascades_workflow_state_artifacts_and_jobs(self) -> None:
        with self.sessions.begin() as session:
            user, project = self._user_project(session, "cascade@example.com")
            run = self.models.WorkflowStageRun(
                project_id=project.id,
                stage_id="figures",
                requested_by_user_id=user.id,
                status="succeeded",
            )
            session.add(run)
            session.flush()
            artifact = self.models.WorkflowArtifact(
                project_id=project.id,
                logical_name="figures/F001.svg",
                artifact_type="svg",
                relative_path=".artifacts/figures/F001.svg",
                content_sha256="b" * 64,
                size_bytes=12,
                mtime_ns=1,
                producer_stage="figures",
                producer_run_id=run.id,
            )
            job = self.models.WorkflowJob(
                user_id=user.id,
                project_id=project.id,
                scope="project",
                job_type="figure-redraw",
                status="succeeded",
                idempotency_key=str(uuid.uuid4()),
                payload_json={"figure_id": "F001"},
            )
            session.add_all(
                [
                    self.models.WorkflowStageState(
                        project_id=project.id,
                        stage_id="figures",
                        status="completed",
                        current_run_id=run.id,
                    ),
                    artifact,
                    job,
                ]
            )
            project_id = project.id

        with self.sessions.begin() as session:
            session.delete(session.get(Project, project_id))

        with self.sessions() as session:
            self.assertEqual(
                0,
                len(
                    session.scalars(
                        select(self.models.WorkflowStageState).where(
                            self.models.WorkflowStageState.project_id == project_id
                        )
                    ).all()
                ),
            )
            self.assertEqual(0, len(session.scalars(select(self.models.WorkflowStageRun)).all()))
            self.assertEqual(0, len(session.scalars(select(self.models.WorkflowArtifact)).all()))
            self.assertEqual(0, len(session.scalars(select(self.models.WorkflowJob)).all()))


if __name__ == "__main__":
    unittest.main()
