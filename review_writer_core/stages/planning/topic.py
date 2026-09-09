"""Pure topic classification and routing rules for Planning."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from review_writer_core.classification_axes import (
    CLASSIFICATION_CONTRACT_VERSION,
    canonical_classification_contract,
    classification_contract_from_document,
    normalize_classification_axes_semantics,
)
from review_writer_core.review_fact_readiness import required_fact_roles
from review_writer_core.review_structure import infer_section_role


_PATH_ONLY_FACT_CANDIDATE = re.compile(
    r"^\s*(?:(?:images?|figures?|assets?)[/\\])?[^\r\n]+\.(?:png|jpe?g|webp|gif|svg)\s*$",
    re.IGNORECASE,
)


TOPIC_GUIDED_STYLE = "topic-guided"
TOPIC_AXIS_LABELS: dict[str, dict[str, str]] = {
    "reaction_type": {"en": "reaction type", "zh": "反应类型"},
    "stereochemical_regime": {
        "en": "stereochemical regime",
        "zh": "立体化学模式",
    },
    "catalyst_or_method": {
        "en": "catalytic or promoting system",
        "zh": "催化或促进体系",
    },
    "substrate": {"en": "substrate class", "zh": "底物类别"},
    "product": {"en": "product class", "zh": "产物类别"},
    "organometallic_partner": {
        "en": "organometallic partner",
        "zh": "金属有机试剂",
    },
    "ligand_or_chiral_source": {
        "en": "ligand or chiral source",
        "zh": "配体或手性来源",
    },
    "leaving_group": {"en": "leaving-group class", "zh": "离去基团类别"},
    "document_scope": {"en": "evidence or document type", "zh": "证据或文献类型"},
}
TOPIC_AXIS_ALIASES: dict[str, tuple[str, ...]] = {
    "reaction_type": (
        "reaction type",
        "reaction types",
        "transformation type",
        "mechanistic strategy",
        "反应类型",
        "转化类型",
    ),
    "stereochemical_regime": (
        "stereochemical regime",
        "stereochemical mode",
        "racemic versus enantioselective",
        "racemic and enantioselective",
        "立体化学模式",
        "消旋与不对称合成",
    ),
    "catalyst_or_method": (
        "catalytic/promoting system",
        "catalytic or promoting system",
        "catalytic system",
        "promoting system",
        "catalyst and method",
        "catalyst",
        "catalysts",
        "催化/促进体系",
        "催化或促进体系",
        "催化体系",
        "促进体系",
    ),
    "substrate": (
        "substrate class",
        "substrate type",
        "different substrates",
        "substrate",
        "substrates",
        "底物类别",
        "底物类型",
        "不同底物",
    ),
    "product": (
        "product class",
        "product type",
        "products",
        "product",
        "产物类别",
        "产物类型",
        "产物",
    ),
    "organometallic_partner": (
        "organometallic partner",
        "organometallic reagent",
        "metal-organic reagent",
        "金属有机试剂",
        "有机金属试剂",
    ),
    "ligand_or_chiral_source": (
        "ligand or chiral source",
        "chiral source",
        "ligands",
        "ligand",
        "配体或手性来源",
        "手性来源",
        "配体",
    ),
    "leaving_group": (
        "leaving group",
        "leaving groups",
        "离去基团",
    ),
    "document_scope": (
        "document type",
        "document types",
        "document scope",
        "evidence type",
        "文献类型",
        "文献范围",
        "证据类型",
    ),
}
TOPIC_PARTITION_BOUNDARY_LABEL = "Topic-partition boundary cases"
_TOPIC_MATCH_STOPWORDS = {
    "and",
    "or",
    "the",
    "of",
    "for",
    "review",
    "evidence",
    "method",
    "methods",
    "study",
    "studies",
}


def _clean_topic_partition(value: Any) -> str:
    label = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,;:.-")
    label = re.sub(r"^(?:the|a|an)\s+", "", label, flags=re.I)
    return label[:100]


def _topic_partitions(topic: str) -> list[str]:
    text = str(topic or "")
    match = re.search(
        r"\b(?:separately\s+discuss|discuss\s+separately|separate\s+discussion\s+of)\s+"
        r"(.{3,220}?)(?=[.;]|$)",
        text,
        re.I,
    )
    if match:
        values = re.split(r"\s+(?:and|versus|vs\.?)\s+|\s*[、；]\s*", match.group(1), flags=re.I)
    else:
        chinese = re.search(r"分别(?:讨论|比较|分析)\s*(.{3,160}?)(?=[。；;]|$)", text)
        values = re.split(r"\s*(?:与|和|及|、)\s*", chinese.group(1)) if chinese else []
    return list(
        dict.fromkeys(
            label
            for value in values
            if 2 <= len(label := _clean_topic_partition(value)) <= 100
        )
    )[:4]


def _matrix_classification_axes(
    matrix: dict[str, Any],
    topic_partitions: list[str],
) -> list[dict[str, Any]]:
    # Runtime coverage/recommendation fields are derived from the current
    # Matrix. They must not become extraction inputs or every successful
    # refresh would change its own source fingerprint and trigger another run.
    source_contract = classification_contract_from_document(
        matrix,
        primary_axis_hint=str(
            (matrix.get("classification_recommendation") or {}).get(
                "primary_axis_id"
            )
            or ""
        ),
        source="matrix_evidence_contract",
    )
    axes = [
        {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in {"evidence_coverage", "role_status"}
        }
        for item in source_contract.get("axes") or []
        if isinstance(item, dict)
        and str(item.get("axis_id") or "").strip()
        and isinstance(item.get("partitions"), list)
    ]
    if axes and any(axis.get("partitions") for axis in axes):
        return normalize_classification_axes_semantics(axes)[:4]
    if not topic_partitions:
        return normalize_classification_axes_semantics(axes)[:4]
    return normalize_classification_axes_semantics([
        *axes,
        {
            "axis_id": "topic_independent_partition",
            "label": "Topic-requested independent discussion",
            "source_surface": "separately discussed Topic partitions",
            "source_type": "explicit_topic",
            "axis_role": "required_independent_discussion",
            "role_status": "explicit",
            "mutual_exclusivity": "partially_overlapping",
            "heading_requirement": "secondary_heading",
            "partitions": [
                {
                    "partition_id": re.sub(
                        r"[^a-z0-9_]+", "_", label.casefold()
                    ).strip("_")
                    or f"partition_{index:02d}",
                    "label": label,
                    "aliases": [label],
                    "positive_discriminators": [label],
                    "negative_or_ambiguous_signals": [],
                }
                for index, label in enumerate(topic_partitions, start=1)
            ],
        }
    ])


def _matrix_required_fact_roles(
    review_topic: Any,
    classification_axes: list[dict[str, Any]] | None,
) -> list[str]:
    """Derive one fact-role contract shared by retrieval and publication."""

    axis_requirement_text: list[Any] = []
    for axis in classification_axes or []:
        if not isinstance(axis, dict):
            continue
        axis_requirement_text.append(axis.get("label"))
        axis_requirement_text.extend(
            partition.get("label")
            for partition in axis.get("partitions") or []
            if isinstance(partition, dict)
        )
    return required_fact_roles(review_topic, *axis_requirement_text)


def _usable_fact_candidate(content_type: Any, content: Any) -> bool:
    """Reject retrieval hits that contain no human-readable source evidence."""

    kind = str(content_type or "").strip().casefold()
    text = str(content or "").strip()
    if not text or kind in {"header", "footer", "page_number"}:
        return False
    if _PATH_ONLY_FACT_CANDIDATE.fullmatch(text.replace("\\", "/")):
        return False
    return True


def _split_topic_examples(value: Any) -> list[str]:
    values = re.split(
        r"\s*(?:,(?!\s*\d)|，|、|;|；|/|\band\b|\bor\b|以及|及|和)\s*",
        str(value or ""),
        flags=re.I,
    )
    cleaned: list[str] = []
    for raw in values:
        label = re.sub(r"\b(?:etc|and so on)\.?\b", "", raw, flags=re.I)
        label = _clean_topic_partition(label)
        if 1 <= len(label) <= 100 and label not in cleaned:
            cleaned.append(label)
    return cleaned[:16]


def _topic_axis_examples(topic: str, axes: list[str]) -> dict[str, list[str]]:
    """Read parenthetical examples attached to any declared organization axis."""

    text = str(topic or "")
    result: dict[str, list[str]] = {}
    for axis in axes:
        aliases = sorted(TOPIC_AXIS_ALIASES.get(axis, ()), key=len, reverse=True)
        for alias in aliases:
            match = re.search(
                rf"(?<![a-z0-9]){re.escape(alias)}\s*[（(]([^()（）]{{1,240}})[）)]",
                text,
                re.I,
            )
            if not match:
                continue
            examples = _split_topic_examples(match.group(1))
            if examples:
                result[axis] = examples
                break
    return result


def _topic_focus_dimensions(topic: str) -> list[str]:
    """Keep an explicit focus clause as coverage context without naming its science."""

    match = re.search(
        r"(?:\bfocus(?:ing|ed)?\s+on\b|\bwith\s+emphasis\s+on\b|重点关注|聚焦于?)\s*"
        r"(.{3,240}?)(?=\b(?:organize|organise|categorize|categorise|separately\s+discuss)\b|[.;。；]|$)",
        str(topic or ""),
        re.I,
    )
    if not match:
        return []
    value = re.sub(r"\s+", " ", match.group(1)).strip(" ,;:.-")
    return [value] if value else []


def _topic_outline_intent(
    topic: Any,
    discovery: dict[str, Any] | None,
    classification_axes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract explicit organization instructions already present in the Topic.

    Discovery's validated query plan is the primary source because it has
    already resolved user terminology. Deterministic text parsing is only a
    fallback and does not invent a disciplinary taxonomy.
    """

    text = str(topic or "").strip()
    query_plan = (
        dict((discovery or {}).get("query_plan") or {})
        if isinstance(discovery, dict)
        else {}
    )
    raw_contract_axes = normalize_classification_axes_semantics([
        deepcopy(axis)
        for axis in (
            classification_axes
            if classification_axes is not None
            else query_plan.get("classification_axes") or []
        )
        if isinstance(axis, dict)
        and str(axis.get("axis_id") or "")
        and str(axis.get("axis_role") or "") != "scope_filter"
    ])
    declared_groups = [
        str(axis)
        for axis in query_plan.get("group_by") or []
        if str(axis).strip()
    ]
    for index, axis_id in enumerate(declared_groups):
        if any(
            str(axis.get("axis_id") or "") == axis_id
            for axis in raw_contract_axes
        ):
            continue
        raw_contract_axes.append(
            {
                "axis_id": axis_id,
                "label": str(
                    (TOPIC_AXIS_LABELS.get(axis_id) or {}).get("en")
                    or axis_id.replace("_", " ").title()
                ),
                "source_surface": axis_id,
                "source_type": "agent_recommended",
                "axis_role": (
                    "primary_organization"
                    if index == 0
                    else "comparison_dimension"
                ),
                "heading_requirement": (
                    "primary_heading" if index == 0 else "comparison_only"
                ),
                "mutual_exclusivity": "partially_overlapping",
                "partitions": [],
            }
        )
    primary_hint = declared_groups[0] if declared_groups else ""
    classification_contract = canonical_classification_contract(
        raw_contract_axes,
        primary_axis_hint=primary_hint,
        source=(
            "matrix_evidence_classification_contract"
            if classification_axes is not None
            else "validated_query_plan_and_topic"
        ),
    )
    contract_axes = list(classification_contract["axes"])
    # A provider can encode two academic instructions in one contract:
    # "organize by reaction type" plus "separately discuss racemic versus
    # enantioselective". Fact extraction correctly repairs the latter to a
    # stereochemical axis, but that repair must not replace the requested
    # reaction-type hierarchy. Split the dimensions only for outline planning.
    repaired_primary = next(
        (
            axis
            for axis in contract_axes
            if str(axis.get("axis_role") or "") == "primary_organization"
        ),
        None,
    )
    reaction_type_requested = "reaction_type" in {
        str(axis) for axis in query_plan.get("group_by") or []
    } or bool(
        re.search(
            r"(?:organiz(?:e|ed|ation)|group(?:ed|ing)?|classif(?:y|ied|ication))"
            r".{0,80}\breaction\s+types?\b|(?:按照|按)\s*反应(?:种类|类型)",
            " ".join(
                str((repaired_primary or {}).get(key) or "")
                for key in ("source_surface", "recommendation_rationale")
            ),
            re.I,
        )
    )
    if (
        repaired_primary is not None
        and str(repaired_primary.get("axis_id") or "")
        == "stereochemical_regime"
        and isinstance(repaired_primary.get("semantic_repair"), dict)
        and reaction_type_requested
        and not any(
            str(axis.get("axis_id") or "") == "reaction_type"
            for axis in contract_axes
        )
    ):
        stereochemical = deepcopy(repaired_primary)
        stereochemical["axis_role"] = "required_independent_discussion"
        stereochemical["heading_requirement"] = "secondary_heading"
        reaction_axis = {
            "axis_id": "reaction_type",
            "label": "Reaction type",
            "source_surface": str(
                repaired_primary.get("source_surface") or "reaction type"
            ),
            "source_type": str(
                repaired_primary.get("source_type") or "explicit_topic"
            ),
            "axis_role": "primary_organization",
            "mutual_exclusivity": "partially_overlapping",
            "heading_requirement": "primary_heading",
            "recommendation_rationale": (
                "Preserve the Topic-requested reaction-type hierarchy while "
                "treating stereochemical regime as an independent discussion axis."
            ),
            "partitions": [],
            "semantic_split": {
                "status": "auto_split",
                "source_axis_id": "stereochemical_regime",
                "reason": (
                    "Reaction type controls the outline hierarchy; racemic versus "
                    "enantioselective evidence remains a separate stereochemical axis."
                ),
            },
        }
        contract_axes = [
            reaction_axis,
            stereochemical,
            *[axis for axis in contract_axes if axis is not repaired_primary],
        ]
    primary_contract = next(
        (
            axis
            for axis in contract_axes
            if str(axis.get("axis_role") or "") == "primary_organization"
        ),
        contract_axes[0] if contract_axes else None,
    )
    secondary_contracts = [
        axis
        for axis in contract_axes
        if axis is not primary_contract
        and str(axis.get("axis_role") or "")
        in {"required_independent_discussion", "comparison_dimension"}
    ]
    axes = [
        str(primary_contract.get("axis_id") or "")
        if primary_contract is not None
        else "",
        *[str(axis.get("axis_id") or "") for axis in secondary_contracts],
    ]
    axes = [axis for axis in axes if axis]
    if not axes:
        axes = [
            str(axis)
            for axis in query_plan.get("group_by") or []
            if str(axis) in TOPIC_AXIS_LABELS
        ]
    if not axes:
        lowered = text.casefold()
        positioned: list[tuple[int, str]] = []
        for axis, aliases in TOPIC_AXIS_ALIASES.items():
            positions = [lowered.find(alias.casefold()) for alias in aliases]
            valid = [position for position in positions if position >= 0]
            if valid:
                positioned.append((min(valid), axis))
        axes = [axis for _position, axis in sorted(positioned)]
    axes = list(dict.fromkeys(axes))[:3]
    contract_partitions = [
        _clean_topic_partition(partition.get("label"))
        for axis in secondary_contracts
        if str(axis.get("axis_role") or "") == "required_independent_discussion"
        for partition in axis.get("partitions") or []
        if isinstance(partition, dict)
        and _clean_topic_partition(partition.get("label"))
    ]
    partitions = list(dict.fromkeys(contract_partitions or _topic_partitions(text)))
    axis_examples = _topic_axis_examples(text, axes)
    comparison_dimensions = list(dict.fromkeys([
        *[
            _clean_topic_partition(partition.get("label"))
            for axis in secondary_contracts
            if str(axis.get("axis_role") or "") == "comparison_dimension"
            for partition in axis.get("partitions") or []
            if isinstance(partition, dict)
            and _clean_topic_partition(partition.get("label"))
        ],
        *[
            value
            for axis in axes[1:]
            for value in axis_examples.get(axis, [])
        ],
    ]))
    focus_dimensions = _topic_focus_dimensions(text)
    available = bool(contract_axes or axes or len(partitions) >= 2)
    primary_axis = axes[0] if axes else "reaction_type"
    primary_axis_label = str(
        (primary_contract or {}).get("label")
        or (TOPIC_AXIS_LABELS.get(primary_axis) or {}).get("en")
        or primary_axis.replace("_", " ")
    )
    return {
        "available": available,
        "source": (
            "matrix_evidence_classification_contract"
            if classification_axes is not None and contract_axes
            else "validated_query_plan_and_topic"
            if query_plan
            else "topic_text"
        ),
        "primary_axis": primary_axis,
        "primary_axis_label": primary_axis_label,
        "secondary_axes": axes[1:],
        "secondary_axis_labels": {
            str(axis.get("axis_id") or ""): str(axis.get("label") or "")
            for axis in secondary_contracts
        },
        "classification_axes": contract_axes,
        "classification_contract": classification_contract,
        "classification_contract_version": CLASSIFICATION_CONTRACT_VERSION,
        "system_recommended": bool(
            primary_contract is not None
            and str(primary_contract.get("source_type") or "") == "agent_recommended"
        ),
        # Only an explicit instruction to discuss categories separately creates
        # an outline-trace requirement.  Named systems and product outcomes are
        # comparison/coverage dimensions; requiring each of them to become a
        # chapter would turn a multi-dimensional Topic into a contradictory
        # flat taxonomy.
        "required_partitions": partitions,
        "partitions": partitions,
        "axis_examples": axis_examples,
        "comparison_dimensions": comparison_dimensions,
        "focus_dimensions": focus_dimensions,
        # Compatibility fields keep existing saved frontend payloads readable.
        # Their values now come only from general axis/focus parsing.
        "named_systems": comparison_dimensions,
        "requested_outcomes": focus_dimensions,
        "outcome_dimensions": focus_dimensions,
        "partition_trace_policy": "source_bounded_model_or_section_contract",
    }


