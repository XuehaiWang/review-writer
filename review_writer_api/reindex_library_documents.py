"""Resumable operator CLI for rebuilding one user's derived Library indexes."""

from __future__ import annotations

import argparse
import json
import uuid

from sqlalchemy import select

from review_writer_api.config import ApiSettings
from review_writer_api.database import User, create_session_factory, database_session
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build missing lexical document indexes without rerunning MinerU."
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    settings = ApiSettings.from_env()
    if settings.deployment_mode != "hosted":
        raise SystemExit("Library document indexing requires hosted mode.")
    try:
        user_id = uuid.UUID(args.user_id)
    except ValueError as exc:
        raise SystemExit("--user-id must be a UUID.") from exc
    sessions, engine = create_session_factory(settings.database_url)
    manager = HostedWorkspaceManager(
        settings.hosted_workspace_root
        or (settings.review_root / ".review-writer" / "hosted-workspaces")
    )
    service = LibraryIndexService(sessions, manager)
    try:
        with database_session(sessions) as session:
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise SystemExit("The requested active user does not exist.")
            statement = select(LibraryPaper.paper_id).where(
                LibraryPaper.user_id == user_id,
                LibraryPaper.deleted_at.is_(None),
                LibraryPaper.status == "active",
            )
            requested = [str(item).strip() for item in args.paper_id if str(item).strip()]
            if requested:
                statement = statement.where(LibraryPaper.paper_id.in_(tuple(requested)))
            paper_ids = list(
                session.scalars(
                    statement.order_by(LibraryPaper.updated_at).limit(
                        max(1, min(int(args.limit), 10000))
                    )
                )
            )
            principal = Principal(
                str(user_id), frozenset({Role.USER}), str(user.email)
            )
        results = []
        for paper_id in paper_ids:
            prepared = service.prepare(principal, paper_id, force=args.force)
            if not prepared.needs_job:
                results.append({"paper_id": paper_id, "status": "already_ready"})
                continue
            try:
                results.append(
                    service.build(
                        principal,
                        paper_id,
                        expected_lineage_hash=prepared.source_lineage_hash,
                    )
                )
            except Exception as exc:
                results.append(
                    {
                        "paper_id": paper_id,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                )
        print(json.dumps({"count": len(results), "items": results}, ensure_ascii=False))
        return 1 if any(item.get("status") == "failed" for item in results) else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
