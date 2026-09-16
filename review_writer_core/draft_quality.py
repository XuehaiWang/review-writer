"""Shared provenance and safety rules for Draft quality artifacts.

The feedback CLI and API both produce or consume quality data.  Keeping the
scope, freshness and score-gate rules here prevents either side from treating
an incremental paragraph snapshot as an authoritative full-draft evaluation.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

from review_writer_core.workflow.artifacts import (
    BLUEPRINT, DRAFT_REWRITE_OVERLAYS, FIGURE_MANIFEST, MATRIX,
    SECTION_DRAFTS, SECTION_EVIDENCE_PACKAGE, SECTION_WRITING_PLAN,
)


DRAFT_QUALITY_RULE_VERSION = "draft-quality/11"
FULL_DRAFT_QUALITY_SCOPE = "full_draft"
DEFAULT_SCORE_TOLERANCE = 1.0
QUALITY_INPUT_ARTIFACTS = {
    "source_matrix_artifact_id": MATRIX,
    "source_blueprint_artifact_id": BLUEPRINT,
    "source_sections_artifact_id": SECTION_DRAFTS,
    "source_section_evidence_artifact_id": SECTION_EVIDENCE_PACKAGE,
    "source_writing_plan_artifact_id": SECTION_WRITING_PLAN,
    "source_figure_manifest_artifact_id": FIGURE_MANIFEST,
    "source_rewrite_overlay_artifact_id": DRAFT_REWRITE_OVERLAYS,
}
QUALITY_INPUT_ARTIFACT_KEYS = tuple(QUALITY_INPUT_ARTIFACTS)


def draft_text_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def paragraph_coverage(paragraphs: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        str(row.get("paragraph_id") or "")
        for row in paragraphs
        if str(row.get("paragraph_id") or "").strip()
    ]


def quality_input_artifact_ids(payload: Mapping[str, Any]) -> dict[str, str]:
    """Select only upstream identities that can change evaluation evidence."""

    return {
        key: str(payload.get(key) or "")
        for key in QUALITY_INPUT_ARTIFACT_KEYS
        if str(payload.get(key) or "").strip()
    }


def full_draft_quality_provenance(
    draft_text: str,
    paragraphs: Iterable[Mapping[str, Any]],
    *,
    input_artifact_ids: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the exact bytes and inputs covered by a full evaluation."""

    return {
        "quality_scope": FULL_DRAFT_QUALITY_SCOPE,
        "evaluation_input_sha256": draft_text_sha256(draft_text),
        "paragraph_coverage": paragraph_coverage(paragraphs),
        "evaluation_rule_version": DRAFT_QUALITY_RULE_VERSION,
        "input_artifact_ids": {
            str(key): str(value)
            for key, value in sorted((input_artifact_ids or {}).items())
            if str(value).strip()
        },
    }


