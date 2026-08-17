"""Stopped, idempotent migration from legacy workflow SQLite files to PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, func, select

from review_writer_api.database import Project, User, database_session, utc_now
from review_writer_api.workflow_models import (
    LibraryArtifact,
    LibraryPaper,
    WorkflowArtifact,
    WorkflowArtifactDependency,
    WorkflowCurrentArtifact,
    WorkflowCurrentJob,
    WorkflowJob,
    WorkflowMigration,
    WorkflowStageRun,
    WorkflowStageState,
    WorkflowSystemState,
)


LEGACY_TABLES = (
    "projects",
    "stage_runs",
    "stage_state",
    "artifact_versions",
    "current_artifacts",
    "artifact_dependencies",
    "jobs",
    "current_jobs",
)
MIGRATION_NAMESPACE = uuid.UUID("bf59879a-a7a8-4a3c-8524-543f577f5092")
LIBRARY_PROJECT_ID = "_library_"


class WorkflowMigrationError(RuntimeError):
    pass


def _filesystem_path(path: Path) -> Path:
    """Use the Windows extended path form for long migration staging paths."""

    if os.name != "nt":
        return path
    absolute = path.absolute()
    text = str(absolute)
    if text.startswith("\\\\?\\"):
        return absolute
    return Path(f"\\\\?\\{text}")


def assert_application_stopped(session_factory, *, max_age_seconds: int = 30) -> None:
    """Refuse migration while the hosted API heartbeat is recent and running."""

    with database_session(session_factory) as session:
        heartbeat = session.get(WorkflowSystemState, "application_heartbeat")
        if heartbeat is None:
            return
        payload = heartbeat.value_json if isinstance(heartbeat.value_json, dict) else {}
        if str(payload.get("status") or "").lower() != "running":
            return
        observed_raw = payload.get("observed_at")
        observed_at = (
            _parse_datetime(observed_raw)
            if observed_raw
            else heartbeat.updated_at
        )
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_seconds = (utc_now() - observed_at).total_seconds()
        if age_seconds <= max_age_seconds:
            raise WorkflowMigrationError(
                "The Review Writer API heartbeat is current. Stop the API before migration."
            )


@dataclass(frozen=True)
class LegacySourceInventory:
    source_path: str
    review_root: str
    owner_hint: str
    is_local: bool
    source_sha256: str
    table_counts: dict[str, int]


@dataclass(frozen=True)
class MigrationInventory:
    workspace_root: str
    sources: tuple[LegacySourceInventory, ...]
    table_counts: dict[str, int]

    @property
    def source_count(self) -> int:
        return len(self.sources)


@dataclass
class MigrationSourceReport:
    source_path: str
    source_sha256: str
    owner_user_id: str = ""
    status: str = "pending"
    imported_counts: dict[str, int] = field(default_factory=dict)
    missing_files: list[dict[str, str]] = field(default_factory=list)
    backup_path: str = ""
    backup_sha256: str = ""
    errors: list[str] = field(default_factory=list)
    drifted_files: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MigrationReport:
    workspace_root: str
    dry_run: bool
    accept_missing_files: bool
    success: bool
    ready: bool
    sources: list[MigrationSourceReport]
    imported_counts: dict[str, int]
    missing_files: list[dict[str, str]]
    backup_paths: list[str]
    errors: list[str]
    drifted_files: list[dict[str, Any]] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _filesystem_path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _readonly_connection(path: Path):
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _discover_sources(workspace_root: Path) -> list[tuple[Path, Path, str, bool]]:
    lexical_root = workspace_root.expanduser().absolute()
    if lexical_root.is_symlink():
        raise WorkflowMigrationError(
            "Legacy workspace root is a symbolic link and cannot be migrated safely."
        )
    root = lexical_root.resolve()
    if lexical_root != root:
        raise WorkflowMigrationError(
            "Legacy workspace root traverses a symbolic link and cannot be migrated safely."
        )
    discovered: list[tuple[Path, Path, str, bool]] = []
    local_source = _trusted_migration_path(
        root, root / ".review-writer" / "workflow.sqlite3", label="database"
    )
    if local_source.is_file():
        discovered.append((local_source, root, "", True))
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_symlink():
                raise WorkflowMigrationError(
                    f"Legacy user workspace is a symbolic link: {child}"
                )
            if not child.is_dir():
                continue
            candidate = _trusted_migration_path(
                child,
                child / ".review-writer" / "workflow.sqlite3",
                label="database",
            )
            if candidate.is_file():
                discovered.append((candidate, child, child.name, False))
    return discovered


def _table_counts(connection: sqlite3.Connection, source_path: Path) -> dict[str, int]:
    available = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [table for table in LEGACY_TABLES if table not in available]
    if missing:
        raise WorkflowMigrationError(
            f"Legacy workflow database is missing tables {missing}: {source_path}"
        )
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in LEGACY_TABLES
    }


def inventory_legacy_workflows(
    workspace_root: str | Path,
    _session_factory=None,
) -> MigrationInventory:
    root = Path(workspace_root).expanduser().absolute()
    sources: list[LegacySourceInventory] = []
    totals = {table: 0 for table in LEGACY_TABLES}
    for source_path, review_root, owner_hint, is_local in _discover_sources(root):
        with _readonly_connection(source_path) as connection:
            counts = _table_counts(connection, source_path)
        for table, count in counts.items():
            totals[table] += count
        sources.append(
            LegacySourceInventory(
                source_path=str(source_path),
                review_root=str(review_root),
                owner_hint=owner_hint,
                is_local=is_local,
                source_sha256=_sha256_file(source_path),
                table_counts=counts,
            )
        )
    return MigrationInventory(
        workspace_root=str(root),
        sources=tuple(sources),
        table_counts=totals,
    )


def _parse_json(raw: Any, *, field_name: str) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except json.JSONDecodeError as exc:
        raise WorkflowMigrationError(f"Invalid JSON in legacy field {field_name}.") from exc


def _parse_datetime(raw: Any, *, default: datetime | None = None) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return default or utc_now()
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowMigrationError(f"Invalid legacy timestamp: {text}") from exc
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _stable_uuid(source_path: str, record_kind: str, legacy_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(legacy_id))
    except ValueError:
        return uuid.uuid5(MIGRATION_NAMESPACE, f"{source_path}\0{record_kind}\0{legacy_id}")


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def _resolve_owner(session, source: LegacySourceInventory, owner_email: str | None) -> User:
    if source.is_local:
        normalized_email = str(owner_email or "").strip().lower()
        if not normalized_email:
            raise WorkflowMigrationError(
                "A local workflow database requires an explicit --owner-email mapping."
            )
        user = session.scalar(select(User).where(func.lower(User.email) == normalized_email))
    else:
        try:
            owner_id = uuid.UUID(source.owner_hint)
        except ValueError as exc:
            raise WorkflowMigrationError(
                f"Hosted workspace directory is not a user UUID: {source.owner_hint}"
            ) from exc
        user = session.get(User, owner_id)
    if user is None:
        owner = owner_email if source.is_local else source.owner_hint
        raise WorkflowMigrationError(f"No PostgreSQL user matches legacy owner {owner}.")
    return user


def _project_config(review_root: Path, slug: str) -> dict[str, Any]:
    path = review_root / "review-projects" / slug / "project_config.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _project_map(
    session,
    owner: User,
    review_root: Path,
    legacy_projects: list[dict[str, Any]],
) -> dict[str, uuid.UUID | None]:
    mapping: dict[str, uuid.UUID | None] = {LIBRARY_PROJECT_ID: None}
    for row in legacy_projects:
        slug = str(row.get("project_id") or "").strip()
        if not slug or slug == LIBRARY_PROJECT_ID:
            continue
        project = session.scalar(
            select(Project).where(
                Project.user_id == owner.id,
                Project.slug == slug,
                Project.deleted_at.is_(None),
            )
        )
        if project is None:
            config = _project_config(review_root, slug)
            project = Project(
                user_id=owner.id,
                slug=slug,
                topic=str(config.get("topic") or ""),
                taxonomy_profile=str(config.get("taxonomy_profile") or "chemistry_general"),
                status="active",
                current_stage="discovery",
                stage_states={},
                created_at=_parse_datetime(row.get("created_at")),
                updated_at=_parse_datetime(row.get("updated_at")),
            )
            session.add(project)
            session.flush()
        mapping[slug] = project.id
    return mapping


def _validate_legacy_references(tables: dict[str, list[dict[str, Any]]]) -> None:
    projects = {str(row["project_id"]) for row in tables["projects"]}
    run_ids = {str(row["run_id"]) for row in tables["stage_runs"]}
    artifact_ids = {
        str(row["artifact_version_id"]) for row in tables["artifact_versions"]
    }
    job_ids = {str(row["job_id"]) for row in tables["jobs"]}
    for table in ("stage_runs", "stage_state", "artifact_versions", "jobs"):
        for row in tables[table]:
            if str(row["project_id"]) not in projects:
                raise WorkflowMigrationError(
                    f"Legacy {table} row references a missing project."
                )
    for row in tables["stage_state"]:
        current_run = str(row.get("current_run_id") or "")
        if current_run and current_run not in run_ids:
            raise WorkflowMigrationError("Legacy stage state references a missing stage run.")
    for row in tables["artifact_versions"]:
        producer_run = str(row.get("producer_run_id") or "")
        if producer_run and producer_run not in run_ids:
            raise WorkflowMigrationError("Legacy artifact references a missing producer run.")
    for row in tables["current_artifacts"]:
        if str(row["artifact_version_id"]) not in artifact_ids:
            raise WorkflowMigrationError("Legacy current artifact references a missing artifact.")
    for row in tables["artifact_dependencies"]:
        output_id = str(row["output_artifact_version_id"])
        input_id = str(row["input_artifact_version_id"])
        if output_id not in artifact_ids or input_id not in artifact_ids:
            raise WorkflowMigrationError(
                "Legacy artifact dependency references a missing artifact."
            )
    for row in tables["current_jobs"]:
        if str(row["job_id"]) not in job_ids:
            raise WorkflowMigrationError("Legacy current job references a missing job.")


def _safe_artifact_path(
    review_root: Path,
    project_slug: str,
    row: dict[str, Any],
    artifact_id: uuid.UUID,
) -> tuple[str, Path, bool]:
    logical_name = str(row.get("logical_name") or "").replace("\\", "/").strip()
    logical = PurePosixPath(logical_name)
    safe = bool(logical_name) and not logical.is_absolute() and ".." not in logical.parts
    project_root = (review_root / "review-projects" / project_slug).resolve()
    source_candidates: list[Path] = []
    if safe:
        source_candidates.append(project_root / Path(logical.as_posix()))
    raw_path = str(row.get("path") or "").strip()
    recorded = Path(raw_path) if raw_path else None
    if recorded is not None:
        source_candidates.append(recorded if recorded.is_absolute() else review_root / recorded)
        portable_parts = PurePosixPath(raw_path.replace("\\", "/")).parts
        folded = [part.casefold() for part in portable_parts]
        for marker in ("review-projects", "review-library", "mineru-outputs"):
            if marker in folded:
                source_candidates.append(
                    review_root.joinpath(*portable_parts[folded.index(marker) :])
                )

    source_path: Path | None = None
    seen: set[str] = set()
    for candidate in source_candidates:
        try:
            lexical = candidate if candidate.is_absolute() else review_root / candidate
            key = str(lexical)
            if key in seen:
                continue
            seen.add(key)
            trusted = _trusted_migration_path(review_root, lexical, label="artifact source")
        except (ValueError, WorkflowMigrationError):
            continue
        if _filesystem_path(trusted).is_file():
            source_path = trusted
            break

    if source_path is None:
        relative_path = logical.as_posix() if safe else f"legacy-external/{artifact_id}/artifact.bin"
        return relative_path, project_root / Path(relative_path), False

    raw_name = logical.name if safe else PurePosixPath(raw_path.replace("\\", "/")).name
    filename = "".join(
        character if character.isalnum() or character in {".", "-", "_"} else "_"
        for character in (raw_name or "artifact.bin")
    )[:240] or "artifact.bin"
    destination_root = _trusted_migration_directory(
        project_root,
        ".artifacts",
        "migrated",
        str(artifact_id),
        label="artifact version",
    )
    destination = destination_root / filename
    _publish_migrated_library_file(source_path, destination)
    relative_path = destination.relative_to(project_root).as_posix()
    return relative_path, destination, True


def _normalize_job_status(status: str) -> str:
    normalized = str(status or "queued").strip().lower()
    return {
        "completed": "succeeded",
        "complete": "succeeded",
        "success": "succeeded",
        "canceled": "cancelled",
        "pending": "queued",
    }.get(normalized, normalized)


def _metadata_value(metadata: dict[str, Any], key: str, default: Any = None) -> Any:
    value = metadata.get(key, default)
    return value.get("value", default) if isinstance(value, dict) else value


def _trusted_migration_directory(
    current: Path, *parts: str, label: str
) -> Path:
    for part in parts:
        if not part or part in {".", ".."} or Path(part).name != part:
            raise WorkflowMigrationError(f"Legacy Library {label} component is unsafe.")
        lexical = current / part
        if lexical.is_symlink():
            raise WorkflowMigrationError(
                f"Legacy Library {label} directory is a symbolic link."
            )
        lexical.mkdir(exist_ok=True)
        resolved = lexical.resolve()
        if lexical.is_symlink() or resolved.parent != current:
            raise WorkflowMigrationError(
                f"Legacy Library {label} directory escaped its user workspace."
            )
        current = resolved
    return current


def _trusted_migration_path(
    review_root: Path, candidate: Path, *, label: str
) -> Path:
    try:
        relative = candidate.relative_to(review_root)
    except ValueError as exc:
        raise WorkflowMigrationError(
            f"Legacy Library {label} escaped its user workspace."
        ) from exc
    current = review_root
    for part in relative.parts:
        lexical = current / part
        if lexical.is_symlink():
            raise WorkflowMigrationError(
                f"Legacy Library {label} path contains a symbolic link."
            )
        resolved = lexical.resolve()
        if lexical.is_symlink() or resolved.parent != current:
            raise WorkflowMigrationError(
                f"Legacy Library {label} escaped its user workspace."
            )
        current = resolved
    return current


def _legacy_library_path(review_root: Path, raw: Any, fallback: Path) -> tuple[str, Path, bool]:
    value = str(raw or "").strip()
    recorded = Path(value) if value else fallback
    candidates = [recorded] if recorded.is_absolute() else [review_root / recorded]
    portable_parts = PurePosixPath(value.replace("\\", "/")).parts if value else fallback.parts
    folded = [part.casefold() for part in portable_parts]
    for marker in ("review-library", "mineru-outputs"):
        if marker in folded:
            candidates.append(
                review_root.joinpath(*portable_parts[folded.index(marker) :])
            )
    seen: set[str] = set()
    for candidate in candidates:
        try:
            lexical = candidate if candidate.is_absolute() else review_root / candidate
            key = str(lexical)
            if key in seen:
                continue
            seen.add(key)
            relative_path = lexical.relative_to(review_root)
        except ValueError:
            continue
        resolved = _trusted_migration_path(
            review_root, review_root / relative_path, label="source"
        )
        relative = relative_path.as_posix()
        if resolved.is_file():
            return relative, resolved, True
    resolved = _trusted_migration_path(
        review_root, review_root / fallback, label="source"
    )
    relative = fallback.as_posix()
    return relative, resolved, False


def _legacy_library_directory(review_root: Path, raw: Any) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    recorded = Path(value)
    portable_parts = PurePosixPath(value.replace("\\", "/")).parts
    candidates = [recorded] if recorded.is_absolute() else [review_root / recorded]
    folded = [part.casefold() for part in portable_parts]
    for marker in ("review-library", "mineru-outputs"):
        if marker in folded:
            candidates.append(
                review_root.joinpath(*portable_parts[folded.index(marker) :])
            )
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            trusted = _trusted_migration_path(
                review_root, candidate, label="MinerU extraction"
            )
        except WorkflowMigrationError:
            continue
        if trusted.is_dir():
            return trusted
    return None


def _publish_migrated_library_file(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise WorkflowMigrationError(
            f"Immutable Library artifact is a symbolic link: {destination}"
        )
    source_io = _filesystem_path(source)
    destination_io = _filesystem_path(destination)
    if destination_io.is_file():
        if _sha256_file(destination) != _sha256_file(source):
            raise WorkflowMigrationError(
                f"Immutable Library artifact has conflicting content: {destination}"
            )
        return
    temporary = destination.parent / f".rw-{uuid.uuid4().hex[:8]}.part"
    temporary_io = _filesystem_path(temporary)
    try:
        shutil.copy2(source_io, temporary_io)
        temporary_io.replace(destination_io)
    finally:
        temporary_io.unlink(missing_ok=True)


def _mineru_tree_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    manifest: list[tuple[str, str]] = []
    for item in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise WorkflowMigrationError(
                "Legacy MinerU extraction contains a symbolic link or junction."
            )
        try:
            relative = item.resolve().relative_to(root)
        except ValueError as exc:
            raise WorkflowMigrationError(
                "Legacy MinerU extraction escaped its source directory."
            ) from exc
        if item.is_file():
            manifest.append((relative.as_posix(), _sha256_file(item)))
    return tuple(manifest)


def _publish_migrated_library_tree(source: Path, destination: Path) -> None:
    source_manifest = _mineru_tree_manifest(source)
    destination_io = _filesystem_path(destination)
    if destination_io.exists():
        if not destination_io.is_dir() or _mineru_tree_manifest(destination) != source_manifest:
            raise WorkflowMigrationError(
                f"Immutable MinerU artifact has conflicting content: {destination}"
            )
        return
    temporary = destination.parent / f".rw-{uuid.uuid4().hex[:8]}.part"
    temporary_io = _filesystem_path(temporary)
    try:
        shutil.copytree(_filesystem_path(source), temporary_io)
        if _mineru_tree_manifest(temporary) != source_manifest:
            raise WorkflowMigrationError(
                "Published MinerU artifact does not match its source."
            )
        temporary_io.replace(destination_io)
    finally:
        if temporary_io.exists():
            shutil.rmtree(temporary_io)


def _publish_migrated_library_metadata(
    destination: Path, metadata: dict[str, Any]
) -> None:
    content = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    if destination.is_symlink():
        raise WorkflowMigrationError(
            f"Immutable Library metadata is a symbolic link: {destination}"
        )
    destination_io = _filesystem_path(destination)
    if destination_io.is_file():
        if destination_io.read_bytes() != content:
            raise WorkflowMigrationError(
                f"Immutable Library metadata has conflicting content: {destination}"
            )
        return
    temporary = destination.parent / f".rw-{uuid.uuid4().hex[:8]}.part"
    temporary_io = _filesystem_path(temporary)
    try:
        with temporary_io.open("xb") as handle:
            handle.write(content)
        temporary_io.replace(destination_io)
    finally:
        temporary_io.unlink(missing_ok=True)


def _import_library_catalog(
    session,
    owner: User,
    review_root: Path,
    report: MigrationSourceReport,
) -> int:
    metadata_root = _trusted_migration_directory(
        review_root,
        "review-library",
        "metadata",
        "papers",
        label="metadata",
    )
    imported = 0
    for metadata_path in sorted(metadata_root.glob("*.metadata.json")):
        if metadata_path.is_symlink() or metadata_path.resolve().parent != metadata_root:
            raise WorkflowMigrationError(
                f"Legacy Library metadata path is a symbolic link: {metadata_path}"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowMigrationError(
                f"Legacy Library metadata is unreadable: {metadata_path}"
            ) from exc
        if not isinstance(metadata, dict):
            raise WorkflowMigrationError(
                f"Legacy Library metadata is not an object: {metadata_path}"
            )
        paper_id = str(metadata.get("paper_id") or metadata_path.name.split(".", 1)[0]).strip()
        if not paper_id:
            raise WorkflowMigrationError(f"Legacy Library metadata is missing paper_id: {metadata_path}")
        source_paths = metadata.get("source_paths") if isinstance(metadata.get("source_paths"), dict) else {}
        source_file = metadata.get("source_file") if isinstance(metadata.get("source_file"), dict) else {}
        extraction = metadata.get("extraction") if isinstance(metadata.get("extraction"), dict) else {}
        extraction_inputs = (
            extraction.get("inputs") if isinstance(extraction.get("inputs"), dict) else {}
        )
        pdf_relative, pdf_path, pdf_ready = _legacy_library_path(
            review_root,
            source_paths.get("pdf") or source_file.get("relative_pdf_path"),
            Path("review-library") / "uploads" / f"{paper_id}.pdf",
        )
        markdown_relative, markdown_path, markdown_ready = _legacy_library_path(
            review_root,
            source_paths.get("markdown"),
            Path("mineru-outputs") / "markdown" / f"{paper_id}.md",
        )
        for label, path, ready in (
            ("library_pdf", pdf_path, pdf_ready),
            ("library_markdown", markdown_path, markdown_ready),
        ):
            if not ready:
                report.missing_files.append(
                    {"project_id": LIBRARY_PROJECT_ID, "logical_name": f"{paper_id}:{label}", "path": str(path)}
                )
        digest = str(source_file.get("sha256") or "").strip().lower()
        if (not digest or len(digest) != 64) and pdf_ready:
            digest = _sha256_file(pdf_path)
        if not digest or len(digest) != 64:
            digest = hashlib.sha256(f"missing:{owner.id}:{paper_id}".encode("utf-8")).hexdigest()
        markdown_sha256 = (
            _sha256_file(markdown_path)
            if markdown_ready
            else hashlib.sha256(
                f"missing:{owner.id}:{paper_id}:markdown".encode("utf-8")
            ).hexdigest()
        )
        source_metadata_sha256 = _sha256_file(metadata_path)
        extracted_source = _legacy_library_directory(
            review_root,
            source_paths.get("extracted_dir") or extraction_inputs.get("extracted_dir"),
        )
        content_list_relative: Path | None = None
        content_list_source: Path | None = None
        raw_content_list = source_paths.get("content_list") or extraction_inputs.get("content_list")
        if extracted_source is not None and raw_content_list:
            _, candidate_content_list, content_ready = _legacy_library_path(
                review_root, raw_content_list, Path("__missing_mineru_content_list__")
            )
            if content_ready:
                try:
                    content_list_relative = candidate_content_list.relative_to(extracted_source)
                except ValueError:
                    content_list_relative = None
                else:
                    content_list_source = candidate_content_list
        mineru_sha256 = (
            _sha256_file(content_list_source) if content_list_source is not None else ""
        )
        artifact_ids = {
            kind: _stable_uuid(
                str(review_root),
                "library-artifact",
                f"{paper_id}:{kind}:{content_sha256}",
            )
            for kind, content_sha256 in {
                "pdf": digest,
                "markdown": markdown_sha256,
                "metadata": source_metadata_sha256,
                **({"mineru": mineru_sha256} if mineru_sha256 else {}),
            }.items()
        }
        artifact_paper_root = _trusted_migration_directory(
            review_root,
            "review-library",
            ".artifacts",
            paper_id,
            label="artifact",
        )
        artifact_version_roots = {
            kind: _trusted_migration_directory(
                artifact_paper_root,
                str(artifact_id),
                label="artifact version",
            )
            for kind, artifact_id in artifact_ids.items()
        }
        artifact_destinations = {
            "pdf": artifact_version_roots["pdf"] / f"{paper_id}.pdf",
            "markdown": artifact_version_roots["markdown"] / f"{paper_id}.md",
            "metadata": (
                artifact_version_roots["metadata"] / f"{paper_id}.metadata.json"
            ),
        }
        if "mineru" in artifact_ids and content_list_relative is not None:
            artifact_destinations["mineru"] = (
                artifact_version_roots["mineru"]
                / "extracted"
                / content_list_relative
            )
        if pdf_ready:
            _publish_migrated_library_file(pdf_path, artifact_destinations["pdf"])
        if markdown_ready:
            _publish_migrated_library_file(
                markdown_path, artifact_destinations["markdown"]
            )
        if (
            "mineru" in artifact_ids
            and extracted_source is not None
            and content_list_relative is not None
        ):
            _publish_migrated_library_tree(
                extracted_source,
                artifact_version_roots["mineru"] / "extracted",
            )
            if not artifact_destinations["mineru"].is_file():
                raise WorkflowMigrationError(
                    f"Published MinerU content list is unavailable for {paper_id}."
                )
        artifact_paths = {
            kind: destination.relative_to(review_root).as_posix()
            for kind, destination in artifact_destinations.items()
        }
        metadata = dict(metadata)
        metadata["_artifact_ids"] = {
            kind: str(artifact_id) for kind, artifact_id in artifact_ids.items()
        }
        metadata["_artifact_paths"] = dict(artifact_paths)
        stored_source_paths = dict(metadata.get("source_paths") or {})
        stored_source_paths["pdf"] = str(artifact_destinations["pdf"])
        stored_source_paths["markdown"] = str(artifact_destinations["markdown"])
        if "mineru" in artifact_destinations:
            mineru_root = artifact_version_roots["mineru"] / "extracted"
            stored_source_paths["extracted_dir"] = str(mineru_root)
            stored_source_paths["content_list"] = str(artifact_destinations["mineru"])
        metadata["source_paths"] = stored_source_paths
        if "mineru" in artifact_destinations:
            stored_extraction = dict(metadata.get("extraction") or {})
            stored_inputs = dict(stored_extraction.get("inputs") or {})
            stored_inputs["extracted_dir"] = str(
                artifact_version_roots["mineru"] / "extracted"
            )
            stored_inputs["content_list"] = str(artifact_destinations["mineru"])
            stored_extraction["inputs"] = stored_inputs
            metadata["extraction"] = stored_extraction
        stored_source_file = dict(metadata.get("source_file") or {})
        stored_source_file["pdf_name"] = artifact_destinations["pdf"].name
        stored_source_file["relative_pdf_path"] = artifact_paths["pdf"]
        stored_source_file["sha256"] = digest
        metadata["source_file"] = stored_source_file
        _publish_migrated_library_metadata(
            artifact_destinations["metadata"], metadata
        )
        artifact_sources = {
            "pdf": (artifact_destinations["pdf"], pdf_ready),
            "markdown": (artifact_destinations["markdown"], markdown_ready),
            "metadata": (artifact_destinations["metadata"], True),
        }
        if "mineru" in artifact_destinations:
            artifact_sources["mineru"] = (artifact_destinations["mineru"], True)
        for kind, (path, ready) in artifact_sources.items():
            artifact = session.get(LibraryArtifact, artifact_ids[kind])
            stat = _filesystem_path(path).stat() if ready else None
            artifact_values = {
                "user_id": owner.id,
                "paper_id": paper_id,
                "kind": kind,
                "relative_path": artifact_paths[kind],
                "content_sha256": (
                    _sha256_file(path)
                    if ready
                    else hashlib.sha256(
                        f"missing:{owner.id}:{paper_id}:{kind}".encode("utf-8")
                    ).hexdigest()
                ),
                "size_bytes": stat.st_size if stat else 0,
                "mtime_ns": stat.st_mtime_ns if stat else 0,
                "availability": "available" if ready else "missing",
            }
            if artifact is None:
                session.add(LibraryArtifact(id=artifact_ids[kind], **artifact_values))
            else:
                for key, value in artifact_values.items():
                    setattr(artifact, key, value)
        row = session.scalar(
            select(LibraryPaper).where(
                LibraryPaper.user_id == owner.id,
                LibraryPaper.content_sha256 == digest,
            )
        )
        if row is None:
            row = session.scalar(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == owner.id,
                    LibraryPaper.paper_id == paper_id,
                )
            )
        values = {
            "user_id": owner.id,
            "paper_id": paper_id,
            "content_sha256": digest,
            "original_filename": str(
                source_file.get("original_upload_name")
                or source_file.get("pdf_name")
                or pdf_path.name
            ),
            "title": str(_metadata_value(metadata, "title", paper_id) or paper_id),
            "authors_json": _metadata_value(metadata, "authors", []) or [],
            "keywords_json": _metadata_value(metadata, "keywords", []) or [],
            "tags_json": _metadata_value(metadata, "structured_tags", {}) or {},
            "metadata_json": metadata,
            "pdf_relative_path": artifact_paths["pdf"],
            "markdown_relative_path": artifact_paths["markdown"],
            "status": "active",
            "deleted_at": None,
        }
        if row is None:
            session.add(LibraryPaper(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        imported += 1
    session.flush()
    return imported


def _upsert_ledger(
    session,
    source: LegacySourceInventory,
    *,
    status: str,
    report: dict[str, Any],
    error_message: str = "",
) -> WorkflowMigration:
    migration = session.scalar(
        select(WorkflowMigration).where(
            WorkflowMigration.source_kind == "sqlite",
            WorkflowMigration.source_identity == source.source_path,
        )
    )
    if migration is None:
        migration = WorkflowMigration(
            source_kind="sqlite",
            source_identity=source.source_path,
            source_sha256=source.source_sha256,
            status=status,
            report_json=report,
            error_message=error_message,
        )
        session.add(migration)
    else:
        migration.source_sha256 = source.source_sha256
        migration.status = status
        migration.report_json = report
        migration.error_message = error_message
    migration.finished_at = utc_now() if status in {"succeeded", "failed", "requires_acknowledgement"} else None
    return migration


def _import_source(
    session,
    source: LegacySourceInventory,
    *,
    owner_email: str | None,
    accept_missing_files: bool,
    accept_file_drift: bool,
) -> MigrationSourceReport:
    report = MigrationSourceReport(
        source_path=source.source_path,
        source_sha256=source.source_sha256,
    )
    review_root = Path(source.review_root).resolve()
    with _readonly_connection(Path(source.source_path)) as connection:
        tables = {table: _rows(connection, table) for table in LEGACY_TABLES}
    _validate_legacy_references(tables)
    owner = _resolve_owner(session, source, owner_email)
    report.owner_user_id = str(owner.id)
    projects = _project_map(session, owner, review_root, tables["projects"])
    missing_project_ids = {
        str(row["project_id"])
        for table in ("stage_runs", "stage_state", "artifact_versions", "jobs")
        for row in tables[table]
        if str(row["project_id"]) not in projects
    }
    if missing_project_ids:
        raise WorkflowMigrationError(
            f"Legacy rows could not map projects: {sorted(missing_project_ids)}"
        )

    run_map: dict[str, uuid.UUID] = {}
    for row in tables["stage_runs"]:
        legacy_id = str(row["run_id"])
        run_id = _stable_uuid(source.source_path, "stage-run", legacy_id)
        project_id = projects[str(row["project_id"])]
        assert project_id is not None
        run = session.scalar(select(WorkflowStageRun).where(WorkflowStageRun.legacy_id == legacy_id))
        if run is None:
            run = session.get(WorkflowStageRun, run_id)
        values = {
            "legacy_id": legacy_id,
            "project_id": project_id,
            "stage_id": str(row["stage_id"]),
            "requested_by_user_id": owner.id,
            "status": str(row["status"]),
            "attempt": int(row.get("attempt") or 1),
            "input_fingerprint": str(row.get("input_fingerprint") or ""),
            "input_snapshot": _parse_json(row.get("input_snapshot_json"), field_name="input_snapshot_json"),
            "output_fingerprint": str(row.get("output_fingerprint") or ""),
            "output_snapshot": _parse_json(row.get("output_snapshot_json"), field_name="output_snapshot_json"),
            "progress_current": int(row.get("progress_current") or 0),
            "progress_total": int(row.get("progress_total") or 0),
            "error_message": str(row.get("error_message") or ""),
            "metadata_json": _parse_json(row.get("metadata_json"), field_name="metadata_json"),
            "started_at": _parse_datetime(row.get("started_at")),
            "updated_at": _parse_datetime(row.get("updated_at")),
            "finished_at": _parse_datetime(row.get("finished_at")) if row.get("finished_at") else None,
        }
        if run is None:
            run = WorkflowStageRun(id=run_id, **values)
            session.add(run)
        else:
            for key, value in values.items():
                setattr(run, key, value)
        run_map[legacy_id] = run.id
    session.flush()

    for row in tables["stage_state"]:
        project_id = projects[str(row["project_id"])]
        assert project_id is not None
        stage_id = str(row["stage_id"])
        state = session.scalar(
            select(WorkflowStageState).where(
                WorkflowStageState.project_id == project_id,
                WorkflowStageState.stage_id == stage_id,
            )
        )
        current_run = str(row.get("current_run_id") or "")
        values = {
            "status": str(row["status"]),
            "revision": max(1, int(state.revision if state else 1)),
            "current_run_id": run_map.get(current_run) if current_run else None,
            "input_fingerprint": str(row.get("input_fingerprint") or ""),
            "output_fingerprint": str(row.get("output_fingerprint") or ""),
            "error_message": str(row.get("error_message") or ""),
            "created_at": _parse_datetime(row.get("updated_at")),
            "updated_at": _parse_datetime(row.get("updated_at")),
        }
        if state is None:
            state = WorkflowStageState(project_id=project_id, stage_id=stage_id, **values)
            session.add(state)
        else:
            for key, value in values.items():
                setattr(state, key, value)
    session.flush()

    artifact_map: dict[str, uuid.UUID] = {}
    for row in tables["artifact_versions"]:
        legacy_id = str(row["artifact_version_id"])
        artifact_id = _stable_uuid(source.source_path, "artifact", legacy_id)
        project_slug = str(row["project_id"])
        project_id = projects[project_slug]
        assert project_id is not None
        relative_path, file_path, available = _safe_artifact_path(
            review_root, project_slug, row, artifact_id
        )
        actual_sha256 = ""
        expected_sha256 = str(row["content_sha256"])
        drifted = False
        if available:
            actual_sha256 = _sha256_file(file_path)
            drifted = actual_sha256 != expected_sha256
            if drifted:
                report.drifted_files.append(
                    {
                        "source_path": source.source_path,
                        "project_id": project_slug,
                        "logical_name": str(row.get("logical_name") or ""),
                        "legacy_path": str(row.get("path") or ""),
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                        "expected_size_bytes": int(row.get("size_bytes") or 0),
                        "actual_size_bytes": _filesystem_path(file_path).stat().st_size,
                    }
                )
        else:
            report.missing_files.append(
                {
                    "source_path": source.source_path,
                    "project_id": project_slug,
                    "logical_name": str(row.get("logical_name") or ""),
                    "legacy_path": str(row.get("path") or ""),
                }
            )
        artifact = session.scalar(
            select(WorkflowArtifact).where(WorkflowArtifact.legacy_id == legacy_id)
        )
        if artifact is None:
            artifact = session.get(WorkflowArtifact, artifact_id)
        metadata = _parse_json(row.get("metadata_json"), field_name="metadata_json")
        if not isinstance(metadata, dict):
            metadata = {"legacy_metadata": metadata}
        metadata.update(
            {
                "legacy_artifact_version_id": legacy_id,
                "legacy_path": str(row.get("path") or ""),
                "legacy_source": source.source_path,
            }
        )
        if drifted:
            metadata.update(
                {
                    "legacy_content_sha256": expected_sha256,
                    "migrated_actual_sha256": actual_sha256,
                    "legacy_size_bytes": int(row.get("size_bytes") or 0),
                    "file_drift_acknowledged": bool(accept_file_drift),
                }
            )
        producer_run = str(row.get("producer_run_id") or "")
        lineage_sha256 = hashlib.sha256(
            json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        admitted_drift = bool(drifted and accept_file_drift)
        content_sha256 = actual_sha256 if admitted_drift else expected_sha256
        migrated_stat = _filesystem_path(file_path).stat() if admitted_drift else None
        size_bytes = migrated_stat.st_size if migrated_stat else int(row.get("size_bytes") or 0)
        mtime_ns = migrated_stat.st_mtime_ns if migrated_stat else int(row.get("mtime_ns") or 0)
        availability = (
            "missing"
            if not available
            else "integrity_mismatch"
            if drifted and not accept_file_drift
            else "available"
        )
        values = {
            "legacy_id": legacy_id,
            "project_id": project_id,
            "logical_name": str(row["logical_name"]),
            "artifact_type": str(row["artifact_type"]),
            "relative_path": relative_path,
            "content_sha256": content_sha256,
            "lineage_sha256": lineage_sha256,
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "availability": availability,
            "producer_stage": str(row.get("producer_stage") or ""),
            "producer_run_id": run_map.get(producer_run) if producer_run else None,
            "metadata_json": metadata,
            "created_at": _parse_datetime(row.get("created_at")),
        }
        if artifact is None:
            artifact = WorkflowArtifact(id=artifact_id, **values)
            session.add(artifact)
        else:
            for key, value in values.items():
                setattr(artifact, key, value)
        artifact_map[legacy_id] = artifact.id
    session.flush()

    for row in tables["current_artifacts"]:
        project_id = projects[str(row["project_id"])]
        assert project_id is not None
        logical_name = str(row["logical_name"])
        pointer = session.get(
            WorkflowCurrentArtifact,
            {"project_id": project_id, "logical_name": logical_name},
        )
        artifact_id = artifact_map[str(row["artifact_version_id"])]
        if pointer is None:
            session.add(
                WorkflowCurrentArtifact(
                    project_id=project_id,
                    logical_name=logical_name,
                    artifact_id=artifact_id,
                    updated_at=_parse_datetime(row.get("updated_at")),
                )
            )
        else:
            pointer.artifact_id = artifact_id
            pointer.updated_at = _parse_datetime(row.get("updated_at"))

    for row in tables["artifact_dependencies"]:
        key = {
            "output_artifact_id": artifact_map[str(row["output_artifact_version_id"])],
            "input_artifact_id": artifact_map[str(row["input_artifact_version_id"])],
            "dependency_role": str(row.get("dependency_role") or "input"),
        }
        if session.get(WorkflowArtifactDependency, key) is None:
            session.add(WorkflowArtifactDependency(**key))
    session.flush()

    job_map: dict[str, uuid.UUID] = {}
    for row in tables["jobs"]:
        legacy_id = str(row["job_id"])
        job_id = _stable_uuid(source.source_path, "job", legacy_id)
        project_slug = str(row["project_id"])
        project_id = projects[project_slug]
        payload = _parse_json(row.get("payload_json"), field_name="payload_json")
        if not isinstance(payload, dict):
            payload = {"legacy_payload": payload}
        job = session.scalar(select(WorkflowJob).where(WorkflowJob.legacy_id == legacy_id))
        if job is None:
            job = session.get(WorkflowJob, job_id)
        status = _normalize_job_status(str(row.get("status") or "queued"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        error_message = str(payload.get("error") or payload.get("error_message") or "")
        values = {
            "legacy_id": legacy_id,
            "user_id": owner.id,
            "project_id": project_id,
            "scope": "library" if project_slug == LIBRARY_PROJECT_ID else "project",
            "job_type": str(row["job_type"]),
            "status": status,
            "idempotency_scope_key": (
                LIBRARY_PROJECT_ID if project_id is None else str(project_id)
            ),
            "idempotency_key": legacy_id,
            "payload_json": payload,
            "result_json": result,
            "progress_current": int(payload.get("progress_current") or 0),
            "progress_total": int(payload.get("progress_total") or 0),
            "error_message": error_message,
            "created_at": _parse_datetime(row.get("started_at")),
            "started_at": _parse_datetime(row.get("started_at")),
            "updated_at": _parse_datetime(row.get("updated_at")),
            "finished_at": _parse_datetime(row.get("finished_at")) if row.get("finished_at") else None,
        }
        if job is None:
            job = WorkflowJob(id=job_id, **values)
            session.add(job)
        else:
            for key, value in values.items():
                setattr(job, key, value)
        job_map[legacy_id] = job.id
    session.flush()

    for row in tables["current_jobs"]:
        project_slug = str(row["project_id"])
        project_id = projects[project_slug]
        scope_key = LIBRARY_PROJECT_ID if project_id is None else str(project_id)
        job_type = str(row["job_type"])
        key = {"user_id": owner.id, "scope_key": scope_key, "job_type": job_type}
        pointer = session.get(WorkflowCurrentJob, key)
        values = {
            "project_id": project_id,
            "job_id": job_map[str(row["job_id"])],
            "updated_at": _parse_datetime(row.get("updated_at")),
        }
        if pointer is None:
            session.add(WorkflowCurrentJob(**key, **values))
        else:
            for value_key, value in values.items():
                setattr(pointer, value_key, value)

    library_paper_count = _import_library_catalog(
        session, owner, review_root, report
    )

    report.imported_counts = {
        "projects": len([key for key in projects if key != LIBRARY_PROJECT_ID]),
        "stage_runs": len(tables["stage_runs"]),
        "stage_states": len(tables["stage_state"]),
        "artifacts": len(tables["artifact_versions"]),
        "current_artifacts": len(tables["current_artifacts"]),
        "artifact_dependencies": len(tables["artifact_dependencies"]),
        "jobs": len(tables["jobs"]),
        "current_jobs": len(tables["current_jobs"]),
        "library_papers": library_paper_count,
    }
    acknowledgement_required = bool(
        (report.missing_files and not accept_missing_files)
        or (report.drifted_files and not accept_file_drift)
    )
    report.status = "requires_acknowledgement" if acknowledgement_required else "succeeded"
    _upsert_ledger(
        session,
        source,
        status=report.status,
        report={
            "imported_counts": report.imported_counts,
            "missing_files": report.missing_files,
            "accept_missing_files": accept_missing_files,
            "drifted_files": report.drifted_files,
            "accept_file_drift": accept_file_drift,
        },
    )
    return report


def _backup_source(
    source: LegacySourceInventory,
    backup_root: Path,
    run_id: str,
) -> Path:
    owner_label = source.owner_hint or "local"
    destination = backup_root / run_id / owner_label / "workflow.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _readonly_connection(Path(source.source_path)) as source_connection:
        with closing(sqlite3.connect(destination)) as destination_connection:
            source_connection.backup(destination_connection)
    with _readonly_connection(destination) as backup_connection:
        integrity = str(backup_connection.execute("PRAGMA integrity_check").fetchone()[0])
        backup_counts = _table_counts(backup_connection, destination)
    if integrity.lower() != "ok" or backup_counts != source.table_counts:
        raise WorkflowMigrationError(f"SQLite backup validation failed: {destination}")
    return destination


def _set_inventory_state(session_factory, inventory: MigrationInventory) -> None:
    payload = {
        "workspace_root": inventory.workspace_root,
        "source_count": inventory.source_count,
        "sources": [
            {
                "source_path": source.source_path,
                "source_sha256": source.source_sha256,
                "table_counts": source.table_counts,
            }
            for source in inventory.sources
        ],
        "recorded_at": utc_now().isoformat(),
    }
    with database_session(session_factory) as session:
        state = session.get(WorkflowSystemState, "legacy_source_inventory")
        if state is None:
            session.add(WorkflowSystemState(key="legacy_source_inventory", value_json=payload))
        else:
            state.value_json = payload
            state.updated_at = utc_now()
        ready = session.get(WorkflowSystemState, "workflow_ready")
        if ready is not None:
            session.delete(ready)


def validate_migrated_workflows(
    session_factory,
    report: MigrationReport,
) -> list[str]:
    errors = list(report.errors)
    if not report.success:
        return errors or ["Migration report is not successful."]
    with database_session(session_factory) as session:
        for source in report.sources:
            ledger = session.scalar(
                select(WorkflowMigration).where(
                    WorkflowMigration.source_kind == "sqlite",
                    WorkflowMigration.source_identity == source.source_path,
                )
            )
            if ledger is None:
                errors.append(f"Missing migration ledger for {source.source_path}.")
            elif ledger.source_sha256 != source.source_sha256:
                errors.append(f"Migration checksum mismatch for {source.source_path}.")
            elif ledger.status not in {"succeeded", "requires_acknowledgement"}:
                errors.append(f"Migration ledger is not successful for {source.source_path}.")
        missing_current_artifacts = session.scalar(
            select(func.count())
            .select_from(WorkflowCurrentArtifact)
            .outerjoin(
                WorkflowArtifact,
                WorkflowArtifact.id == WorkflowCurrentArtifact.artifact_id,
            )
            .where(WorkflowArtifact.id.is_(None))
        )
        if missing_current_artifacts:
            errors.append("A current artifact points to a missing artifact row.")
    return errors


def migrate_legacy_workflows(
    workspace_root: str | Path,
    backup_root: str | Path,
    session_factory,
    *,
    owner_email: str | None = None,
    dry_run: bool = False,
    accept_missing_files: bool = False,
    accept_file_drift: bool = False,
) -> MigrationReport:
    inventory = inventory_legacy_workflows(workspace_root, session_factory)
    if not inventory.sources:
        raise WorkflowMigrationError("No legacy workflow.sqlite3 databases were found.")
    if any(source.is_local for source in inventory.sources) and not str(owner_email or "").strip():
        raise WorkflowMigrationError(
            "A local workflow database requires an explicit --owner-email mapping."
        )

    source_reports = [
        MigrationSourceReport(
            source_path=source.source_path,
            source_sha256=source.source_sha256,
            status="planned" if dry_run else "pending",
        )
        for source in inventory.sources
    ]
    report = MigrationReport(
        workspace_root=inventory.workspace_root,
        dry_run=dry_run,
        accept_missing_files=accept_missing_files,
        success=True,
        ready=False,
        sources=source_reports,
        imported_counts={},
        missing_files=[],
        backup_paths=[],
        errors=[],
        drifted_files=[],
    )
    if dry_run:
        return report

    assert_application_stopped(session_factory)
    _set_inventory_state(session_factory, inventory)
    run_id = f"{utc_now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    backup_directory = Path(backup_root).expanduser().resolve()
    for index, source in enumerate(inventory.sources):
        source_report = source_reports[index]
        try:
            backup_path = _backup_source(source, backup_directory, run_id)
            source_report.backup_path = str(backup_path)
            source_report.backup_sha256 = _sha256_file(backup_path)
            report.backup_paths.append(str(backup_path))
            with database_session(session_factory) as session:
                imported = _import_source(
                    session,
                    source,
                    owner_email=owner_email,
                    accept_missing_files=accept_missing_files,
                    accept_file_drift=accept_file_drift,
                )
                imported.backup_path = str(backup_path)
                imported.backup_sha256 = source_report.backup_sha256
                source_reports[index] = imported
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            source_report.status = "failed"
            source_report.errors.append(message)
            report.errors.append(message)
            report.success = False
            with database_session(session_factory) as session:
                _upsert_ledger(
                    session,
                    source,
                    status="failed",
                    report={"errors": [message]},
                    error_message=message,
                )

    report.imported_counts = {
        key: sum(source.imported_counts.get(key, 0) for source in source_reports)
        for key in {
            key for source in source_reports for key in source.imported_counts
        }
    }
    report.missing_files = [
        item for source in source_reports for item in source.missing_files
    ]
    report.drifted_files = [
        item for source in source_reports for item in source.drifted_files
    ]
    validation_errors = validate_migrated_workflows(session_factory, report)
    if validation_errors:
        report.errors = list(dict.fromkeys(report.errors + validation_errors))
        report.success = False
    report.ready = bool(
        report.success
        and (accept_missing_files or not report.missing_files)
        and (accept_file_drift or not report.drifted_files)
    )
    if report.ready:
        with database_session(session_factory) as session:
            state = WorkflowSystemState(
                key="workflow_ready",
                value_json={
                    "status": "ready",
                    "completed_at": utc_now().isoformat(),
                    "source_count": len(source_reports),
                    "accept_missing_files": accept_missing_files,
                    "accept_file_drift": accept_file_drift,
                },
            )
            existing = session.get(WorkflowSystemState, "workflow_ready")
            if existing is None:
                session.add(state)
            else:
                existing.value_json = state.value_json
                existing.updated_at = utc_now()
    return report
