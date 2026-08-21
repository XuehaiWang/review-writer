"""Migrate legacy automatic Metadata Tags to the project-neutral policy.

The migration preserves human-verified Tags, publishes a new immutable
metadata artifact for every changed paper, and writes a recovery snapshot
before making changes.  It is intentionally an explicit administrative
command rather than an application-startup side effect.
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.config import ApiSettings
from review_writer_api.database import create_session_factory, database_session
from review_writer_api.domain_services.library import LibraryService
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager
from review_writer_core.metadata_tags import (
    STRUCTURED_TAG_KEYS,
    neutral_structured_tag_values,
    structured_tags_are_verified,
)


NEUTRAL_SOURCE = "project_neutral_unverified"
NEUTRAL_POLICY = "project_neutral_until_human_verified"
MIGRATION_NOTE = "legacy_unverified_structured_tags_cleared"


def _meaningful_tag_values(metadata: dict[str, Any]) -> bool:
    field = metadata.get("structured_tags")
    values = field.get("value") if isinstance(field, dict) else None
    if not isinstance(values, dict):
        return False
    neutral = {"", "not specified", "unknown", "none", "n/a"}
    return any(
        str(values.get(key) or "").strip().casefold() not in neutral
        for key in STRUCTURED_TAG_KEYS
    )


def needs_migration(metadata: dict[str, Any], tags_json: Any) -> bool:
    if structured_tags_are_verified(metadata):
        return False
    field = metadata.get("structured_tags")
    source = str(field.get("source") or "") if isinstance(field, dict) else ""
    return bool(tags_json) or _meaningful_tag_values(metadata) or source != NEUTRAL_SOURCE


def neutralized_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(metadata)
    result["structured_tags"] = {
        "value": neutral_structured_tag_values(),
        "source": NEUTRAL_SOURCE,
        "confidence": 0.0,
        "human_checked": False,
    }

    extraction = result.setdefault("extraction", {})
    if isinstance(extraction, dict):
        inputs = extraction.setdefault("inputs", {})
        if isinstance(inputs, dict):
            inputs["tag_policy"] = NEUTRAL_POLICY
        notes = extraction.setdefault("notes", [])
        if isinstance(notes, list) and MIGRATION_NOTE not in notes:
            notes.append(MIGRATION_NOTE)

    quality = result.setdefault("quality", {})
    if isinstance(quality, dict):
        raw_warnings = quality.get("warnings")
        quality["warnings"] = list(
            dict.fromkeys(
                str(item)
                for item in (raw_warnings if isinstance(raw_warnings, list) else [])
                if not str(item).startswith("structured_tag_not_specified_")
            )
        )
        confidences: list[float] = []
        for key in ("title", "authors", "year", "journal", "doi", "abstract"):
            field = result.get(key)
            if isinstance(field, dict):
                confidences.append(float(field.get("confidence") or 0))
        quality["overall_confidence"] = (
            round(sum(confidences) / len(confidences), 3) if confidences else 0
        )
        missing = quality.get("missing_fields")
        review = result.get("human_review")
        review_status = review.get("status") if isinstance(review, dict) else ""
        quality["needs_human_check"] = bool(
            (missing if isinstance(missing, list) else [])
            or quality["warnings"]
            or review_status != "reviewed"
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear legacy unverified Metadata Tags without touching verified Tags."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--user-id",
        action="append",
        default=[],
        help="Limit migration to one user UUID; repeat to select several users.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = ApiSettings.from_env()
    if settings.deployment_mode != "hosted":
        raise RuntimeError("This command requires REVIEW_WRITER_DEPLOYMENT_MODE=hosted.")

    session_factory, engine = create_session_factory(settings.database_url)
    workspace_root = settings.hosted_workspace_root or (
        settings.review_root / ".review-writer" / "hosted-workspaces"
    )
    manager = HostedWorkspaceManager(workspace_root)
    service = LibraryService(session_factory, manager)
    selected_users = {uuid.UUID(item) for item in args.user_id}

    with database_session(session_factory) as session:
        statement = select(LibraryPaper).where(
            LibraryPaper.deleted_at.is_(None),
            LibraryPaper.status == "active",
        )
        if selected_users:
            statement = statement.where(LibraryPaper.user_id.in_(selected_users))
        rows = list(session.scalars(statement).all())
        candidates = [
            {
                "user_id": str(row.user_id),
                "paper_id": row.paper_id,
                "metadata": copy.deepcopy(dict(row.metadata_json or {})),
                "tags_json": copy.deepcopy(row.tags_json or {}),
            }
            for row in rows
            if needs_migration(dict(row.metadata_json or {}), row.tags_json)
        ]
        verified = sum(
            1
            for row in rows
            if structured_tags_are_verified(dict(row.metadata_json or {}))
        )

    summary: dict[str, Any] = {
        "scanned": len(rows),
        "eligible": len(candidates),
        "verified_preserved": verified,
        "dry_run": bool(args.dry_run),
        "updated": 0,
        "failed": [],
    }
    if args.dry_run or not candidates:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        engine.dispose()
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = workspace_root.parent / "migration-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"unverified-metadata-tags-{timestamp}.json"
    backup_path.write_text(
        json.dumps(
            {
                "created_at": timestamp,
                "policy": NEUTRAL_POLICY,
                "papers": candidates,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary["backup_path"] = str(backup_path)

    for item in candidates:
        principal = Principal(
            user_id=item["user_id"],
            roles=frozenset({Role.USER}),
        )
        try:
            service.update_metadata(
                principal,
                item["paper_id"],
                neutralized_metadata(item["metadata"]),
            )
            summary["updated"] += 1
        except Exception as exc:  # continue so one corrupt paper cannot stop the migration
            summary["failed"].append(
                {
                    "user_id": item["user_id"],
                    "paper_id": item["paper_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    engine.dispose()
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
