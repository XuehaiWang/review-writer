"""Local vector publication coordinator; PostgreSQL remains the sole pointer.

No provider calls occur here. Leases fence publishers; readers borrow formal
objects under durable read leases, so no download cache or new service is needed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import shutil
import tempfile
import time
import threading
import uuid

from sqlalchemy import select, update, or_, delete
from sqlalchemy.exc import IntegrityError

from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import WorkflowValidationError
from review_writer_api.job_lease_context import active_job_lease
from review_writer_api.persistent_storage import LocalPersistentStorage, StoredObject
from review_writer_api.sqlite_vectors import SQLiteVectors, VectorRow
from review_writer_api.workflow_models import (
    LibraryVectorStore, LibraryVectorVersion, LibraryVectorReadLease,
    LibraryPaper, LibraryDocumentIndex, LibraryDocumentChunk,
    LibraryVectorJobPin, WorkflowJob,
)


def profile_key(model, dimension):
    return hashlib.sha256(json.dumps(["retrieval_embedding", model, dimension, "COSINE", "FLOAT32"]).encode()).hexdigest()


@contextmanager
def renewed_lease(renew, *, check_result=True):
    """Keep a cross-process DB lease alive through hashing and long I/O.

    A failed renewal never authorizes publication: callers still fence inside
    their final transaction. Read callers also reject results after lease loss.
    """
    stopped, lost = threading.Event(), threading.Event()
    def heartbeat():
        while not stopped.wait(45):
            try:
                if not renew():
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    thread = threading.Thread(target=heartbeat, name="vector-lease", daemon=True)
    thread.start()
    try:
        yield
        if check_result and lost.is_set():
            raise WorkflowValidationError("Vector storage lease renewal failed; retry the operation.")
    finally:
        stopped.set()
        thread.join(timeout=1)


class LocalVectorStore:
    def __init__(self, sessions, workspaces, extension):
        self.sessions = sessions
        self.workspaces = workspaces
        self.adapter = SQLiteVectors(extension)
        self.storage = LocalPersistentStorage()

    def _ensure(self, user_id):
        try:
            with database_session(self.sessions) as session:
                if session.get(LibraryVectorStore, user_id) is None:
                    session.add(LibraryVectorStore(user_id=user_id))
        except IntegrityError:
            # A concurrent first publication may have inserted the same user.
            with database_session(self.sessions) as session:
                if session.get(LibraryVectorStore, user_id) is None:
                    raise

    @contextmanager
    def writer(self, user_id, *, wait_seconds=0):
        user_id = uuid.UUID(str(user_id))
        self._ensure(user_id)
        token = uuid.uuid4()
        deadline = time.monotonic()+wait_seconds
        while True:
            with database_session(self.sessions) as session:
                generation = session.execute(update(LibraryVectorStore).where(
                    LibraryVectorStore.user_id == user_id,
                    or_(LibraryVectorStore.lease_token.is_(None), LibraryVectorStore.lease_expires_at < utc_now()),
                ).values(lease_token=token, lease_expires_at=utc_now()+timedelta(minutes=5),
                         generation=LibraryVectorStore.generation+1).returning(LibraryVectorStore.generation)).scalar_one_or_none()
            if generation is not None:
                break
            if time.monotonic() >= deadline:
                raise WorkflowValidationError("The user's vector library is being updated; retry this index task.")
            time.sleep(0.25)
        try:
            def renew():
                with database_session(self.sessions) as session:
                    return session.execute(update(LibraryVectorStore).where(
                        LibraryVectorStore.user_id==user_id, LibraryVectorStore.lease_token==token,
                        LibraryVectorStore.generation==generation, LibraryVectorStore.lease_expires_at>utc_now(),
                    ).values(lease_expires_at=utc_now()+timedelta(minutes=5))).rowcount == 1
            with renewed_lease(renew, check_result=False):
                yield token, generation
        finally:
            with database_session(self.sessions) as session:
                session.execute(update(LibraryVectorStore).where(
                    LibraryVectorStore.user_id == user_id, LibraryVectorStore.lease_token == token,
                    LibraryVectorStore.generation == generation,
                ).values(lease_token=None, lease_expires_at=None))

    def _fence(self, session, user_id, fence):
        token, generation = fence
        row = session.scalar(select(LibraryVectorStore).where(
            LibraryVectorStore.user_id == uuid.UUID(str(user_id)),
            LibraryVectorStore.lease_token == token, LibraryVectorStore.generation == generation,
            LibraryVectorStore.lease_expires_at > utc_now(),
        ).with_for_update())
        if row is None:
            raise WorkflowValidationError("Vector publisher lost its lease; no pointer was changed.")
        active = active_job_lease()
        if active is not None:
            job = session.scalar(select(WorkflowJob).where(
                WorkflowJob.id == uuid.UUID(active.job_id),
                WorkflowJob.user_id == uuid.UUID(str(user_id)),
                WorkflowJob.lease_token == uuid.UUID(active.lease_token),
                WorkflowJob.lease_generation == active.lease_generation,
                WorkflowJob.lease_expires_at > utc_now(), WorkflowJob.status == "running").with_for_update())
            if job is None:
                raise WorkflowValidationError("Vector task lost its execution lease or was cancelled.")
        row.lease_expires_at = utc_now()+timedelta(minutes=5)
        return row

    def source_rows(self, session, user_id, *, papers=None, model=None, dimension=None):
        stmt = select(LibraryDocumentChunk).join(LibraryDocumentIndex,
            LibraryDocumentChunk.index_id == LibraryDocumentIndex.id).join(LibraryPaper,
            LibraryPaper.id == LibraryDocumentIndex.library_paper_id).where(
            LibraryDocumentChunk.user_id == uuid.UUID(str(user_id)),
            LibraryDocumentIndex.user_id == uuid.UUID(str(user_id)),
            LibraryPaper.user_id == uuid.UUID(str(user_id)),
            LibraryPaper.status == "active", LibraryPaper.deleted_at.is_(None),
            LibraryDocumentIndex.status == "ready", LibraryDocumentIndex.is_current.is_(True),
            LibraryDocumentChunk.is_reference.is_(False),
        )
        if papers is not None:
            stmt = stmt.where(LibraryDocumentChunk.paper_id.in_(papers))
        if model is not None:
            stmt = stmt.where(LibraryDocumentIndex.semantic_status == "ready",
                LibraryDocumentIndex.embedding_model_snapshot == model,
                LibraryDocumentIndex.embedding_dimension == dimension)
        return list(session.scalars(stmt.order_by(LibraryDocumentChunk.id)))

    @staticmethod
    def source_hash(rows):
        return hashlib.sha256(json.dumps([(str(r.id), str(r.index_id), r.content_sha256)
            for r in rows], separators=(",", ":")).encode()).hexdigest()

    def publish(self, user_id, model, dimension, rows: list[VectorRow], *,
                replace_indexes=(), fence=None, ready_indexes=()):
        """Source CAS + file-before-pointer + readiness in ONE DB commit."""
        if fence is None:
            with self.writer(user_id, wait_seconds=30) as claimed:
                return self.publish(user_id, model, dimension, rows, replace_indexes=replace_indexes,
                                    fence=claimed, ready_indexes=ready_indexes)
        user_id = str(uuid.UUID(str(user_id)))
        key = profile_key(model, dimension)
        with database_session(self.sessions) as session:
            store = self._fence(session, user_id, fence)
            heads = dict(store.heads_json)
            base_id = heads.get(key)
            base = session.get(LibraryVectorVersion, uuid.UUID(base_id)) if base_id else None
            sources = self.source_rows(session, user_id)
            source_hash = self.source_hash(sources)
        permitted = {str(r.id): r for r in sources}
        for index_id in ready_indexes:
            required = {str(r.id) for r in sources if str(r.index_id)==str(index_id) and r.content.strip()}
            supplied = {r.chunk_row_id for r in rows if r.index_id==str(index_id)}
            if required != supplied:
                raise WorkflowValidationError("Cannot publish an incomplete document embedding set.")
        for row in rows:
            source = permitted.get(row.chunk_row_id)
            if source is None or (row.paper_id, row.index_id, row.content_sha256) != (
                source.paper_id, str(source.index_id), source.content_sha256):
                raise WorkflowValidationError("Vector update does not match a current authorized source.")
        root = self.workspaces.user_root(user_id)
        staging = self.workspaces.trusted_user_directory(user_id, ".review-writer", "vector-staging")
        required_bytes = max(16*1024*1024, (base.size_bytes if base else 0)*3 + len(rows)*dimension*12)
        if shutil.disk_usage(staging).free < required_bytes:
            raise WorkflowValidationError("Insufficient free space to publish a safe vector snapshot.")
        version_id = uuid.uuid4()
        self.workspaces.trusted_user_directory(user_id, "review-library", "vectors", str(version_id))
        object_key = f"review-library/vectors/{version_id}/index.sqlite"
        with tempfile.TemporaryDirectory(prefix=f"build-{fence[0]}-", dir=staging) as temporary:
            from pathlib import Path
            candidate = Path(temporary)/"index.sqlite"
            base_path = None
            if base:
                obj = StoredObject(base.object_key, base.size_bytes, base.sha256)
                if self.storage.inspect_object(root, obj.key) != obj:
                    raise WorkflowValidationError("Base vector snapshot is missing or corrupted.")
                base_path = self.storage.resolve(root, Path(obj.key))
            count = self.adapter.build(candidate, user_id=user_id, model=model, dimension=dimension,
                rows=rows, base=base_path, replace_indexes=tuple(replace_indexes),
                valid_indexes=tuple({str(r.index_id) for r in sources}))
            with database_session(self.sessions) as session:
                self._fence(session, user_id, fence)  # renew before final file verification
            obj = self.storage.commit_object(candidate, root, object_key)
        with database_session(self.sessions) as session:
            store = self._fence(session, user_id, fence)
            if store.heads_json != heads or self.source_hash(self.source_rows(session, user_id)) != source_hash:
                raise WorkflowValidationError("Vector sources changed during publication; retry without overwriting the current version.")
            session.add(LibraryVectorVersion(id=version_id, user_id=uuid.UUID(user_id), profile_key=key,
                model=model, dimension=dimension, object_key=obj.key, size_bytes=obj.size_bytes,
                sha256=obj.sha256, row_count=count, source_hash=source_hash,
                parent_id=uuid.UUID(base_id) if base_id else None))
            store.heads_json = {**heads, key: str(version_id)}
            for index_id in ready_indexes:
                index = session.get(LibraryDocumentIndex, uuid.UUID(str(index_id)))
                if index is None or str(index.user_id) != user_id or not index.is_current:
                    raise WorkflowValidationError("Document source changed before vector publication.")
                index.semantic_status = "ready"
                index.embedding_profile = "retrieval_embedding"
                index.embedding_model_snapshot = model
                index.embedding_dimension = dimension
                index.embedding_count = sum(r.index_id == str(index_id) for r in rows)
                index.semantic_error_code = index.semantic_error_message = ""
        return str(version_id)

    @contextmanager
    def borrow(self, user_id, model, dimension):
        user_id = uuid.UUID(str(user_id))
        lease_id = uuid.uuid4()
        with database_session(self.sessions) as session:
            store = session.scalar(select(LibraryVectorStore).where(
                LibraryVectorStore.user_id == user_id).with_for_update())
            key = profile_key(model, dimension)
            raw = (store.heads_json if store else {}).get(key)
            active = active_job_lease()
            job = None
            if active:
                job = session.scalar(select(WorkflowJob).where(
                    WorkflowJob.id == uuid.UUID(active.job_id), WorkflowJob.user_id == user_id,
                    WorkflowJob.status == "running", WorkflowJob.lease_token == uuid.UUID(active.lease_token),
                    WorkflowJob.lease_generation == active.lease_generation, WorkflowJob.lease_expires_at > utc_now()))
                if job is None:
                    raise WorkflowValidationError("Vector query lost its user task lease.")
            pin = session.get(LibraryVectorJobPin, (uuid.UUID(active.job_id), key)) if active else None
            if pin:
                raw = str(pin.version_id)
            version = session.get(LibraryVectorVersion, uuid.UUID(raw)) if raw else None
            if version is None or version.user_id != user_id:
                raise WorkflowValidationError("No published SQLite snapshot matches the embedding model.")
            if active and not pin:
                session.add(LibraryVectorJobPin(job_id=job.id, profile_key=key, version_id=version.id))
            session.add(LibraryVectorReadLease(id=lease_id, version_id=version.id,
                expires_at=utc_now()+timedelta(minutes=10)))
        try:
            def renew():
                with database_session(self.sessions) as session:
                    return session.execute(update(LibraryVectorReadLease).where(
                        LibraryVectorReadLease.id==lease_id, LibraryVectorReadLease.expires_at>utc_now(),
                    ).values(expires_at=utc_now()+timedelta(minutes=10))).rowcount == 1
            with renewed_lease(renew), self.storage.borrow_object(self.workspaces.user_root(str(user_id)),
                    StoredObject(version.object_key, version.size_bytes, version.sha256)) as path:
                yield path
        finally:
            with database_session(self.sessions) as session:
                session.execute(delete(LibraryVectorReadLease).where(LibraryVectorReadLease.id == lease_id))

    def rank(self, user_id, model, dimension, query, papers, limit, min_similarity, paper_limit=0):
        with self.borrow(user_id, model, dimension) as path:
            with database_session(self.sessions) as session:
                sources = self.source_rows(session, user_id, papers=papers, model=model, dimension=dimension)
                allowed = {str(r.id): r.content_sha256 for r in sources}
            ranked = self.adapter.rank(path, user_id=str(user_id), model=model, dimension=dimension,
                query=query, allowed_chunks=allowed, limit=limit,
                min_similarity=min_similarity, per_paper_limit=paper_limit)
            # Recheck revocation/current source after the scan, never trust an old cache.
            with database_session(self.sessions) as session:
                current = {str(r.id): r.content_sha256 for r in self.source_rows(
                    session, user_id, papers=papers, model=model, dimension=dimension)}
            return [(uuid.UUID(k), v) for k, v in ranked if k in current and current[k] == allowed[k]]

    def prune(self, user_id):
        """Only registered older snapshots; never recurse into workspace/cache."""
        removed = []
        with self.writer(user_id) as fence:
            with database_session(self.sessions) as session:
                store = self._fence(session, user_id, fence)
                versions = list(session.scalars(select(LibraryVectorVersion).where(
                    LibraryVectorVersion.user_id == uuid.UUID(str(user_id)))))
                keep = {uuid.UUID(v) for v in store.heads_json.values()}
                keep |= {v.parent_id for v in versions if v.id in keep and v.parent_id}
                keep |= set(session.scalars(select(LibraryVectorReadLease.version_id).where(
                    LibraryVectorReadLease.expires_at > utc_now())))
                session.execute(delete(LibraryVectorReadLease).where(LibraryVectorReadLease.expires_at <= utc_now()))
                # Allow seven days for failed-job diagnosis/retry. Active and
                # queued jobs remain pinned without a time limit; a new retry
                # job acquires its own current snapshot pin.
                completed = select(WorkflowJob.id).where(or_(
                    WorkflowJob.status.in_(("succeeded", "cancelled")),
                    (WorkflowJob.status == "failed") &
                    (WorkflowJob.updated_at < utc_now() - timedelta(days=7))))
                session.execute(delete(LibraryVectorJobPin).where(LibraryVectorJobPin.job_id.in_(completed)))
                keep |= set(session.scalars(select(LibraryVectorJobPin.version_id)))
                for version in versions:
                    created = version.created_at
                    cutoff = utc_now()-timedelta(days=1)
                    if created.tzinfo is None:
                        cutoff = cutoff.replace(tzinfo=None)
                    if version.id in keep or created > cutoff:
                        continue
                    from pathlib import Path
                    if version.object_key != f"review-library/vectors/{version.id}/index.sqlite":
                        raise WorkflowValidationError("Refusing to prune an object outside the vector namespace.")
                    path = self.storage.resolve(self.workspaces.user_root(str(user_id)), Path(version.object_key))
                    path.unlink(missing_ok=True)
                    session.delete(version)
                    removed.append(str(version.id))
                # Failed file-before-pointer commits leave unregistered UUID
                # objects. No valid publisher can compete while we hold its lease.
                registered = {str(v.id) for v in versions}
                directory = self.workspaces.trusted_user_directory(str(user_id), "review-library", "vectors")
                cutoff_time = time.time()-86400
                for child in directory.iterdir():
                    if child.is_symlink() or not child.is_dir() or child.name in registered:
                        continue
                    try:
                        if str(uuid.UUID(child.name)) != child.name:
                            continue
                    except ValueError:
                        continue
                    path = child/"index.sqlite"
                    if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff_time:
                        path.unlink()
                        removed.append(child.name)
                # Only our fenced SQLite work directories, never arbitrary job
                # staging or user files. The exclusive writer lease rules out a
                # valid builder; a day of grace also protects crash recovery.
                staging = self.workspaces.trusted_user_directory(str(user_id), ".review-writer", "vector-staging")
                for child in staging.iterdir():
                    if child.is_symlink() or not child.is_dir() or not child.name.startswith("build-"):
                        continue
                    try:
                        token = uuid.UUID(child.name[6:42])
                    except ValueError:
                        continue
                    if token == fence[0] or child.stat().st_mtime >= cutoff_time:
                        continue
                    files = list(child.iterdir())
                    if any(p.is_symlink() or not p.is_file() or p.name not in {
                        "index.sqlite", "index.sqlite-journal", "index.sqlite-wal", "index.sqlite-shm"
                    } or p.stat().st_mtime >= cutoff_time for p in files):
                        continue
                    for path in files:
                        path.unlink()
                    child.rmdir()
        return removed
