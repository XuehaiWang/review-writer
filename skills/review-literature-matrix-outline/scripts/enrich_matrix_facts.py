#!/usr/bin/env python3
"""Extract source-addressable, discipline-neutral Matrix fact cards."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "review_writer_core").is_dir() and (parent / "skills").is_dir()
    ),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.model_gateway_client import call_json_model  # noqa: E402


EPISTEMIC_STATUSES = {
    "direct_source_report",
    "source_author_interpretation",
    "abstract_level_report",
}


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Matrix enrichment input is not an object.")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def compact(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalized_contains(content: str, excerpt: str) -> bool:
    source = compact(content, 100_000).casefold()
    target = compact(excerpt, 2_000).casefold()
    return bool(target and target in source)


def prompt_for_paper(topic: str, paper: dict[str, Any]) -> str:
    candidates = [
        {
            "evidence_key": item.get("evidence_key"),
            "question_ids": item.get("question_ids"),
            "content_type": item.get("content_type"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_path": item.get("section_path"),
            "content": compact(item.get("content"), 2400),
        }
        for item in paper.get("evidence_candidates") or []
        if isinstance(item, dict)
    ][:14]
    return f"""Extract reusable scientific fact cards for one paper in a narrative review.

Review topic: {topic}
Paper ID: {paper.get('paper_id')}
Paper title: {paper.get('title')}

Return one JSON object with keys `facts` and `failed_fields`. Each fact must have:
- field_id: exactly one question_id offered by its selected evidence;
- value: a concise factual normalization, not a vague paper summary;
- support_excerpt: an exact contiguous quotation copied from the selected content;
- evidence_key: exactly one supplied evidence_key;
- epistemic_status: direct_source_report, source_author_interpretation, or abstract_level_report;
- confidence: number from 0 to 1;
- evidence_ceiling: a short statement of what must not be inferred.

Use only supplied evidence. Do not combine separate passages into one fact. Do not infer a
mechanism from outcomes, convert absence into a limitation, or turn an abstract into detailed
conditions or numerical claims. If a field is unsupported, list it in failed_fields and omit the
fact. Prefer at most one strong fact per field and at most six facts total.

