"""One publication uploaded as two PDF files must not become two Library rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "view" / "local_pdf_ingestion.py"
_SPEC = importlib.util.spec_from_file_location("local_pdf_ingestion", MODULE_PATH)
ingestion = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(ingestion)


def _row(**metadata):
    return (Path("P001.metadata.json"), {"paper_id": "P001", **metadata})


def _field(value):
    return {"value": value, "source": "test", "confidence": 0.9, "human_checked": False}


EXISTING = _row(
    title=_field("A Practical Solution to Stereodefined Tetrasubstituted Allenes"),
    authors=_field(["Alice Smith", "Bob Jones"]),
    year=_field(2021),
    doi=_field("10.1021/ja910503k"),
)


def test_same_doi_and_title_is_one_publication():
    rows = [EXISTING]
    assert ingestion._duplicate_for_identity(
        rows,
        doi="https://doi.org/10.1021/JA910503K",
        title="A Practical Solution to Stereodefined Tetrasubstituted Allenes",
        first_author="alicesmith",
        year="2021",
    ) is not None


def test_matching_title_with_a_conflicting_doi_is_a_different_paper():
    """A legacy row can carry a wrong DOI; requiring the title keeps them apart."""

    rows = [EXISTING]
    assert ingestion._duplicate_for_identity(
        rows,
        doi="10.1002/anie.202515741",
        title="A Practical Solution to Stereodefined Tetrasubstituted Allenes",
        first_author="alicesmith",
        year="2021",
    ) is None


def test_title_matching_does_not_merge_wrong_doi_neighbours():
    rows = [EXISTING]
    assert ingestion._duplicate_for_identity(
        rows,
        doi="10.1021/ja910503k",
        title="Pd-Bronsted acid co-catalyzed dehydrative coupling of alcohols",
        first_author="zhaoqiangchen",
        year="2025",
    ) is None


def test_doi_less_records_match_on_title_author_and_year():
    rows = [_row(title=_field("Polarity-reversal catalysis of hydrogen-atom abstraction"), authors=_field(["Carol Lee"]), year=_field(2019), doi=_field(None))]
    assert ingestion._duplicate_for_identity(
        rows,
        doi="",
        title="Polarity reversal catalysis of hydrogen atom abstraction",
        first_author="carollee",
        year="2019",
    ) is not None
    assert ingestion._duplicate_for_identity(
        rows,
        doi="",
        title="Polarity reversal catalysis of hydrogen atom abstraction",
        first_author="carollee",
        year="2020",
    ) is None


@pytest.mark.parametrize("title", ["Acknowledgement", "Experimental Section", "ChemComm"])
def test_short_or_boilerplate_titles_never_merge(title):
    assert ingestion._duplicate_for_identity(
        [_row(title=_field(title), authors=_field(["Alice Smith"]), year=_field(2021), doi=_field("10.1/a"))],
        doi="",
        title=title,
        first_author="alicesmith",
        year="2021",
    ) is None


def test_different_files_same_digest_helper_is_unchanged():
    rows = [_row(source_file={"sha256": "abc"})]
    assert ingestion._duplicate_for_digest(rows, "abc") is not None
    assert ingestion._duplicate_for_digest(rows, "different") is None
