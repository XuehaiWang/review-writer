"""Recovery stays source-bound and works against real PostgreSQL query semantics."""
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, func, literal, select, text
from review_writer_api.config import database_url_from_env
from review_writer_api.domain_services.library_index import EvidenceHit, LibraryIndexService, postgres_term_group_constraint
from review_writer_api.domain_services.actions.planning.matrix import PlanningMatrixActionsMixin
from review_writer_api.security import Principal, Role
from review_writer_api.errors import WorkflowValidationError
from review_writer_core.evidence_queries import build_fact_query_plans


class PdfRecoveryContractTests(unittest.TestCase):
    def test_fact_source_map_keeps_legacy_version_and_preserves_explicit_si(self):
        row = {"paper_id": "article", "fact_enrichment": {"source_lineage_hash": "old-article"}}
        self.assertEqual({"article": "old-article"}, PlanningMatrixActionsMixin.fact_source_lineages(row))
        row["fact_enrichment"]["source_lineages"] = {"article": "current-article", "SI": "current-si"}
        self.assertEqual({"article": "current-article", "SI": "current-si"}, PlanningMatrixActionsMixin.fact_source_lineages(row))


    def test_exact_resolver_reopens_pdf_pages_and_rejects_different_content_hash(self):
        service = LibraryIndexService.__new__(LibraryIndexService)
        service.session_factory = None
        principal = Principal("1872bdbb-c312-4b80-b15b-cbd60985bc64", frozenset({Role.USER}))
        digest = "a" * 64
        requests = [f"pdf-text:{digest}:p{page}" for page in range(1, 5)]
        requests += ["pdf-text:" + "b" * 64 + ":p5", "invalid:page", requests[0]]
        calls = []
        def recover(_principal, paper_id, pages, *, expected_lineage):
            calls.append((paper_id, pages, expected_lineage))
            return [EvidenceHit(paper_id=paper_id, chunk_id=f"pdf-text:{digest}:p{page}", content="Yield was 91%.",
                page_start=page, page_end=page, section_path=("PDF",), content_type="pdf_text", asset_refs=(),
                score=0, match_reason="source_text_recovery", is_neighbor=False, index_id="", source_lineage_hash=expected_lineage)
                for page in pages]
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        with patch("review_writer_api.domain_services.library_index.database_session") as session, patch.object(service, "recover_pdf_pages", side_effect=recover):
            session.return_value.__enter__.return_value = db
            resolved = service.resolve_source_chunks(principal, "P1", requests, expected_lineage="original-version")
        self.assertEqual(requests[:4], [item["chunk_id"] for item in resolved])
        self.assertEqual([("P1", [1, 2, 3], "original-version"), ("P1", [4, 5], "original-version")], calls)

    def test_registered_page_recovery_is_bounded_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "paper.pdf"
            path.write_bytes(b"registered fake pdf; decoder is mocked")
            paper = SimpleNamespace(pdf_relative_path="paper.pdf", content_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            service = LibraryIndexService.__new__(LibraryIndexService)
            service.workspace_manager = SimpleNamespace(user_root=lambda _: root)
            service._paper_and_lineage = lambda *a: (paper, [], {}, "version-current")
            principal = SimpleNamespace(user_id="owner")
            pages = [SimpleNamespace(extract_text=lambda: "The original value is 245 K.")] * 8
            with patch("pypdf.PdfReader", return_value=SimpleNamespace(pages=pages)) as reader:
                hits = service.recover_pdf_pages(principal, "paper-A", [1, 2, 3, 4, 5], expected_lineage="version-current")
                self.assertEqual([1, 2, 3], [hit.page_start for hit in hits])
                self.assertTrue(all(paper.content_sha256 in hit.chunk_id for hit in hits))
                self.assertTrue(all(hit.source_lineage_hash == "version-current" for hit in hits))
                with self.assertRaises(WorkflowValidationError):
                    service.recover_pdf_pages(principal, "paper-A", [1], expected_lineage="obsolete")
                path.write_bytes(b"tampered")
                with self.assertRaises(WorkflowValidationError):
                    service.recover_pdf_pages(principal, "paper-A", [1], expected_lineage="version-current")
                self.assertEqual(1, reader.call_count)


@unittest.skipUnless(os.environ.get("REVIEW_WRITER_RUN_POSTGRES_TESTS") == "1", "PostgreSQL integration opt-in")
class PostgreSQLFactRecoveryTests(unittest.TestCase):
    def test_long_question_recovers_source_without_literal_instruction_matching(self):
        # SELECT-only: no user records, temporary schemas or production mutations.
        engine = create_engine(database_url_from_env())
        try:
            with engine.connect() as connection:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                for field, targets, source in [
                    ("quantitative_results", ["external cohort", "accuracy"], "External cohort accuracy was 84%."),
                    ("method_conditions", ["annealing", "temperature"], "The annealing procedure used a temperature of 245 K."),
                    ("quantitative_results", ["benchmark", "latency"], "The benchmark result reported latency of 12 ms."),
                ]:
                    question = "Please provide all surrounding discussion and determine the exact published details."
                    vector = func.to_tsvector("simple", literal(source))
                    self.assertFalse(connection.scalar(select(vector.op("@@")(func.websearch_to_tsquery("simple", question)))))
                    plan = build_fact_query_plans({"field_id": field, "query": question, "target_terms": targets})[0]
                    result = connection.scalar(select(literal(True)).where(
                        vector.op("@@")(func.websearch_to_tsquery("simple", plan["websearch_query"])),
                        postgres_term_group_constraint(vector, plan["term_groups"])))
                    self.assertTrue(result, (field, plan))
        finally:
            engine.dispose()
