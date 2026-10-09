"""Bibliography comparison must tolerate notation while keeping real conflicts."""

import pytest

from review_writer_core.bibliography_audit import (
    _author_keys,
    _dois_equivalent,
    _field_values_conflict,
    _field_values_equivalent,
    _journals_equivalent,
    _title_key,
    audit_bibliography,
    bibliography_audit_reusable,
    bibliography_input_fingerprint,
)
from review_writer_core.paper_sources.base import SourceSearchResult


# --------------------------------------------------------------------------
# Field-level comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("J. Am. Chem. Soc", "Journal of the American Chemical Society"),
        ("J. AM. CHEM. SOC", "Journal of the American Chemical Society"),
        ("Angew. Chem. Int. Ed", "Angewandte Chemie International Edition"),
        ("Org. Chem. Front", "Organic Chemistry Frontiers"),
        ("Chem. Soc. Rev", "Chemical Society Reviews"),
        ("RSC Adv", "RSC Advances"),
        ("Chem. Rev", "Chemical Reviews"),
        ("J. Org. Chem", "The Journal of Organic Chemistry"),
    ],
)
def test_journal_abbreviations_match_full_titles(left, right):
    assert _journals_equivalent(left, right)
    assert _field_values_equivalent("journal", left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("Nat. Commun.", "Nature Chemistry"),
        ("Chem. Sci.", "Chem. Soc. Rev."),
        ("Tetrahedron", "Tetrahedron Letters"),
        ("J. Org. Chem.", "J. Am. Chem. Soc."),
        ("Org. Lett.", "Org. Biomol. Chem."),
    ],
)
def test_distinct_journals_are_never_merged(left, right):
    assert not _journals_equivalent(left, right)


@pytest.mark.parametrize(
    "left,right",
    [
        ("Cobalt-catalyzed deuteration with D 2 O", "Cobalt-catalyzed deuteration with D2O"),
        ("Synthesis of α-Amino Acids", "Synthesis of α‐Amino Acids"),
        ("A <i>via</i> study", "A via study"),
        ("Copper-catalysed SN2 substitution", "Copper-catalysed SN 2 substitution"),
    ],
)
def test_title_markup_and_spacing_is_not_a_conflict(left, right):
    assert _title_key(left) == _title_key(right)
    assert _field_values_equivalent("title", left, right)


def test_genuinely_different_titles_still_conflict():
    assert _field_values_conflict(
        "title", ["Recent advances in allene chemistry", "Synthesis of axially chiral allenes"]
    )


@pytest.mark.parametrize(
    "left,right",
    [
        (["a Guowei Kang", "ab Sreekumar Vellalath"], ["Guowei Kang", "Sreekumar Vellalath"]),
        (["[a] Zhongtian Wu", "(b) Yifan Sun"], ["Zhongtian Wu", "Yifan Sun"]),
        (["Anaelle Dumas", "Lukas Lercher"], ["Ana \u00a8elle Dumas", "Lukas Lercher"]),
        (["Anaëlle Dumas"], ["Anaelle Dumas"]),
    ],
)
def test_author_footnote_markers_and_accents_are_ignored(left, right):
    assert _author_keys(left) == _author_keys(right)
    assert _field_values_equivalent("authors", left, right)


def test_lowercase_name_particles_are_not_stripped_as_footnotes():
    assert "vanderwaals" in "".join(_author_keys(["van der Waals, J."]))
    assert "delacruz" in "".join(_author_keys(["M. de la Cruz"]))


def test_missing_authors_remain_a_real_conflict():
    assert _field_values_conflict(
        "authors", [["Peng Zhang", "Ying Shao"], ["Shengbiao Tang", "Peng Zhang", "Ying Shao", "Jiangtao Sun"]]
    )


@pytest.mark.parametrize(
    "left,right",
    [
        ("10.1021/acs.orglett.2c01794", "10.1021/acs.orglett.2c01794org"),
        ("10.1021/acs.orglett.2c01794", "10.1021/acs.orglett.2c01794.pdf"),
    ],
)
def test_pdf_extraction_domain_artifacts_are_same_doi(left, right):
    assert _dois_equivalent(left, right)


def test_different_dois_are_kept_apart():
    assert not _dois_equivalent("10.1038/s44160-021-00008-x", "10.1002/anie.202515741")
    assert not _dois_equivalent("10.1021/ja910503k", "10.1021/ol300296k")


def test_year_only_date_is_compatible_with_year_month():
    assert _field_values_equivalent("first_publication_date", "2025", "2025-11-11")
    assert not _field_values_equivalent("first_publication_date", "2025", "2024-11-11")


# --------------------------------------------------------------------------
# End-to-end audit behaviour
# --------------------------------------------------------------------------

META = {
    "paper_id": "P1",
    "title": {"value": "A study of allene synthesis", "confidence": 0.95},
    "authors": {"value": ["Alice Smith"], "confidence": 0.95},
    "journal": {"value": "J. Am. Chem. Soc", "confidence": 0.95},
    "year": {"value": 2020, "confidence": 0.95},
    "doi": {"value": "10.1000/example", "confidence": 1.0},
}


class Crossref:
    name = "crossref"

    def __init__(self, candidate, name="crossref"):
        self.candidate = candidate
        self.name = name

    def search(self, request):
        return SourceSearchResult(source=self.name, status="completed", candidates=[self.candidate])


def agent(fields):
    return {"status": "reliable", "method": "bounded_document_agent", "fields": fields}


