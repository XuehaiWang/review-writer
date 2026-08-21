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
import re
from collections import Counter
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
    years = sorted(
        year
        for row in rows
        if isinstance(row, dict)
        if (year := _year(row.get("year"))) is not None
    )
    defaults: dict[str, Any] = {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "topic": review_topic,
        "review_type": "bounded_evidence_synthesis",
        "target_question": (
            f"How should the evidence on {review_topic} be organized and compared, "
            "and which conclusions and limitations are supported across studies?"
        ),
        "review_objective": (
            f"Build an evidence-grounded field map of {review_topic} using {axis_label.casefold()}, "
            "then identify transferable findings, boundaries and testable research directions."
        ),
        "time_span": {
            "from": years[0] if years else None,
            "to": years[-1] if years else None,
            "basis": "selected_matrix",
        },
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
    years = [_year(row.get("year")) for row in rows]
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
    return {
        "schema_version": ACADEMIC_SCHEMA_VERSION,
        "coverage_claim": "selected_corpus_only",
        "global_coverage_percentage": None,
        "selected_paper_count": len(rows),
        "search_cutoff_date": _text(scope.get("search_cutoff_date")) or None,
        "year_distribution": dict(sorted(year_counts.items())),
        "year_unknown_count": len(rows) - len(known_years),
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
        ],
    }


def taxonomy_diagnostics(
    sections: Iterable[dict[str, Any]],
    matrix_paper_ids: Iterable[Any],
) -> dict[str, Any]:
    """Diagnose structural blockers without modifying a user outline."""

    matrix_ids = _unique(matrix_paper_ids)
    matrix_set = set(matrix_ids)
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(sections, start=1):
        if not isinstance(source, dict):
            continue
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
    contextual = _unique(
        paper_id for item in normalized for paper_id in item["context_paper_ids"]
    )
    routed_ids = set(assigned) | set(contextual)
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
            for paper_id in [*item["paper_ids"], *item["context_paper_ids"]]
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
        "orphan_paper_ids": orphan_ids,
        "assigned_paper_count": len(set(assigned) & matrix_set),
        "contextual_paper_count": len(set(contextual) & matrix_set),
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
