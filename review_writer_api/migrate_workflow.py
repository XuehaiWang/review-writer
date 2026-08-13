"""Maintenance CLI for stopped legacy workflow migration."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from review_writer_api.config import database_url_from_env
from review_writer_api.database import create_session_factory
from review_writer_api.workflow_migration import (
    MigrationReport,
    MigrationSourceReport,
    WorkflowMigrationError,
    inventory_legacy_workflows,
    migrate_legacy_workflows,
    validate_migrated_workflows,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-writer-migrate-workflow",
        description="Inventory, back up, migrate, and validate legacy workflow SQLite data.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="Read legacy sources without changing them.")
    inventory.add_argument("--workspace-root", required=True, type=Path)
    inventory.add_argument("--report", required=True, type=Path)

    migrate = commands.add_parser("migrate", help="Back up and import every discovered source.")
    migrate.add_argument("--workspace-root", required=True, type=Path)
    migrate.add_argument("--backup-root", required=True, type=Path)
    migrate.add_argument("--report", required=True, type=Path)
    migrate.add_argument("--owner-email")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--confirm-stopped", action="store_true")
    migrate.add_argument("--accept-missing-files", action="store_true")
    migrate.add_argument("--accept-file-drift", action="store_true")

    validate = commands.add_parser("validate", help="Validate a saved migration report.")
    validate.add_argument("--workspace-root", required=True, type=Path)
    validate.add_argument("--report", required=True, type=Path)
    return parser


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _database() -> tuple[Any, Any]:
    database_url = database_url_from_env()
    if not database_url:
        raise WorkflowMigrationError(
            "Set REVIEW_WRITER_DATABASE_URL or PostgreSQL connection variables."
        )
    return create_session_factory(database_url)


def _load_report(path: Path) -> MigrationReport:
    payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    sources = [MigrationSourceReport(**source) for source in payload.get("sources", [])]
    return MigrationReport(
        workspace_root=str(payload.get("workspace_root") or ""),
        dry_run=bool(payload.get("dry_run")),
        accept_missing_files=bool(payload.get("accept_missing_files")),
        success=bool(payload.get("success")),
        ready=bool(payload.get("ready")),
        sources=sources,
        imported_counts=dict(payload.get("imported_counts") or {}),
        missing_files=list(payload.get("missing_files") or []),
        backup_paths=list(payload.get("backup_paths") or []),
        errors=list(payload.get("errors") or []),
        drifted_files=list(payload.get("drifted_files") or []),
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(list(argv) if argv is not None else None)
    engine = None
    try:
        if arguments.command == "inventory":
            inventory = inventory_legacy_workflows(arguments.workspace_root)
            payload = asdict(inventory)
            payload["source_count"] = inventory.source_count
            _write_json(arguments.report, payload)
            return 0

        if arguments.command == "migrate":
            if not arguments.dry_run and not arguments.confirm_stopped:
                raise WorkflowMigrationError(
                    "Formal migration requires --confirm-stopped after the API is stopped."
                )
        session_factory, engine = _database()
        if arguments.command == "migrate":
            report = migrate_legacy_workflows(
                arguments.workspace_root,
                arguments.backup_root,
                session_factory,
                owner_email=arguments.owner_email,
                dry_run=arguments.dry_run,
                accept_missing_files=arguments.accept_missing_files,
                accept_file_drift=arguments.accept_file_drift,
            )
            _write_json(arguments.report, asdict(report))
            completed = report.success and (report.dry_run or report.ready)
            return 0 if completed else 2

        report = _load_report(arguments.report)
        expected_workspace = arguments.workspace_root.expanduser().resolve()
        reported_workspace = Path(report.workspace_root).expanduser().resolve()
        if expected_workspace != reported_workspace:
            raise WorkflowMigrationError(
                "The migration report belongs to a different workspace root."
            )
        errors = validate_migrated_workflows(session_factory, report)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 2
        return 0
    except (OSError, ValueError, json.JSONDecodeError, WorkflowMigrationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
