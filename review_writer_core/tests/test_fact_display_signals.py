# -*- coding: utf-8 -*-
"""The per-paper fact signals the Planning list uses instead of fact records."""
from review_writer_core.scientific_facts import fact_display_signals


def test_signals_match_the_shared_table_the_paper_list_relies_on():
    """Keep this table in step with the digest equivalence test in the frontend.

    ``frontend/src/features/planning/MatrixAnalysisStatus.test.tsx`` asserts the
    same rows, so a change on either side fails one of the two suites.
    """
    cases = [
        ("no findings", [], False, False),
        ("verified record", [{"verification": {"status": "supported"}}], False, False),
        ("context-only record", [{"support_level": "context_only"}], False, True),
        ("uncertain record", [{"verification": {"status": "uncertain"}}], False, True),
        ("rejected record", [{"verification": {"status": "rejected"}}], False, True),
        ("unverified revision", [{"revision_of_fact_id": "old"}], True, False),
        ("superseded revision", [{"revision_of_fact_id": "old", "superseded_by_fact_id": "new"}], False, False),
    ]
    for name, facts, pending, limited in cases:
        assert fact_display_signals(facts) == {
            "has_pending_revision": pending,
            "has_limited_fact": limited,
        }, name


def test_pending_revision_ids_count_as_a_pending_revision():
    assert fact_display_signals([], pending_revision_fact_ids=["F1"]) == {
        "has_pending_revision": True,
        "has_limited_fact": False,
    }


def test_non_mapping_records_are_ignored_instead_of_crashing_the_list():
    assert fact_display_signals([None, "not a fact", {"support_level": "context_only"}]) == {
        "has_pending_revision": False,
        "has_limited_fact": True,
    }
