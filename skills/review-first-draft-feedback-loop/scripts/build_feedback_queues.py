#!/usr/bin/env python3
"""Build portable rewrite/final-polish queues and a first-draft gate status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = args.review_root.resolve() / "review-projects" / args.project_id
    first = project / "04_first_draft"
    evaluation_path = first / "rubric_evaluation.json"
    if not evaluation_path.is_file():
        evaluation_path = first / "evaluator_report.json"
    findings_path = first / "reviewer_findings.json"
    preflight_path = first / "first_draft_preflight.json"
    for path in [evaluation_path, preflight_path]:
        if not path.is_file():
            raise FileNotFoundError(path)

    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
    if findings_path.is_file():
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
    else:
        legacy_gate_path = first / "first_draft_multireviewer_gate" / "gate_decision.json"
        legacy_gate = (
            json.loads(legacy_gate_path.read_text(encoding="utf-8"))
            if legacy_gate_path.is_file()
            else {}
        )
        findings = (
            legacy_gate.get("blocking_findings", [])
            + legacy_gate.get("final_polish_findings", [])
        )
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    rewrite, final_polish = [], []
    seen: set[tuple[str, str, str]] = set()

    def append_unique(target: list[dict], value: dict) -> None:
        dimensions = ",".join(sorted(str(item) for item in value.get("failed_dimensions", [])))
        key = (str(value.get("paragraph_id") or ""), dimensions or str(value.get("rule") or ""), str(value.get("route") or ""))
        if key not in seen:
            seen.add(key)
            target.append(value)

    for item in evaluation.get("paragraph_failures", []):
        route = item.get("route", "section_rewrite")
        failed_dimensions = set(item.get("failed_dimensions", []))
        if failed_dimensions == {"G07"} and item.get("severity") not in {
            "critical",
            "major_protected_fact",
        }:
            route = "final_polish"
        append_unique(
            final_polish if route == "final_polish" else rewrite,
            {"origin": "rubric", **item, "route": route},
        )
    for item in findings:
        route = item.get("route", "section_rewrite")
        if item.get("severity") in {"critical", "major"} and route != "final_polish":
            append_unique(rewrite, {"origin": "independent_review", **item})
        else:
            append_unique(final_polish, {"origin": "independent_review", **item})
    for item in preflight.get("paragraph_findings", []):
        append_unique(rewrite, {"origin": "deterministic_preflight", **item})

    score = float(evaluation.get("total_score", 0))
    hard = sorted(set(evaluation.get("hard_gate_failures", []) + preflight.get("hard_regressions", [])))
    threshold = float(
        evaluation.get("pass_threshold", evaluation.get("pass_threshold_user", 90))
    )
    released = score >= threshold and not hard and not rewrite
    decision = "GATE_RELEASE" if released else "GATE_HOLD_REWRITE_REQUIRED"
    status = "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH" if released else "REWRITE_REQUIRED"

    rewrite_payload = {
        "project_id": args.project_id,
        "items": rewrite,
        "target": "02_section_drafting/section_drafts.json and matching sections/*.md; then re-merge and repeat the complete gate",
        "hash_manifest_created": False,
    }
    polish_payload = {
        "project_id": args.project_id,
        "items": final_polish,
        "policy": "Use only after first-draft release. Preserve protected facts, citations, and figure identity.",
        "hash_manifest_created": False,
    }
    gate = {
        "project_id": args.project_id,
        "status": status,
        "gate_decision": decision,
        "unified_rubric_score": score,
        "hard_gate_failures": hard,
        "rewrite_queue_path": "04_first_draft/first_draft_rewrite_queue.json",
        "final_polish_queue_path": "04_first_draft/first_draft_final_polish_queue.json",
        "next_action": "Generate the grounded conclusion and run selective final polish." if released else "Rewrite queued paragraphs in the section-writing layer, merge, and repeat the complete gate.",
        "hash_manifest_created": False,
    }
    (first / "first_draft_rewrite_queue.json").write_text(json.dumps(rewrite_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (first / "first_draft_final_polish_queue.json").write_text(json.dumps(polish_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (first / "first_draft_gate_status.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"decision": decision, "score": score, "rewrite_items": len(rewrite), "final_polish_items": len(final_polish)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
