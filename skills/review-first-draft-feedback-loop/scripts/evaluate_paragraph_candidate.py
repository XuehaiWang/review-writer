#!/usr/bin/env python3
"""Evaluate one current or accepted paragraph without rescoring the whole draft."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import feedback_loop as loop


def _local_preflight(preflight: dict, paragraph_id: str) -> dict:
    """Keep deterministic findings attributable to the changed paragraph only."""

    return {
        "case_word_range": preflight.get("case_word_range"),
        "checks": preflight.get("checks") or {},
        "hard_regressions": [
            value
            for value in preflight.get("hard_regressions") or []
            if paragraph_id in str(value)
        ],
        "paragraph_checks": [
            item
            for item in preflight.get("paragraph_checks") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ],
        "paragraph_findings": [
            item
            for item in preflight.get("paragraph_findings") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == paragraph_id
        ],
    }


def evaluate(args: argparse.Namespace) -> dict:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    request = loop.read_json(first / "paragraph_candidate_evaluation_request.json", {}) or {}
    paragraph_id = str(request.get("paragraph_id") or args.paragraph_id)
    evaluation_mode = str(request.get("evaluation_mode") or "accepted_candidate")
    current_paragraph_mode = evaluation_mode == "current_paragraph"
    original_text = str(
        (
            request.get("paragraph_text")
            if current_paragraph_mode
            else request.get("original_text")
        )
        or ""
    )
    candidate_text = str(
        (
            request.get("paragraph_text")
            if current_paragraph_mode
            else request.get("candidate_text")
        )
        or ""
    )
    if not paragraph_id or not original_text.strip() or not candidate_text.strip():
        raise RuntimeError("The paragraph evaluation request is incomplete.")

    validation_warnings: list[str] = []
    if not current_paragraph_mode:
        validation_errors, validation_warnings = loop.validate_rewrite_report(
            original_text,
            candidate_text,
            args.min_case_words if bool(request.get("word_range_applicable", True)) else 1,
            args.max_case_words,
            allowed_unsupported_claims=[
                str(value)
                for value in request.get("allowed_unsupported_claims") or []
                if str(value).strip()
            ],
        )
        if validation_errors:
            raise RuntimeError(
                "The accepted paragraph candidate failed integrity validation: "
                + ", ".join(validation_errors)
            )

    markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    paragraphs = loop.parse_marked_paragraphs(markdown)
    paragraph = next(
        (
            item
            for item in paragraphs
            if str(item.get("paragraph_id") or "") == paragraph_id
        ),
        None,
    )
    if paragraph is None:
        raise RuntimeError(f"Paragraph not found in candidate draft: {paragraph_id}")
    if loop.clean_text(paragraph.get("text")) != loop.clean_text(candidate_text):
        subject = "current paragraph" if current_paragraph_mode else "candidate paragraph"
        raise RuntimeError(f"The {subject} does not match the materialized draft.")

    rubric_path = Path(__file__).resolve().parents[1] / "references" / "unified_rubric.json"
    rubric = loop.read_json(rubric_path, {}) or {}
    loop.rubric_dimensions(rubric)
    preflight = loop.deterministic_preflight(
        review_root,
        args.project_id,
        min_words=args.min_case_words,
        max_words=args.max_case_words,
    )
    local_preflight = _local_preflight(preflight, paragraph_id)
    structured = loop.paragraph_metadata(project)
    rows = loop.matrix_rows(project)
    evidence = {
        paragraph_id: loop.source_evidence(
            review_root,
            project,
            paragraph,
            structured.get(paragraph_id, {}),
            rows,
            {},
        )
    }
    raw = loop.call_json_model(
        loop.evaluation_prompt(
            rubric,
            [paragraph],
            evidence,
            local_preflight,
            float(args.goal),
            float(args.paragraph_goal),
            draft_structure=[
                {
                    "paragraph_id": str(item.get("paragraph_id") or ""),
                    "heading": loop.clean_text(item.get("heading")),
                }
                for item in paragraphs
            ],
        ),
        label=(
            f"Current paragraph evaluation {paragraph_id}"
            if current_paragraph_mode
            else f"Accepted paragraph evaluation {paragraph_id}"
        ),
    )
    evaluation = loop.normalize_evaluation(
        raw,
        rubric,
        [paragraph],
        local_preflight,
        float(args.goal),
        float(args.paragraph_goal),
        evidence=evidence,
    )
    paragraph_score = dict((evaluation.get("paragraph_scores") or [])[0])
    source_report = loop.original_source_check_report(project, evaluation, evidence)
    result = {
        "schema_version": 1,
        "evaluation_scope": "single_paragraph",
        "evaluation_mode": evaluation_mode,
        "paragraph_id": paragraph_id,
        "paragraph_score": paragraph_score,
        "local_dimension_scores": evaluation.get("dimension_scores") or [],
        "local_hard_gate_failures": evaluation.get("hard_gate_failures") or [],
        "local_preflight": local_preflight,
        "source_check_entry": next(iter(source_report.get("entries") or []), {}),
        "validation_warnings": validation_warnings,
        "evaluated_at": loop.utc_now(),
    }
    loop.write_json(first / "paragraph_candidate_evaluation.json", result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--paragraph-id", required=True)
    parser.add_argument("--goal", type=float, default=90.0)
    parser.add_argument("--paragraph-goal", type=float, default=85.0)
    parser.add_argument(
        "--min-case-words", type=int, default=loop.CASE_PARAGRAPH_MIN_WORDS
    )
    parser.add_argument(
        "--max-case-words", type=int, default=loop.CASE_PARAGRAPH_MAX_WORDS
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(evaluate(parse_args()), ensure_ascii=False))
