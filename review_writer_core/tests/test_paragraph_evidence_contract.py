"""The body-section "shopping list" is derived, never asked of the model."""

from review_writer_core.section_narrative_contracts import build_section_writing_task


def _task(role):
    return build_section_writing_task({
        "section_id": "S1", "heading": "H", "section_role": role,
        "writing_objective": "Explain the method.", "primary_papers": ["P1"],
    })


def test_body_section_gets_the_shopping_list():
    task = _task("body")
    assert task["required_evidence"] == [
        "transformation", "key_catalyst", "outcome", "scope_class", "decisive_boundary",
    ]
    assert task["excluded_by_default"] == [
        "per_compound_id", "individual_yield", "screening_steps",
        "routine_ops", "unrelated_mechanism", "closing_summary",
    ]


def test_body_section_keeps_table_records_out_of_the_compression():
    # Prose compression must not also shrink result_context: the reader table is
    # projected from those records, and two rows from two papers are required.
    task = _task("body")
    policy = task["table_record_policy"]
    assert "result_context_records_stay_complete" in policy
    assert "one_record_per_reported_method" in policy
    assert "fewer_than_two_records_from_two_papers_drops_the_table" in policy


def test_introduction_and_conclusion_keep_their_own_guidance():
    for role in ("introduction", "conclusion"):
        task = _task(role)
        assert "required_evidence" not in task
        assert "excluded_by_default" not in task
        assert "table_record_policy" not in task


def test_existing_task_fields_are_unchanged_for_body():
    task = _task("body")
    # The added list must not disturb the existing navigation fields.
    assert task["heading"] == "H"
    assert task["section_role"] == "body"
    assert task["primary_papers"] == ["P1"]
    assert "evidence_checks" in task