def test_abbreviated_journal_no_longer_blocks_readiness():
    connector = Crossref(
        {
            "title": META["title"]["value"],
            "authors": ["Alice Smith"],
            "year": 2020,
            "journal": "Journal of the American Chemical Society",
            "identifiers": {"doi": "10.1000/example"},
        }
    )
    result = audit_bibliography(
        META,
        connectors=[connector],
        network_mode="force",
        document_agent_extraction=agent({"journal": {"value": "J. AM. CHEM. SOC", "confidence": 0.9}}),
    )
    assert result["status"] == "verified"
    assert result["unresolved_conflicts"] == []
    assert all(row["field"] != "journal" for row in result["conflicts"])


def test_weak_agent_dissent_is_not_a_conflict():
    result = audit_bibliography(
        META,
        connectors=[],
        network_mode="disabled",
        document_agent_extraction=agent(
            {"authors": {"value": ["directed evolution of plant-derived nonheme Fe"], "confidence": 0.9}}
        ),
    )
    assert result["unresolved_conflicts"] == []


def test_provider_identity_outranks_an_unsourced_canonical_value():
    connector = Crossref(
        {
            "title": META["title"]["value"],
            "authors": ["Alice Smith", "Bob Jones"],
            "year": 2020,
            "journal": "Journal of the American Chemical Society",
            "identifiers": {"doi": "10.1000/example"},
        }
    )
    result = audit_bibliography(META, connectors=[connector], network_mode="force")
    assert result["status"] == "verified"
    assert result["unresolved_conflicts"] == []
    assert result["canonical_updates"]["authors"]["value"] == ["Alice Smith", "Bob Jones"]


def test_equal_standing_disagreement_stays_unresolved():
    # Two curated providers both matched the DOI exactly but report different
    # journals.  Neither outranks the other, so a human still decides.
    base = {
        "title": META["title"]["value"],
        "authors": ["Alice Smith"],
        "year": 2020,
        "identifiers": {"doi": "10.1000/example"},
    }
    result = audit_bibliography(
        META,
        connectors=[
            Crossref({**base, "journal": "Journal of Organic Chemistry"}, name="crossref"),
            Crossref({**base, "journal": "Organic Letters"}, name="openalex"),
        ],
        network_mode="force",
    )
    assert result["status"] == "conflict"
    assert any(row["field"] == "journal" for row in result["unresolved_conflicts"])


def test_manually_checked_field_is_flagged_but_never_rewritten():
    """A wrong hand-entered DOI must surface with a suggestion, not be replaced."""

    metadata = {
        **META,
        "doi": {"value": "10.1021/ol300296k", "source": "human_review", "confidence": 1, "human_checked": True},
    }
    connector = Crossref(
        {
            "title": META["title"]["value"],
            "authors": ["Alice Smith"],
            "year": 2020,
            "journal": "Journal of the American Chemical Society",
            "identifiers": {"doi": "10.1000/example"},
        }
    )
    result = audit_bibliography(metadata, connectors=[connector], network_mode="force")
    assert "doi" not in result["canonical_updates"]
    flagged = [row for row in result["manual_review_conflicts"] if row["field"] == "doi"]
    assert flagged and flagged[0]["human_checked"] is True
    assert flagged[0]["suggested"]["value"] == "10.1000/example"
    assert result["field_readiness"]["manual_review_conflicts"]


def test_clean_manually_checked_field_is_not_flagged():
    metadata = {
        **META,
        "doi": {"value": "10.1000/example", "source": "human_review", "confidence": 1, "human_checked": True},
    }
    result = audit_bibliography(metadata, connectors=[], network_mode="disabled")
    assert result["manual_review_conflicts"] == []


# --------------------------------------------------------------------------
# Unchanged re-runs must not pay for the model again
# --------------------------------------------------------------------------


def test_audit_fingerprint_is_stable_and_input_sensitive():
    metadata = {"paper_id": "P1", "title": {"value": "A study"}, "source_file": {"sha256": "abc"}}
    base = bibliography_input_fingerprint(metadata, "markdown body")
    assert base == bibliography_input_fingerprint(metadata, "markdown body")
    assert base != bibliography_input_fingerprint({**metadata, "year": {"value": 2020}}, "markdown body")
    assert base != bibliography_input_fingerprint(metadata, "a different markdown body")
    assert base != bibliography_input_fingerprint(
        {**metadata, "source_file": {"sha256": "xyz"}}, "markdown body"
    )


def test_matching_fingerprint_reuses_the_previous_audit():
    previous = {"status": "verified", "input_fingerprint": "fp", "sources": {"crossref": {}}}
    assert bibliography_audit_reusable(previous, fingerprint="fp")
    assert not bibliography_audit_reusable(previous, fingerprint="other")
    assert not bibliography_audit_reusable({**previous, "status": "running"}, fingerprint="fp")
    assert not bibliography_audit_reusable(None, fingerprint="fp")
    assert not bibliography_audit_reusable(
        {**previous, "status": "not_audited"}, fingerprint="fp"
    )


def test_forced_network_retry_is_never_reused():
    previous = {"status": "verified", "input_fingerprint": "fp"}
    assert not bibliography_audit_reusable(previous, fingerprint="fp", network_mode="force")
    assert bibliography_audit_reusable(previous, fingerprint="fp", network_mode="fallback")


def test_fingerprint_ignores_non_identity_noise():
    metadata = {"paper_id": "P1", "title": {"value": "A study"}, "source_file": {"sha256": "abc"}}
    base = bibliography_input_fingerprint(metadata, "markdown body")
    noisy = {
        **metadata,
        "_artifact_ids": {"metadata": "new-version"},
        "quality": {"warnings": ["changed"]},
    }
    assert bibliography_input_fingerprint(noisy, "markdown body") == base