def reusable_full_draft_quality(
    quality: Mapping[str, Any] | None,
    *,
    draft_artifact_id: str,
    draft_text: str,
    paragraphs: Iterable[Mapping[str, Any]],
    input_artifact_ids: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Return whether ``quality`` is an exact, complete baseline."""

    reasons = full_draft_quality_mismatch_reasons(
        quality,
        draft_text=draft_text,
        paragraphs=paragraphs,
        input_artifact_ids=input_artifact_ids,
    )
    value = quality or {}
    if str(value.get("source_draft_artifact_id") or "") != str(draft_artifact_id):
        reasons.append("source_draft_artifact_mismatch")
    return not reasons, reasons


def full_draft_quality_mismatch_reasons(
    quality: Mapping[str, Any] | None,
    *,
    draft_text: str,
    paragraphs: Iterable[Mapping[str, Any]],
    input_artifact_ids: Mapping[str, Any] | None = None,
) -> list[str]:
    """Describe why a Quality report does not cover these exact draft bytes."""

    value = quality or {}
    expected = full_draft_quality_provenance(
        draft_text,
        paragraphs,
        input_artifact_ids=input_artifact_ids,
    )
    reasons: list[str] = []
    if str(value.get("quality_scope") or "") != FULL_DRAFT_QUALITY_SCOPE:
        reasons.append("quality_scope_not_full_draft")
    if str(value.get("evaluation_input_sha256") or "") != expected["evaluation_input_sha256"]:
        reasons.append("evaluation_input_mismatch")
    if list(value.get("paragraph_coverage") or []) != expected["paragraph_coverage"]:
        reasons.append("paragraph_coverage_mismatch")
    if str(value.get("evaluation_rule_version") or "") != DRAFT_QUALITY_RULE_VERSION:
        reasons.append("evaluation_rule_version_mismatch")
    if dict(value.get("input_artifact_ids") or {}) != expected["input_artifact_ids"]:
        reasons.append("input_artifacts_changed")
    return reasons


def quality_score(value: Mapping[str, Any]) -> float:
    """Read a normalized 0-100 score from API or rubric quality payloads.

    API quality artifacts expose ``score`` while the feedback-loop rubric uses
    ``total_score``.  Accepting both names here prevents a valid rubric score
    from being treated as zero at workflow boundaries.
    """

    return max(0.0, min(float(value.get("score") or value.get("total_score") or 0), 100.0))


def pending_claim_downgrade_paragraph_ids(
    dispositions: Mapping[str, Any], source_quality: Mapping[str, Any]
) -> set[str]:
    """Keep historical dispositions, but require edits only for unresolved sources.

    The caller must validate the baseline against the current manuscript and
    evidence inputs before applying a candidate. A candidate's own verification
    cannot discharge this requirement: a newly narrowed claim still needs the
    existing accuracy-improvement proof.
    """

    verified = {
        str(row.get("paragraph_id") or "")
        for row in source_quality.get("paragraph_scores") or []
        if isinstance(row, Mapping)
        and row.get("source_check_status") == "verified"
        and row.get("source_evidence_refs")
        and not row.get("unsupported_claims")
        and row.get("evidence_problem_type", "none") == "none"
    }
    return {
        str(row["paragraph_id"])
        for row in dispositions.values()
        if isinstance(row, Mapping) and row.get("paragraph_id")
    } - verified


def _dimension_scores(value: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in value.get("dimension_scores") or []:
        if not isinstance(row, Mapping) or not str(row.get("id") or "").strip():
            continue
        try:
            if row.get("level") is not None:
                score = float(row["level"]) * 25.0
            elif row.get("score") is not None:
                score = float(row["score"])
            else:
                continue
        except (TypeError, ValueError):
            continue
        result[str(row["id"])] = max(0.0, min(score, 100.0))
    return result


def _unsupported_claims(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(row.get("paragraph_id") or ""), " ".join(str(claim).split()).casefold())
        for row in value.get("paragraph_scores") or []
        if isinstance(row, Mapping)
        for claim in row.get("unsupported_claims") or []
        if str(claim).strip()
    }


def _issue_identities(value: Mapping[str, Any]) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for row in value.get("paragraph_scores") or []:
        if not isinstance(row, Mapping):
            continue
        paragraph_id = str(row.get("paragraph_id") or "")
        identities.update(
            (paragraph_id, f"dimension:{dimension}")
            for dimension in row.get("failed_dimensions") or []
            if str(dimension).strip()
        )
        route = str(row.get("route") or "")
        if route and route not in {"pass", "final_polish"}:
            identities.add((paragraph_id, "blocking_route"))
    preflight = value.get("preflight") or {}
    if isinstance(preflight, Mapping):
        for row in preflight.get("paragraph_findings") or []:
            if not isinstance(row, Mapping):
                continue
            identity = str(row.get("rule") or row.get("issue_type") or "")
            if identity:
                identities.add(
                    (str(row.get("paragraph_id") or ""), f"preflight:{identity}")
                )
    return identities


def full_draft_score_gate(
    source: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    tolerance: float = DEFAULT_SCORE_TOLERANCE,
    source_text: str | None = None,
    candidate_text: str | None = None,
) -> dict[str, Any]:
    """Apply the shared no-material-regression gate on a 0-100 scale."""

    safe_tolerance = max(0.0, min(float(tolerance), 10.0))
    source_score = quality_score(source)
    candidate_score = quality_score(candidate)
    reasons: list[str] = []
    if candidate.get("hard_gate_failures"):
        reasons.append("full_draft_integrity_failure")
    if candidate_score < source_score - safe_tolerance:
        reasons.append("overall_score_regression")
    introduced_unsupported = sorted(
        _unsupported_claims(candidate) - _unsupported_claims(source)
    )
    introduced_issues = sorted(
        _issue_identities(candidate) - _issue_identities(source)
    )
    unchanged_ids = set()
    if source_text is not None and candidate_text is not None:
        from .stages.draft.text import paragraph_spans
        originals = {row["paragraph_id"]: row["text"] for row in paragraph_spans(source_text)}
        unchanged_ids = {row["paragraph_id"] for row in paragraph_spans(candidate_text)
                         if originals.get(row["paragraph_id"]) == row["text"]}
    additional_findings = [row for row in introduced_issues if row[0] in unchanged_ids]
    additional_unsupported = [row for row in introduced_unsupported if row[0] in unchanged_ids]
    introduced_unsupported = [row for row in introduced_unsupported if row[0] not in unchanged_ids]
    introduced_issues = [row for row in introduced_issues if row[0] not in unchanged_ids]
    if introduced_unsupported:
        reasons.append("new_unsupported_claims")
    if additional_findings or additional_unsupported:
        reasons.append("additional_findings_on_unchanged_text")
    if introduced_issues:
        reasons.append("new_quality_issues")

    source_dimensions = _dimension_scores(source)
    candidate_dimensions = _dimension_scores(candidate)
    dimension_deltas: dict[str, float] = {}
    if source_dimensions and set(source_dimensions) != set(candidate_dimensions):
        reasons.append("dimension_scores_incomplete")
    else:
        for dimension_id, old_score in source_dimensions.items():
            delta = round(candidate_dimensions[dimension_id] - old_score, 3)
            dimension_deltas[dimension_id] = delta
            if delta < -safe_tolerance:
                reasons.append(f"dimension_regression:{dimension_id}")

    return {
        "allowed": not reasons,
        "score_tolerance": safe_tolerance,
        "source_score": round(source_score, 2),
        "candidate_score": round(candidate_score, 2),
        "overall_score_delta": round(candidate_score - source_score, 2),
        "dimension_score_deltas": dimension_deltas,
        "introduced_unsupported_claims": [
            {"paragraph_id": paragraph_id, "claim": claim}
            for paragraph_id, claim in introduced_unsupported
        ],
        "introduced_issue_ids": [
            {"paragraph_id": paragraph_id, "issue": issue}
            for paragraph_id, issue in introduced_issues
        ],
        "additional_findings_on_unchanged_text": [
            {"paragraph_id": paragraph_id, "issue": issue}
            for paragraph_id, issue in additional_findings
        ],
        "additional_unsupported_claims_on_unchanged_text": [
            {"paragraph_id": paragraph_id, "claim": claim}
            for paragraph_id, claim in additional_unsupported
        ],
        "reasons": reasons,
    }
