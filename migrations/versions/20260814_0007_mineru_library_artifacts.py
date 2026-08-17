"""Backfill immutable MinerU Library artifact versions.

Revision ID: 20260814_0007
Revises: 20260813_0006
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import sqlalchemy as sa
from alembic import op


revision = "20260814_0007"
down_revision = "20260813_0006"
branch_labels = None
depends_on = None

ARTIFACT_NAMESPACE = uuid.UUID("9de45402-0dc0-469c-940a-188745fa5a9a")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _trusted_existing_path(user_root: Path, candidate: Path, *, label: str) -> Path:
    lexical = candidate if candidate.is_absolute() else user_root / candidate
    try:
        relative = lexical.relative_to(user_root)
    except ValueError as exc:
        raise RuntimeError(f"Existing Library {label} escaped its user workspace.") from exc
    current = user_root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise RuntimeError(f"Existing Library {label} path is unsafe.")
        current = current / part
        if _is_link(current):
            raise RuntimeError(f"Existing Library {label} contains a symbolic link or junction.")
        resolved = current.resolve()
        if resolved.parent != current.parent.resolve():
            raise RuntimeError(f"Existing Library {label} escaped its user workspace.")
        current = resolved
    return current


def _recorded_user_path(user_root: Path, raw: object, *, label: str) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    posix = PurePosixPath(value.replace("\\", "/"))
    windows = PureWindowsPath(value)
    candidates: list[Path] = []
    recorded = Path(value)
    if recorded.is_absolute() or windows.is_absolute() or windows.drive:
        candidates.append(recorded)
    else:
        candidates.append(user_root.joinpath(*posix.parts))
    folded = [part.casefold() for part in posix.parts]
    for marker in ("review-library", "mineru-outputs"):
        if marker in folded:
            candidates.append(user_root.joinpath(*posix.parts[folded.index(marker) :]))
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            trusted = _trusted_existing_path(user_root, candidate, label=label)
        except RuntimeError:
            continue
        if trusted.exists():
            return trusted
    return None


def _trusted_child_directory(current: Path, part: str, *, label: str) -> Path:
    if not part or part in {".", ".."} or Path(part).name != part:
        raise RuntimeError(f"Existing Library {label} component is unsafe.")
    lexical = current / part
    if _is_link(lexical):
        raise RuntimeError(f"Existing Library {label} directory is not trusted.")
    lexical.mkdir(exist_ok=True)
    resolved = lexical.resolve()
    if _is_link(lexical) or resolved.parent != current:
        raise RuntimeError(f"Existing Library {label} directory escaped its user workspace.")
    return resolved


def _trusted_root(path: Path, *, label: str) -> Path:
    lexical = path.expanduser().absolute()
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current = _trusted_child_directory(current, part, label=label)
    return current


def _tree_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    manifest: list[tuple[str, str]] = []
    for item in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if _is_link(item):
            raise RuntimeError("MinerU extracted output contains a symbolic link or junction.")
        resolved = item.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("MinerU extracted output escaped its directory.") from exc
        if item.is_file():
            manifest.append((relative.as_posix(), _sha256(item)))
    return tuple(manifest)


def _publish_tree(source: Path, destination: Path) -> None:
    source_manifest = _tree_manifest(source)
    if destination.exists():
        if not destination.is_dir() or _tree_manifest(destination) != source_manifest:
            raise RuntimeError(
                f"Immutable MinerU artifact already exists with different content: {destination}"
            )
        return
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
    try:
        shutil.copytree(source, temporary)
        if _tree_manifest(temporary) != source_manifest:
            raise RuntimeError("Published MinerU artifact did not match its source.")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _publish_bytes(destination: Path, content: bytes) -> None:
    if _is_link(destination):
        raise RuntimeError(f"Immutable Library artifact is not trusted: {destination}")
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


def _backfill_mineru_artifacts() -> None:
    bind = op.get_bind()
    papers = sa.table(
        "library_papers",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("paper_id", sa.String()),
        sa.column("metadata_json", sa.JSON()),
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
    rows = list(
        bind.execute(
            sa.select(papers).where(
                papers.c.deleted_at.is_(None), papers.c.status != "deleted"
            )
        ).mappings()
    )
    candidates = []
    for row in rows:
        metadata = dict(row["metadata_json"] or {})
        source_paths = metadata.get("source_paths") or {}
        extraction_inputs = (metadata.get("extraction") or {}).get("inputs") or {}
        raw_extracted = source_paths.get("extracted_dir") or extraction_inputs.get("extracted_dir")
        raw_content = source_paths.get("content_list") or extraction_inputs.get("content_list")
        if raw_extracted and raw_content:
            candidates.append((row, metadata, raw_extracted, raw_content))
    if not candidates:
        return
    configured_root = str(os.environ.get("REVIEW_WRITER_HOSTED_WORKSPACE_ROOT") or "").strip()
    if not configured_root:
        raise RuntimeError(
            "REVIEW_WRITER_HOSTED_WORKSPACE_ROOT is required to backfill MinerU artifacts."
        )
    hosted_root = _trusted_root(Path(configured_root), label="hosted workspace")
    now = datetime.now(timezone.utc)
    for row, metadata, raw_extracted, raw_content in candidates:
        artifact_ids = dict(metadata.get("_artifact_ids") or {})
        existing_mineru = str(artifact_ids.get("mineru") or "").strip()
        if existing_mineru:
            try:
                existing_uuid = uuid.UUID(existing_mineru)
            except ValueError:
                existing_uuid = None
            if existing_uuid is not None:
                registered = bind.execute(
                    sa.select(artifacts.c.id).where(
                        artifacts.c.id == existing_uuid,
                        artifacts.c.user_id == row["user_id"],
                        artifacts.c.paper_id == row["paper_id"],
                        artifacts.c.kind == "mineru",
                        artifacts.c.availability == "available",
                    )
                ).first()
                if registered is not None:
                    continue

        user_id = uuid.UUID(str(row["user_id"]))
        user_root = _trusted_child_directory(
            hosted_root, str(user_id), label="user workspace"
        )
        extracted_source = _recorded_user_path(
            user_root, raw_extracted, label="MinerU extraction"
        )
        content_source = _recorded_user_path(
            user_root, raw_content, label="MinerU content list"
        )
        if extracted_source is None or content_source is None:
            continue
        if not extracted_source.is_dir() or not content_source.is_file():
            continue
        try:
            content_relative = content_source.relative_to(extracted_source)
        except ValueError:
            continue

        paper_id = str(row["paper_id"])
        mineru_id = uuid.uuid5(ARTIFACT_NAMESPACE, f"{row['id']}:mineru")
        metadata_id = uuid.uuid5(
            ARTIFACT_NAMESPACE, f"{row['id']}:metadata:mineru-backfill"
        )
        review_library = _trusted_child_directory(user_root, "review-library", label="Library")
        artifact_root = _trusted_child_directory(review_library, ".artifacts", label="artifact")
        paper_root = _trusted_child_directory(artifact_root, paper_id, label="paper artifact")
        mineru_version = _trusted_child_directory(
            paper_root, str(mineru_id), label="MinerU artifact version"
        )
        metadata_version = _trusted_child_directory(
            paper_root, str(metadata_id), label="metadata artifact version"
        )
        extracted_destination = mineru_version / "extracted"
        _publish_tree(extracted_source, extracted_destination)
        content_destination = extracted_destination / content_relative
        if not content_destination.is_file():
            raise RuntimeError("Published MinerU content list is unavailable.")

        metadata_destination = metadata_version / f"{paper_id}.metadata.json"
        artifact_paths = dict(metadata.get("_artifact_paths") or {})
        artifact_ids["mineru"] = str(mineru_id)
        artifact_ids["metadata"] = str(metadata_id)
        artifact_paths["mineru"] = content_destination.relative_to(user_root).as_posix()
        artifact_paths["metadata"] = metadata_destination.relative_to(user_root).as_posix()
        metadata["_artifact_ids"] = artifact_ids
        metadata["_artifact_paths"] = artifact_paths
        source_paths = dict(metadata.get("source_paths") or {})
        source_paths["extracted_dir"] = str(extracted_destination)
        source_paths["content_list"] = str(content_destination)
        metadata["source_paths"] = source_paths
        extraction = dict(metadata.get("extraction") or {})
        extraction_inputs = dict(extraction.get("inputs") or {})
        extraction_inputs["extracted_dir"] = str(extracted_destination)
        extraction_inputs["content_list"] = str(content_destination)
        extraction["inputs"] = extraction_inputs
        metadata["extraction"] = extraction
        metadata_bytes = (
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _publish_bytes(metadata_destination, metadata_bytes)

        for artifact_id, kind, path in (
            (mineru_id, "mineru", content_destination),
            (metadata_id, "metadata", metadata_destination),
        ):
            exists = bind.execute(
                sa.select(artifacts.c.id).where(artifacts.c.id == artifact_id)
            ).first()
            if exists is not None:
                continue
            stat = path.stat()
            bind.execute(
                sa.insert(artifacts).values(
                    id=artifact_id,
                    user_id=user_id,
                    paper_id=paper_id,
                    kind=kind,
                    relative_path=path.relative_to(user_root).as_posix(),
                    content_sha256=_sha256(path),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    availability="available",
                    created_at=now,
                )
            )
        bind.execute(
            sa.update(papers)
            .where(papers.c.id == row["id"])
            .values(metadata_json=metadata)
        )


def upgrade() -> None:
    _backfill_mineru_artifacts()


def downgrade() -> None:
    # This migration publishes immutable filesystem versions. Keeping them and
    # their rows is safer than silently restoring mutable legacy paths.
    pass
