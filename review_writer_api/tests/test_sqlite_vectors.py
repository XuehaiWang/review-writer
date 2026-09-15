from __future__ import annotations
from dataclasses import replace
from datetime import timedelta
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, select, update, delete, text
from sqlalchemy.orm import sessionmaker
from review_writer_api.database import Base, User, database_session, utc_now
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.persistent_storage import LocalPersistentStorage
from review_writer_api.sqlite_vectors import SQLiteVectors, VectorRow, vector_blob
from review_writer_api.vector_store import LocalVectorStore
from review_writer_api.workflow_models import (LibraryPaper, LibraryDocumentIndex, LibraryDocumentChunk,
    LibraryVectorStore, LibraryVectorVersion, LibraryVectorReadLease)
from review_writer_api.workspaces import HostedWorkspaceManager

EXTENSION = os.environ.get("REVIEW_WRITER_SQLITE_VECTOR_EXTENSION", "")


class StorageContractTests(unittest.TestCase):
    def test_object_publish_once_borrow_and_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/"source"
            source.write_bytes(b"immutable")
            storage = LocalPersistentStorage()
            obj = storage.commit_object(source, root, "formal/version/data")
            with storage.borrow_object(root, obj) as path:
                self.assertEqual(b"immutable", path.read_bytes())
            self.assertTrue(path.exists())
            with self.assertRaises(FileExistsError):
                storage.commit_object(source, root, obj.key)
            path.write_bytes(b"changed")
            with self.assertRaises(WorkflowValidationError):
                with storage.borrow_object(root, obj):
                    pass
            with self.assertRaises(WorkflowValidationError):
                storage.commit_object(source, root, "../escaped")

    def test_invalid_vectors(self):
        for values in ([0,0], [float("nan"),1], [1], [float("inf"),0]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                vector_blob(values, 2)


@unittest.skipUnless(Path(EXTENSION).is_file() if EXTENSION else importlib.util.find_spec("sqlite_vec"),
                     "Install the pinned sqlite-vec dependency")
class SQLitePublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        url = os.environ.get("SQLITE_VECTOR_TEST_DATABASE_URL")
        if url and not url.endswith("/vector_validation"):
            raise RuntimeError("This fixture only permits the isolated vector_validation database.")
        self.engine = create_engine(url or f"sqlite:///{self.root/'state.sqlite'}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.workspaces = HostedWorkspaceManager(self.root/"users")
        self.store = LocalVectorStore(self.sessions, self.workspaces, EXTENSION)
        self.user, self.other = uuid.uuid4(), uuid.uuid4()
        with database_session(self.sessions) as s:
            s.add_all([User(id=self.user,email=f"{self.user}@test.invalid",password_hash="x"),
                       User(id=self.other,email=f"{self.other}@test.invalid",password_hash="x")])
        self.rows = []
        for paper_id, values in (("P1",[[1.,0.],[.99,.01],[.98,.02]]), ("P2",[[.8,.2],[.7,.3]])):
            with database_session(self.sessions) as s:
                paper = LibraryPaper(user_id=self.user,paper_id=paper_id,content_sha256=paper_id,
                    original_filename="same.pdf",pdf_relative_path="p.pdf",markdown_relative_path="p.md")
                s.add(paper); s.flush()
                index = LibraryDocumentIndex(user_id=self.user,library_paper_id=paper.id,paper_id=paper_id,
                    source_lineage_hash=paper_id,chunker_version="test",status="ready",is_current=True,
                    semantic_status="ready",embedding_model_snapshot="m",embedding_dimension=2,embedding_count=len(values))
                s.add(index); s.flush()
                for ordinal, vector in enumerate(values):
                    content = f"Evidence {paper_id}-{ordinal}"
                    chunk = LibraryDocumentChunk(user_id=self.user,paper_id=paper_id,index_id=index.id,
                        chunk_id=f"c{ordinal}",ordinal=ordinal,content=content,normalized_content=content.lower(),
                        content_sha256=hashlib.sha256(content.encode()).hexdigest(),block_start=0,block_end=0)
                    s.add(chunk); s.flush()
                    self.rows.append(VectorRow(str(chunk.id),paper_id,str(index.id),chunk.content_sha256,ordinal,vector))

    def tearDown(self):
        if self.engine.dialect.name == "postgresql":
            with database_session(self.sessions) as s:
                s.execute(delete(User).where(User.id.in_((self.user,self.other))))
        self.engine.dispose(); self.tmp.cleanup()

    def publish(self, rows=None, **kwargs):
        return self.store.publish(str(self.user), "m",2,self.rows if rows is None else rows,
            **kwargs)

    def test_filtered_balanced_rank_and_user_isolation(self):
        self.publish()
        ranked = self.store.rank(str(self.user),"m",2,[1,0],["P2"],10,-1)
        self.assertEqual({uuid.UUID(r.chunk_row_id) for r in self.rows if r.paper_id=="P2"}, {r[0] for r in ranked})
        ranked = self.store.rank(str(self.user),"m",2,[1,0],["P1","P2"],2,-1,1)
        self.assertEqual({uuid.UUID(self.rows[0].chunk_row_id),uuid.UUID(self.rows[3].chunk_row_id)}, {r[0] for r in ranked})
        with self.assertRaises(WorkflowValidationError):
            self.store.rank(str(self.other),"m",2,[1,0],["P1"],10,-1)
        with self.assertRaises(WorkflowValidationError):
            self.store.rank(str(self.user),"wrong-model",2,[1,0],["P1"],10,-1)

    def test_deleted_paper_and_changed_hash_not_returned(self):
        self.publish()
        with database_session(self.sessions) as s:
            s.execute(update(LibraryPaper).where(LibraryPaper.paper_id=="P1").values(deleted_at=utc_now()))
            s.execute(update(LibraryDocumentChunk).where(LibraryDocumentChunk.id==uuid.UUID(self.rows[3].chunk_row_id)).values(content_sha256="changed"))
        result = self.store.rank(str(self.user),"m",2,[1,0],["P1","P2"],10,-1)
        self.assertEqual([uuid.UUID(self.rows[4].chunk_row_id)], [r[0] for r in result])

    def test_update_preserves_other_papers_and_old_snapshot(self):
        first = self.publish()
        with self.store.borrow(str(self.user),"m",2) as old:
            before = old.read_bytes()
            self.publish([replace(self.rows[0], values=[0,1])], replace_indexes=(self.rows[0].index_id,))
            self.assertEqual(before, old.read_bytes())
            with self.store.adapter.connect(old,2,readonly=True) as db:
                self.assertEqual(5,db.execute("SELECT count(*) FROM vectors").fetchone()[0])
        with self.store.borrow(str(self.user),"m",2) as new:
            with self.store.adapter.connect(new,2,readonly=True) as db:
                self.assertEqual(3,db.execute("SELECT count(*) FROM vectors").fetchone()[0])

    def test_busy_and_expired_publishers_are_fenced(self):
        with self.store.writer(self.user) as fence:
            with self.assertRaises(WorkflowValidationError):
                with self.store.writer(self.user):
                    pass
            with database_session(self.sessions) as s:
                s.execute(update(LibraryVectorStore).where(LibraryVectorStore.user_id==self.user)
                    .values(lease_expires_at=utc_now()-timedelta(seconds=1)))
            with self.assertRaises(WorkflowValidationError):
                self.publish(fence=fence)
        with database_session(self.sessions) as session:
            self.assertEqual({}, session.get(LibraryVectorStore, self.user).heads_json)

    def test_failed_commit_keeps_head_and_releases_writer(self):
        first=self.publish()
        with patch.object(self.store.storage,"commit_object",side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.publish()
        with database_session(self.sessions) as s:
            row=s.get(LibraryVectorStore,self.user)
            self.assertEqual([first],list(row.heads_json.values()))
            self.assertIsNone(row.lease_token)

    def test_prune_keeps_parent_and_active_reader(self):
        first=self.publish()
        with self.store.borrow(str(self.user),"m",2) as old:
            self.publish(); self.publish()
            with database_session(self.sessions) as s:
                s.execute(update(LibraryVectorVersion).values(created_at=utc_now()-timedelta(days=2)))
            self.assertEqual([],self.store.prune(self.user))
            self.assertTrue(old.exists())
        self.assertEqual([first],self.store.prune(self.user))

    def test_prune_releases_only_old_failed_pins(self):
        from review_writer_api.workflow_models import WorkflowJob, LibraryVectorJobPin
        first = self.publish()
        self.publish(); self.publish()
        with database_session(self.sessions) as s:
            job = WorkflowJob(user_id=self.user, scope="library", job_type="library.ingest",
                idempotency_key="pin-retention", status="failed")
            s.add(job); s.flush(); job_id = job.id
            s.add(LibraryVectorJobPin(job_id=job.id, profile_key="test", version_id=uuid.UUID(first)))
            s.execute(update(LibraryVectorVersion).values(created_at=utc_now()-timedelta(days=10)))
        self.assertEqual([], self.store.prune(self.user))
        with database_session(self.sessions) as s:
            s.execute(update(WorkflowJob).where(WorkflowJob.id==job_id).values(
                updated_at=utc_now()-timedelta(days=8), status="running"))
        self.assertEqual([], self.store.prune(self.user))
        with database_session(self.sessions) as s:
            s.execute(update(WorkflowJob).where(WorkflowJob.id==job_id).values(
                updated_at=utc_now()-timedelta(days=8), status="failed"))
        self.assertEqual([first], self.store.prune(self.user))

    def test_new_document_uses_sqlite_without_pgvector_or_double_write(self):
        from review_writer_api.domain_services.library_index import LibraryIndexService
        from review_writer_api.security import Principal, Role
        self.publish()
        class Gateway:
            def embed_for_active_job(self, inputs, **kwargs):
                return {"model":"m","dimension":2,"embeddings":[[1,0] for _ in inputs]}
        service=LibraryIndexService(self.sessions,self.workspaces,vector_enabled=True,
            embedding_gateway=Gateway(),sqlite_vector_extension=EXTENSION)
        principal=Principal(str(self.user),frozenset({Role.USER}))
        result=service.build_embeddings(principal,"P1")
        relevance=service.retrieve_paper_relevance(principal,
            [{"query_id":"q1","kind":"topic","query":"Evidence"}],["P1","P2"])
        self.assertEqual("ready",relevance["semantic_status"],relevance)
        self.assertEqual("ready",result["status"],result)
        self.assertEqual(3,result["embedding_count"])

    def test_source_change_during_build_does_not_publish(self):
        first=self.publish()
        original=self.store.adapter.build
        def changing(*args,**kwargs):
            result=original(*args,**kwargs)
            with database_session(self.sessions) as s:
                s.execute(update(LibraryDocumentChunk).where(LibraryDocumentChunk.id==uuid.UUID(self.rows[0].chunk_row_id))
                    .values(content_sha256="new"))
            return result
        with patch.object(self.store.adapter,"build",side_effect=changing), self.assertRaises(WorkflowValidationError):
            self.publish()
        with database_session(self.sessions) as s:
            self.assertEqual([first],list(s.get(LibraryVectorStore,self.user).heads_json.values()))

    def test_task_pin_survives_publication_and_blocks_pruning(self):
        from review_writer_api.workflow_models import WorkflowJob
        from review_writer_api.job_lease_context import bind_job_lease
        first=self.publish()
        job_id,token=uuid.uuid4(),uuid.uuid4()
        with database_session(self.sessions) as s:
            s.add(WorkflowJob(id=job_id,user_id=self.user,scope="library",job_type="test",status="running",
                idempotency_key=str(job_id),lease_token=token,lease_generation=1,
                lease_expires_at=utc_now()+timedelta(minutes=5)))
        with bind_job_lease(str(job_id),str(token),1), self.store.borrow(str(self.user),"m",2) as initial:
            original=initial.read_bytes()
        self.publish(); self.publish()
        with database_session(self.sessions) as s:
            s.execute(update(LibraryVectorVersion).values(created_at=utc_now()-timedelta(days=2)))
        self.assertEqual([],self.store.prune(self.user))
        with bind_job_lease(str(job_id),str(token),1), self.store.borrow(str(self.user),"m",2) as same:
            self.assertEqual(initial,same)
            self.assertEqual(original,same.read_bytes())
        with bind_job_lease(str(job_id),str(uuid.uuid4()),1), self.assertRaises(WorkflowValidationError):
            with self.store.borrow(str(self.user),"m",2):
                pass
        with database_session(self.sessions) as s:
            s.get(WorkflowJob,job_id).status="succeeded"
        self.assertEqual([first],self.store.prune(self.user))

    def test_batch_publishes_once_and_keeps_failed_document_failed(self):
        from review_writer_api.domain_services.library_index import LibraryIndexService
        from review_writer_api.security import Principal, Role
        self.publish()
        class Gateway:
            fail_first=False
            def embed_for_active_job(self,inputs,**kwargs):
                if self.fail_first and any("P1" in v for v in inputs):
                    raise RuntimeError("temporary provider failure")
                return {"model":"m","dimension":2,"embeddings":[[1,0] for _ in inputs]}
        gateway=Gateway()
        service=LibraryIndexService(self.sessions,self.workspaces,vector_enabled=True,
            embedding_gateway=gateway,sqlite_vector_extension=EXTENSION)
        principal=Principal(str(self.user),frozenset({Role.USER}))
        with patch.object(service.vector_store.storage,"commit_object",wraps=service.vector_store.storage.commit_object) as publish:
            result=service.build_embedding_batch(principal,["P1","P2"])
            self.assertEqual(1,publish.call_count)
        self.assertEqual(["ready","ready"],[result[p]["status"] for p in ["P1","P2"]])
        gateway.fail_first=True
        result=service.build_embedding_batch(principal,["P1","P2"])
        self.assertEqual("failed",result["P1"]["status"])
        self.assertEqual("ready",result["P2"]["status"])
        with patch.object(service.vector_store.storage,"commit_object",side_effect=OSError("full")):
            result=service.build_embedding_batch(principal,["P2"])
        self.assertEqual("failed",result["P2"]["status"])
        with database_session(self.sessions) as s:
            status=s.scalar(select(LibraryDocumentIndex.semantic_status).where(
                LibraryDocumentIndex.id==uuid.UUID(self.rows[-1].index_id)))
            self.assertEqual("failed",status)

    def test_concurrent_publishers_merge_latest_base(self):
        if self.engine.dialect.name != "postgresql":
            self.skipTest("Requires PostgreSQL row locking")
        from concurrent.futures import ThreadPoolExecutor
        self.publish()
        groups=[[replace(r,values=[0,1]) for r in self.rows if r.paper_id==p] for p in ("P1","P2")]
        def publish(group):
            return self.store.publish(self.user,"m",2,group,replace_indexes=(group[0].index_id,))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(publish,groups))
        self.assertEqual(2,len(set(results)))
        with self.store.borrow(str(self.user),"m",2) as path:
            with self.store.adapter.connect(path,2,readonly=True) as db:
                blobs=list(db.execute("SELECT embedding FROM vectors"))
                self.assertEqual(5,len(blobs))
                self.assertTrue(all(row[0]==vector_blob([0,1],2) for row in blobs))

    def test_crashed_staging_cleanup_is_narrow_and_age_guarded(self):
        import time
        staging=self.workspaces.trusted_user_directory(str(self.user),".review-writer","vector-staging")
        old=staging/f"build-{uuid.uuid4()}-old"
        active=staging/f"build-{uuid.uuid4()}-recent"
        unknown=staging/f"build-{uuid.uuid4()}-unknown"
        for directory in (old,active,unknown):
            directory.mkdir()
            (directory/"index.sqlite").write_bytes(b"unpublished")
        (unknown/"keep.txt").write_text("unknown user file")
        before=time.time()-2*86400
        for directory in (old,unknown):
            for file in directory.iterdir():
                os.utime(file,(before,before))
            os.utime(directory,(before,before))
        self.store.prune(self.user)
        self.assertFalse(old.exists())
        self.assertTrue(active.exists())
        self.assertTrue((unknown/"keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
