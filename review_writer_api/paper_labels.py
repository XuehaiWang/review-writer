"""Read-only user-library labels; never use display numbers as identity."""
import uuid

from sqlalchemy import select
from review_writer_api.database import database_session
from review_writer_api.workflow_models import LibraryPaper


def library_paper_labels(session_factory, user_id: str) -> dict[str, str]:
    with database_session(session_factory) as session:
        ids = session.scalars(select(LibraryPaper.paper_id).where(
            LibraryPaper.user_id == uuid.UUID(str(user_id)),
            LibraryPaper.deleted_at.is_(None),
        ).order_by(LibraryPaper.created_at.asc(), LibraryPaper.id.asc())).all()
    return {paper_id: f"P{index:03d}" for index, paper_id in enumerate(ids, 1)}
