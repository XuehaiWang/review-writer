#!/usr/bin/env python3
"""Normalize legacy project-local gate reports into the portable contract."""

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
    first = (
        args.review_root.resolve()
        / "review-projects"
        / args.project_id
        / "04_first_draft"
    )
    evaluator_path = first / "evaluator_report.json"
    gate_path = first / "first_draft_multireviewer_gate" / "gate_decision.json"
    if not evaluator_path.is_file():
        raise FileNotFoundError(evaluator_path)
    evaluator = json.loads(evaluator_path.read_text(encoding="utf-8"))
    evaluation = dict(evaluator)
    evaluation["rubric_model"] = evaluation.get(
        "rubric_model", "readability_first_unified_review_rubric"
    )
    evaluation["pass_threshold"] = evaluation.get(
        "pass_threshold", evaluation.get("pass_threshold_user", 90)
    )
    evaluation["hash_manifest_created"] = False
    (first / "rubric_evaluation.json").write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    gate = (
        json.loads(gate_path.read_text(encoding="utf-8"))
        if gate_path.is_file()
        else {}
    )
    raw_findings = (
        gate.get("blocking_findings", [])
        + gate.get("final_polish_findings", [])
    )
    findings = []
    for index, item in enumerate(raw_findings, 1):
        normalized = dict(item)
        normalized.setdefault("id", f"LEGACY-{index:03d}")
        normalized.setdefault("reviewer", "legacy_multireviewer")
        normalized.setdefault("paragraph_id", (item.get("paragraph_ids") or [None])[0])
        normalized.setdefault("fragment", item.get("original", ""))
        normalized.setdefault("recommended_direction", "")
        normalized.setdefault("confidence", "medium")
        normalized["route"] = (
            "final_polish"
            if item.get("route") in {
                "safe selective polish",
                "final evidence-aware polish",
            }
            else item.get("route", "section_rewrite")
        )
        findings.append(normalized)
    (first / "reviewer_findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "rubric_dimensions": len(evaluation.get("dimension_scores", [])),
        "reviewer_findings": len(findings),
        "hash_manifest_created": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
