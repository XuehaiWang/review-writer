from __future__ import annotations

from review_writer_core.draft_quality import (
    full_draft_quality_provenance,
    full_draft_score_gate,
    reusable_full_draft_quality,
    quality_score,
    pending_claim_downgrade_paragraph_ids,
)


def _paragraphs() -> list[dict[str, str]]:
    return [
        {"paragraph_id": "S01-p1"},
        {"paragraph_id": "S01-p2"},
    ]


def test_unchanged_prose_findings_are_not_attributed_to_rewrite():
    source = {"score": 90, "paragraph_scores": [{"paragraph_id": "p1", "failed_dimensions": []}]}
    candidate = {"score": 90, "paragraph_scores": [{"paragraph_id": "p1", "failed_dimensions": ["C03"]}]}
    prose = "# Review\n\nThe authors discussed prior results.\n\n<!-- paragraph_id: p1 -->\n"
    gate = full_draft_score_gate(source, candidate, source_text=prose, candidate_text=prose)
    assert gate["introduced_issue_ids"] == []
    assert gate["additional_findings_on_unchanged_text"] == [{"paragraph_id": "p1", "issue": "dimension:C03"}]
    assert not gate["allowed"]  # Still recorded and reviewed, never silently dismissed.
    changed = full_draft_score_gate(source, candidate, source_text=prose,
                                    candidate_text=prose.replace("prior results", "their results"))
    assert changed["introduced_issue_ids"]


def test_quality_score_accepts_api_and_rubric_field_names() -> None:
    assert quality_score({"score": 81.25}) == 81.25
    assert quality_score({"total_score": 82.5}) == 82.5
    assert quality_score({"score": 0, "total_score": 83.75}) == 83.75


def test_historical_claim_dispositions_do_not_reopen_verified_source_paragraphs() -> None:
    dispositions = {
        "old-claim": {"paragraph_id": "S01-p1"},
        "current-claim": {"paragraph_id": "S01-p2"},
    }
    source = {"paragraph_scores": [{
        "paragraph_id": "S01-p1", "source_check_status": "verified",
        "source_evidence_refs": ["evidence-1"], "unsupported_claims": [],
    }]}
    assert pending_claim_downgrade_paragraph_ids(dispositions, source) == {"S01-p2"}
    assert len(dispositions) == 2  # Closure history remains available for audit.


def test_claim_downgrade_stays_required_without_unambiguous_source_verification() -> None:
    dispositions = {"claim": {"paragraph_id": "S01-p1"}}
    verified = {
        "paragraph_id": "S01-p1", "source_check_status": "verified",
        "source_evidence_refs": ["evidence-1"], "unsupported_claims": [],
    }
    for override in [
        {"source_check_status": "partially_supported"},
        {"source_evidence_refs": []},
        {"unsupported_claims": ["A remaining unsupported assertion"]},
        {"evidence_problem_type": "unsupported_claim"},
    ]:
        source = {"paragraph_scores": [{**verified, **override}]}
        assert pending_claim_downgrade_paragraph_ids(dispositions, source) == {"S01-p1"}


def test_only_exact_full_draft_quality_is_reusable() -> None:
    text = "First paragraph.\n\nSecond paragraph.\n"
    inputs = {
        "source_matrix_artifact_id": "matrix-v1",
        "source_section_evidence_artifact_id": "evidence-v1",
    }
    quality = {
        "source_draft_artifact_id": "draft-v1",
        "score": 88,
        **full_draft_quality_provenance(
            text,
            _paragraphs(),
            input_artifact_ids=inputs,
        ),
    }

    reusable, reasons = reusable_full_draft_quality(
        quality,
        draft_artifact_id="draft-v1",
        draft_text=text,
        paragraphs=_paragraphs(),
        input_artifact_ids=inputs,
    )
    assert reusable
    assert not reasons

    partial = {**quality, "quality_scope": "batch_selected_paragraphs"}
    reusable, reasons = reusable_full_draft_quality(
        partial,
        draft_artifact_id="draft-v1",
        draft_text=text,
        paragraphs=_paragraphs(),
        input_artifact_ids=inputs,
    )
    assert not reusable
    assert "quality_scope_not_full_draft" in reasons

    reusable, reasons = reusable_full_draft_quality(
        quality,
        draft_artifact_id="draft-v1",
        draft_text=text + "Changed.\n",
        paragraphs=_paragraphs(),
        input_artifact_ids=inputs,
    )
    assert not reusable
    assert "evaluation_input_mismatch" in reasons

    reusable, reasons = reusable_full_draft_quality(
        quality,
        draft_artifact_id="draft-v1",
        draft_text=text,
        paragraphs=_paragraphs(),
        input_artifact_ids={**inputs, "source_matrix_artifact_id": "matrix-v2"},
    )
    assert not reusable
    assert "input_artifacts_changed" in reasons

    reusable, reasons = reusable_full_draft_quality(
        {**quality, "evaluation_rule_version": "draft-quality/2"},
        draft_artifact_id="draft-v1",
        draft_text=text,
        paragraphs=_paragraphs(),
        input_artifact_ids=inputs,
    )
    assert not reusable
    assert "evaluation_rule_version_mismatch" in reasons


def test_full_draft_score_gate_allows_only_bounded_regression() -> None:
    source = {
        "score": 90,
        "dimension_scores": [
            {"id": "accuracy", "level": 4},
            {"id": "synthesis", "level": 3},
        ],
        "paragraph_scores": [
            {"paragraph_id": "S01-p1", "unsupported_claims": []}
        ],
    }
    within_tolerance = {
        "score": 89,
        "dimension_scores": [
            {"id": "accuracy", "score": 99},
            {"id": "synthesis", "score": 74},
        ],
        "paragraph_scores": [
            {"paragraph_id": "S01-p1", "unsupported_claims": []}
        ],
        "hard_gate_failures": [],
    }
    assert full_draft_score_gate(source, within_tolerance)["allowed"]

    regressed = {
        **within_tolerance,
        "score": 88.9,
        "dimension_scores": [
            {"id": "accuracy", "score": 97},
            {"id": "synthesis", "score": 74},
        ],
        "paragraph_scores": [
            {
                "paragraph_id": "S01-p1",
                "unsupported_claims": ["A new unsupported mechanism"],
                "failed_dimensions": ["citation_granularity"],
                "route": "section_rewrite",
            }
        ],
    }
    gate = full_draft_score_gate(source, regressed)
    assert not gate["allowed"]
    assert "overall_score_regression" in gate["reasons"]
    assert "dimension_regression:accuracy" in gate["reasons"]
    assert "new_unsupported_claims" in gate["reasons"]
    assert "new_quality_issues" in gate["reasons"]
