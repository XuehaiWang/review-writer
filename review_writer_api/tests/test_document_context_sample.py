import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from review_writer_api.database import Base, User, database_session
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryPaper, LibraryDocumentIndex, LibraryDocumentChunk


def test_no_abstract_sample_spans_document_and_checks_ownership_lineage_and_archive():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner, other = uuid.uuid4(), uuid.uuid4()
    with database_session(sessions) as session:
        session.add_all([User(id=uid, email=f"{uid}@test.invalid", password_hash="test") for uid in (owner, other)])
        session.flush()
        paper = LibraryPaper(user_id=owner, paper_id="P1", content_sha256="test",
                             original_filename="sample.pdf", pdf_relative_path="p.pdf", markdown_relative_path="p.md")
        session.add(paper)
        session.flush()
        index = LibraryDocumentIndex(user_id=owner, library_paper_id=paper.id, paper_id="P1",
                                     source_lineage_hash="v1", chunker_version="test", status="ready", is_current=True)
        session.add(index)
        session.flush()
        for ordinal in range(21):
            session.add(LibraryDocumentChunk(user_id=owner, paper_id="P1", index_id=index.id,
                chunk_id=f"c{ordinal}", ordinal=ordinal, content=f"Body {ordinal}.",
                normalized_content=f"body {ordinal}.", content_sha256=str(ordinal), block_start=ordinal,
                block_end=ordinal, is_reference=ordinal == 20))
        paper_pk = paper.id
    service = LibraryIndexService(sessions, None)
    principal = Principal(str(owner), frozenset({Role.USER}))
    sampled = service.sample_document_context(principal, "P1", expected_lineage="v1", limit=100)
    assert len(sampled) == 6
    assert sampled[0]["chunk_id"] == "c0" and sampled[-1]["chunk_id"] == "c19"
    assert [row['chunk_id'] for row in service.sample_document_context(
        principal, 'P1', expected_lineage='v1', limit=3, opening_only=True)] == ['c0', 'c1', 'c2']
    assert service.sample_document_context(principal, 'P1', expected_lineage='old', opening_only=True) == []
    assert service.sample_document_context(principal, "P1", expected_lineage="old") == []
    assert service.sample_document_context(Principal(str(other), frozenset({Role.USER})), "P1", expected_lineage="v1") == []
    assert service.sample_document_context(principal, "missing", expected_lineage="v1") == []
    with database_session(sessions) as session:
        session.get(LibraryPaper, paper_pk).status = "archived"
    assert service.sample_document_context(principal, "P1", expected_lineage="v1") == []
    engine.dispose()
