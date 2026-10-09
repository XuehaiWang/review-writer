from review_writer_core.draft_bibliography import (
    clean_reference_doi,
    clean_reference_field,
    format_citation_group,
    strip_numeric_callouts,
)
from review_writer_core.metadata_fields import metadata_value, unwrap_metadata_value


def test_chemical_locants_are_not_treated_as_citations():
    # A locant is a bracketed number followed by a hyphen; citations never are, and
    # treating one as a citation used to delete the locant from real drafts.
    assert (
        strip_numeric_callouts("[2,3]-sigmatropic rearrangement was observed.")
        == "[2,3]-sigmatropic rearrangement was observed."
    )
    assert (
        strip_numeric_callouts("[3,3]-rearrangement and [1,2]-shift were reported.")
        == "[3,3]-rearrangement and [1,2]-shift were reported."
    )
    # Real callouts are still removed, including ranges and groups.
    assert strip_numeric_callouts("Yields were high [2,3] and broad [4-6].") == "Yields were high and broad."
    assert strip_numeric_callouts("Reported earlier [7].") == "Reported earlier."
    assert format_citation_group([3, 1]) == "[1, 3]"


def test_reference_cleaners_remove_provider_residue_and_normalize_doi():
    assert (
        clean_reference_field("  Example title ★ Read Online extra controls ")
        == "Example title"
    )
    assert clean_reference_doi("https://doi.org/10.1000/example.)") == "10.1000/example"


def test_metadata_value_accepts_raw_and_provenance_wrapped_fields():
    assert unwrap_metadata_value({"value": "wrapped", "source": "crossref"}) == "wrapped"
    assert metadata_value({"title": {"value": "Article"}}, "title") == "Article"
    assert metadata_value({"year": 2025}, "year") == 2025
