"""Compose bootstrap: upgrade PostgreSQL and migrate legacy SQLite exactly once."""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from review_writer_api.config import database_url_from_env
from review_writer_api.database import create_session_factory, database_session
from review_writer_api.workflow_migration import (
    MigrationInventory,
    WorkflowMigrationError,
    inventory_legacy_workflows,
    migrate_legacy_workflows,
)
from review_writer_api.workflow_models import WorkflowSystemState
from review_writer_core.atomic_io import atomic_write_json


def _inventory_sources(inventory: MigrationInventory) -> list[dict[str, Any]]:
    return [
        {
            "source_path": source.source_path,
            "source_sha256": source.source_sha256,
            "table_counts": dict(source.table_counts),
        }
        for source in inventory.sources
    ]


def _already_ready(session_factory, inventory: MigrationInventory) -> bool:
    expected = _inventory_sources(inventory)
    with database_session(session_factory) as session:
        recorded = session.get(WorkflowSystemState, "legacy_source_inventory")
        ready = session.get(WorkflowSystemState, "workflow_ready")
        if recorded is None or ready is None:
            return False
        ready_payload = ready.value_json if isinstance(ready.value_json, dict) else {}
        recorded_payload = (
            recorded.value_json if isinstance(recorded.value_json, dict) else {}
        )
        return (
            str(ready_payload.get("status") or "").casefold() == "ready"
            and recorded_payload.get("sources") == expected
        )


def run_legacy_migration(
    *,
    workspace_root: Path,
    backup_root: Path,
    report_root: Path,
    session_factory,
    owner_email: str | None = None,
    accept_missing_files: bool = False,
    accept_file_drift: bool = False,
) -> dict[str, Any]:
    """Inventory and migrate legacy sources, preserving reports and verified backups."""

    inventory = inventory_legacy_workflows(workspace_root, session_factory)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    inventory_payload = asdict(inventory)
    inventory_payload["source_count"] = inventory.source_count
    atomic_write_json(report_root / f"{run_id}-inventory.json", inventory_payload)

    if not inventory.sources:
        result = {
            "status": "fresh_install",
            "ready": True,
            "source_count": 0,
            "workspace_root": str(workspace_root.expanduser().resolve()),
        }
        atomic_write_json(report_root / "latest.json", result)
        return result

    if _already_ready(session_factory, inventory):
        result = {
            "status": "already_migrated",
            "ready": True,
            "source_count": inventory.source_count,
            "workspace_root": inventory.workspace_root,
        }
        atomic_write_json(report_root / "latest.json", result)
        return result

    dry_run = migrate_legacy_workflows(
        workspace_root,
        backup_root,
        session_factory,
        owner_email=owner_email,
        dry_run=True,
        accept_missing_files=accept_missing_files,
        accept_file_drift=accept_file_drift,
    )
    atomic_write_json(report_root / f"{run_id}-dry-run.json", asdict(dry_run))

    report = migrate_legacy_workflows(
        workspace_root,
        backup_root,
        session_factory,
        owner_email=owner_email,
        accept_missing_files=accept_missing_files,
        accept_file_drift=accept_file_drift,
    )
    payload = asdict(report)
    payload["status"] = "migrated" if report.ready else "blocked"
    atomic_write_json(report_root / f"{run_id}-migration.json", payload)
    atomic_write_json(report_root / "latest.json", payload)
    if not report.ready:
        if report.missing_files and not accept_missing_files:
            raise WorkflowMigrationError(
                "Migration found missing files. Review latest.json, restore the files or set "
                "REVIEW_WRITER_MIGRATION_ACCEPT_MISSING_FILES=true to acknowledge them."
            )
        if report.drifted_files and not accept_file_drift:
            raise WorkflowMigrationError(
                "Migration found files whose SHA-256 differs from the legacy ledger. "
                "Review latest.json or set REVIEW_WRITER_MIGRATION_ACCEPT_FILE_DRIFT=true "
                "to preserve the actual bytes with both hashes in artifact metadata."
            )
        raise WorkflowMigrationError(
            "Migration validation failed. Review the persisted migration report."
        )
    return payload


def _flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise WorkflowMigrationError(f"{name} must be a boolean value.")


def main() -> int:
    engine = None
    try:
        project_root = Path(__file__).resolve().parents[1]
        alembic_config = Config(str(project_root / "alembic.ini"))
        command.upgrade(alembic_config, "head")

        database_url = database_url_from_env()
        if not database_url:
            raise WorkflowMigrationError("PostgreSQL connection settings are required.")
        session_factory, engine = create_session_factory(database_url)
        workspace_root = Path(
            os.environ.get(
                "REVIEW_WRITER_HOSTED_WORKSPACE_ROOT",
                "/app/.review-writer/hosted-workspaces",
            )
        )
        backup_root = Path(
            os.environ.get("REVIEW_WRITER_MIGRATION_BACKUP_ROOT", "/app/migration-backups")
        )
        report_root = Path(
            os.environ.get("REVIEW_WRITER_MIGRATION_REPORT_ROOT", "/app/migration-reports")
        )
        run_legacy_migration(
            workspace_root=workspace_root,
            backup_root=backup_root,
            report_root=report_root,
            session_factory=session_factory,
            owner_email=str(os.environ.get("REVIEW_WRITER_MIGRATION_OWNER_EMAIL") or "").strip()
            or None,
            accept_missing_files=_flag(
                "REVIEW_WRITER_MIGRATION_ACCEPT_MISSING_FILES", False
            ),
            accept_file_drift=_flag(
                "REVIEW_WRITER_MIGRATION_ACCEPT_FILE_DRIFT", False
            ),
        )
        return 0
    except (OSError, ValueError, WorkflowMigrationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
