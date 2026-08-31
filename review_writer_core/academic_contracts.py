"""Pure academic-organization contracts shared by Planning and Sections.

The module intentionally contains no database or model-provider code.  It
turns the current outline, Matrix and evidence identities into deterministic
contracts that can be validated before a model is allowed to write prose.
Domain profiles may add requirements, but the generic rules remain usable for
non-chemistry reviews.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, Iterable


ACADEMIC_SCHEMA_VERSION = 1

_STYLE_AXES = {
    "substrate": ("substrate_classes", "Substrate classes and applicability boundaries"),
    "catalyst": ("catalyst_or_method", "Catalysts, enabling methods and operating principles"),
    "reaction": ("reaction_strategy", "Transformation logic and mechanistic strategy"),
    "custom": ("user_defined", "User-defined academic question"),
}

_CATCH_ALL_EXACT = {
    "other",
    "others",
    "miscellaneous",
    "miscellany",
    "unspecified",
    "other or unspecified",
    "other/unspecified",
    "其他",
    "其它",
    "未分类",
    "未指定",
    "其他或未指定",
    "其它或未指定",
}

_ROUTING_PLACEHOLDERS = (
    "routing required",
    "unresolved routing",
    "requires classification",
    "待分类",
    "待路由",
    "需要分类",
)

_BOUNDARY_HEADING_TERMS = (
    "cross-category",
    "cross category",
    "boundary cases",
    "cross-cutting evidence",
    "跨类别",
    "边界案例",
    "交叉类别",
)

_MECHANISM_TERMS = (
    "mechanism",
    "mechanistic",
    "catalytic cycle",
    "transition state",
    "机制",
    "机理",
    "催化循环",
    "过渡态",
)

_HISTORY_TERMS = (
    "history",
    "historical",
    "evolution",
    "development",
    "timeline",
    "历史",
    "演进",
    "发展",
    "时间线",
)


def _text(value: Any) -> str:
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _unique(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            text for value in values if (text := _text(value))
        )
    )


def normalized_heading(value: Any) -> str:
    heading = _text(value).casefold()
    heading = re.sub(r"^\s*\d+(?:\.\d+)*[.)、：:\-]?\s*", "", heading)
    return heading.strip(" .:：—–-_/\\")


_CONTRACT_MATCH_STOPWORDS = {
    "and",
    "or",
    "the",
    "a",
    "an",
    "of",
    "for",
    "in",
    "review",
    "reviews",
    "discussion",
    "evidence",
    "method",
    "methods",
    "study",
    "studies",
}


def _normalized_contract_text(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9\u3400-\u9fff]+",
        " ",
        normalized_heading(value),
    ).strip()


def _contract_text_values(value: Any) -> list[str]:
    """Flatten section-contract prose without interpreting its discipline."""

    if isinstance(value, dict):
        return _unique(
            text
            for item in value.values()
            for text in _contract_text_values(item)
        )
    if isinstance(value, (list, tuple, set)):
        return _unique(
            text
            for item in value
            for text in _contract_text_values(item)
        )
    return [_text(value)] if _text(value) else []


def _partition_aliases(
    value: Any,
    *,
    declared_aliases: Iterable[Any] = (),
) -> list[str]:
    """Build conservative aliases from the user's own label.

    Parenthetical forms are treated as aliases, so a requirement such as
    ``enantioselective ATA (EATA)`` can be traced by either the expanded label
    or ``EATA``. No discipline-specific synonym list is introduced here.
    """

    raw = _text(value)
    if not raw:
        return []
    parenthetical = re.findall(r"[（(]([^()（）]{2,40})[）)]", raw)
    without_parenthetical = re.sub(r"\s*[（(][^()（）]{2,40}[）)]\s*", " ", raw)
    return _unique(
        normalized
        for candidate in (
            raw,
            without_parenthetical,
            *parenthetical,
            *declared_aliases,
        )
        if (normalized := _normalized_contract_text(candidate))
    )


def _declared_partition_aliases(
    contract: dict[str, Any],
    required_partitions: list[str],
) -> dict[str, list[str]]:
    """Return model-declared aliases keyed by the canonical Topic label.

    Query planning and Matrix fact extraction may preserve a user's wording as
    a partition label while adding a shorter scientific alias.  Blueprint
    headings are allowed to use that alias, so diagnostics must carry the same
    contract vocabulary instead of matching only the literal Topic phrase.
    """

    axes = contract.get("classification_axes") or []
    nested_contract = contract.get("classification_contract")
    if not axes and isinstance(nested_contract, dict):
        axes = nested_contract.get("axes") or []
    required_by_alias: dict[str, str] = {}
    for label in required_partitions:
        for alias in _partition_aliases(label):
            required_by_alias.setdefault(alias, label)
    result: dict[str, list[str]] = {label: [] for label in required_partitions}
    for axis in axes:
        if not isinstance(axis, dict) or _text(axis.get("axis_role")) not in {
            "primary_organization",
            "required_independent_discussion",
        }:
            continue
        for partition in axis.get("partitions") or []:
            if not isinstance(partition, dict):
                continue
            candidates = _unique(
                [
                    partition.get("label"),
                    *(partition.get("aliases") or []),
                ]
            )
            canonical = next(
                (
                    required_by_alias[normalized]
                    for candidate in candidates
                    if (normalized := _normalized_contract_text(candidate))
                    in required_by_alias
                ),
                "",
            )
            if canonical:
                result[canonical] = _unique([*result[canonical], *candidates])
    return result


def _partition_trace(
    partition: str,
    body: list[dict[str, Any]],
    *,
    sibling_partitions: list[str],
    declared_aliases: Iterable[Any] = (),
) -> list[str]:
    aliases = _partition_aliases(partition, declared_aliases=declared_aliases)
    if not aliases:
        return []
    sibling_tokens = [
        {
            token
            for token in _normalized_contract_text(label).split()
            if len(token) >= 3 and token not in _CONTRACT_MATCH_STOPWORDS
        }
        for label in sibling_partitions
    ]
    common_tokens = (
        set.intersection(*sibling_tokens)
        if len(sibling_tokens) > 1 and all(sibling_tokens)
        else set()
    )
    wanted_tokens = {
        token
        for token in _normalized_contract_text(
            re.sub(r"\s*[（(][^()（）]{2,40}[）)]\s*", " ", partition)
        ).split()
        if len(token) >= 3
        and token not in _CONTRACT_MATCH_STOPWORDS
        and token not in common_tokens
    }
    matches: list[str] = []
    for item in body:
        haystacks = [
            _normalized_contract_text(value)
            for value in item.get("contract_trace_values") or []
            if _normalized_contract_text(value)
        ]
        represented = any(
            alias == haystack
            or (
                len(alias) >= 4
                and re.search(rf"(?:^|\s){re.escape(alias)}(?:$|\s)", haystack)
            )
            for alias in aliases
            for haystack in haystacks
        )
        if not represented and len(wanted_tokens) >= 2:
            represented = any(
                len(wanted_tokens & set(haystack.split())) / len(wanted_tokens) >= 0.8
                for haystack in haystacks
            )
        if represented:
            matches.append(item["section_id"])
    return matches


def is_catch_all_heading(value: Any) -> bool:
    """Return whether a body heading is a catch-all rather than a real concept.

    Exact matching avoids flagging meaningful headings such as "Other
    applications of allenes".  Explicit routing placeholders are also caught
    because they are workflow states, not publishable taxonomy nodes.
    """

    heading = normalized_heading(value)
    if heading in _CATCH_ALL_EXACT:
        return True
    return any(heading.startswith(prefix) for prefix in _ROUTING_PLACEHOLDERS)


def is_boundary_heading(value: Any) -> bool:
    """Return whether a heading explicitly represents residual boundary cases."""

    heading = normalized_heading(value)
    return any(term in heading for term in _BOUNDARY_HEADING_TERMS)


def evidence_key(paper_id: Any, chunk_id: Any, source_lineage_hash: Any) -> str:
    """Build a stable evidence identity from immutable source coordinates."""

    payload = {
        "chunk_id": _text(chunk_id),
        "paper_id": _text(paper_id),
        "source_lineage_hash": _text(source_lineage_hash),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _year(value: Any) -> int | None:
    raw = _text(value)
    match = re.search(r"(?:18|19|20|21)\d{2}", raw)
    if not match:
        return None
    year = int(match.group(0))
    return year if 1800 <= year <= 2199 else None


def _row_publication_year(row: dict[str, Any]) -> int | None:
    """Use first publication date for scope inclusion, then safe fallbacks."""

    return (
        _year(row.get("first_publication_date"))
        or _year(row.get("bibliographic_year"))
        or _year(row.get("year"))
    )


def _topic_year_range(topic: Any) -> tuple[int | None, int | None]:
    text = _text(topic)
    match = re.search(
        r"(?<!\d)((?:18|19|20|21)\d{2})\s*(?:-|–|—|to|至)\s*((?:18|19|20|21)\d{2})(?!\d)",
        text,
        re.I,
    )
    if not match:
        return None, None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if start <= end else (end, start)


def derive_scope_contract(
    topic: Any,
    outline_style: Any,
    matrix_rows: Iterable[dict[str, Any]],
    *,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a safe default Scope while preserving explicit user values."""

    review_topic = _text(topic) or "the selected research topic"
    style = _text(outline_style).casefold() or "custom"
    axis, axis_label = _STYLE_AXES.get(style, _STYLE_AXES["custom"])
    rows = list(matrix_rows)
    explicit_year_from, explicit_year_to = _topic_year_range(review_topic)
    years = sorted(
        year
        for row in rows
        if isinstance(row, dict)
        if (year := _row_publication_year(row)) is not None
    )
    defaults: dict[str, Any] = {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "topic": review_topic,
        "review_type": "narrative_topic_review",
        "target_question": (
            f"How should the evidence on {review_topic} be organized and compared, "
            "and which conclusions and limitations are supported across studies?"
        ),
        "review_objective": (
            f"Build an evidence-grounded field map of {review_topic} using {axis_label.casefold()}, "
            "then identify transferable findings, boundaries and testable research directions."
        ),
        "time_span": {
            "from": explicit_year_from if explicit_year_from is not None else years[0] if years else None,
            "to": explicit_year_to if explicit_year_to is not None else years[-1] if years else None,
            "basis": "user_topic" if explicit_year_from is not None else "selected_matrix",
        },
        "core_window": {
            "from": explicit_year_from if explicit_year_from is not None else years[0] if years else None,
            "to": explicit_year_to if explicit_year_to is not None else years[-1] if years else None,
            "basis": "user_topic" if explicit_year_from is not None else "selected_matrix_default",
        },
        "historical_background": {
            "allowed": True,
            "counted_in_core_coverage": False,
            "paper_ids": [],
        },
        "latest_update_cutoff": None,
        "observed_corpus_range": {
            "from": years[0] if years else None,
            "to": years[-1] if years else None,
        },
        "time_range_date_field": "first_publication_date",
        "coverage_mode": "local_bounded",
        "coverage_basis": {
            "kind": "selected_matrix",
            "selected_paper_count": len(rows),
            "global_literature_coverage_claimed": False,
        },
        "inclusion_criteria": [
            "Falls within the confirmed review topic and selected Matrix",
            "Provides direct evidence, a defensible synthesis premise, or a clearly identified foundational role",
        ],
        "exclusion_criteria": [
            "Outside the confirmed review question",
            "Cannot support a stated evidence role after source inspection",
        ],
        "evidence_availability_policy": (
            "Unavailable full text does not silently justify quantitative or mechanistic claims; "
            "the resulting evidence ceiling must be recorded."
        ),
        "primary_navigation_axis": axis,
        "secondary_axes": [],
        "target_readers": ["Researchers in the topic area", "Graduate readers entering the field"],
        "required_reader_outcomes": [
            "Understand the organizing logic and terminology",
            "Compare the major evidence-backed approaches",
            "Distinguish established findings, source interpretations and review-level inferences",
        ],
        "source": "auto_derived",
    }
    if not isinstance(current, dict):
        return defaults
    merged = dict(defaults)
    for key, value in current.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    merged["schema_version"] = ACADEMIC_SCHEMA_VERSION
    return merged


