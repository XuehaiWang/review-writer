"""Shared writing contracts used by planning, evaluation, and rewrite stages."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from .quality_rules import finding_category, rule_categories

CASE_PARAGRAPH_MIN_WORDS = 120
CASE_PARAGRAPH_MAX_WORDS = 300
DRAFT_PASS_THRESHOLD = 90.0
PARAGRAPH_PASS_THRESHOLD = 85.0

WRITING_SCOPE_CONTRACT_VERSION = 1


def writing_scope_prompt_block(writing_scope_contract: dict[str, Any], *, stage: str) -> str:
    """One Scope projection shared by Blueprint enhancement and section calls."""
    if str(writing_scope_contract.get("status") or "") != "active":
        return ("Writing Scope contract: unavailable in this legacy Blueprint. "
                "Do not infer a broader review scope from missing fields.")
    instruction = (
        "Give this section a distinct responsibility that advances the central question "
        "and review objective. Use the primary navigation axis for organization, use "
        "secondary axes only for explicit comparison, and plan reader takeaways for the declared audience."
        if stage == "planning" else
        "Realize only the approved section responsibility and Claims within this Scope. "
        "Do not broaden the time window, corpus coverage, inclusion rules, organizing "
        "axes, audience, or evidence ceiling while turning the plan into prose."
    )
    return ("Executable Writing Scope (binding for this call; missing values are boundaries, "
            "not permission to infer them):\n"
            + json.dumps(writing_scope_contract, ensure_ascii=False, sort_keys=True)
            + "\nScope application rule: " + instruction
            + " Apply the inclusion/exclusion, time, coverage, and evidence-availability "
            "policies exactly; never imply exhaustive field coverage when the contract is locally bounded.")


def section_constraint_prompt_block(task: dict[str, Any]) -> str:
    """Carry additional saved constraints as writing instructions, not evidence."""
    values = task.get("avoid_points") or task.get("avoid_patterns") or []
    if isinstance(values, str):
        values = [values]
    clauses = list(dict.fromkeys(re.sub(r"\s+", " ", item).strip()
                                for item in values if isinstance(item, str) and item.strip()))
    return ("Additional section writing constraints (not scientific evidence):\n"
            + json.dumps(clauses, ensure_ascii=False)) if clauses else ""

def paragraph_finding_is_blocking(finding: dict[str, Any]) -> bool:
    category = finding_category(finding)
    if category == "presentation":
        return False
    if (finding.get("unsupported_claims") or finding.get("missing_core_claim_ids")
            or finding.get("evidence_problem_type") == "conflict"
            or finding.get("source_check_status") in {"unsupported", "contradicted"}):
        return True
    if category == "unknown" and finding.get("route") == "final_polish":
        return False
    # A score, severity or incomplete excerpt is not proof of a scientific
    # defect. Registered integrity checks still protect their precise targets.
    rules = [*re.split(r"[/,\s]+", str(finding.get("rule") or finding.get("rule_id") or "")),
             *(finding.get("failed_dimensions") or [])]
    scientific_rule = any(rule_categories().get(rule) in {"evidence", "core_argument", "figure", "bibliography", "export", "metadata"}
                          for rule in rules)
    return bool(category in {"evidence", "core_argument", "figure", "bibliography", "export", "metadata"}
            and bool(finding.get("rule") or finding.get("rule_id") or finding.get("failed_dimensions"))
            and finding.get("route") != "pass"
            and (scientific_rule or finding.get("rule") or finding.get("rule_id")))


def substantive_quality_findings(quality: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect substantive findings; manual workflow approval preserves them."""
    findings: list[dict[str, Any]] = []
    covered_paragraphs: set[str] = set()
    for key in ("issues", "paragraph_scores", "paragraph_findings"):
        rows = [item for item in quality.get(key) or []
                if isinstance(item, dict) and paragraph_finding_is_blocking(item)]
        # A score is a second projection of its paragraph's issues, not another
        # issue. Keep distinct issues in the same collection, including locations.
        findings.extend(item for item in rows
                        if not item.get("paragraph_id") or str(item["paragraph_id"]) not in covered_paragraphs)
        covered_paragraphs.update(str(item["paragraph_id"]) for item in rows if item.get("paragraph_id"))
    return findings


def _scope_text(value: Any, *, limit: int = 2400) -> str:
    """Normalize editable Scope prose without interpreting its discipline."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _scope_list(values: Any, *, item_limit: int = 800, count: int = 24) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values not in (None, "") else []
    return list(
        dict.fromkeys(
            text
            for value in list(values)[:count]
            if (text := _scope_text(value, limit=item_limit))
        )
    )


def _scope_mapping(value: Any, fields: Iterable[str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    output: dict[str, Any] = {}
    for field in fields:
        item = source.get(field)
        if isinstance(item, str):
            item = _scope_text(item, limit=800)
        if item not in (None, "", [], {}):
            output[field] = item
    return output


def derive_writing_scope_contract(scope_contract: Any) -> dict[str, Any]:
    """Build the compact, immutable Scope passed to both section model calls.

    Blueprint Scope remains the editable source of truth.  This projection keeps
    only fields that can constrain organization or prose, and attaches a stable
    fingerprint so generated artifacts can prove which Scope they followed.
    Legacy Blueprints without Scope receive an explicit inactive contract rather
    than being rejected.
    """

    scope = scope_contract if isinstance(scope_contract, dict) else {}
    historical = _scope_mapping(
        scope.get("historical_background"),
        ("allowed", "counted_in_core_coverage", "paper_ids"),
    )
    if "paper_ids" in historical:
        historical["paper_ids"] = _scope_list(historical["paper_ids"], item_limit=160)
    payload: dict[str, Any] = {
        "schema_version": WRITING_SCOPE_CONTRACT_VERSION,
        "status": "active" if scope else "unavailable",
        "source": "blueprint.scope_contract",
        "topic": _scope_text(scope.get("topic")),
        "target_question": _scope_text(scope.get("target_question")),
        "review_objective": _scope_text(scope.get("review_objective")),
        "target_readers": _scope_list(scope.get("target_readers")),
        "required_reader_outcomes": _scope_list(
            scope.get("required_reader_outcomes")
        ),
        "time_policy": {
            "declared_span": _scope_mapping(
                scope.get("time_span"), ("from", "to", "basis")
            ),
            "core_window": _scope_mapping(
                scope.get("core_window"), ("from", "to", "basis")
            ),
            "observed_corpus_range": _scope_mapping(
                scope.get("observed_corpus_range"), ("from", "to")
            ),
            "historical_background": historical,
            "latest_update_cutoff": _scope_text(
                scope.get("latest_update_cutoff"), limit=160
            ),
            "date_field": _scope_text(scope.get("time_range_date_field"), limit=160),
        },
        "coverage_policy": {
            "mode": _scope_text(scope.get("coverage_mode"), limit=160),
            "basis": _scope_mapping(
                scope.get("coverage_basis"),
                (
                    "kind",
                    "selected_paper_count",
                    "global_literature_coverage_claimed",
                ),
            ),
        },
        "inclusion_criteria": _scope_list(scope.get("inclusion_criteria")),
        "exclusion_criteria": _scope_list(scope.get("exclusion_criteria")),
        "evidence_availability_policy": _scope_text(
            scope.get("evidence_availability_policy")
        ),
        "primary_navigation_axis": _scope_text(
            scope.get("primary_navigation_axis"), limit=400
        ),
        "secondary_axes": _scope_list(scope.get("secondary_axes"), item_limit=400),
        "source_scope_schema_version": scope.get("schema_version"),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["fingerprint"] = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return payload
