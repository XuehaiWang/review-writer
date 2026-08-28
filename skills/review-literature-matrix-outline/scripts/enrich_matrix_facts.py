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
from review_writer_core.classification_axes import (  # noqa: E402
    axis_requires_formal_route,
    normalize_classification_axes_semantics,
)


EPISTEMIC_STATUSES = {
    "direct_source_report",
    "source_author_interpretation",
    "abstract_level_report",
}
PARTITION_CONFIDENCE_THRESHOLD = 0.75
FACT_CONFIDENCE_THRESHOLD = 0.75
FACT_CONTEXT_THRESHOLD = 0.60
CLASSIFICATION_OUTCOMES = {
    "insufficient_evidence",
    "cross_category",
    "out_of_scope",
}
CLASSIFICATION_RELATIONS = {
    "primary_contribution",
    "secondary_contribution",
    "comparison_context",
    "background_mention",
    "uncertain",
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


def prompt_for_paper(
    topic: str,
    paper: dict[str, Any],
    topic_partitions: list[str] | None = None,
    classification_axes: list[dict[str, Any]] | None = None,
) -> str:
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
    partition_candidates = [
        {
            "evidence_key": item.get("evidence_key"),
            "content_type": item.get("content_type"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_path": item.get("section_path"),
            "content": compact(item.get("content"), 2400),
        }
        for item in paper.get("partition_evidence_candidates") or []
        if isinstance(item, dict)
    ][:10]
    partitions = [
        compact(value, 100)
        for value in topic_partitions or []
        if compact(value, 100)
    ]
    axes = [
        {
            "axis_id": compact(axis.get("axis_id"), 80),
            "label": compact(axis.get("label"), 120),
            "axis_role": compact(axis.get("axis_role"), 80),
            "mutual_exclusivity": compact(axis.get("mutual_exclusivity"), 80),
            "partitions": [
                {
                    "partition_id": compact(partition.get("partition_id"), 80),
                    "label": compact(partition.get("label"), 120),
                    "aliases": [compact(value, 120) for value in partition.get("aliases") or []],
                    "positive_discriminators": [
                        compact(value, 160)
                        for value in partition.get("positive_discriminators") or []
                    ],
                    "negative_or_ambiguous_signals": [
                        compact(value, 160)
                        for value in partition.get("negative_or_ambiguous_signals") or []
                    ],
                }
                for partition in axis.get("partitions") or []
                if isinstance(partition, dict)
            ],
        }
        for axis in classification_axes or []
        if isinstance(axis, dict) and axis.get("axis_id")
    ]
    taxonomy_profile = compact(paper.get("taxonomy_profile"), 80).casefold()
    profile_guidance = ""
    if taxonomy_profile == "allene" or taxonomy_profile.startswith("chemistry"):
        profile_guidance = """
For chemistry papers, `intervention_role` may normalize only roles explicitly supported by
the passage and reported loading/equivalents. Distinguish catalyst, co-catalyst, promoter,
stoichiometric reagent, chiral auxiliary/reagent, and an explicitly described amine role.
Never label a substance a catalyst merely because its name appears. Include reported loading
or equivalents in the value when present. For `safety_cost_sustainability`, extract only an
explicit source statement or reported quantity; do not infer cost, greenness, or hazard from
a chemical name alone.
"""
    partition_guidance = (
        f"""
The Topic explicitly requests these independently discussed partitions:
{json.dumps(partitions, ensure_ascii=False)}

Also return `topic_partition_classification` with:
- partition: exactly one supplied partition label, or null;
- confidence: number from 0 to 1;
- evidence_key: one supplied partition-evidence key, or null;
- support_excerpt: an exact contiguous quotation from that evidence, or an empty string;
- rationale: a concise explanation based only on the quoted passage;
- boundary_reason: why no partition can be supported when partition is null;
- evidence_ceiling: what the passage does not establish.

Classify only when the supplied passage positively establishes the requested distinction.
Never assign one side because evidence for another side is absent. Never treat missing ee,
missing demographic labels, missing study design, or any other omitted property as proof of
the contrasting partition. When evidence is ambiguous, return partition null.
"""
        if partitions
        else "\nReturn `topic_partition_classification` as null because no independent Topic partitions were declared.\n"
    )
    axis_guidance = (
        f"""
The project uses these classification axes:
{json.dumps(axes, ensure_ascii=False)}

Also return `topic_classification_assignments` as a list. Each assignment must contain:
- axis_id and partition_id copied exactly from the supplied axes;
- relation_to_paper: primary_contribution, secondary_contribution,
  comparison_context, background_mention, or uncertain;
- confidence, evidence_key, support_excerpt, rationale, and evidence_ceiling.

Only primary_contribution and secondary_contribution may become formal Tags. The quoted
passage must positively state what this paper reports. Do not classify from a related-work
mention or from the absence of a contrasting property.

For every axis with no supported assignment, add one item to `classification_outcomes`:
- status must be insufficient_evidence, cross_category, or out_of_scope;
- use cross_category only when a supplied passage positively establishes a genuinely
  cross-category contribution, never as a synonym for uncertainty;
- use out_of_scope only when supplied evidence positively shows the paper is outside the
  stated review scope;
- include axis_id, reason, and any evidence_key/support_excerpt when available.
"""
        if axes
        else "\nReturn empty `topic_classification_assignments` and `classification_outcomes` lists.\n"
    )
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
fact. Prefer at most one strong fact per field and at most nine facts total.
{profile_guidance}
{partition_guidance}
{axis_guidance}

Evidence candidates:
{json.dumps(candidates, ensure_ascii=False)}

Partition evidence candidates:
{json.dumps(partition_candidates, ensure_ascii=False)}
"""


def targeted_classification_prompt(
    topic: str,
    paper: dict[str, Any],
    unresolved_axes: list[dict[str, Any]],
    topic_partitions: list[str],
) -> str:
    """Ask once more about routing using only focused source passages."""

    candidates = [
        {
            "evidence_key": item.get("evidence_key"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_path": item.get("section_path"),
            "content_type": item.get("content_type"),
            "matched_partitions": item.get("matched_partitions") or [],
            "content": compact(item.get("content"), 2800),
        }
        for item in [
            *(paper.get("partition_evidence_candidates") or []),
            *(paper.get("evidence_candidates") or []),
        ]
        if isinstance(item, dict) and item.get("evidence_key")
    ][:18]
    axes = [
        {
            "axis_id": axis.get("axis_id"),
            "label": axis.get("label"),
            "axis_role": axis.get("axis_role"),
            "partitions": axis.get("partitions") or [],
        }
        for axis in unresolved_axes
    ]
    return f"""Perform one targeted evidence recheck for unresolved academic routing.

Review topic: {topic}
Paper ID: {paper.get('paper_id')}
Paper title: {paper.get('title')}
Unresolved axes: {json.dumps(axes, ensure_ascii=False)}
Topic partitions: {json.dumps(topic_partitions, ensure_ascii=False)}

Return JSON with `facts` as an empty list, `failed_fields` as an empty list,
`topic_classification_assignments`, `classification_outcomes`, and
`topic_partition_classification`. Use the same assignment fields as the first
pass: axis_id, partition_id, relation_to_paper, confidence, evidence_key,
support_excerpt, rationale, and evidence_ceiling.

Use only the passages below. A support excerpt must be an exact contiguous
quotation. Classify only a primary or secondary contribution positively stated
by a passage. Never infer a racemic result, control group, method, population,
or any contrasting category from an omitted property. If no partition is
positively established after this focused search, return insufficient_evidence;
the system will route the paper automatically without asking the user.

Candidate passages:
{json.dumps(candidates, ensure_ascii=False)}
"""


def targeted_routing_prompt(
    topic: str,
    paper: dict[str, Any],
    routing_axis_id: str,
    routing_categories: list[dict[str, Any]],
    current_result: dict[str, Any],
) -> str:
    """Adjudicate one still-unrouted primary study from bounded evidence."""

    candidates = [
        {
            "evidence_key": item.get("evidence_key"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_path": item.get("section_path"),
            "content_type": item.get("content_type"),
            "content": compact(item.get("content"), 3000),
        }
        for item in [
            *(paper.get("partition_evidence_candidates") or []),
            *(paper.get("evidence_candidates") or []),
        ]
        if isinstance(item, dict) and item.get("evidence_key")
    ]
    deduplicated: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in candidates:
        key = str(item.get("evidence_key") or "")
        if key and key not in seen_keys:
            seen_keys.add(key)
            deduplicated.append(item)
    facts = [
        {
            "field_id": fact.get("field_id"),
            "value": compact(fact.get("value"), 1000),
            "support_excerpt": compact(fact.get("support_excerpt"), 1200),
            "evidence_key": str(
                ((fact.get("evidence_refs") or [{}])[0]).get("evidence_key") or ""
            ),
        }
        for fact in current_result.get("facts") or []
        if isinstance(fact, dict) and fact.get("evidence_refs")
    ][:10]
    return f"""Perform one final, evidence-bounded routing decision for a selected primary study.

Review topic: {topic}
Paper ID: {paper.get('paper_id')}
Paper title: {paper.get('title')}
Abstract: {compact(paper.get('abstract'), 2400)}
Primary routing axis: {routing_axis_id}
Allowed publication categories: {json.dumps(routing_categories, ensure_ascii=False)}
Already validated scientific facts: {json.dumps(facts, ensure_ascii=False)}

Return JSON with an empty `facts` list and one `routing_recommendation` object:
- status: classified or insufficient_evidence;
- label: exactly one allowed category label when classified, otherwise an empty string;
- confidence: 0 to 1;
- evidence_key: exactly one supplied evidence key;
- support_excerpt: an exact contiguous quotation from that evidence;
- rationale: why the quoted study design belongs to the selected category;
- evidence_ceiling: what the passage does not establish.

Classify the paper's reported transformation, not a related-work mention. A title, abstract,
or source passage may support routing when it positively states the substrates, operation,
and reported product. Do not require a detailed mechanism when an allowed category is defined
by reaction inputs or operation rather than mechanism. If no allowed category is positively
supported, return insufficient_evidence. Do not relabel an unresolved primary study as a
review, perspective, or Introduction-only source.

Candidate passages:
{json.dumps(deduplicated[:20], ensure_ascii=False)}
"""


def normalize_routing_recommendation(
    paper: dict[str, Any],
    generated: dict[str, Any],
    routing_axis_id: str,
    routing_categories: list[dict[str, Any]],
) -> dict[str, Any]:
    allowed = {
        compact(item.get("label"), 160).casefold(): compact(item.get("label"), 160)
        for item in routing_categories
        if isinstance(item, dict) and compact(item.get("label"), 160)
    }
    raw = generated.get("routing_recommendation")
    if not isinstance(raw, dict):
        raw = {}
    requested_label = compact(raw.get("label"), 160)
    label = allowed.get(requested_label.casefold(), "")
    candidates = {
        str(item.get("evidence_key") or ""): item
        for item in [
            *(paper.get("partition_evidence_candidates") or []),
            *(paper.get("evidence_candidates") or []),
        ]
        if isinstance(item, dict) and item.get("evidence_key")
    }
    key = str(raw.get("evidence_key") or "")
    source = candidates.get(key)
    excerpt = compact(raw.get("support_excerpt"), 1600)
    excerpt_valid = bool(
        source is not None
        and excerpt
        and normalized_contains(str(source.get("content") or ""), excerpt)
    )
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    classified = bool(
        str(raw.get("status") or "").casefold() == "classified"
        and label
        and excerpt_valid
        and confidence >= PARTITION_CONFIDENCE_THRESHOLD
    )
    evidence_refs = (
        [
            {
                "evidence_key": key,
                "chunk_id": source.get("chunk_id"),
                "page_start": source.get("page_start"),
                "page_end": source.get("page_end"),
                "section_path": source.get("section_path") or [],
                "source_lineage_hash": source.get("source_lineage_hash"),
            }
        ]
        if classified and source is not None
        else []
    )
    return {
        "schema_version": 1,
        "axis_id": compact(routing_axis_id, 80),
        "status": "classified" if classified else "insufficient_evidence",
        "label": label if classified else "",
        "candidate_label": label if label and not classified else "",
        "confidence": round(confidence, 4),
        "rationale": compact(raw.get("rationale"), 800),
        "reason": (
            ""
            if classified
            else compact(
                raw.get("reason")
                or "The bounded routing adjudicator found no supported publication category.",
                800,
            )
        ),
        "support_excerpt": excerpt if classified else "",
        "evidence_ceiling": compact(
            raw.get("evidence_ceiling")
            or "Do not extend this routing decision beyond the cited study design.",
            600,
        ),
        "evidence_refs": evidence_refs,
        "review_status": "not_required" if classified else "auto_unresolved",
        "extraction_method": "model_routed_from_bounded_source",
    }


def _canonical_partition(value: Any, partitions: list[str]) -> str:
    normalized = compact(value, 100).casefold()
    if not normalized:
        return ""
    for label in partitions:
        aliases = [
            label,
            re.sub(r"\s*[（(][^()（）]{2,40}[）)]\s*", " ", label).strip(),
            *re.findall(r"[（(]([^()（）]{2,40})[）)]", label),
        ]
        if any(
            normalized == compact(alias, 100).casefold()
            for alias in aliases
            if compact(alias, 100)
        ):
            return label
    return ""


def normalize_partition_classification(
    paper: dict[str, Any],
    generated: dict[str, Any],
    topic_partitions: list[str],
) -> dict[str, Any]:
    partitions = [
        compact(value, 100) for value in topic_partitions if compact(value, 100)
    ]
    if not partitions:
        return {
            "schema_version": 1,
            "status": "not_requested",
            "partition": "",
            "confidence": 0.0,
            "evidence_refs": [],
        }
    raw = generated.get("topic_partition_classification")
    if not isinstance(raw, dict):
        raw = {}
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    partition = _canonical_partition(raw.get("partition"), partitions)
    candidates = {
        str(item.get("evidence_key") or ""): item
        for item in [
            *(paper.get("partition_evidence_candidates") or []),
            *(paper.get("evidence_candidates") or []),
        ]
        if isinstance(item, dict) and item.get("evidence_key")
    }
    key = str(raw.get("evidence_key") or "")
    source = candidates.get(key)
    excerpt = compact(raw.get("support_excerpt"), 1600)
    excerpt_valid = bool(
        source is not None
        and excerpt
        and normalized_contains(str(source.get("content") or ""), excerpt)
    )
    evidence_valid = bool(partition and excerpt_valid)
    evidence_refs = (
        [
            {
                "evidence_key": key,
                "chunk_id": source.get("chunk_id"),
                "page_start": source.get("page_start"),
                "page_end": source.get("page_end"),
                "section_path": source.get("section_path") or [],
                "source_lineage_hash": source.get("source_lineage_hash"),
            }
        ]
        if source is not None and excerpt_valid
        else []
    )
    classified = evidence_valid and confidence >= PARTITION_CONFIDENCE_THRESHOLD
    boundary_reason = compact(raw.get("boundary_reason"), 800)
    if not classified and not boundary_reason:
        boundary_reason = (
            "The supplied source passages do not positively establish one declared Topic partition."
            if not partition or not evidence_valid
            else f"The evidence-bound classification confidence is below {PARTITION_CONFIDENCE_THRESHOLD:.2f}."
        )
    return {
        "schema_version": 1,
        "status": "classified" if classified else "insufficient_evidence",
        "partition": partition if classified else "",
        "candidate_partition": partition if partition and not classified else "",
        "confidence": round(confidence, 4),
        "rationale": compact(raw.get("rationale"), 800),
        "boundary_reason": boundary_reason,
        "support_excerpt": excerpt if evidence_refs else "",
        "evidence_ceiling": compact(
            raw.get("evidence_ceiling")
            or "Do not infer a contrasting partition from information absent in the cited passage.",
            600,
        ),
        "evidence_refs": evidence_refs,
        "review_status": "not_required" if classified else "needs_review",
        "extraction_method": "model_classified_from_bounded_source",
    }


def program_assertion_ceiling(source: dict[str, Any], epistemic_status: str) -> str:
    content_type = compact(source.get("content_type"), 80).casefold()
    if content_type == "abstract" or epistemic_status == "abstract_level_report":
        return "abstract_report_only"
    if epistemic_status == "source_author_interpretation":
        return "attributed_author_interpretation"
    if content_type in {"table", "figure", "caption", "image"}:
        return "direct_report_with_local_context"
    return "direct_source_report"


def numerical_tokens_supported(value: str, excerpt: str) -> bool:
    """Prevent normalized facts from introducing numbers absent from their quote."""

    numbers = re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:\s*%)?", value)
    if not numbers:
        return True
    normalized_excerpt = re.sub(r"\s+", "", excerpt).casefold()
    return all(re.sub(r"\s+", "", number).casefold() in normalized_excerpt for number in numbers)


def normalize_axis_classification(
    paper: dict[str, Any],
    generated: dict[str, Any],
    classification_axes: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    axes = {
        compact(axis.get("axis_id"), 80): axis
        for axis in classification_axes
        if isinstance(axis, dict) and compact(axis.get("axis_id"), 80)
    }
    partitions = {
        axis_id: {
            compact(partition.get("partition_id"), 80): partition
            for partition in axis.get("partitions") or []
            if isinstance(partition, dict) and compact(partition.get("partition_id"), 80)
        }
        for axis_id, axis in axes.items()
    }
    candidates = {
        str(item.get("evidence_key") or ""): item
        for item in [
            *(paper.get("partition_evidence_candidates") or []),
            *(paper.get("evidence_candidates") or []),
        ]
        if isinstance(item, dict) and item.get("evidence_key")
    }
    tags: dict[str, list[dict[str, Any]]] = {}
    classification_facts: list[dict[str, Any]] = []
    accepted_axes: set[str] = set()
    for raw in generated.get("topic_classification_assignments") or []:
        if not isinstance(raw, dict):
            continue
        axis_id = compact(raw.get("axis_id"), 80)
        partition_id = compact(raw.get("partition_id"), 80)
        axis = axes.get(axis_id)
        partition = (partitions.get(axis_id) or {}).get(partition_id)
        relation = compact(raw.get("relation_to_paper"), 80).casefold()
        if axis is None or partition is None or relation not in CLASSIFICATION_RELATIONS:
            continue
        key = str(raw.get("evidence_key") or "")
        source = candidates.get(key)
        excerpt = compact(raw.get("support_excerpt"), 1600)
        if source is None or not normalized_contains(str(source.get("content") or ""), excerpt):
            continue
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < FACT_CONTEXT_THRESHOLD:
            continue
        if (
            relation not in {"primary_contribution", "secondary_contribution"}
            or confidence < PARTITION_CONFIDENCE_THRESHOLD
        ):
            continue
        fact_id = "MF-" + hashlib.sha256(
            f"{paper.get('paper_id')}\0topic_partition\0{axis_id}\0{partition_id}\0{key}".encode(
                "utf-8"
            )
        ).hexdigest()[:16].upper()
        evidence_ref = {
            "evidence_key": key,
            "chunk_id": source.get("chunk_id"),
            "page_start": source.get("page_start"),
            "page_end": source.get("page_end"),
            "section_path": source.get("section_path") or [],
            "source_lineage_hash": source.get("source_lineage_hash"),
        }
        assertion_ceiling = program_assertion_ceiling(source, "direct_source_report")
        classification_fact = {
            "fact_id": fact_id,
            "field_id": "topic_partition",
            "value": f"{compact(axis.get('label'), 120)}: {compact(partition.get('label'), 120)}",
            "support_excerpt": excerpt,
            "epistemic_status": "direct_source_report",
            "confidence": round(confidence, 4),
            "human_checked": False,
            "review_status": "not_required",
            "source_channel": (
                "abstract"
                if str(source.get("content_type") or "").casefold() == "abstract"
                else "body"
            ),
            "support_level": (
                "abstract_limited"
                if str(source.get("content_type") or "").casefold() == "abstract"
                else "direct"
            ),
            "assertion_ceiling": assertion_ceiling,
            "evidence_ceiling": compact(
                raw.get("evidence_ceiling")
                or "Do not extend this classification beyond the cited contribution passage.",
                600,
            ),
            "evidence_refs": [evidence_ref],
            "classification_axis_id": axis_id,
            "classification_partition_id": partition_id,
            "extraction_method": "model_classified_from_bounded_source",
        }
        classification_facts.append(classification_fact)
        tags.setdefault(axis_id, []).append(
            {
                "axis_label": compact(axis.get("label"), 120),
                "axis_role": compact(axis.get("axis_role"), 80),
                "partition_id": partition_id,
                "partition_label": compact(partition.get("label"), 120),
                "relation_to_paper": relation,
                "fact_ids": [fact_id],
                "evidence_refs": [evidence_ref],
                "confidence": round(confidence, 4),
                "assertion_ceiling": assertion_ceiling,
            }
        )
        accepted_axes.add(axis_id)

    outcomes: list[dict[str, Any]] = []
    for raw in generated.get("classification_outcomes") or []:
        if not isinstance(raw, dict):
            continue
        axis_id = compact(raw.get("axis_id"), 80)
        if axis_id not in axes or axis_id in accepted_axes:
            continue
        status = compact(raw.get("status"), 80).casefold()
        if status not in CLASSIFICATION_OUTCOMES:
            status = "insufficient_evidence"
        key = str(raw.get("evidence_key") or "")
        source = candidates.get(key)
        excerpt = compact(raw.get("support_excerpt"), 1600)
        evidence_refs = []
        if source is not None and normalized_contains(str(source.get("content") or ""), excerpt):
            evidence_refs.append(
                {
                    "evidence_key": key,
                    "chunk_id": source.get("chunk_id"),
                    "page_start": source.get("page_start"),
                    "page_end": source.get("page_end"),
                    "section_path": source.get("section_path") or [],
                    "source_lineage_hash": source.get("source_lineage_hash"),
                }
            )
        # cross_category and out_of_scope are positive claims. Without a valid
        # quote they degrade to unresolved evidence instead of becoming gates.
        if status in {"cross_category", "out_of_scope"} and not evidence_refs:
            status = "insufficient_evidence"
        outcomes.append(
            {
                "axis_id": axis_id,
                "axis_role": compact(axes[axis_id].get("axis_role"), 80),
                "status": status,
                "reason": compact(raw.get("reason"), 800)
                or "The supplied passages do not support a formal partition assignment.",
                "support_excerpt": excerpt if evidence_refs else "",
                "evidence_refs": evidence_refs,
                "resolution": "auto_route_from_positive_evidence_only",
                "user_action_required": False,
            }
        )
    for axis_id in axes:
        if axis_id in accepted_axes or any(item["axis_id"] == axis_id for item in outcomes):
            continue
        outcomes.append(
            {
                "axis_id": axis_id,
                "axis_role": compact(axes[axis_id].get("axis_role"), 80),
                "status": "insufficient_evidence",
                "reason": "No source-validated classification assignment was returned for this axis.",
                "support_excerpt": "",
                "evidence_refs": [],
                "resolution": "auto_route_from_positive_evidence_only",
                "user_action_required": False,
            }
        )
    return tags, classification_facts, outcomes


def normalize_result(
    paper: dict[str, Any],
    generated: dict[str, Any],
    topic_partitions: list[str] | None = None,
    classification_axes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidates = {
        str(item.get("evidence_key") or ""): item
        for item in paper.get("evidence_candidates") or []
        if isinstance(item, dict) and item.get("evidence_key")
    }
    facts: list[dict[str, Any]] = []
    used_fields: set[str] = set()
    for raw in (generated.get("facts") or [])[:10]:
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
        if not numerical_tokens_supported(value, excerpt):
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
            "abstract_limited"
            if source_channel == "abstract"
            else "direct"
            if confidence >= FACT_CONFIDENCE_THRESHOLD
            else "context_only"
        )
        assertion_ceiling = program_assertion_ceiling(source, epistemic)
        if support_level == "context_only":
            assertion_ceiling = "context_only_until_higher_confidence_evidence"
        facts.append(
            {
                "fact_id": fact_id,
                "field_id": field_id,
                "value": value,
                "support_excerpt": excerpt,
                "epistemic_status": epistemic,
                "confidence": round(confidence, 4),
                "human_checked": False,
                "review_status": (
                    "not_required"
                    if confidence >= FACT_CONFIDENCE_THRESHOLD
                    else "auto_limited"
                ),
                "source_channel": source_channel,
                "support_level": support_level,
                "assertion_ceiling": assertion_ceiling,
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
    evidence_backed_tags, classification_facts, classification_outcomes = (
        normalize_axis_classification(
            paper,
            generated,
            list(classification_axes or []),
        )
    )
    facts.extend(classification_facts)
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
    unresolved_required_axes = [
        str(outcome.get("axis_id") or "")
        for outcome in classification_outcomes
        if axis_requires_formal_route(
            next(
                (
                    axis
                    for axis in classification_axes or []
                    if str(axis.get("axis_id") or "")
                    == str(outcome.get("axis_id") or "")
                ),
                {},
            )
        )
    ]
    auto_handled = bool(
        failed_fields
        or classification_outcomes
        or unresolved_required_axes
        or any(fact.get("support_level") == "context_only" for fact in facts)
    )
    review_status = (
        "needs_review"
        if status == "failed"
        else "auto_resolved"
        if auto_handled
        else "not_required"
    )
    return {
        "paper_id": str(paper.get("paper_id") or ""),
        "status": status,
        "facts": facts,
        "failed_fields": failed_fields,
        "review_status": review_status,
        "topic_partition_classification": normalize_partition_classification(
            paper,
            generated,
            list(topic_partitions or []),
        ),
        "evidence_backed_tags": evidence_backed_tags,
        "classification_outcomes": classification_outcomes,
        "automatic_resolution": {
            "status": "resolved" if auto_handled else "not_needed",
            "targeted_recheck_attempted": False,
            "unresolved_required_axes": unresolved_required_axes,
            "safe_route_policy": "positive_evidence_only_with_automatic_boundary_routing",
            "user_action_required": status == "failed",
        },
        "error": "" if facts else "No source-validated fact survived normalization.",
    }


def unresolved_axes_for_targeted_recheck(
    result: dict[str, Any],
    classification_axes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accepted = set((result.get("evidence_backed_tags") or {}).keys())
    return [
        axis
        for axis in classification_axes
        if axis_requires_formal_route(axis)
        and str(axis.get("axis_id") or "") not in accepted
    ]


def merge_targeted_recheck(
    base: dict[str, Any],
    retry: dict[str, Any],
    unresolved_axes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge only stronger evidence-bound classifications from one retry."""

    axis_ids = {
        str(axis.get("axis_id") or "")
        for axis in unresolved_axes
        if str(axis.get("axis_id") or "")
    }
    merged_tags = {
        str(axis_id): list(values)
        for axis_id, values in (base.get("evidence_backed_tags") or {}).items()
    }
    resolved_axis_ids: list[str] = []
    for axis_id, values in (retry.get("evidence_backed_tags") or {}).items():
        if axis_id in axis_ids and values:
            merged_tags[axis_id] = list(values)
            resolved_axis_ids.append(axis_id)

    retry_outcomes = {
        str(item.get("axis_id") or ""): item
        for item in retry.get("classification_outcomes") or []
        if isinstance(item, dict)
    }
    outcomes: list[dict[str, Any]] = []
    for item in base.get("classification_outcomes") or []:
        if not isinstance(item, dict):
            continue
        axis_id = str(item.get("axis_id") or "")
        if axis_id in resolved_axis_ids:
            continue
        outcomes.append(dict(retry_outcomes.get(axis_id) or item))

    facts_by_id = {
        str(fact.get("fact_id") or ""): dict(fact)
        for fact in base.get("facts") or []
        if isinstance(fact, dict) and str(fact.get("fact_id") or "")
    }
    for fact in retry.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        fact_id = str(fact.get("fact_id") or "")
        if fact_id and str(fact.get("classification_axis_id") or "") in resolved_axis_ids:
            facts_by_id[fact_id] = dict(fact)

    merged = dict(base)
    merged["facts"] = list(facts_by_id.values())
    merged["evidence_backed_tags"] = merged_tags
    merged["classification_outcomes"] = outcomes
    if str((retry.get("topic_partition_classification") or {}).get("status") or "") == "classified":
        merged["topic_partition_classification"] = dict(
            retry["topic_partition_classification"]
        )
    unresolved_after = sorted(
        axis_id for axis_id in axis_ids if axis_id not in merged_tags
    )
    merged["automatic_resolution"] = {
        "status": "resolved",
        "targeted_recheck_attempted": True,
        "resolved_axis_ids": sorted(resolved_axis_ids),
        "unresolved_required_axes": unresolved_after,
        "safe_route_policy": "positive_evidence_only_with_automatic_boundary_routing",
        "user_action_required": merged.get("status") == "failed",
    }
    if merged.get("status") != "failed":
        merged["review_status"] = "auto_resolved" if unresolved_after else "not_required"
    return merged


def derive_topic_partition_from_formal_tags(
    result: dict[str, Any],
    topic_partitions: list[str],
) -> dict[str, Any]:
    """Reuse a formal axis tag instead of asking two classifiers to agree."""

    current = dict(result.get("topic_partition_classification") or {})
    if not topic_partitions or current.get("status") == "classified":
        return result
    facts = {
        str(fact.get("fact_id") or ""): fact
        for fact in result.get("facts") or []
        if isinstance(fact, dict)
    }
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for tags in (result.get("evidence_backed_tags") or {}).values():
        for tag in tags or []:
            if not isinstance(tag, dict):
                continue
            partition = _canonical_partition(
                tag.get("partition_label"), topic_partitions
            )
            fact = next(
                (
                    facts.get(str(fact_id))
                    for fact_id in tag.get("fact_ids") or []
                    if facts.get(str(fact_id)) is not None
                ),
                None,
            )
            if partition and fact is not None:
                matches.append((partition, tag, fact))
    unique = {partition for partition, _tag, _fact in matches}
    if len(unique) != 1:
        return result
    partition, tag, fact = matches[0]
    updated = dict(result)
    updated["topic_partition_classification"] = {
        "schema_version": 1,
        "status": "classified",
        "partition": partition,
        "confidence": float(tag.get("confidence") or fact.get("confidence") or 0),
        "rationale": "Reused the matching formal evidence-backed classification tag.",
        "boundary_reason": "",
        "support_excerpt": str(fact.get("support_excerpt") or ""),
        "evidence_ceiling": str(fact.get("evidence_ceiling") or ""),
        "evidence_refs": list(fact.get("evidence_refs") or []),
        "review_status": "not_required",
        "extraction_method": "derived_from_formal_axis_tag",
    }
    return updated


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
    topic_partitions = [
        compact(item, 100)
        for item in source.get("topic_partitions") or []
        if compact(item, 100)
    ]
    classification_axes = normalize_classification_axes_semantics([
        dict(item)
        for item in source.get("classification_axes") or []
        if isinstance(item, dict) and compact(item.get("axis_id"), 80)
    ])
    routing_axis_id = compact(source.get("routing_axis_id"), 80)
    routing_categories = [
        {
            "label": compact(item.get("label"), 160),
            "aliases": [
                compact(value, 160)
                for value in item.get("aliases") or []
                if compact(value, 160)
            ][:16],
        }
        for item in source.get("routing_categories") or []
        if isinstance(item, dict) and compact(item.get("label"), 160)
    ]
    results: list[dict[str, Any]] = []
    attempted = 0
    succeeded_attempts = 0
    for index, paper in enumerate(papers, start=1):
        paper_id = str(paper.get("paper_id") or "")
        previous = entries.get(paper_id)
        previous_is_current = bool(
            isinstance(previous, dict)
            and previous.get("source_fingerprint") == paper.get("source_fingerprint")
            and isinstance(previous.get("result"), dict)
        )
        # Publish the active paper before the model call begins.  Previously the
        # first observable update arrived only after a whole paper had finished,
        # which made a healthy extraction look stalled for several minutes.
        write_json(
            progress_path,
            {
                "phase": "restoring" if previous_is_current else "extracting",
                "current": index - 1,
                "total": len(papers),
                "current_paper_id": paper_id,
                "completed_papers": [item.get("paper_id") for item in results],
                "failed_papers": [
                    item.get("paper_id")
                    for item in results
                    if item.get("status") == "failed"
                ],
                "updated_at_epoch": time.time(),
            },
        )
        if (
            previous_is_current
        ):
            result = dict(previous["result"])
        elif not paper.get("evidence_candidates") and not paper.get(
            "partition_evidence_candidates"
        ):
            result = {
                "paper_id": paper_id,
                "status": "failed",
                "facts": [],
                "failed_fields": ["all"],
                "topic_partition_classification": {
                    "schema_version": 1,
                    "status": "insufficient_evidence" if topic_partitions else "not_requested",
                    "partition": "",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "boundary_reason": "No source-addressable evidence candidate is available.",
                },
                "evidence_backed_tags": {},
                "classification_outcomes": [
                    {
                        "axis_id": compact(axis.get("axis_id"), 80),
                        "status": "insufficient_evidence",
                        "reason": "No source-addressable evidence candidate is available.",
                        "support_excerpt": "",
                        "evidence_refs": [],
                    }
                    for axis in classification_axes
                ],
                "error": "No full-text or abstract evidence candidate is available.",
            }
        else:
            attempted += 1
            try:
                generated = call_json_model(
                    prompt_for_paper(
                        str(source.get("review_topic") or ""),
                        paper,
                        topic_partitions,
                        classification_axes,
                    ),
                    label=f"matrix-facts-{paper_id}"[:80],
                    timeout_seconds=330,
                    required_list="facts",
                )
                result = normalize_result(
                    paper,
                    generated,
                    topic_partitions,
                    classification_axes,
                )
                unresolved_axes = unresolved_axes_for_targeted_recheck(
                    result, classification_axes
                )
                if unresolved_axes and paper.get("partition_evidence_candidates"):
                    write_json(
                        progress_path,
                        {
                            "phase": "targeted_recheck",
                            "current": index - 1,
                            "total": len(papers),
                            "current_paper_id": paper_id,
                            "target_axis_ids": [
                                compact(axis.get("axis_id"), 80)
                                for axis in unresolved_axes
                            ],
                            "completed_papers": [
                                item.get("paper_id") for item in results
                            ],
                            "failed_papers": [
                                item.get("paper_id")
                                for item in results
                                if item.get("status") == "failed"
                            ],
                            "updated_at_epoch": time.time(),
                        },
                    )
                    try:
                        retry_generated = call_json_model(
                            targeted_classification_prompt(
                                str(source.get("review_topic") or ""),
                                paper,
                                unresolved_axes,
                                topic_partitions,
                            ),
                            label=f"matrix-route-recheck-{paper_id}"[:80],
                            timeout_seconds=240,
                            required_list="facts",
                        )
                        retry_result = normalize_result(
                            paper,
                            retry_generated,
                            topic_partitions,
                            unresolved_axes,
                        )
                        result = merge_targeted_recheck(
                            result, retry_result, unresolved_axes
                        )
                    except Exception:
                        # The first pass remains valid. A repair-provider outage
                        # must not turn a complete Matrix fact set into failure.
                        automatic = dict(result.get("automatic_resolution") or {})
                        automatic.update(
                            {
                                "status": "resolved",
                                "targeted_recheck_attempted": True,
                                "targeted_recheck_completed": False,
                                "safe_route_policy": "positive_evidence_only_with_automatic_boundary_routing",
                                "user_action_required": False,
                            }
                        )
                        result["automatic_resolution"] = automatic
                routing_needed = bool(
                    routing_axis_id
                    and routing_categories
                    and not compact(paper.get("deterministic_routing_label"), 160)
                    and routing_axis_id
                    not in (result.get("evidence_backed_tags") or {})
                )
                if routing_needed:
                    write_json(
                        progress_path,
                        {
                            "phase": "routing_adjudication",
                            "current": index - 1,
                            "total": len(papers),
                            "current_paper_id": paper_id,
                            "routing_axis_id": routing_axis_id,
                            "completed_papers": [
                                item.get("paper_id") for item in results
                            ],
                            "failed_papers": [
                                item.get("paper_id")
                                for item in results
                                if item.get("status") == "failed"
                            ],
                            "updated_at_epoch": time.time(),
                        },
                    )
                    try:
                        routing_generated = call_json_model(
                            targeted_routing_prompt(
                                str(source.get("review_topic") or ""),
                                paper,
                                routing_axis_id,
                                routing_categories,
                                result,
                            ),
                            label=f"matrix-route-adjudicate-{paper_id}"[:80],
                            timeout_seconds=240,
                            required_list="facts",
                        )
                        result["routing_recommendation"] = (
                            normalize_routing_recommendation(
                                paper,
                                routing_generated,
                                routing_axis_id,
                                routing_categories,
                            )
                        )
                    except Exception as exc:
                        result["routing_recommendation"] = {
                            "schema_version": 1,
                            "axis_id": routing_axis_id,
                            "status": "insufficient_evidence",
                            "label": "",
                            "confidence": 0.0,
                            "reason": (
                                "The bounded routing adjudicator was unavailable: "
                                + compact(exc, 500)
                            ),
                            "evidence_refs": [],
                            "review_status": "auto_unresolved",
                            "extraction_method": "model_routing_unavailable",
                        }
                    automatic = dict(result.get("automatic_resolution") or {})
                    automatic.update(
                        {
                            "routing_adjudication_attempted": True,
                            "routing_axis_id": routing_axis_id,
                            "routing_status": str(
                                (result.get("routing_recommendation") or {}).get(
                                    "status"
                                )
                                or "insufficient_evidence"
                            ),
                        }
                    )
                    result["automatic_resolution"] = automatic
                elif routing_axis_id:
                    deterministic_label = compact(
                        paper.get("deterministic_routing_label"), 160
                    )
                    result["routing_recommendation"] = {
                        "schema_version": 1,
                        "axis_id": routing_axis_id,
                        "status": (
                            "deterministic_route_available"
                            if deterministic_label
                            else "formal_axis_route_available"
                        ),
                        "label": deterministic_label,
                        "confidence": 1.0,
                        "evidence_refs": [],
                        "review_status": "not_required",
                        "extraction_method": "formal_axis_route_reused",
                    }
                result = derive_topic_partition_from_formal_tags(
                    result, topic_partitions
                )
                succeeded_attempts += 1
            except Exception as exc:
                result = {
                    "paper_id": paper_id,
                    "status": "failed",
                    "facts": [],
                    "failed_fields": ["all"],
                    "topic_partition_classification": {
                        "schema_version": 1,
                        "status": "insufficient_evidence" if topic_partitions else "not_requested",
                        "partition": "",
                        "confidence": 0.0,
                        "evidence_refs": [],
                        "boundary_reason": "The evidence-bounded model classification was unavailable.",
                    },
                    "evidence_backed_tags": {},
                    "classification_outcomes": [
                        {
                            "axis_id": compact(axis.get("axis_id"), 80),
                            "status": "insufficient_evidence",
                            "reason": "The evidence-bounded model classification was unavailable.",
                            "support_excerpt": "",
                            "evidence_refs": [],
                        }
                        for axis in classification_axes
                    ],
                    "error": compact(exc, 1000),
                }
        if routing_axis_id and "routing_recommendation" not in result:
            result["routing_recommendation"] = {
                "schema_version": 1,
                "axis_id": routing_axis_id,
                "status": "insufficient_evidence",
                "label": "",
                "confidence": 0.0,
                "reason": (
                    "No source-addressable evidence was available for bounded routing."
                    if not paper.get("evidence_candidates")
                    and not paper.get("partition_evidence_candidates")
                    else "The evidence-bounded extraction did not produce a routing decision."
                ),
                "evidence_refs": [],
                "review_status": "auto_unresolved",
                "extraction_method": "model_routing_not_completed",
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