def _basis_with_axis_contract(
    basis: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Attach the canonical academic axes to the legacy diagnostics basis."""

    updated = deepcopy(basis)
    axes = [
        deepcopy(axis)
        for axis in contract.get("axes") or []
        if isinstance(axis, dict) and str(axis.get("axis_id") or "")
    ]
    primary_axis = str(contract.get("primary_axis_id") or "")
    if primary_axis:
        updated["primary_axis"] = primary_axis
        updated["overview_axis"] = primary_axis
    secondary = [
        str(axis.get("axis_id") or "")
        for axis in axes
        if str(axis.get("axis_id") or "") != primary_axis
    ]
    updated["orthogonal_axes"] = list(
        dict.fromkeys([*(updated.get("orthogonal_axes") or []), *secondary])
    )
    updated["overview_secondary_axes"] = list(
        dict.fromkeys(
            [*(updated.get("overview_secondary_axes") or []), *secondary]
        )
    )
    updated["axis_contract_version"] = int(
        contract.get("contract_version") or CLASSIFICATION_CONTRACT_VERSION
    )
    updated["axis_contract_fingerprint"] = str(
        contract.get("fingerprint") or ""
    )
    updated["required_route_axis_ids"] = list(
        contract.get("required_route_axis_ids") or []
    )
    updated["section_partition_policy"] = str(
        contract.get("section_partition_policy") or "single_primary_axis"
    )
    updated["minimum_body_papers"] = int(
        contract.get("minimum_body_papers") or 2
    )
    updated["single_paper_section_policy"] = str(
        contract.get("single_paper_section_policy")
        or "merge_unless_scientifically_justified"
    )
    updated["classification_axes"] = axes
    return updated


def _topic_partition_for_text(text: str, partitions: list[str]) -> str:
    if not partitions:
        return ""
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", text.casefold())
    token_sets = [
        {
            term
            for term in re.findall(r"[a-z0-9\u3400-\u9fff]{3,}", label.casefold())
            if term not in _TOPIC_MATCH_STOPWORDS
        }
        for label in partitions
    ]
    common_terms = (
        set.intersection(*token_sets)
        if len(token_sets) > 1 and all(token_sets)
        else set()
    )
    ranked: list[tuple[int, int, str]] = []
    for index, label in enumerate(partitions):
        parenthetical = re.findall(r"[（(]([^()（）]{2,40})[）)]", label)
        base = re.sub(r"\s*[（(][^()（）]{2,40}[）)]\s*", " ", label)
        aliases = [base, *parenthetical]
        exact_score = max(
            (
                len(alias_normalized.split()) * 20
                for alias in aliases
                if (
                    alias_normalized := re.sub(
                        r"[^a-z0-9\u3400-\u9fff]+",
                        " ",
                        alias.casefold(),
                    ).strip()
                )
                and re.search(
                    rf"(?:^|\s){re.escape(alias_normalized)}(?:$|\s)",
                    normalized,
                )
            ),
            default=0,
        )
        specific_terms = token_sets[index] - common_terms
        term_score = sum(
            10
            for term in specific_terms
            if re.search(rf"(?:^|\s){re.escape(term)}(?:$|\s)", normalized)
        )
        ranked.append((max(exact_score, term_score), -index, label))
    best = max(ranked, default=(0, 0, ""))
    tied = sum(1 for score, _index, _label in ranked if score == best[0]) > 1
    return best[2] if best[0] > 0 and not tied else ""


def _canonical_declared_partition(value: Any, partitions: list[str]) -> str:
    normalized = _clean_topic_partition(value).casefold()
    if not normalized:
        return ""
    for label in partitions:
        aliases = [
            label,
            re.sub(r"\s*[（(][^()（）]{2,40}[）)]\s*", " ", label).strip(),
            *re.findall(r"[（(]([^()（）]{2,40})[）)]", label),
        ]
        if any(
            normalized == _clean_topic_partition(alias).casefold()
            for alias in aliases
            if _clean_topic_partition(alias)
        ):
            return label
    return ""


def _topic_partition_for_row(
    row: dict[str, Any],
    partitions: list[str],
    fallback_text: str,
) -> str:
    """Use source-bound model classification before conservative text matching."""

    formal_matches: list[tuple[float, str]] = []
    formal_tags = row.get("evidence_backed_tags") or {}
    if isinstance(formal_tags, dict):
        for values in formal_tags.values():
            for tag in values or []:
                if not isinstance(tag, dict):
                    continue
                label = _canonical_declared_partition(
                    tag.get("partition_label"), partitions
                )
                try:
                    confidence = float(tag.get("confidence") or 0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if (
                    label
                    and confidence >= 0.75
                    and bool(tag.get("fact_ids"))
                    and bool(tag.get("evidence_refs"))
                ):
                    formal_matches.append((confidence, label))
    if formal_matches:
        formal_matches.sort(reverse=True)
        best_confidence = formal_matches[0][0]
        best_labels = list(
            dict.fromkeys(
                label
                for confidence, label in formal_matches
                if confidence == best_confidence
            )
        )
        return best_labels[0] if len(best_labels) == 1 else ""

    classification = row.get("topic_partition_classification")
    if isinstance(classification, dict):
        status = str(classification.get("status") or "").casefold()
        if status == "classified":
            label = _canonical_declared_partition(
                classification.get("partition"), partitions
            )
            try:
                confidence = float(classification.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            if (
                label
                and confidence >= 0.75
                and bool(classification.get("evidence_refs"))
            ):
                return label
            return ""
        if status in {
            "boundary",
            "insufficient_evidence",
            "cross_category",
            "out_of_scope",
        }:
            # A completed evidence-bound model pass explicitly found no safe
            # route. Do not overrule it with a keyword appearing in related
            # work, a caption, or an unsupported negative inference.
            return ""
    return _topic_partition_for_text(fallback_text, partitions)


def _required_topic_partitions_from_outline(
    outline: dict[str, Any],
) -> list[str]:
    """Read independent-discussion partitions from current and legacy outlines."""

    intent = (
        outline.get("topic_outline_intent")
        if isinstance(outline.get("topic_outline_intent"), dict)
        else {}
    )
    basis = (
        outline.get("classification_basis")
        if isinstance(outline.get("classification_basis"), dict)
        else {}
    )
    values: list[Any] = [
        *(intent.get("required_partitions") or []),
        *(basis.get("required_outline_partitions") or []),
        *(basis.get("topic_partitions") or []),
    ]
    contract = (
        outline.get("classification_contract")
        if isinstance(outline.get("classification_contract"), dict)
        else {}
    )
    for axis in contract.get("axes") or []:
        if not isinstance(axis, dict) or str(axis.get("axis_role") or "") != (
            "required_independent_discussion"
        ):
            continue
        values.extend(
            partition.get("label")
            for partition in axis.get("partitions") or []
            if isinstance(partition, dict)
        )
    return list(
        dict.fromkeys(
            label
            for value in values
            if (label := _clean_topic_partition(value))
        )
    )


def _topic_partition_routes(
    sections: list[dict[str, Any]],
    rows_by_id: dict[str, dict[str, Any]],
    partitions: list[str],
    text_by_paper: dict[str, str],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Resolve evidence-backed Topic partitions into body-section contracts.

    The primary outline hierarchy is left untouched. A secondary partition is
    traceable when a source-supported Matrix classification points to a paper
    owned by that body section. The second return value retains Matrix-wide
    support so a genuine scope/routing boundary can be distinguished from a
    missing classification.
    """

    supported_papers: dict[str, list[str]] = {
        partition: [] for partition in partitions
    }
    partition_by_paper: dict[str, str] = {}
    for paper_id, row in rows_by_id.items():
        partition = _topic_partition_for_row(
            row,
            partitions,
            text_by_paper.get(paper_id, ""),
        )
        if not partition:
            continue
        partition_by_paper[paper_id] = partition
        supported_papers.setdefault(partition, []).append(paper_id)

    routes: dict[str, dict[str, list[str]]] = {}
    for section in sections:
        if infer_section_role(
            section.get("title"), section.get("section_role")
        ) != "body":
            continue
        section_id = str(section.get("section_id") or "").strip()
        if not section_id:
            continue
        section_routes: dict[str, list[str]] = {}
        for paper_id in (
            section.get("primary_papers")
            or section.get("paper_ids")
            or section.get("major_papers")
            or []
        ):
            normalized_paper_id = str(paper_id or "").strip()
            partition = partition_by_paper.get(normalized_paper_id, "")
            if partition:
                section_routes.setdefault(partition, []).append(normalized_paper_id)
        if section_routes:
            routes[section_id] = {
                partition: list(dict.fromkeys(paper_ids))
                for partition, paper_ids in section_routes.items()
            }
    return routes, {
        partition: list(dict.fromkeys(paper_ids))
        for partition, paper_ids in supported_papers.items()
    }
