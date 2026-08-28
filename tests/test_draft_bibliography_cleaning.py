from review_writer_core.draft_bibliography import (
    clean_reference_doi,
    clean_reference_field,
)
from review_writer_core.metadata_fields import metadata_value, unwrap_metadata_value


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
