"""Persistent file storage contract used by workflow artifacts and exports.

P0 ships only the local-volume backend. The interface deliberately separates
database lineage from byte storage so an object-store backend can be added
without changing stage services or public APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol
from review_writer_api.ttl_cache import TTLCache

from review_writer_api.errors import WorkflowValidationError


@dataclass(frozen=True)
class StoredFileStat:
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class PersistentStorage(Protocol):
    def commit_staged(self, source: Path, destination: Path) -> StoredFileStat: ...

    def resolve(self, root: Path, relative_path: Path) -> Path: ...

    def trash(self, source: Path, destination: Path) -> Path: ...

    def commit_object(self, source: Path, root: Path, key: str) -> StoredObject: ...

    def inspect_object(self, root: Path, key: str) -> StoredObject: ...

    def borrow_object(self, root: Path, obj: StoredObject): ...


class LocalPersistentStorage:
    """Atomic local-volume implementation preserving existing file layout."""

    def __init__(self):
        self._verified = TTLCache(capacity=256, ttl=300)

    @staticmethod
    def _identity(path):
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def commit_staged(self, source: Path, destination: Path) -> StoredFileStat:
        destination.parent.mkdir(parents=True, exist_ok=False)
        if source.stat().st_dev != destination.parent.stat().st_dev:
            self.commit_object(source, destination.parent, destination.name)
            source.unlink()
            stat = destination.stat()
            return StoredFileStat(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        stat = source.stat()
        source.replace(destination)
        return StoredFileStat(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def _object_path(self, root: Path, key: str) -> Path:
        if (not key or "\\" in key or PurePosixPath(key).is_absolute()
                or PureWindowsPath(key).drive or ".." in PurePosixPath(key).parts):
            raise WorkflowValidationError("Persistent object key must be a relative portable path.")
        return self.resolve(root, Path(key))

    def inspect_object(self, root: Path, key: str) -> StoredObject:
        path = self._object_path(root, key)
        if not path.is_file():
            raise WorkflowValidationError("Persistent object is missing.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return StoredObject(key, path.stat().st_size, digest)

    def commit_object(self, source: Path, root: Path, key: str) -> StoredObject:
        """Publish once, including across filesystems; never replace a version."""
        destination = self._object_path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the final name with a hard link after copying and fsyncing on
        # the destination filesystem. Unlike replace(), this cannot overwrite.
        import os
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as out:
                temporary = Path(out.name)
                with source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, out)
                out.flush()
                os.fsync(out.fileno())
            with source.open("rb") as incoming, temporary.open("rb") as copied:
                if hashlib.file_digest(incoming, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
                    raise WorkflowValidationError("Persistent object copy failed verification.")
            os.link(temporary, destination)
            if os.name != "nt":
                fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return self.inspect_object(root, key)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def borrow_object(self, root: Path, obj: StoredObject):
        """Borrow formal bytes, NOT a cache entry. Release must never delete it.

        Caller owns the database version reference for the duration of use.
        """
        path = self._object_path(root, obj.key)
        identity = self._identity(path)
        key = (str(path), obj)
        if self._verified.get(key) != identity:
            if self.inspect_object(root, obj.key) != obj or self._identity(path) != identity:
                raise WorkflowValidationError("Persistent object integrity check failed.")
            self._verified[key] = identity
        yield path
        if self._identity(path) != identity:
            raise WorkflowValidationError("Persistent object changed while borrowed.")

    def resolve(self, root: Path, relative_path: Path) -> Path:
        trusted_root = root.resolve()
        path = (trusted_root / relative_path).resolve()
        try:
            path.relative_to(trusted_root)
        except ValueError as exc:
            raise WorkflowValidationError(
                "Persistent storage path escaped its workspace."
            ) from exc
        return path

    def trash(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise WorkflowValidationError(
                "Workspace and persistent trash must use the same filesystem."
            )
        source.replace(destination)
        return destination
