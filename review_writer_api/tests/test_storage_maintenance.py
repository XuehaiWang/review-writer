from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from review_writer_api.database import Base, utc_now
from review_writer_api.storage_maintenance import StorageMaintenance, scan_workspace, safe_tree, disk_status
from review_writer_api.workflow_models import WorkflowJob, LibraryArtifact, WorkflowArtifact


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.service = StorageMaintenance(self.sessions, self.root)
        self.user = uuid.uuid4()
        self.old = utc_now() - timedelta(days=3)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def job(self, status="succeeded", **extra):
        job_id = uuid.uuid4()
        with self.sessions.begin() as session:
            session.add(WorkflowJob(id=job_id, user_id=self.user, scope="library", job_type="library.parse",
                status=status, idempotency_key=str(job_id), updated_at=self.old, **extra))
        path = self.root / str(self.user) / ".review-writer" / "job-staging" / str(job_id)
        path.mkdir(parents=True)
        (path / "scratch.txt").write_text("temporary")
        self.age(path)
        return job_id, path

    def age(self, path):
        stamp = self.old.timestamp()
        for p in [*path.rglob("*"), path]:
            os.utime(p, (stamp, stamp))

    def test_removes_only_old_success_and_preserves_user_outputs(self):
        _, old = self.job()
        protected = [self.job(status)[1] for status in ("running", "queued", "failed", "cancelled", "interrupted")]
        unknown = old.parent / str(uuid.uuid4())
        unknown.mkdir()
        paper = self.root / str(self.user) / "review-library" / "paper.pdf"
        paper.parent.mkdir()
        paper.write_bytes(b"original")
        report = self.service.run()
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in protected))
        self.assertTrue(unknown.exists())
        self.assertEqual(b"original", paper.read_bytes())
        self.assertEqual(1, report["last_cleanup"]["removed_directories"])
        self.assertEqual(9, report["last_cleanup"]["freed_bytes"])
        self.assertTrue(self.service.run(manual=True)["cooldown"])
        self.assertEqual(report["last_cleanup"], self.service.snapshot()["last_cleanup"])

    def test_orphan_zip_cleanup_protects_active_archives_and_library_pdfs(self):
        staging = self.root / str(self.user) / "review-library" / ".upload-staging"
        staging.mkdir(parents=True)
        orphan = staging / f"{uuid.uuid4()}.zip.part"
        orphan.write_bytes(b"orphan")
        token = str(uuid.uuid4())
        active = staging / f"{token}.zip.part"
        active.write_bytes(b"active")
        pdf = staging / f"{uuid.uuid4()}.pdf.part"
        pdf.write_bytes(b"pdf")
        self.age(staging)
        with self.sessions.begin() as session:
            session.add(WorkflowJob(user_id=self.user, scope="library", job_type="library.archive",
                status="running", idempotency_key=token, payload_json={"archive_id": token}))
        report = self.service.run()["last_cleanup"]
        self.assertFalse(orphan.exists())
        self.assertTrue(active.exists())
        self.assertTrue(pdf.exists())
        self.assertEqual(1, report["removed_files"])

    def test_references_recent_writes_and_leases_prevent_deletion(self):
        source, referenced = self.job()
        self.job("failed", retry_of_job_id=source)
        _, recent = self.job()
        (recent / "scratch.txt").write_text("new write")
        _, leased = self.job(lease_owner="worker")
        artifact_id, artifact_path = self.job()
        with self.sessions.begin() as session:
            session.add(LibraryArtifact(user_id=self.user, paper_id="p", kind="pdf",
                relative_path=f".review-writer/job-staging/{artifact_id}/scratch.txt", content_sha256="a" * 64))
        self.service.run()
        self.assertTrue(all(p.exists() for p in [referenced, recent, leased, artifact_path]))

    def test_unknown_foreign_user_and_recent_job_are_retained(self):
        job_id, path = self.job()
        with self.sessions.begin() as session:
            session.get(WorkflowJob, job_id).user_id = uuid.uuid4()
        _, recent = self.job()
        with self.sessions.begin() as session:
            session.get(WorkflowJob, uuid.UUID(recent.name)).updated_at = utc_now()
        self.service.run()
        self.assertTrue(path.exists())
        self.assertTrue(recent.exists())

    def test_outside_and_symlink_paths_rejected(self):
        _, path = self.job()
        self.assertIsNone(safe_tree(self.root.parent, self.root, utc_now().timestamp()))
        outside = self.root / "original.txt"
        outside.write_text("keep")
        try:
            (path / "link").symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        self.age(path)
        self.assertIsNone(safe_tree(path, self.root, utc_now().timestamp()))
        self.service.run()
        self.assertEqual("keep", outside.read_text())
        self.assertTrue(path.exists())

    def test_scan_categories_and_explicit_partial_results(self):
        _, path = self.job()
        report = scan_workspace(self.root)
        self.assertEqual(9, report["categories"]["temporary"])
        self.assertFalse(report["partial"])
        self.assertTrue(scan_workspace(self.root, max_files=0)["partial"])
        with patch("review_writer_api.storage_maintenance.shutil.disk_usage", return_value=type("Usage", (), {"total": 100 * 1024**3, "free": 4 * 1024**3, "used": 96 * 1024**3})()):
            self.assertTrue(disk_status(self.root)["low_space"])

    def test_deletion_error_is_recorded_without_deleting_other_files(self):
        _, path = self.job()
        with patch("review_writer_api.storage_maintenance.shutil.rmtree", side_effect=PermissionError):
            report = self.service.run()
        self.assertEqual(1, report["last_cleanup"]["errors"])
        self.assertEqual(["permission_denied"], report["last_cleanup"]["error_reasons"])
        self.assertTrue(path.exists())

    def test_metadata_file_dependencies_protect_but_provenance_is_not_a_dependency(self):
        job_id, referenced = self.job()
        provenance_id, removable = self.job()
        with self.sessions.begin() as session:
            for ident, metadata in [
                (job_id, {"file": f".review-writer\\job-staging\\{job_id}\\scratch.txt"}),
                (provenance_id, {"job_id": str(provenance_id)}),
            ]:
                session.add(WorkflowArtifact(project_id=uuid.uuid4(), logical_name=str(ident),
                    artifact_type="text", relative_path=f".artifacts/{ident}/published.txt", content_sha256="a" * 64,
                    metadata_json=metadata))
        self.service.run()
        self.assertTrue(referenced.exists())
        self.assertFalse(removable.exists())
