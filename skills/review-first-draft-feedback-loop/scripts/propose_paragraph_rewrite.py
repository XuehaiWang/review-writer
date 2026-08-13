#!/usr/bin/env python3
"""Generate one validated Stage-8 rewrite candidate without changing the draft."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import feedback_loop as loop


def source_entry(first: Path, paragraph_id: str) -> dict:
    report = loop.read_json(first / "original_source_check.json", {}) or {}
    return next(
        (
            item
            for item in report.get("entries") or []
            if isinstance(item, dict) and str(item.get("paragraph_id") or "") == paragraph_id
        ),
        {},
    )


def paragraph_evidence(entry: dict) -> dict:
    papers = []
    for paper in entry.get("papers") or []:
        if not isinstance(paper, dict):
            continue
        passages = paper.get("passages") or []
        papers.append(
            {
                **paper,
                "original_passages": passages,
                "original_text_available": bool(passages),
            }
        )
    return {
        "paper_ids": entry.get("paper_ids") or [],
        "evidence_scope": entry.get("evidence_scope") or "local_source",
        "original_source_ready": bool(papers),
        "evidence": papers,
    }


def propose(args: argparse.Namespace) -> dict:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    status = loop.read_json(first / "feedback_loop_status.json", {}) or {}
    current_hash = loop.sha256_file(draft_path)
    evaluated_hash = str(
        status.get("output_draft_sha256")
        or status.get("source_draft_sha256")
        or ""
    )
    if not current_hash or current_hash != evaluated_hash:
        raise RuntimeError("Evaluate the saved current draft before requesting an AI rewrite.")
    if str(status.get("status") or "") == "running":
        raise RuntimeError("Wait for the current quality run to finish first.")

    markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    paragraph = next(
        (
            item
            for item in loop.parse_marked_paragraphs(markdown)
            if str(item.get("paragraph_id") or "") == args.paragraph_id
        ),
        None,
    )
    if not paragraph:
        raise RuntimeError(f"Paragraph not found: {args.paragraph_id}")

    evaluation = loop.read_json(first / "rubric_evaluation.json", {}) or {}
    finding = next(
        (
            item
            for item in evaluation.get("paragraph_failures") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == args.paragraph_id
        ),
        None,
    )
    if not finding:
        raise RuntimeError("This paragraph is not in the current problem queue.")
    evidence = paragraph_evidence(source_entry(first, args.paragraph_id))
    rewrite_mode = loop.automatic_rewrite_mode(
        finding,
        evidence,
        paragraph_goal=float(status.get("paragraph_goal") or 85),
    )
    if not rewrite_mode:
        raise RuntimeError(
            "This issue requires manual source confirmation and is not eligible for automatic rewriting."
        )

    preflight = loop.read_json(first / "first_draft_preflight.json", {}) or {}
    check = next(
        (
            item
            for item in preflight.get("paragraph_checks") or []
            if isinstance(item, dict)
            and str(item.get("paragraph_id") or "") == args.paragraph_id
        ),
        {},
    )
    word_range_applicable = bool(check.get("word_range_applicable", True))
    minimum = args.min_case_words if word_range_applicable else 1
    allowed_unsupported_claims = [
        str(value)
        for value in finding.get("unsupported_claims") or []
        if str(value).strip()
    ]
    candidate = ""
    validation_errors: list[str] = []
    attempts = []
    for attempt in range(1, loop.MAX_REWRITE_ATTEMPTS + 1):
        prompt = (
            loop.rewrite_prompt(
                paragraph,
                finding,
                evidence,
                minimum,
                args.max_case_words,
                word_range_applicable=word_range_applicable,
                rewrite_mode=rewrite_mode,
            )
            if attempt == 1
            else loop.rewrite_repair_prompt(
                str(paragraph["text"]),
                candidate,
                validation_errors,
                minimum,
                args.max_case_words,
                word_range_applicable=word_range_applicable,
                allowed_unsupported_claims=allowed_unsupported_claims,
            )
        )
        response = loop.call_json_model(
            prompt,
            label=f"Paragraph rewrite candidate {args.paragraph_id}",
        )
        candidate = str(response.get("text") or "").strip()
        validation_errors = loop.validate_rewrite(
            str(paragraph["text"]),
            candidate,
            minimum,
            args.max_case_words,
            allowed_unsupported_claims=allowed_unsupported_claims,
        )
        attempts.append({"attempt": attempt, "errors": validation_errors})
        if not validation_errors:
            break
    if validation_errors:
        raise RuntimeError(
            "The proposed rewrite failed integrity validation: "
            + ", ".join(validation_errors)
        )

    path = first / "feedback_rewrite_candidates.json"
    payload = loop.read_json(path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        entries = {}
    original = str(paragraph["text"])
    entry = {
        "paragraph_id": args.paragraph_id,
        "status": "pending_human_review",
        "source_text_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "draft_sha256": current_hash,
        "original_text": original,
        "candidate_text": candidate,
        "route": str(finding.get("route") or rewrite_mode),
        "diagnosis": str(finding.get("diagnosis") or ""),
        "validation_errors": [],
        "attempts": attempts,
        "created_at": loop.utc_now(),
    }
    entries[args.paragraph_id] = entry
    loop.write_json(
        path,
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "entries": entries,
        },
    )
    return entry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--paragraph-id", required=True)
    parser.add_argument("--min-case-words", type=int, default=140)
    parser.add_argument("--max-case-words", type=int, default=280)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(propose(parse_args()), ensure_ascii=False))
