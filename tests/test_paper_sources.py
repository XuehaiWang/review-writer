from __future__ import annotations

import unittest
from unittest.mock import patch

from review_writer_core.paper_sources import PaperSearchRequest, SourceSearchResult, search_paper_sources
from review_writer_core.paper_sources.crossref import CrossrefConnector
from review_writer_core.paper_sources.deduplicate import deduplicate_candidates


def candidate(
    source: str,
    provider_id: str,
    *,
    title: str,
    doi: str = "",
    authors: list[str] | None = None,
    year: int = 2024,
    rank: int = 1,
) -> dict:
    return {
        "identifiers": {
            "doi": doi,
            "arxiv_id": "",
            "openalex_id": provider_id if source == "openalex" else "",
            "semantic_scholar_id": provider_id if source == "semantic_scholar" else "",
        },
        "title": title,
        "abstract": f"Evidence about {title}",
        "authors": authors or ["Ada Lovelace"],
        "year": year,
        "publication_date": f"{year}-01-01",
        "journal": "Journal",
        "document_type": "journal-article",
        "citation_count": 10,
        "landing_url": f"https://example.test/{provider_id}",
        "pdf_url": "",
        "open_access": {"is_oa": False, "license": "", "source": source},
        "sources": [
            {
                "name": source,
                "provider_id": provider_id,
                "provider_rank": rank,
                "provider_score": None,
            }
        ],
        "abstract_decision": {"status": "not_run", "reason": "", "model_tier_snapshot": ""},
        "selected_for_download": False,
    }


class FakeConnector:
    def __init__(self, name: str, candidates: list[dict] | None = None, error: str = ""):
        self.name = name
        self.candidates = candidates or []
        self.error = error

    def search(self, _request: PaperSearchRequest) -> SourceSearchResult:
        if self.error:
            return SourceSearchResult(source=self.name, status="failed", error=self.error)
        return SourceSearchResult(source=self.name, status="completed", candidates=self.candidates)


class MultiSourceSearchTests(unittest.TestCase):
    def test_crossref_uses_exact_work_endpoint_for_doi_query(self) -> None:
        connector = CrossrefConnector(max_retries=0)
        payload = {
            "message": {
                "DOI": "10.1002/anie.201204796",
                "title": ["Enantioselective Decarboxylative Amination"],
                "author": [{"given": "A.", "family": "Author"}],
                "issued": {"date-parts": [[2013]]},
                "container-title": ["Angewandte Chemie International Edition"],
                "URL": "https://doi.org/10.1002/anie.201204796",
            }
        }
        with patch.object(connector, "_request_json", return_value=payload) as request:
            result = connector.search(
                PaperSearchRequest(query="10.1002/anie.201204796", limit=5)
            )

        requested_url = request.call_args.args[0]
        self.assertIn("/works/10.1002%2Fanie.201204796", requested_url)
        self.assertNotIn("query.bibliographic", requested_url)
        self.assertEqual("completed", result.status)
        self.assertEqual(2013, result.candidates[0]["year"])
        self.assertEqual(
            "10.1002/anie.201204796",
            result.candidates[0]["identifiers"]["doi"],
        )

    def test_crossref_uses_bibliographic_search_for_article_title(self) -> None:
        connector = CrossrefConnector(max_retries=0)
        payload = {"message": {"items": []}}
        title = "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol"
        with patch.object(connector, "_request_json", return_value=payload) as request:
            result = connector.search(PaperSearchRequest(query=title, limit=5))

        requested_url = request.call_args.args[0]
        self.assertIn("/works?", requested_url)
        self.assertIn("query.bibliographic=Preparation", requested_url)
        self.assertNotIn("/works/preparation", requested_url.casefold())
        self.assertEqual("completed", result.status)

    def test_partial_source_failure_preserves_results_and_deduplicates_strong_ids(self) -> None:
        connectors = [
            FakeConnector(
                "crossref",
                [candidate("crossref", "10.1/example", title="Catalyst Design", doi="10.1/example", rank=1)],
            ),
            FakeConnector(
                "openalex",
                [candidate("openalex", "W1", title="Catalyst Design", doi="https://doi.org/10.1/EXAMPLE", rank=2)],
            ),
            FakeConnector(
                "arxiv",
                [candidate("arxiv", "2401.00001", title="General Catalyst Screening", rank=1)],
            ),
            FakeConnector("semantic_scholar", error="rate limited"),
        ]
        events = []
        result = search_paper_sources(
            PaperSearchRequest(query="catalyst design", topic="catalyst design"),
            connectors=connectors,
            status_callback=lambda *event: events.append(event),
        )

        self.assertEqual("partial", result["completion_state"])
        self.assertTrue(result["degraded"])
        self.assertEqual("rate limited", result["source_errors"]["semantic_scholar"])
        self.assertEqual(2, len(result["candidates"]))
        merged = next(item for item in result["candidates"] if item["doi"] == "10.1/example")
        self.assertEqual({"crossref", "openalex"}, {item["name"] for item in merged["sources"]})
        self.assertIsInstance(merged["score"], float)
        self.assertIn("source_rank_normalized", merged["score_components"])
        self.assertTrue(any(event[1] == "failed" for event in events))

    def test_all_sources_failed_is_explicit(self) -> None:
        result = search_paper_sources(
            PaperSearchRequest(query="topic"),
            connectors=[FakeConnector("crossref", error="offline"), FakeConnector("arxiv", error="timeout")],
        )
        self.assertEqual("failed", result["completion_state"])
        self.assertEqual([], result["candidates"])
        self.assertEqual({"crossref", "arxiv"}, set(result["source_errors"]))

    def test_title_only_similarity_does_not_merge_without_author_evidence(self) -> None:
        rows = [
            candidate("crossref", "one", title="Same Title", authors=["First Author"]),
            candidate("openalex", "two", title="Same Title", authors=["Different Author"]),
        ]
        self.assertEqual(2, len(deduplicate_candidates(rows)))


if __name__ == "__main__":
    unittest.main()
