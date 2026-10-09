"""The shared experiment note is split into cells so prose and the reader
quick-reference table consume one structured record instead of re-reading."""

from review_writer_core.stages.sections.source_writing import (
    RESULT,
    WRITE_SCHEMA,
    checked_result_records,
)


def _result_schema():
    claim = WRITE_SCHEMA["properties"]["paragraphs"]["items"]["properties"]["claims"]
    return claim["items"]["properties"]["result_context"]


def test_result_declares_separate_catalyst_and_environment_cells():
    assert set(RESULT["properties"]) == {
        "evidence_key",
        "object",
        "catalyst",
        "conditions",
        "environment",
        "result",
        "units",
    }
    # The write schema and repair schema must share the same cell definition.
    assert _result_schema()["items"] is RESULT


def test_every_cell_is_required_so_an_unreported_value_is_an_empty_string():
    assert set(RESULT["required"]) == set(RESULT["properties"])


def test_checked_records_validate_every_cell_against_the_source():
    refs = [{"evidence_key": "E001", "quote": "CuBr and pyrrolidine in DCM at room temperature gave 92% yield."}]
    good = [{
        "evidence_key": "E001",
        "object": "propargylic alcohols",
        "catalyst": "CuBr, pyrrolidine",
        "conditions": "",
        "environment": "DCM, room temperature",
        "result": "92% yield",
        "units": "",
    }]
    valid, issues = checked_result_records(good, refs)
    assert valid == good
    assert issues == []

    # A quantitative anchor invented outside the selected passage is rejected
    # even when the other cells are correct.
    fabricated = [{**good[0], "result": "88% yield"}]
    valid, issues = checked_result_records(fabricated, refs)
    assert valid == []
    assert issues == ["result_context_exceeds_selected_source"]


def test_legacy_five_cell_records_stay_readable():
    """Historical records predate the split; missing cells behave as empty."""
    refs = [{"evidence_key": "E001", "quote": "Terminal alkynes with paraformaldehyde and a Cu salt gave 60% yield."}]
    legacy = [{
        "evidence_key": "E001",
        "object": "terminal alkynes",
        "conditions": "Cu salt, paraformaldehyde",
        "result": "60% yield",
        "units": "",
    }]
    valid, issues = checked_result_records(legacy, refs)
    assert valid == legacy
    assert issues == []
