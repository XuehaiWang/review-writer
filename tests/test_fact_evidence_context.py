from copy import deepcopy
from itertools import permutations
import json
import os
import subprocess
import sys

from review_writer_core.fact_evidence_context import select_fact_evidence_context
from review_writer_core.section_narrative_contracts import (
    build_section_writing_guidance, derive_narrative_diagnostics,
)


def test_round_robin_preserves_late_role_without_duplicating_sources():
    rows = [{"evidence_key": str(i), "question_ids": ["method"], "content": "Method detail."}
            for i in range(20)]
    rows += [{"evidence_key": "result", "question_ids": ["result"], "content": "Important result."},
             {"evidence_key": "result", "question_ids": ["scope"], "content": "Important result."}]
    before = deepcopy(rows)
    selected = select_fact_evidence_context(rows, roles=["method", "result", "scope"], max_items=3)
    assert len(selected) == 3
    assert sum(r["evidence_key"] == "result" for r in selected) == 1
    assert selected[1]["question_ids"] == ["result", "scope"]
    assert rows == before


def test_windows_are_bounded_contiguous_and_do_not_add_ellipsis():
    rows = [{"evidence_key": str(i), "content": "Sentence one. Sentence two. " * 30} for i in range(8)]
    selected = select_fact_evidence_context(rows, max_chars=250, per_source_chars=100)
    assert sum(len(r["content"]) for r in selected) <= 250
    for row in selected:
        assert " ".join(rows[int(row["evidence_key"])]["content"].split()).startswith(row["content"])
        assert row["source_window"]["end"] == len(row["content"])
        assert row["source_window"]["truncated"]


def test_legacy_quotas_cannot_become_a_style_gate():
    result = derive_narrative_diagnostics({"paragraphs": [{}]}, {
        "required_paragraph_roles": ["comparison"], "minimum_comparison_paragraphs": 5,
        "requires_section_synthesis_exit": True})
    assert result["missing_requirements"] == []
    result = derive_narrative_diagnostics({"section_review": {
        "status": "needs_revision", "issues": ["Repeated opening."]}})
    assert result["missing_requirements"] == []
    assert result["issues"] == ["Repeated opening."]


def test_repair_window_preserves_known_multi_span_support_in_same_chunk():
    text = "Background. " * 300 + "Experiment B gave 91% yield. It used 5 mol% catalyst."
    quotes = ["Experiment B gave 91% yield.", "It used 5 mol% catalyst."]
    row = select_fact_evidence_context([{"evidence_key": "e", "content": text}],
        per_source_chars=150, support_spans=[{"evidence_key": "e", "support_excerpt": q} for q in quotes])[0]
    assert all(q in row["content"] for q in quotes)
    assert row["source_window"]["start"] > 0
    assert text[row["source_window"]["start"]:row["source_window"]["end"]] == row["content"]


def test_only_explicit_user_length_is_soft_guidance():
    assert "requested_length" not in build_section_writing_guidance({"target_words": 2000})
    assert build_section_writing_guidance({"target_words": 500, "target_words_source": "user"})["requested_length"] == 500
    assert build_section_writing_guidance({"depth_contract": {
        "diagnostic_policy": "user_defined", "target_word_min": 300}})["requested_length"] == {"target_word_min": 300}


def test_candidate_reordering_preserves_selection_under_source_and_character_budgets():
    rows = [
        {"evidence_key": "method-b", "question_ids": ["method"], "content": "Method B. " * 20},
        {"evidence_key": "method-a", "question_ids": ["method"], "content": "Method A. " * 20},
        {"evidence_key": "result", "question_ids": ["result"], "content": "Result and boundary. " * 20},
        {"evidence_key": "result", "question_ids": ["scope"], "content": "Result and boundary. " * 20},
        {"evidence_key": "abstract", "content_type": "abstract", "content": "Abstract. " * 20},
    ]
    before = deepcopy(rows)
    options = dict(roles=["method", "result", "scope"], max_chars=230,
                   max_items=3, per_source_chars=100)
    expected = select_fact_evidence_context(rows, **options)
    assert [row["evidence_key"] for row in expected[:2]] == ["method-a", "result"]
    assert sum(len(row["content"]) for row in expected) <= 230
    for reordered in permutations(rows):
        assert select_fact_evidence_context(reordered, **options) == expected
    assert rows == before


def test_explicit_role_priority_remains_semantic_and_abstract_remains_last():
    rows = [
        {"evidence_key": "a", "question_ids": ["method"], "content": "Method."},
        {"evidence_key": "z", "question_ids": ["result"], "content": "Result."},
        {"evidence_key": "0", "content_type": "abstract", "content": "Abstract."},
    ]
    assert select_fact_evidence_context(rows, roles=["result", "method"], max_items=1)[0]["evidence_key"] == "z"
    assert select_fact_evidence_context(rows, roles=["method", "result"], max_items=1)[0]["evidence_key"] == "a"
    assert select_fact_evidence_context(rows, roles=["result", "method"])[-1]["evidence_key"] == "0"


def test_contribution_priority_is_explicit_not_candidate_insertion_order():
    rows = [
        {"evidence_key": "a", "content": "Incidental context."},
        {"evidence_key": "z", "content": "The current work reports its main contribution.",
         "retrieval_passes": ["primary_contribution_sample"]},
        {"evidence_key": "result", "question_ids": ["result"], "content": "Main result."},
    ]
    for reordered in permutations(rows):
        selected = select_fact_evidence_context(reordered, roles=["result"], max_items=2)
        assert [row["evidence_key"] for row in selected] == ["z", "result"]


def test_order_normalization_preserves_repair_spans_and_real_source_changes():
    content = "Background. " * 40 + "Experiment A gave 91% yield. It used 5 mol% catalyst."
    row = {"evidence_key": "source", "question_ids": ["result", "method"], "content": content}
    spans = [{"evidence_key": "source", "support_excerpt": quote} for quote in
             ["Experiment A gave 91% yield.", "It used 5 mol% catalyst."]]
    selected = select_fact_evidence_context([row], roles=["method", "result"],
        per_source_chars=120, support_spans=spans)
    reordered = {**row, "question_ids": ["method", "result"]}
    assert select_fact_evidence_context([reordered], roles=["method", "result"],
        per_source_chars=120, support_spans=list(reversed(spans))) == selected
    assert all(span["support_excerpt"] in selected[0]["content"] for span in spans)
    assert select_fact_evidence_context([{**row, "content": "Experiment A gave 51% yield."}],
        roles=["method", "result"], per_source_chars=120) != selected


def test_set_roles_and_candidate_membership_are_stable_across_hash_seeds():
    script = """
import json
from review_writer_core.fact_evidence_context import select_fact_evidence_context
roles = frozenset({"method", "result", "scope"})
rows = [{"evidence_key": key, "question_ids": list(roles), "content": key + " result."}
        for key in {"source-a", "source-b", "source-c", "source-d"}]
print(json.dumps(select_fact_evidence_context(rows, roles=roles, max_items=2), sort_keys=True))
"""
    outputs = [subprocess.check_output([sys.executable, "-c", script], text=True,
        env={**os.environ, "PYTHONHASHSEED": str(seed)}) for seed in [1, 19, 301]]
    assert outputs[0] == outputs[1] == outputs[2]
    assert len(json.loads(outputs[0])) == 2
