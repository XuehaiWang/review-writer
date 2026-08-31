"""Queue bounded bibliography verification for historical Library papers."""

from __future__ import annotations

import argparse
import json
import uuid

from sqlalchemy import select

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import database_session
from review_writer_api.errors import WorkflowConflict
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue bibliography verification for unresolved historical papers."
    )
    parser.add_argument("--user-id", action="append", default=[])
    parser.add_argument("--paper-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-network",
        action="store_true",
        help="Consult the external provider even when local PDF evidence is reliable.",
    )
    parser.add_argument(
        "--include-verified",
        action="store_true",
        help="Recheck already verified rows after verification rules change.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    settings = ApiSettings.from_env()
    application = create_app(settings)
    sessions = application.state.session_factory
    library = application.state.library_service
    jobs = application.state.job_service
    run_id = str(arguments.run_id or uuid.uuid4().hex).strip()
    selected_users = {str(value).strip() for value in arguments.user_id if str(value).strip()}
    selected_papers = {str(value).strip() for value in arguments.paper_id if str(value).strip()}

    with database_session(sessions) as session:
        statement = select(LibraryPaper.user_id).where(
            LibraryPaper.deleted_at.is_(None),
            LibraryPaper.status == "active",
        )
        if selected_users:
            statement = statement.where(LibraryPaper.user_id.in_(selected_users))
        user_ids = sorted({str(value) for value in session.scalars(statement)})

    summary: dict[str, object] = {
        "run_id": run_id,
        "dry_run": bool(arguments.dry_run),
        "users": len(user_ids),
        "examined": 0,
        "eligible": 0,
        "queued": 0,
        "already_active": 0,
        "skipped_verified": 0,
        "job_ids": [],
    }
    limit = max(0, int(arguments.limit or 0))
    stop = False
    for user_id in user_ids:
        principal = Principal(user_id, frozenset({Role.USER}))
        for record in library.list(principal):
            if selected_papers and record.paper_id not in selected_papers:
                continue
            summary["examined"] = int(summary["examined"]) + 1
            audit_status = str((record.bibliography_audit or {}).get("status") or "")
            if audit_status == "verified" and not arguments.include_verified:
                summary["skipped_verified"] = int(summary["skipped_verified"]) + 1
                continue
            if limit and int(summary["eligible"]) >= limit:
                stop = True
                break
            summary["eligible"] = int(summary["eligible"]) + 1
            if arguments.dry_run:
                continue
            try:
                job = jobs.submit(
                    principal,
                    scope="library",
                    project_id=None,
                    job_type="library.bibliography-audit",
                    idempotency_key=f"historical:{run_id}:{record.paper_id}",
                    payload={
                        "paper_id": record.paper_id,
                        "metadata": record.metadata,
                        "pdf_relative_path": record.pdf_relative_path,
                        "markdown_relative_path": record.markdown_relative_path,
                        "previous_audit": record.bibliography_audit,
                        "network_mode": (
                            "force" if arguments.force_network else "disabled"
                        ),
                        "task_kind": "bibliography_verification",
                        "adds_candidate_papers": False,
                        "batch_run_id": run_id,
                    },
                    operation_key=f"bibliography-audit:{record.paper_id}",
                )
            except WorkflowConflict:
                summary["already_active"] = int(summary["already_active"]) + 1
                continue
            summary["queued"] = int(summary["queued"]) + 1
            job_ids = summary["job_ids"]
            if isinstance(job_ids, list) and len(job_ids) < 20:
                job_ids.append(job.id)
        if stop:
            break

    jobs.shutdown(wait=False)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