Evidence candidates:
{json.dumps(candidates, ensure_ascii=False)}
"""


def normalize_result(paper: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    candidates = {
        str(item.get("evidence_key") or ""): item
        for item in paper.get("evidence_candidates") or []
        if isinstance(item, dict) and item.get("evidence_key")
    }
    facts: list[dict[str, Any]] = []
    used_fields: set[str] = set()
    for raw in (generated.get("facts") or [])[:8]:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("evidence_key") or "")
        source = candidates.get(key)
        if source is None:
            continue
        field_id = compact(raw.get("field_id"), 80).casefold()
        allowed_fields = {str(item) for item in source.get("question_ids") or []}
        if field_id not in allowed_fields or field_id in used_fields:
            continue
        excerpt = compact(raw.get("support_excerpt"), 1600)
        if not normalized_contains(str(source.get("content") or ""), excerpt):
            continue
        value = compact(raw.get("value"), 1800)
        if not value:
            continue
        epistemic = compact(raw.get("epistemic_status"), 80).casefold()
        if source.get("content_type") == "abstract":
            epistemic = "abstract_level_report"
        elif epistemic not in EPISTEMIC_STATUSES:
            epistemic = "direct_source_report"
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        fact_id = "MF-" + hashlib.sha256(
            f"{paper.get('paper_id')}\0{field_id}\0{key}\0{value}".encode("utf-8")
        ).hexdigest()[:16].upper()
        content_type = str(source.get("content_type") or "body").casefold()
        source_channel = (
            "abstract"
            if content_type == "abstract"
            else "table"
            if content_type == "table"
            else "figure_caption"
            if content_type in {"image", "caption", "figure"}
            else "body"
        )
        support_level = (
            "abstract_limited" if source_channel == "abstract" else "direct"
        )
        facts.append(
            {
                "fact_id": fact_id,
                "field_id": field_id,
                "value": value,
                "support_excerpt": excerpt,
                "epistemic_status": epistemic,
                "confidence": round(confidence, 4),
                "human_checked": False,
                "review_status": "needs_review",
                "source_channel": source_channel,
                "support_level": support_level,
                "evidence_ceiling": compact(
                    raw.get("evidence_ceiling")
                    or "Do not generalize beyond the cited source passage.",
                    600,
                ),
                "evidence_refs": [
                    {
                        "evidence_key": key,
                        "chunk_id": source.get("chunk_id"),
                        "page_start": source.get("page_start"),
                        "page_end": source.get("page_end"),
                        "section_path": source.get("section_path") or [],
                        "source_lineage_hash": source.get("source_lineage_hash"),
                    }
                ],
                "extraction_method": "model_normalized_from_bounded_source",
            }
        )
        used_fields.add(field_id)
    failed_fields = list(
        dict.fromkeys(
            compact(item, 80).casefold()
            for item in generated.get("failed_fields") or []
            if compact(item, 80)
        )
    )
    abstract_only = bool(facts) and all(
        fact["epistemic_status"] == "abstract_level_report" for fact in facts
    )
    status = (
        "complete"
        if len(facts) >= 3 and not abstract_only
        else "limited"
        if abstract_only
        else "partial"
        if facts
        else "failed"
    )
    review_status = (
        "needs_review"
        if failed_fields or any(float(fact.get("confidence") or 0) < 0.75 for fact in facts)
        else "not_required"
    )
    return {
        "paper_id": str(paper.get("paper_id") or ""),
        "status": status,
        "facts": facts,
        "failed_fields": failed_fields,
        "review_status": review_status,
        "error": "" if facts else "No source-validated fact survived normalization.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--progress", required=True)
    parser.add_argument("--checkpoint", required=True)
    args = parser.parse_args()
    source = read_json(Path(args.input))
    output_path = Path(args.output)
    progress_path = Path(args.progress)
    checkpoint_path = Path(args.checkpoint)
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {}
    entries = dict(checkpoint.get("entries") or {}) if isinstance(checkpoint, dict) else {}
    papers = [item for item in source.get("papers") or [] if isinstance(item, dict)]
    results: list[dict[str, Any]] = []
    attempted = 0
    succeeded_attempts = 0
    for index, paper in enumerate(papers, start=1):
        paper_id = str(paper.get("paper_id") or "")
        previous = entries.get(paper_id)
        if (
            isinstance(previous, dict)
            and previous.get("source_fingerprint") == paper.get("source_fingerprint")
            and isinstance(previous.get("result"), dict)
        ):
            result = dict(previous["result"])
        elif not paper.get("evidence_candidates"):
            result = {
                "paper_id": paper_id,
                "status": "failed",
                "facts": [],
                "failed_fields": ["all"],
                "error": "No full-text or abstract evidence candidate is available.",
            }
        else:
            attempted += 1
            try:
                generated = call_json_model(
                    prompt_for_paper(str(source.get("review_topic") or ""), paper),
                    label=f"matrix-facts-{paper_id}"[:80],
                    timeout_seconds=330,
                    required_list="facts",
                )
                result = normalize_result(paper, generated)
                succeeded_attempts += 1
            except Exception as exc:
                result = {
                    "paper_id": paper_id,
                    "status": "failed",
                    "facts": [],
                    "failed_fields": ["all"],
                    "error": compact(exc, 1000),
                }
        results.append(result)
        entries[paper_id] = {
            "source_fingerprint": paper.get("source_fingerprint"),
            "result": result,
        }
        write_json(
            checkpoint_path,
            {
                "schema_version": 1,
                "source_matrix_artifact_id": source.get("source_matrix_artifact_id"),
                "entries": entries,
            },
        )
        write_json(
            progress_path,
            {
                "phase": "extracting" if index < len(papers) else "finalizing",
                "current": index,
                "total": len(papers),
                "current_paper_id": paper_id,
                "completed_papers": [item.get("paper_id") for item in results],
                "failed_papers": [
                    item.get("paper_id") for item in results if item.get("status") == "failed"
                ],
                "updated_at_epoch": time.time(),
            },
        )
    output = {
        "schema_version": 1,
        "project_id": source.get("project_id"),
        "source_matrix_artifact_id": source.get("source_matrix_artifact_id"),
        "papers": results,
    }
    write_json(output_path, output)
    # An all-paper provider or extraction failure is still a valid terminal
    # result for this batch.  Publishing the per-paper failures lets the host
    # offer an explicit retry or user-chosen limited mode instead of leaving
    # Matrix preparation permanently in a generic failed-job state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