def classification_basis(outline_style: Any) -> dict[str, Any]:
    style = _text(outline_style).casefold() or "custom"
    axis, description = _STYLE_AXES.get(style, _STYLE_AXES["custom"])
    return {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "primary_axis": axis,
        "overview_axis": axis,
        "overview_axis_policy": "The overview figure must use the same primary axis as the manuscript body.",
        "description": description,
        "orthogonal_axes": [],
        "overview_secondary_axes": [],
        "same_level_consistency_required": True,
        "catch_all_sections_allowed": False,
    }


def scope_diagnostics(scope: dict[str, Any]) -> dict[str, Any]:
    """Validate the minimum executable Scope without imposing domain fields."""

    issues: list[dict[str, Any]] = []
    required_text = {
        "target_question": "State the central question the review will answer.",
        "review_objective": "State the review's intended academic contribution.",
        "primary_navigation_axis": "Declare one primary navigation axis.",
    }
    for field, message in required_text.items():
        if not _text(scope.get(field)):
            issues.append(
                {
                    "rule_id": f"scope.{field}_missing",
                    "severity": "planning_blocker",
                    "field": field,
                    "message": message,
                }
            )
    for field, message in (
        ("inclusion_criteria", "Add at least one inclusion criterion."),
        ("exclusion_criteria", "Add at least one exclusion criterion."),
        ("target_readers", "Identify at least one target reader group."),
        ("required_reader_outcomes", "State at least one reader outcome."),
    ):
        if not _unique(scope.get(field) or []):
            issues.append(
                {
                    "rule_id": f"scope.{field}_missing",
                    "severity": "planning_blocker",
                    "field": field,
                    "message": message,
                }
            )
    return {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "can_confirm": not issues,
        "blocking_issue_count": len(issues),
        "issues": issues,
    }


