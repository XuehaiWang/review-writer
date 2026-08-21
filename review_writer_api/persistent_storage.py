"""Persistent file storage contract used by workflow artifacts and exports.

P0 ships only the local-volume backend. The interface deliberately separates
database lineage from byte storage so an object-store backend can be added
without changing stage services or public APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from review_writer_api.errors import WorkflowValidationError


@dataclass(frozen=True)
class StoredFileStat:
    size_bytes: int
    mtime_ns: int


class PersistentStorage(Protocol):
    def commit_staged(self, source: Path, destination: Path) -> StoredFileStat: ...

    def resolve(self, root: Path, relative_path: Path) -> Path: ...

    def trash(self, source: Path, destination: Path) -> Path: ...


class LocalPersistentStorage:
    """Atomic local-volume implementation preserving existing file layout."""

    def commit_staged(self, source: Path, destination: Path) -> StoredFileStat:
        destination.parent.mkdir(parents=True, exist_ok=False)
        if source.stat().st_dev != destination.parent.stat().st_dev:
            raise WorkflowValidationError(
                "Staging and persistent artifact storage must use the same filesystem."
            )
        stat = source.stat()
        source.replace(destination)
        return StoredFileStat(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)

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
