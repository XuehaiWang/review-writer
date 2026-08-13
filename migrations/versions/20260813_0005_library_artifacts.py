"""Add immutable user-owned Library artifact versions.

Revision ID: 20260813_0005
Revises: 20260813_0004
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import sqlalchemy as sa
from alembic import op


revision = "20260813_0005"
down_revision = "20260813_0004"
branch_labels = None
depends_on = None

PAPER_ID = re.compile(r"^P[0-9]{1,93}$")
ARTIFACT_NAMESPACE = uuid.UUID("9de45402-0dc0-469c-940a-188745fa5a9a")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_user_file(user_root: Path, relative_path: str, *, label: str) -> Path:
    raw = str(relative_path or "")
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise RuntimeError(f"Existing Library {label} path is unsafe: {raw!r}")
    current = user_root
    for part in posix.parts:
        lexical = current / part
        if lexical.is_symlink():
            raise RuntimeError(
                f"Existing Library {label} path contains a symbolic link: {raw!r}"
            )
        resolved = lexical.resolve()
        if lexical.is_symlink() or resolved.parent != current:
            raise RuntimeError(
                f"Existing Library {label} escaped its user workspace: {raw!r}"
            )
        current = resolved
    return current


def _trusted_root(path: Path, *, label: str) -> Path:
    lexical = path.expanduser().absolute()
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        candidate = current / part
        if candidate.is_symlink():
            raise RuntimeError(
                f"Existing Library {label} directory is a symbolic link."
            )
        candidate.mkdir(exist_ok=True)
        resolved = candidate.resolve()
        if candidate.is_symlink() or resolved.parent != current:
            raise RuntimeError(
                f"Existing Library {label} directory is not trusted."
            )
        current = resolved
    return current


def _trusted_child_directory(current: Path, part: str, *, label: str) -> Path:
    if not part or part in {".", ".."} or Path(part).name != part:
        raise RuntimeError(f"Existing Library {label} component is unsafe.")
    lexical = current / part
    if lexical.is_symlink():
        raise RuntimeError(f"Existing Library {label} directory is a symbolic link.")
    lexical.mkdir(exist_ok=True)
    resolved = lexical.resolve()
    if lexical.is_symlink() or resolved.parent != current:
        raise RuntimeError(f"Existing Library {label} directory escaped its user root.")
    return resolved


def _publish_bytes(destination: Path, content: bytes) -> None:
    if destination.is_symlink():
        raise RuntimeError(
            f"Immutable Library artifact is a symbolic link: {destination}"
        )
    if destination.is_file():
        if destination.read_bytes() != content:
            raise RuntimeError(
                f"Immutable Library artifact already exists with different content: {destination}"
            )
        return
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_file(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise RuntimeError(
            f"Immutable Library artifact is a symbolic link: {destination}"
        )
    if destination.is_file():
        if _sha256(destination) != _sha256(source):
            raise RuntimeError(
                f"Immutable Library artifact already exists with different content: {destination}"
            )
        return
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _backfill_existing_library_artifacts() -> None:
    bind = op.get_bind()
    papers = sa.table(
        "library_papers",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("paper_id", sa.String()),
        sa.column("metadata_json", sa.JSON()),
        sa.column("pdf_relative_path", sa.String()),
        sa.column("markdown_relative_path", sa.String()),
        sa.column("status", sa.String()),
        sa.column("deleted_at", sa.DateTime(timezone=True)),
    )
    artifacts = sa.table(
        "library_artifacts",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("paper_id", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("relative_path", sa.String()),
        sa.column("content_sha256", sa.String()),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("mtime_ns", sa.BigInteger()),
        sa.column("availability", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = list(bind.execute(sa.select(papers)).mappings())
    if not rows:
        return
    configured_root = str(
        os.environ.get("REVIEW_WRITER_HOSTED_WORKSPACE_ROOT") or ""
    ).strip()
    if not configured_root:
        raise RuntimeError(
            "REVIEW_WRITER_HOSTED_WORKSPACE_ROOT is required to backfill existing Library artifacts."
        )
    hosted_root = _trusted_root(
        Path(configured_root), label="hosted workspace"
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        paper_id = str(row["paper_id"] or "")
        if not PAPER_ID.fullmatch(paper_id):
            raise RuntimeError(
                f"Existing Library paper_id cannot be migrated safely: {paper_id!r}"
            )
        user_id = uuid.UUID(str(row["user_id"]))
        user_root = _trusted_child_directory(
            hosted_root, str(user_id), label="user workspace"
        )
        pdf_source = _safe_user_file(
            user_root, row["pdf_relative_path"], label="PDF"
        )
        markdown_source = _safe_user_file(
            user_root, row["markdown_relative_path"], label="Markdown"
        )
        active = row["deleted_at"] is None and str(row["status"] or "") != "deleted"
        if active and not pdf_source.is_file():
            raise RuntimeError(
                f"Active Library paper {paper_id} is missing its PDF: {pdf_source}"
            )
        if active and not markdown_source.is_file():
            raise RuntimeError(
                f"Active Library paper {paper_id} is missing its Markdown: {markdown_source}"
            )
        artifact_ids = {
            kind: uuid.uuid5(ARTIFACT_NAMESPACE, f"{row['id']}:{kind}")
            for kind in ("pdf", "markdown", "metadata")
        }
        review_library = _trusted_child_directory(
            user_root, "review-library", label="Library"
        )
        artifacts_root = _trusted_child_directory(
            review_library, ".artifacts", label="artifact"
        )
        paper_root = _trusted_child_directory(
            artifacts_root, paper_id, label="paper artifact"
        )
        destinations = {
            "pdf": paper_root / str(artifact_ids["pdf"]) / f"{paper_id}.pdf",
            "markdown": (
                paper_root / str(artifact_ids["markdown"]) / f"{paper_id}.md"
            ),
            "metadata": (
                paper_root
                / str(artifact_ids["metadata"])
                / f"{paper_id}.metadata.json"
            ),
        }
        for kind, destination in destinations.items():
            version_root = _trusted_child_directory(
                paper_root, str(artifact_ids[kind]), label="artifact version"
            )
            if destination.parent != version_root:
                raise RuntimeError("Library artifact destination escaped its version root.")
        if pdf_source.is_file():
            _publish_file(pdf_source, destinations["pdf"])
        if markdown_source.is_file():
            _publish_file(markdown_source, destinations["markdown"])
        relative_paths = {
            kind: destination.relative_to(user_root).as_posix()
            for kind, destination in destinations.items()
        }
        metadata = dict(row["metadata_json"] or {})
        metadata["paper_id"] = paper_id
        metadata["_artifact_ids"] = {
            kind: str(artifact_id) for kind, artifact_id in artifact_ids.items()
        }
        metadata["_artifact_paths"] = dict(relative_paths)
        source_paths = dict(metadata.get("source_paths") or {})
        source_paths["pdf"] = str(destinations["pdf"])
        source_paths["markdown"] = str(destinations["markdown"])
        metadata["source_paths"] = source_paths
        source_file = dict(metadata.get("source_file") or {})
        source_file["pdf_name"] = destinations["pdf"].name
        source_file["relative_pdf_path"] = relative_paths["pdf"]
        if destinations["pdf"].is_file():
            source_file["sha256"] = _sha256(destinations["pdf"])
        metadata["source_file"] = source_file
        metadata_bytes = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _publish_bytes(destinations["metadata"], metadata_bytes)
        artifact_rows = []
        for kind, destination in destinations.items():
            available = active and destination.is_file()
            stat = destination.stat() if destination.is_file() else None
            content_sha256 = (
                _sha256(destination)
                if destination.is_file()
                else hashlib.sha256(
                    f"missing:{user_id}:{paper_id}:{kind}".encode("utf-8")
                ).hexdigest()
            )
            artifact_rows.append(
                {
                    "id": artifact_ids[kind],
                    "user_id": user_id,
                    "paper_id": paper_id,
                    "kind": kind,
                    "relative_path": relative_paths[kind],
                    "content_sha256": content_sha256,
                    "size_bytes": stat.st_size if stat else 0,
                    "mtime_ns": stat.st_mtime_ns if stat else 0,
                    "availability": "available" if available else "trashed",
                    "created_at": now,
                }
            )
        bind.execute(sa.insert(artifacts), artifact_rows)
        bind.execute(
            sa.update(papers)
            .where(papers.c.id == row["id"])
            .values(
                metadata_json=metadata,
                pdf_relative_path=relative_paths["pdf"],
                markdown_relative_path=relative_paths["markdown"],
            )
        )


def upgrade() -> None:
    op.create_table(
        "library_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.String(length=96), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_library_artifacts_user_id", "library_artifacts", ["user_id"]
    )
    op.create_index(
        "ix_library_artifacts_user_paper_kind",
        "library_artifacts",
        ["user_id", "paper_id", "kind"],
    )
    _backfill_existing_library_artifacts()


def downgrade() -> None:
    op.drop_table("library_artifacts")
