"""Shared writing contracts used by planning, evaluation, and rewrite stages."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

CASE_PARAGRAPH_MIN_WORDS = 120
CASE_PARAGRAPH_MAX_WORDS = 300

WRITING_SCOPE_CONTRACT_VERSION = 1


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
