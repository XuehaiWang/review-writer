"""Library search must distinguish a usable open PDF from an article link."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_writer_api import scientific_tasks


def _module(sources, attempts):
    return SimpleNamespace(
        normalize_doi=lambda value: str(value or ""),
        resolve_pdf_sources=lambda *_args, **_kwargs: (sources, attempts),
    )


def test_open_pdf_is_offered_only_after_pdf_header_check():
    source = {"provider": "europe_pmc", "pdf_url": "https://example.org/paper.pdf"}
    with patch("review_writer_api.fulltext_probe.probe", return_value={"state": "available"}) as probe:
        access = scientific_tasks.check_literature_candidate_access(
            _module([source], []), {"doi": "10.1234/paper"}, email=""
        )
    assert access["state"] == "available"
    assert access["source_found"] is True
    assert access["provider"] == "europe_pmc"
    probe.assert_called_once_with({"pdf_url": source["pdf_url"]})


def test_missing_pdf_is_not_mislabeled_as_certain_paywall():
    candidate = {"doi": "10.1234/paper", "landing_url": "https://example.org/article"}
    found_none = scientific_tasks.check_literature_candidate_access(
        _module([], [{"provider": "europe_pmc", "status": "no_open_access_pdf"}]),
        candidate, email="",
    )
    provider_failed = scientific_tasks.check_literature_candidate_access(
        _module([], [{"provider": "europe_pmc", "status": "provider_error"}]),
        candidate, email="",
    )
    assert found_none["state"] == "not_found"
    assert provider_failed["state"] == "unknown"
    assert found_none["source_found"] is False


def test_closed_oa_catalog_means_no_known_pdf_despite_other_provider_rate_limit():
    candidate = {"doi": "10.1016/j.measurement.2026.121114"}
    access = scientific_tasks.check_literature_candidate_access(
        _module([], [
            {"provider": "semantic_scholar", "status": "provider_error"},
            {"provider": "openalex", "status": "no_open_access_pdf", "oa_status": "closed"},
        ]),
        candidate, email="",
    )
    assert access["state"] == "not_found"
    assert access["source_found"] is False


def test_openalex_pdf_is_source_candidate_but_requires_header_check():
    source = {"provider": "openalex", "pdf_url": "https://repository.example/paper.pdf"}
    with patch("review_writer_api.fulltext_probe.probe", return_value={"state": "unknown"}):
        access = scientific_tasks.check_literature_candidate_access(
            _module([source], [{"provider": "openalex", "status": "open_access_pdf"}]),
            {"doi": "10.1234/example"}, email="",
        )
    assert access["state"] == "unknown"
    assert access["source_found"] is True
    assert access["provider"] == "openalex"


def test_unreachable_open_pdf_lead_remains_unverified():
    source = {"provider": "unpaywall", "pdf_url": "https://example.org/paper.pdf"}
    with patch("review_writer_api.fulltext_probe.probe", return_value={"state": "unknown"}):
        access = scientific_tasks.check_literature_candidate_access(
            _module([source], []), {"doi": "10.1234/paper"}, email=""
        )
    assert access["state"] == "unknown"
    assert access["source_found"] is True


def test_search_writes_pdf_availability_for_each_candidate(tmp_path: Path):
    module = _module([{"provider": "europe_pmc", "pdf_url": "https://example.org/paper.pdf"}], [])
    module.load_dotenv_if_present = lambda *_args: None
    module.search_crossref = lambda *_args, **_kwargs: [
        {"candidate_id": "a", "doi": "10.1234/a"},
        {"candidate_id": "b", "doi": "10.1234/b"},
    ]
    args = argparse.Namespace(review_root=tmp_path, output=tmp_path / "search.json",
                              topic="reaction chemistry", year_from=None, year_to=None,
                              limit=20, mailto="")
    with patch.object(scientific_tasks, "literature_module", return_value=module), patch(
        "review_writer_api.fulltext_probe.probe", return_value={"state": "available"}
    ):
        assert scientific_tasks.search(args) == 0
    saved = json.loads(args.output.read_text(encoding="utf-8"))
    assert saved["candidate_count"] == 2
    assert [row["availability"]["state"] for row in saved["candidates"]] == ["available", "available"]


def test_hyphenated_paper_name_matches_distinctive_query_prefix():
    module = scientific_tasks.literature_module()
    title = "DEFSR-Net: A joint learning network of super-resolution features"
    assert module._candidate_score("defsr", title, "", 2026, 0) >= 0.7
    assert module._candidate_score("net", title, "", 2026, 0) < 0.1


def test_single_distinctive_term_drops_unrelated_crossref_padding():
    module = scientific_tasks.literature_module()
    rows = [
        {"DOI": "10.1234/defsr", "title": ["DEFSR-Net: Dual-branch edge features"],
         "published": {"date-parts": [[2026]]}},
        {"DOI": "10.1234/other", "title": ["Unrelated image segmentation method"],
         "published": {"date-parts": [[2026]]}},
    ]
    results = module.search_crossref(
        "defsr", request_json=lambda *_args, **_kwargs: {"message": {"items": rows}}
    )
    assert [row["doi"] for row in results] == ["10.1234/defsr"]


def test_openalex_requires_oa_pdf_location():
    module = scientific_tasks.literature_module()
    closed = module.resolve_openalex(
        "10.1234/closed",
        request_json=lambda *_args, **_kwargs: {
            "open_access": {"oa_status": "closed"}, "locations": [{"is_oa": False, "pdf_url": None}],
        },
    )
    assert closed == {"status": "no_open_access_pdf", "provider": "openalex", "oa_status": "closed"}
    opened = module.resolve_openalex(
        "10.1234/open",
        request_json=lambda *_args, **_kwargs: {
            "best_oa_location": {"is_oa": True, "pdf_url": "https://repository.example/paper.pdf", "license": "cc-by"},
        },
    )
    assert opened["status"] == "open_access_pdf"
    assert opened["pdf_url"] == "https://repository.example/paper.pdf"