def coverage_diagnostics(
    scope: dict[str, Any], matrix_rows: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Describe the selected corpus without claiming unknowable global coverage."""

    rows = [row for row in matrix_rows if isinstance(row, dict)]
    years = [_row_publication_year(row) for row in rows]
    known_years = [year for year in years if year is not None]
    year_counts = Counter(str(year) for year in known_years)
    journals = Counter(
        _text(row.get("journal") or row.get("venue")) or "unknown"
        for row in rows
    )

    tag_values: list[str] = []
    for row in rows:
        tags = row.get("tags") or row.get("structured_tags") or {}
        pending = [tags]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                pending.extend(value)
            else:
                label = _text(value)
                if label and label.casefold() not in {"unknown", "other", "unspecified"}:
                    tag_values.append(label)
    clusters = Counter(tag_values)
    latest_year = max(known_years) if known_years else None
    recent_count = (
        sum(1 for year in known_years if year >= latest_year - 4)
        if latest_year is not None
        else 0
    )
    warnings: list[dict[str, Any]] = []
    time_span = (
        scope.get("core_window")
        if isinstance(scope.get("core_window"), dict)
        else scope.get("time_span")
        if isinstance(scope.get("time_span"), dict)
        else {}
    )
    year_from = _year(time_span.get("from"))
    year_to = _year(time_span.get("to"))
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    missing_years: list[int] = []
    if year_from is not None and year_to is not None and year_to - year_from <= 40:
        observed = set(known_years)
        missing_years = [year for year in range(year_from, year_to + 1) if year not in observed]
    outside_window: list[str] = []
    foundational_outside_window: list[str] = []
    if year_from is not None and year_to is not None:
        for row in rows:
            paper_id = _text(row.get("paper_id"))
            paper_year = _row_publication_year(row)
            if not paper_id or paper_year is None or year_from <= paper_year <= year_to:
                continue
            evidence_role = _text(
                row.get("scope_role")
                or row.get("evidence_role")
                or row.get("role")
            ).casefold()
            if evidence_role in {
                "background",
                "foundational",
                "historical_context",
                "context",
            }:
                foundational_outside_window.append(paper_id)
            else:
                outside_window.append(paper_id)
    if rows and len(known_years) < len(rows):
        warnings.append(
            {
                "rule_id": "coverage.publication_year_missing",
                "severity": "warning",
                "paper_count": len(rows) - len(known_years),
                "message": "Some selected papers have no normalized publication year.",
            }
        )
    if not _text(scope.get("search_cutoff_date")):
        warnings.append(
            {
                "rule_id": "coverage.search_cutoff_unrecorded",
                "severity": "warning",
                "message": "The search cutoff date has not been recorded.",
            }
        )
    span = (year_to - year_from + 1) if year_from is not None and year_to is not None else 0
    if span and len(missing_years) >= max(2, (span + 2) // 3):
        warnings.append(
            {
                "rule_id": "coverage.explicit_year_range_sparse",
                "severity": "advisory",
                "missing_years": missing_years,
                "message": "The selected corpus is sparse across the declared year range; consider an online supplementary search.",
            }
        )
    if outside_window:
        warnings.append(
            {
                "rule_id": "coverage.selected_papers_outside_declared_window",
                "severity": "warning",
                "paper_ids": outside_window,
                "message": (
                    "Some analytical papers fall outside the declared publication window. "
                    "Re-evaluate their inclusion or explicitly assign a foundational/background role."
                ),
            }
        )
    coverage_mode = _text(scope.get("coverage_mode")) or "local_bounded"
    return {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "coverage_mode": coverage_mode,
        "coverage_claim": (
            "selected_multi_source_corpus_only"
            if coverage_mode == "multi_source"
            else "selected_local_corpus_only"
        ),
        "global_coverage_percentage": None,
        "selected_paper_count": len(rows),
        "search_cutoff_date": _text(scope.get("search_cutoff_date")) or None,
        "latest_update_cutoff": _text(
            scope.get("latest_update_cutoff") or scope.get("search_cutoff_date")
        )
        or None,
        "core_window": {"from": year_from, "to": year_to},
        "observed_corpus_range": {
            "from": min(known_years) if known_years else None,
            "to": max(known_years) if known_years else None,
        },
        "year_distribution": dict(sorted(year_counts.items())),
        "year_unknown_count": len(rows) - len(known_years),
        "declared_year_from": year_from,
        "declared_year_to": year_to,
        "missing_years": missing_years,
        "outside_window_paper_ids": outside_window,
        "foundational_outside_window_paper_ids": foundational_outside_window,
        "recent_paper_ratio": round(recent_count / len(known_years), 4) if known_years else None,
        "source_distribution": dict(journals.most_common(20)),
        "topic_clusters": [
            {"label": label, "paper_count": count}
            for label, count in clusters.most_common(20)
        ],
        "warnings": warnings,
        "limitations": [
            "Coverage is measured only against the user-selected Matrix and configured sources.",
            "No complete field benchmark corpus is assumed, so no global coverage percentage is reported.",
            (
                "Online topic search was not enabled; coverage is bounded to the local Library."
                if coverage_mode == "local_bounded"
                else "Online sources supplemented the local Library, but exhaustive global coverage is not claimed."
            ),
        ],
    }


def taxonomy_diagnostics(
    sections: Iterable[dict[str, Any]],
    matrix_paper_ids: Iterable[Any],
    *,
    classification_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnose routing and classification-contract drift.

    The contract check is intentionally explainable: it never infers a
    discipline-specific taxonomy, but it does verify that the generated
    sections still honor the axis and explicit partitions selected upstream.
    """

    matrix_ids = _unique(matrix_paper_ids)
    matrix_set = set(matrix_ids)
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(sections, start=1):
        if not isinstance(source, dict):
            continue
        secondary_routes = source.get("secondary_axis_routes")
        secondary_route_labels = (
            list(secondary_routes)
            if isinstance(secondary_routes, dict)
            else []
        )
        trace_values = _contract_text_values(
            [
                source.get("topic_partition"),
                source.get("title"),
                source.get("purpose"),
                source.get("notes"),
                source.get("section_thesis"),
                source.get("review_problem"),
                source.get("review_claims"),
                secondary_route_labels,
            ]
        )
        normalized.append(
            {
                "section_id": _text(source.get("section_id")) or f"S{index:02d}",
                "title": _text(source.get("title")),
                "section_role": _text(source.get("section_role")).casefold() or "body",
                "paper_ids": _unique(
                    source.get("paper_ids")
                    or source.get("primary_papers")
                    or source.get("major_papers")
                    or []
                ),
                "context_paper_ids": _unique(
                    source.get("context_paper_ids")
                    or source.get("context_papers")
                    or []
                ),
                "excluded_papers": [
                    {
                        "paper_id": _text(exclusion.get("paper_id")),
                        "reason": _text(exclusion.get("reason")),
                    }
                    for exclusion in source.get("excluded_papers") or []
                    if isinstance(exclusion, dict)
                    and _text(exclusion.get("paper_id"))
                ],
                "topic_partition": _text(source.get("topic_partition")),
                "boundary_rationale": _text(source.get("boundary_rationale")),
                "single_paper_justification": _text(
                    source.get("single_paper_justification")
                ),
                "thesis_status": _text(
                    source.get("thesis_status")
                    or (
                        (source.get("scientific_thesis") or {}).get("status")
                        if isinstance(source.get("scientific_thesis"), dict)
                        else ""
                    )
                ),
                "contract_trace_values": trace_values,
            }
        )

    body = [item for item in normalized if item["section_role"] == "body"]
    issues: list[dict[str, Any]] = []
    catch_all_ids = [item["section_id"] for item in body if is_catch_all_heading(item["title"])]
    if catch_all_ids:
        issues.append(
            {
                "rule_id": "taxonomy.catch_all_body_section",
                "severity": "planning_blocker",
                "section_ids": catch_all_ids,
                "message": "Replace catch-all body sections with a defensible academic category and reroute their papers.",
            }
        )

    assigned = _unique(paper_id for item in body for paper_id in item["paper_ids"])
    analytical_count = len(set(assigned) & matrix_set)
    dominant_boundary_threshold = max(4, math.ceil(analytical_count * 0.35))
    dominant_boundary_ids = [
        item["section_id"]
        for item in body
        if analytical_count >= 8
        and is_boundary_heading(item["title"])
        and len(set(item["paper_ids"]) & matrix_set) >= dominant_boundary_threshold
    ]
    if dominant_boundary_ids:
        issues.append(
            {
                "rule_id": "taxonomy.dominant_boundary_section",
                "severity": "planning_blocker",
                "section_ids": dominant_boundary_ids,
                "message": (
                    "A residual boundary section owns too much of the selected corpus. "
                    "Reroute its papers into evidence-based academic categories before writing."
                ),
            }
        )
    contract = (
        classification_contract
        if isinstance(classification_contract, dict)
        else {}
    )
    contract_axes = [
        axis
        for axis in (
            contract.get("classification_axes")
            or contract.get("axes")
            or []
        )
        if isinstance(axis, dict) and _text(axis.get("axis_id"))
    ]
    primary_axis_ids = _unique(
        axis.get("axis_id")
        for axis in contract_axes
        if _text(axis.get("axis_role")) == "primary_organization"
    )
    enforce_single_primary_axis = bool(
        _text(contract.get("primary_axis_id"))
        or _text(contract.get("section_partition_policy"))
        == "single_primary_axis"
    )
    if contract_axes and enforce_single_primary_axis and len(primary_axis_ids) != 1:
        issues.append(
            {
                "rule_id": "taxonomy.primary_axis_contract_invalid",
                "severity": "planning_blocker",
                "axis_ids": primary_axis_ids,
                "message": (
                    "The classification contract must declare exactly one primary organization "
                    "axis. Secondary axes may guide subheadings and comparisons only."
                ),
            }
        )
    boundary_ids = [
        item["section_id"]
        for item in body
        if is_boundary_heading(item["title"]) and item["paper_ids"]
    ]
    boundary_rationale_ids = [
        item["section_id"]
        for item in body
        if item["section_id"] in boundary_ids and item["boundary_rationale"]
    ]
    unresolved_boundary_ids = [
        section_id
        for section_id in boundary_ids
        if section_id not in boundary_rationale_ids
    ]
    if (
        unresolved_boundary_ids
        and contract.get("catch_all_sections_allowed") is False
    ):
        issues.append(
            {
                "rule_id": "taxonomy.boundary_section_outside_contract",
                "severity": "warning",
                "section_ids": unresolved_boundary_ids,
                "message": (
                    "A residual boundary section has no explicit evidence-based rationale. "
                    "Reclassify its papers under the primary axis or record why a cross-category analysis is necessary."
                ),
            }
        )

    required_partitions = _unique(
        contract.get("required_outline_partitions")
        or contract.get("topic_partitions")
        or []
    )
    partition_aliases = _declared_partition_aliases(contract, required_partitions)
    raw_partition_boundaries = contract.get("topic_partition_coverage_boundaries")
    if isinstance(raw_partition_boundaries, dict):
        declared_partition_boundaries = {
            _text(key).casefold(): value
            for key, value in raw_partition_boundaries.items()
            if _text(key) and value
        }
    elif isinstance(raw_partition_boundaries, (list, tuple, set)):
        declared_partition_boundaries = {
            _text(value).casefold(): True
            for value in raw_partition_boundaries or []
            if _text(value)
        }
    else:
        declared_partition_boundaries = {}
    partition_trace: dict[str, list[str]] = {}
    route_gap_partitions: list[str] = []
    bounded_partitions: list[str] = []
    missing_partitions: list[str] = []
    for partition in required_partitions:
        section_ids = _partition_trace(
            partition,
            body,
            sibling_partitions=required_partitions,
            declared_aliases=partition_aliases.get(partition) or [],
        )
        partition_trace[partition] = section_ids
        if not section_ids:
            route_gap_partitions.append(partition)
            if partition.casefold() in declared_partition_boundaries:
                bounded_partitions.append(partition)
            else:
                missing_partitions.append(partition)
    if missing_partitions:
        issues.append(
            {
                "rule_id": "taxonomy.required_topic_partitions_missing",
                "severity": "warning",
                "partitions": missing_partitions,
                "message": (
                    "One or more independently discussed partitions requested in the Topic "
                    "are not traceable in a body heading or section contract. Add an "
                    "evidence-backed route or record the resulting coverage boundary."
                ),
            }
        )
    contextual = _unique(
        paper_id for item in normalized for paper_id in item["context_paper_ids"]
    )
    exclusions = [
        exclusion
        for item in normalized
        for exclusion in item["excluded_papers"]
    ]
    excluded = _unique(
        exclusion["paper_id"]
        for exclusion in exclusions
        if exclusion["reason"]
    )
    exclusion_reason_missing_ids = _unique(
        exclusion["paper_id"]
        for exclusion in exclusions
        if not exclusion["reason"]
    )
    if exclusion_reason_missing_ids:
        issues.append(
            {
                "rule_id": "taxonomy.paper_exclusion_reason_missing",
                "severity": "planning_blocker",
                "paper_ids": exclusion_reason_missing_ids,
                "message": "An excluded selected paper needs a specific scientific or scope reason.",
            }
        )
    routed_ids = set(assigned) | set(contextual) | set(excluded)
    orphan_ids = [paper_id for paper_id in matrix_ids if paper_id not in routed_ids]
    if orphan_ids:
        issues.append(
            {
                "rule_id": "taxonomy.orphan_papers",
                "severity": "planning_blocker",
                "paper_ids": orphan_ids,
                "message": "Every selected paper needs one primary analytical route or an explicit exclusion reason.",
            }
        )

    unknown_ids = sorted(
        {
            paper_id
            for item in normalized
            for paper_id in [
                *item["paper_ids"],
                *item["context_paper_ids"],
                *(exclusion["paper_id"] for exclusion in item["excluded_papers"]),
            ]
            if paper_id not in matrix_set
        }
    )
    if unknown_ids:
        issues.append(
            {
                "rule_id": "taxonomy.unknown_papers",
                "severity": "planning_blocker",
                "paper_ids": unknown_ids,
                "message": "Outline routes must resolve to the current Matrix.",
            }
        )

    if not body:
        issues.append(
            {
                "rule_id": "taxonomy.no_analytical_body",
                "severity": "planning_blocker",
                "message": "The outline needs at least one analytical body section.",
            }
        )

    # A minimum section size is a project classification policy, not a
    # universal property of every taxonomy diagnostic call.  Older callers
    # intentionally omit a classification contract, so they must not acquire
    # a new warning merely by upgrading the shared helper.  Current Planning
    # contracts provide this value explicitly (normally ``2``).
    minimum_body_papers = max(
        1,
        int(
            contract.get("minimum_body_papers")
            or (2 if contract.get("single_paper_section_policy") else 1)
        ),
    )
    single_paper_ids = [
        item["section_id"]
        for item in body
        if len(set(item["paper_ids"]) & matrix_set) == 1
    ]
    unjustified_single_paper_ids = [
        item["section_id"]
        for item in body
        if item["section_id"] in single_paper_ids
        and not item["single_paper_justification"]
        and not item["boundary_rationale"]
    ]
    single_paper_merge_suggestions: list[dict[str, Any]] = []
    if unjustified_single_paper_ids and minimum_body_papers > 1:
        for index, item in enumerate(body):
            if item["section_id"] not in unjustified_single_paper_ids:
                continue
            candidates: list[tuple[int, dict[str, Any]]] = []
            for other_index, other in enumerate(body):
                if other is item:
                    continue
                distance = abs(other_index - index)
                title_tokens = set(re.findall(r"[\w-]+", item["title"].casefold()))
                other_tokens = set(re.findall(r"[\w-]+", other["title"].casefold()))
                overlap = len(title_tokens & other_tokens)
                score = overlap * 100 - distance
                candidates.append((score, other))
            if not candidates:
                continue
            _score, target = max(candidates, key=lambda pair: pair[0])
            single_paper_merge_suggestions.append(
                {
                    "source_section_id": item["section_id"],
                    "target_section_id": target["section_id"],
                    "method": "nearest_semantic_or_adjacent_section",
                    "requires_confirmation": True,
                }
            )
        issues.append(
            {
                "rule_id": "taxonomy.single_paper_section_unjustified",
                "severity": "warning",
                "section_ids": unjustified_single_paper_ids,
                "message": (
                    "A one-paper body section needs an independent scientific thesis and an "
                    "explicit justification, or it should be merged with the nearest compatible "
                    "primary-axis section. Suggested repairs require confirmation and never "
                    "silently merge scientifically distinct categories."
                ),
            }
        )

    title_counts = Counter(normalized_heading(item["title"]) for item in body)
    duplicates = sorted(title for title, count in title_counts.items() if title and count > 1)
    if duplicates:
        issues.append(
            {
                "rule_id": "taxonomy.duplicate_titles",
                "severity": "warning",
                "titles": duplicates,
                "message": "Repeated body headings need distinct questions or boundaries.",
            }
        )

    roles = {item["section_role"] for item in normalized}
    for missing_role in ("introduction", "conclusion"):
        if missing_role not in roles:
            issues.append(
                {
                    "rule_id": f"taxonomy.{missing_role}_missing",
                    "severity": "warning",
                    "message": f"Add an explicit {missing_role} contract before final manuscript assembly.",
                }
            )

    blockers = [issue for issue in issues if issue["severity"] == "planning_blocker"]
    return {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "can_confirm": not blockers,
        "blocking_issue_count": len(blockers),
        "warning_count": len(issues) - len(blockers),
        "catch_all_section_ids": catch_all_ids,
        "dominant_boundary_section_ids": dominant_boundary_ids,
        "boundary_section_ids": boundary_ids,
        "boundary_rationale_section_ids": boundary_rationale_ids,
        "unresolved_boundary_section_ids": unresolved_boundary_ids,
        "single_paper_section_ids": single_paper_ids,
        "unjustified_single_paper_section_ids": unjustified_single_paper_ids,
        "single_paper_merge_suggestions": single_paper_merge_suggestions,
        "required_topic_partitions": required_partitions,
        "missing_topic_partitions": missing_partitions,
        "topic_partition_route_gaps": route_gap_partitions,
        "bounded_topic_partitions": bounded_partitions,
        "topic_partition_trace": partition_trace,
        # These dimensions are intentionally informational: they must be
        # compared or covered, but do not each need a top-level body section.
        "topic_comparison_dimensions": _unique(
            contract.get("topic_comparison_dimensions") or []
        ),
        "topic_axis_examples": dict(
            contract.get("topic_axis_examples") or {}
        ),
        "topic_outcome_dimensions": _unique(
            contract.get("topic_outcome_dimensions") or []
        ),
        "topic_focus_dimensions": _unique(
            contract.get("topic_focus_dimensions")
            or contract.get("topic_outcome_dimensions")
            or []
        ),
        "primary_axis_ids": primary_axis_ids,
        "section_partition_policy": str(
            contract.get("section_partition_policy") or "single_primary_axis"
        ),
        "classification_contract_status": (
            "drift"
            if (
                catch_all_ids
                or dominant_boundary_ids
                or (
                    contract_axes
                    and enforce_single_primary_axis
                    and len(primary_axis_ids) != 1
                )
                or (
                    unresolved_boundary_ids
                    and contract.get("catch_all_sections_allowed") is False
                )
                or missing_partitions
            )
            else "aligned_with_boundaries"
            if bounded_partitions
            else "aligned"
        ),
        "orphan_paper_ids": orphan_ids,
        "excluded_paper_ids": list(excluded),
        "paper_exclusions": [
            exclusion for exclusion in exclusions if exclusion["reason"]
        ],
        "assigned_paper_count": len(set(assigned) & matrix_set),
        "contextual_paper_count": len(set(contextual) & matrix_set),
        "excluded_paper_count": len(set(excluded) & matrix_set),
        "selected_paper_count": len(matrix_ids),
        "issues": issues,
    }


def section_academic_contract(section: dict[str, Any]) -> dict[str, Any]:
    role = _text(section.get("section_role")).casefold() or "body"
    title = _text(section.get("title")) or _text(section.get("section_id"))
    thesis = _text(section.get("section_thesis") or section.get("review_problem"))
    primary = _unique(section.get("primary_papers") or section.get("major_papers") or [])
    supporting = _unique(section.get("supporting_papers") or [])
    if role == "introduction":
        node_type = "navigational"
        academic_role = "scope_and_field_map"
        expected = "Define the scope, core concepts, classification basis and reading roadmap."
    elif role == "conclusion":
        node_type = "reflective"
        academic_role = "cross_section_synthesis_and_roadmap"
        expected = "Answer the review question with bounded conclusions and testable directions."
    else:
        node_type = "analytical"
        academic_role = "evidence_synthesis"
        expected = "Compare evidence on shared dimensions and explain the resulting pattern and boundary."
    return {
        "node_type": node_type,
        "academic_role": academic_role,
        "semantic_scope": thesis or f"Evidence relevant to {title}",
        "writing_objective": thesis or expected,
        "key_questions": [_text(section.get("review_problem")) or f"What does the evidence establish about {title}?"],
        "boundary_exclusions": _unique(section.get("avoid_patterns") or []),
        "expected_synthesis": expected,
        "primary_paper_count": len(primary),
        "supporting_paper_count": len(supporting),
    }


def synthesis_requirements(
    section: dict[str, Any],
    *,
    taxonomy_profile: Any = "general_academic",
) -> list[dict[str, Any]]:
    """Select typed knowledge components from section purpose, not a fixed set."""

    role = _text(section.get("section_role")).casefold() or "body"
    searchable = " ".join(
        _text(section.get(key))
        for key in ("title", "section_thesis", "review_problem", "expected_synthesis")
    ).casefold()
    paper_count = len(
        _unique(
            [
                *(section.get("primary_papers") or section.get("major_papers") or []),
                *(section.get("supporting_papers") or []),
            ]
        )
    )
    # Domain profiles are opt-in. Future non-chemistry profiles must retain
    # the generic contract instead of accidentally inheriting chemistry rules.
    chemistry = _text(taxonomy_profile).casefold().startswith("chemistry")
    requirements: list[dict[str, Any]] = []

    def add(component: str, necessity: str, reason: str) -> None:
        requirements.append(
            {
                "component": component,
                "necessity": necessity,
                "reason": reason,
            }
        )

    if role == "introduction":
        add("glossary", "required" if chemistry else "recommended", "Define concepts at the target reader's first point of need.")
        add("timeline", "recommended", "Connect foundational and modern evidence when the corpus spans multiple periods.")
    elif role == "conclusion":
        add("comparison", "required", "Conclusion claims must be grounded in shared comparison dimensions.")
        add("roadmap", "required", "Future directions need an observation-to-root-cause-to-test chain.")
    else:
        add(
            "comparison",
            "required" if paper_count > 1 else "recommended",
            "Analytical sections should compare studies on explicit shared dimensions.",
        )
        if any(term in searchable for term in _MECHANISM_TERMS):
            add("mechanism", "required", "Mechanism is part of the section's stated argument.")
        elif chemistry:
            add("mechanism", "recommended", "Chemistry profile benefits from explicit mechanistic evidence boundaries.")
        if any(term in searchable for term in _HISTORY_TERMS):
            add("timeline", "required", "The section explicitly claims a historical or developmental relationship.")

    return requirements


def evidence_level(content: Any) -> str:
    """Return a conservative source-level label for wording-ceiling prompts."""

    text = _text(content).casefold()
    if re.search(r"\b(measured|determined|observed|yield|conversion|ee|er|dr)\b", text):
        return "direct_measurement"
    if re.search(r"\b(propose|proposed|suggest|suggested|hypothesi[sz]e|may proceed)\b", text):
        return "author_inference"
    if re.search(r"\b(correlat|associated with|accompanied by)\b", text):
        return "correlated_observation"
    return "reported_result"


def mechanism_evidence_types(content: Any) -> list[str]:
    """Classify observable mechanism-support types without judging causality.

    These labels describe what the source reports, not whether the proposed
    mechanism is ultimately correct.  The writing layer can therefore compare
    evidence strength without collapsing experiment, computation and author
    interpretation into one generic disclaimer.
    """

    text = _text(content).casefold()
    patterns = (
        (
            "intermediate_isolation_or_independent_synthesis",
            r"\b(?:isolat(?:ed|ion)|independent(?:ly)? synthes(?:ized|is)|prepared intermediate|intermediate was synthesized)\b",
        ),
        (
            "intermediate_conversion_or_control_experiment",
            r"\b(?:control experiment|conversion experiment|converted? (?:to|into)|subjected to (?:the )?(?:standard|reaction) conditions)\b",
        ),
        (
            "isotope_label_or_kie",
            r"\b(?:kinetic isotope effect|\bkie\b|isotop(?:e|ic)(?:[- ]label| experiment)|deuterium label|deuterat)\b",
        ),
        (
            "time_course_or_racemization",
            r"\b(?:time[- ]course|reaction profile|racemi[sz]|epimeri[sz]|configurational stability|ee erosion)\b",
        ),
        (
            "computational_chemistry",
            r"\b(?:dft|density functional|transition[- ]state calculation|computational stud|calculated barrier|free[- ]energy profile)\b",
        ),
        (
            "catalyst_state_or_speciation",
            r"\b(?:xps|xanes|exafs|epr|nmr titration|mass spectrometr|catalyst speciation|oxidation state|resting state)\b",
        ),
        (
            "stereochemical_assignment",
            r"\b(?:absolute configuration|x[- ]ray|single[- ]crystal|ecd|vcd|mosher|stereochemical assignment|\br[_ ]?a\b|\bs[_ ]?a\b)\b",
        ),
        (
            "author_proposed_mechanism_only",
            r"\b(?:proposed mechanism|plausible mechanism|mechanism is proposed|may proceed via|was suggested to proceed)\b",
        ),
    )
    return [label for label, pattern in patterns if re.search(pattern, text, re.I)]
