"""PostgreSQL-native Matrix, outline, and Blueprint workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.credentials import (
    ProviderKind,
    ProviderSettingsError,
    ProviderSettingsService,
)
from review_writer_api.domain_services.library_index import LibraryIndexService
from review_writer_api.database import database_session, utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.scientific_runner import (
    SENSITIVE_ENVIRONMENT_KEY,
    ScientificRunner,
)
from review_writer_api.workflow_models import LibraryPaper, WorkflowJob
from review_writer_api.workflow_repository import ArtifactRecord, JobRecord, WorkflowRepository
from review_writer_core.taxonomy import TaxonomyConfigurationError, load_taxonomy_rules
from review_writer_core.metadata_tags import verified_structured_tags
from review_writer_core.bibliography_audit import bibliography_candidates
from review_writer_core.academic_contracts import (
    ACADEMIC_SCHEMA_VERSION,
    classification_basis,
    coverage_diagnostics,
    derive_scope_contract,
    section_academic_contract,
    scope_diagnostics,
    synthesis_requirements,
    taxonomy_diagnostics,
    evidence_key as academic_evidence_key,
)
from review_writer_core.evidence_queries import (
    COMPARISON_FIELD_IDS,
    build_question_query_plans,
)
from review_writer_core.review_structure import (
    assign_primary_paper_sections,
    infer_section_role,
    publication_section_title,
    sanitize_internal_section_title,
)
from review_writer_core.classification_axes import (
    CLASSIFICATION_CONTRACT_VERSION,
    canonical_classification_contract,
    classification_contract_from_document,
    normalize_classification_axes_semantics,
)


MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
OUTLINE_LOGICAL_NAME = "planning/selected_outline.json"
REFERENCE_INDEX_LOGICAL_NAME = "planning/reference_outlines.json"
BLUEPRINT_LOGICAL_NAME = "blueprint/section_blueprint.json"
DISCOVERY_LOGICAL_NAME = "discovery/review.json"
ROUTING_REQUIRED_LABEL = "Routing required — reassign these papers"
CROSS_CATEGORY_BOUNDARY_LABEL = "Cross-category evidence and boundary cases"
# Bump this whenever retrieval/query or source-validation semantics change.
# It is part of every per-paper fingerprint, so previously cached facts are
# re-extracted once under the new scientific contract.
MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION = 7


def _planning_job_payload(job: JobRecord) -> dict[str, Any]:
    actions: list[str] = []
    if job.status in {"queued", "running", "cancel_requested"}:
        actions.append("cancel")
    if job.status in {"failed", "cancelled", "interrupted"}:
        actions.append("retry")
    return {
        "id": job.id,
        "project_id": job.project_id,
        "scope": job.scope,
        "status": job.status,
        "job_type": job.job_type,
        "result": job.result,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "cancellation_requested": job.cancellation_requested,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "retry_of_job_id": job.retry_of_job_id,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "available_actions": actions,
    }

OUTLINE_STYLES: dict[str, dict[str, str]] = {
    "substrate": {
        "en": "Substrate-classified",
        "zh": "按底物分类",
        "axis": "substrate classes and scope",
        "tag_key": "substrate",
        "introduction": "define the review scope and explain why substrate class is the primary comparison axis",
    },
    "catalyst": {
        "en": "Catalyst and method-classified",
        "zh": "按催化剂与方法分类",
        "axis": "catalysts, methods, and operating principles",
        "tag_key": "catalyst_or_method",
        "introduction": "compare how catalysts or methods shape outcomes, evidence quality, and applicability",
    },
    "reaction": {
        "en": "Reaction-type-classified",
        "zh": "按反应类型分类",
        "axis": "transformation and mechanistic strategy",
        "tag_key": "reaction_type",
        "introduction": "organize the literature by transformation logic and mechanistic strategy",
    },
    "topic-guided": {
        "en": "Topic-guided hybrid",
        "zh": "按 Topic 要求组织",
        "axis": "the explicit organization instructions in the user topic",
        "tag_key": "reaction_type",
        "introduction": "define the review scope and explain the organization requested in the topic",
    },
}

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


def _capitalize_outline_heading(value: Any) -> str:
    """Capitalize a generated heading without title-casing chemical names."""

    heading = str(value or "").strip()
    match = re.search(r"[A-Za-z]", heading)
    if not match:
        return heading
    index = match.start()
    return heading[:index] + heading[index].upper() + heading[index + 1 :]


def _sanitize_outline_markdown_headings(markdown: Any) -> str:
    """Sanitize only generated heading text while preserving outline metadata."""

    heading = re.compile(
        r"(?m)^(\s*#{1,6}\s+)(\d+(?:\.\d+)*[.)]?\s+)?(.+?)\s*$"
    )

    def replace(match: re.Match[str]) -> str:
        return (
            f"{match.group(1)}{match.group(2) or ''}"
            f"{sanitize_internal_section_title(match.group(3))}"
        )

    return heading.sub(replace, str(markdown or ""))


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
    if axes:
        return normalize_classification_axes_semantics(axes)[:4]
    if not topic_partitions:
        return []
    return normalize_classification_axes_semantics([
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


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _paper_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("paper_id")) for row in rows if str(row.get("paper_id") or "").strip()]


def _publication_year(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    match = re.search(r"(?:18|19|20|21)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _matrix_publication_year(row: dict[str, Any]) -> int | None:
    return (
        _publication_year(row.get("first_publication_date"))
        or _publication_year(row.get("bibliographic_year"))
        or _publication_year(row.get("year"))
    )


def _outline_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^##\s+(?:\d+[.)]\s*)?(.+?)\s*$", line)
        if heading:
            title = heading.group(1).strip()
            current = {
                "title": title,
                "paper_ids": [],
                "context_paper_ids": [],
                "section_role": infer_section_role(title),
                "purpose": "",
                "notes": "",
                "topic_partition": "",
                "boundary_rationale": "",
            }
            sections.append(current)
            continue
        if current is not None and line.casefold().startswith("section role:"):
            role = line.split(":", 1)[1].strip().casefold()
            if role in {"introduction", "body", "conclusion", "references"}:
                current["section_role"] = role
            continue
        if current is not None and line.casefold().startswith("assigned papers:"):
            assigned = line.split(":", 1)[1].strip().rstrip(".。")
            current["paper_ids"] = list(
                dict.fromkeys(
                    paper_id.strip()
                    for paper_id in re.split(r"[,，;；]", assigned)
                    if paper_id.strip()
                )
            )
            continue
        if current is not None and re.match(
            r"^(?:context|contextual) papers:", line, re.I
        ):
            assigned = line.split(":", 1)[1].strip().rstrip(".。")
            current["context_paper_ids"] = list(
                dict.fromkeys(
                    paper_id.strip()
                    for paper_id in re.split(r"[,，;；]", assigned)
                    if paper_id.strip()
                )
            )
            continue
        if current is not None and line.casefold().startswith("purpose:"):
            current["purpose"] = line.split(":", 1)[1].strip()
            continue
        if current is not None and line.casefold().startswith("notes:"):
            current["notes"] = line.split(":", 1)[1].strip()
            continue
        if current is not None and line.casefold().startswith("topic partition:"):
            current["topic_partition"] = line.split(":", 1)[1].strip().rstrip(".。")
            continue
        if current is not None and line.casefold().startswith("boundary rationale:"):
            current["boundary_rationale"] = line.split(":", 1)[1].strip()
    return sections


def _outline_markdown_from_sections(
    sections: list[dict[str, Any]],
    *,
    outline_style: str,
    automatically_adjusted: bool = False,
) -> str:
    """Render parsed sections back to beginner-readable outline Markdown.

    This renderer is used only for an automatically repaired system outline.
    It preserves the section roles, paper assignments, purposes, and notes
    understood by ``_outline_sections`` while removing temporary routing
    placeholders from the Blueprint-facing outline snapshot.
    """

    definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
    lines = ["# Selected Outline", ""]
    if definition:
        lines.extend([f"Primary structure: {definition['en']}.", ""])
    if automatically_adjusted:
        lines.extend(
            [
                "The system automatically routed previously unclassified papers using the current taxonomy and paper evidence.",
                "",
            ]
        )
    body_number = 0
    for section in sections:
        role = str(section.get("section_role") or "body").casefold()
        title = str(section.get("title") or "").strip()
        if not title:
            continue
        if role == "body":
            body_number += 1
            heading = f"## {body_number}. {title}"
        else:
            heading = f"## {title}"
        lines.extend([heading, f"Section role: {role}"])
        paper_ids = list(dict.fromkeys(section.get("paper_ids") or []))
        if paper_ids:
            lines.append(f"Assigned papers: {', '.join(paper_ids)}.")
        context_ids = list(dict.fromkeys(section.get("context_paper_ids") or []))
        if context_ids:
            lines.append(f"Context papers: {', '.join(context_ids)}.")
        purpose = str(section.get("purpose") or "").strip()
        if purpose:
            lines.append(f"Purpose: {purpose}")
        topic_partition = str(section.get("topic_partition") or "").strip()
        if topic_partition:
            lines.append(f"Topic partition: {topic_partition}.")
        boundary_rationale = str(section.get("boundary_rationale") or "").strip()
        if boundary_rationale:
            lines.append(f"Boundary rationale: {boundary_rationale}")
        notes = str(section.get("notes") or "").strip()
        if notes:
            lines.append(f"Notes: {notes}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _blueprint_restructure_record(
    previous: dict[str, Any] | None,
    current_sections: list[dict[str, Any]],
    *,
    previous_artifact_id: str = "",
    trigger_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Describe a structure change without erasing the prior Blueprint.

    Section IDs are positional and can change during regrouping, so the map is
    based on paper overlap first and normalized headings second.  The record is
    intentionally stored inside the new version for audit and rollback UX.
    """

    old_sections = [
        item
        for item in (previous or {}).get("sections") or []
        if isinstance(item, dict)
    ]

    def paper_ids(section: dict[str, Any]) -> set[str]:
        return {
            str(item)
            for item in (
                section.get("primary_papers")
                or section.get("major_papers")
                or section.get("paper_ids")
                or []
            )
            if str(item).strip()
        }

    def heading(section: dict[str, Any]) -> str:
        return re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+",
            " ",
            str(section.get("title") or "").casefold(),
        ).strip()

    mappings: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for old in old_sections:
        old_id = str(old.get("section_id") or "")
        old_papers = paper_ids(old)
        candidates: list[tuple[float, dict[str, Any]]] = []
        for new in current_sections:
            new_papers = paper_ids(new)
            union = old_papers | new_papers
            overlap = len(old_papers & new_papers) / len(union) if union else 0.0
            if heading(old) and heading(old) == heading(new):
                overlap = max(overlap, 1.0)
            candidates.append((overlap, new))
        score, target = max(candidates, key=lambda item: item[0], default=(0.0, {}))
        target_id = str(target.get("section_id") or "") if score > 0 else ""
        if target_id:
            used_targets.add(target_id)
        mappings.append(
            {
                "previous_section_id": old_id,
                "previous_title": str(old.get("title") or ""),
                "current_section_id": target_id or None,
                "current_title": str(target.get("title") or "") if target_id else None,
                "paper_overlap": round(score, 4),
                "migration_action": "reuse_and_revalidate" if target_id else "retire",
            }
        )
    for new in current_sections:
        new_id = str(new.get("section_id") or "")
        if new_id and new_id not in used_targets:
            mappings.append(
                {
                    "previous_section_id": None,
                    "previous_title": None,
                    "current_section_id": new_id,
                    "current_title": str(new.get("title") or ""),
                    "paper_overlap": 0.0,
                    "migration_action": "generate_new",
                }
            )

    old_signature = [
        (heading(item), sorted(paper_ids(item)), str(item.get("section_role") or "body"))
        for item in old_sections
    ]
    new_signature = [
        (heading(item), sorted(paper_ids(item)), str(item.get("section_role") or "body"))
        for item in current_sections
    ]
    return {
        "is_restructure": bool(old_sections) and old_signature != new_signature,
        "previous_blueprint_artifact_id": previous_artifact_id or None,
        "trigger_reasons": list(dict.fromkeys(trigger_reasons or [])),
        "section_mapping": mappings,
        "rollback_supported": bool(previous_artifact_id),
        "created_at": utc_now().isoformat(),
    }


class PlanningService:
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        *,
        scientific_runner: ScientificRunner | None = None,
        provider_settings: ProviderSettingsService | None = None,
        model_gateway: Any | None = None,
        library_index: LibraryIndexService | None = None,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.scientific_runner = scientific_runner
        self.provider_settings = provider_settings
        self.model_gateway = model_gateway
        self.library_index = library_index
        self.root = Path(__file__).resolve().parents[2]
        self._write_lock = threading.RLock()

    def _begin_reference_gateway_job(
        self, principal: Principal, project_id: str, candidate_id: str
    ) -> SimpleNamespace:
        if self.model_gateway is None:
            raise RuntimeError("The internal model gateway is unavailable.")
        job_id = uuid.uuid4()
        now = utc_now()
        with database_session(self.model_gateway.session_factory) as session:
            session.add(
                WorkflowJob(
                    id=job_id,
                    user_id=uuid.UUID(principal.user_id),
                    project_id=uuid.UUID(project_id),
                    scope="project",
                    job_type="planning.reference-analyze",
                    status="running",
                    idempotency_scope_key=f"project:{project_id}",
                    idempotency_key=candidate_id,
                    payload_json={"candidate_id": candidate_id},
                    started_at=now,
                )
            )
        return SimpleNamespace(
            job_id=str(job_id),
            user_id=principal.user_id,
            project_id=project_id,
            job_type="planning.reference-analyze",
        )

    def _finish_reference_gateway_job(
        self, job_id: str, *, succeeded: bool, error_message: str = ""
    ) -> None:
        if self.model_gateway is None:
            return
        with database_session(self.model_gateway.session_factory) as session:
            row = session.get(WorkflowJob, uuid.UUID(job_id))
            if row is None:
                return
            row.status = "succeeded" if succeeded else "failed"
            row.error_code = "" if succeeded else "REFERENCE_ANALYSIS_FAILED"
            row.error_message = "" if succeeded else error_message[:2000]
            row.finished_at = utc_now()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

    @staticmethod
    def _reference_candidate_is_isolated(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False
        firewall = candidate.get("content_firewall")
        return bool(
            candidate.get("analysis_mode") == "ai_style_only_transfer_v2"
            and candidate.get("content_source") == "current_matrix_only"
            and candidate.get("reference_content_reused") is False
            and isinstance(firewall, dict)
            and firewall.get("transfer_received_reference_text") is False
            and firewall.get("all_heading_levels_content_source")
            == "current_matrix_only"
        )

    def _read_json(
        self,
        principal: Principal,
        project_id: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> tuple[dict[str, Any] | None, ArtifactRecord | None]:
        self._owned_project(principal, project_id)
        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            if required:
                raise WorkflowNotFound("Planning artifact not found.")
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The current Planning artifact is unreadable.") from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict("The current Planning artifact is invalid.")
        return payload, artifact

    def _publish_files(
        self,
        principal: Principal,
        project_id: str,
        *,
        stage_id: str,
        files: dict[str, tuple[bytes, str]],
        input_snapshot: dict[str, Any] | None = None,
    ) -> tuple[dict[str, ArtifactRecord], Any]:
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            stage_id,
            status="succeeded",
            input_snapshot=input_snapshot or {},
        )
        staging = self.artifacts.stage_run_directory(
            principal.user_id, project_id, run.id
        )
        published: dict[str, ArtifactRecord] = {}
        for index, (logical_name, (content, artifact_type)) in enumerate(files.items()):
            filename = f"{index:03d}-{Path(logical_name).name}"
            (staging / filename).write_bytes(content)
            published[logical_name] = self.artifacts.publish(
                principal.user_id,
                project_id,
                run.id,
                filename,
                logical_name=logical_name,
                artifact_type=artifact_type,
                producer_stage=stage_id,
                make_current=False,
            )
        return published, run

    def _matrix(self, principal: Principal, project_id: str):
        matrix, artifact = self._read_json(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        rows = matrix.get("rows") if isinstance(matrix, dict) else None
        if not isinstance(rows, list) or not rows:
            raise WorkflowConflict(
                "No literature matrix is available. Confirm Discovery first."
            )
        return matrix, artifact

    def _with_current_bibliography(
        self,
        principal: Principal,
        matrix: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Overlay canonical Library bibliography while retaining Matrix facts.

        Bibliography verification may complete after the Matrix artifact was
        published.  Planning consumes the latest low-risk canonical fields and
        records their immutable metadata artifact IDs in the Blueprint, while
        scientific facts and user Matrix edits continue to come from Matrix.
        """

        updated = deepcopy(matrix)
        rows = [row for row in updated.get("rows") or [] if isinstance(row, dict)]
        paper_ids = [
            str(row.get("paper_id") or "")
            for row in rows
            if str(row.get("paper_id") or "")
        ]
        if not paper_ids:
            return updated, {}
        with database_session(self.repository.session_factory) as session:
            library_rows = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(paper_ids),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
            audit_by_id = {
                row.paper_id: (
                    dict(row.bibliography_audit_row.audit_json or {})
                    if row.bibliography_audit_row is not None
                    else {}
                )
                for row in library_rows
            }
        by_id = {row.paper_id: row for row in library_rows}
        artifact_ids: dict[str, str] = {}
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            library_row = by_id.get(paper_id)
            if library_row is None:
                continue
            metadata = dict(library_row.metadata_json or {})
            for field in (
                "title",
                "authors",
                "year",
                "first_publication_date",
                "bibliographic_year",
                "publication_status",
                "journal",
                "doi",
            ):
                value = metadata.get(field)
                if isinstance(value, dict) and "value" in value:
                    value = value.get("value")
                if value not in (None, "", []):
                    row[field] = deepcopy(value)
            metadata_artifact_id = str(
                (metadata.get("_artifact_ids") or {}).get("metadata") or ""
            )
            if metadata_artifact_id:
                artifact_ids[paper_id] = metadata_artifact_id
            audit = audit_by_id.get(paper_id) or {}
            audit_status = str(audit.get("status") or "not_audited")
            manual_status = str(audit.get("manual_review_status") or "")
            resolution_complete = manual_status in {
                "approved",
                "resolved",
                "verified",
                "supporting_only",
            }
            row["bibliography_identity"] = {
                "status": audit_status,
                "verified": bool(audit_status == "verified" or resolution_complete),
                "manual_review_status": manual_status or "not_reviewed",
                "resolved_by": str(audit.get("resolved_by") or ""),
                "resolved_at": audit.get("resolved_at"),
                "unresolved_conflict_count": len(
                    audit.get("unresolved_conflicts") or []
                ),
                "missing_fields": list(
                    audit.get("automatic_resolution_missing_fields") or []
                ),
                "candidate_count": len(bibliography_candidates(audit)),
                "verification_method": str(
                    audit.get("verification_method") or ""
                ),
                "bibliography_role": str(
                    audit.get("bibliography_role") or "primary"
                ),
                "direct_claim_eligible": bool(
                    audit.get("direct_claim_eligible", True)
                ),
                "context_only": bool(audit.get("context_only", False)),
                "parent_paper_id": str(audit.get("parent_paper_id") or ""),
            }
        updated["bibliography_overlay"] = {
            "source_metadata_artifact_ids": artifact_ids,
            "applied_at": utc_now().isoformat(),
        }
        return updated, artifact_ids

    @staticmethod
    def _matrix_abstract(row: dict[str, Any]) -> str:
        abstract = row.get("abstract")
        if isinstance(abstract, dict):
            abstract = abstract.get("value")
        normalized = " ".join(str(abstract or "").split()).strip()
        return "" if "unavailable or unreliable" in normalized.casefold() else normalized

    def matrix_enrichment_payload(
        self,
        principal: Principal,
        project_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Prepare source-addressable fact candidates for an asynchronous job."""

        principal.require(Permission.PROJECT_WRITE)
        project = self._owned_project(principal, project_id)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        paper_ids = _paper_ids(rows)
        if (
            self.library_index is not None
            and self.library_index.enabled
            and bool(getattr(self.library_index, "vector_enabled", False))
        ):
            self.library_index.ensure_embeddings(principal, paper_ids)
        summaries = (
            self.library_index.summaries(principal, paper_ids)
            if self.library_index is not None and self.library_index.enabled
            else {}
        )
        topic = str(matrix.get("review_topic") or "")
        topic_partitions = _topic_partitions(topic)
        classification_axes = _matrix_classification_axes(matrix, topic_partitions)
        classification_contract = canonical_classification_contract(
            classification_axes,
            primary_axis_hint=str(
                (matrix.get("classification_recommendation") or {}).get(
                    "primary_axis_id"
                )
                or (matrix.get("classification_contract") or {}).get(
                    "primary_axis_id"
                )
                or ""
            ),
            source="matrix_fact_extraction",
        )
        classification_axes = list(classification_contract["axes"])
        routing_axis_id = str(
            classification_contract.get("primary_axis_id")
            or next(
                (
                    axis.get("axis_id")
                    for axis in classification_axes
                    if str(axis.get("axis_role") or "") == "primary_organization"
                ),
                "",
            )
        ).strip()
        routing_categories: list[dict[str, Any]] = []
        routing_category_labels: set[str] = set()

        def add_routing_category(label: Any, aliases: Any = ()) -> None:
            normalized_label = str(label or "").strip()
            identity = normalized_label.casefold()
            if not normalized_label or identity in routing_category_labels:
                return
            routing_category_labels.add(identity)
            routing_categories.append(
                {
                    "label": normalized_label[:160],
                    "aliases": list(
                        dict.fromkeys(
                            str(value).strip()[:160]
                            for value in aliases or []
                            if str(value or "").strip()
                        )
                    )[:16],
                }
            )

        primary_axis = next(
            (
                axis
                for axis in classification_axes
                if str(axis.get("axis_id") or "") == routing_axis_id
            ),
            {},
        )
        for partition in primary_axis.get("partitions") or []:
            if not isinstance(partition, dict):
                continue
            add_routing_category(
                partition.get("label"),
                [
                    *(partition.get("aliases") or []),
                    *(partition.get("positive_discriminators") or []),
                ],
            )
        if routing_axis_id:
            try:
                for label, category, aliases in load_taxonomy_rules(
                    self.root,
                    profile=project.taxonomy_profile,
                    topic_text=topic,
                ):
                    if str(category or "") == routing_axis_id:
                        add_routing_category(label, aliases)
            except TaxonomyConfigurationError:
                # Formal contract partitions above remain usable.  A missing
                # optional taxonomy profile must not break fact extraction.
                pass
        routing_categories_fingerprint = hashlib.sha256(
            json.dumps(
                routing_categories,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        deterministic_routing_by_paper: dict[str, str] = {}
        if routing_axis_id:
            routing_tags, routing_text = self._outline_sources(principal, rows)
            for row in rows:
                paper_id = str(row.get("paper_id") or "").strip()
                if not paper_id:
                    continue
                label = self._tag_value(
                    (routing_tags.get(paper_id) or {}).get(routing_axis_id)
                )
                if not label:
                    semantic = self._semantic_outline_groups(
                        [row],
                        {paper_id: routing_text.get(paper_id, "")},
                        tag_key=routing_axis_id,
                        taxonomy_profile=project.taxonomy_profile,
                    )
                    label = next(
                        (
                            candidate_label
                            for candidate_label, assigned in semantic.items()
                            if candidate_label != ROUTING_REQUIRED_LABEL
                            and paper_id in assigned
                        ),
                        "",
                    )
                if label:
                    deterministic_routing_by_paper[paper_id] = label
        classification_partition_queries: list[dict[str, str]] = []
        seen_partition_queries: set[tuple[str, str]] = set()
        for axis in classification_axes:
            axis_id = str(axis.get("axis_id") or "")
            for partition in axis.get("partitions") or []:
                if not isinstance(partition, dict):
                    continue
                label = str(partition.get("label") or "").strip()
                if not label:
                    continue
                terms = list(dict.fromkeys(
                    str(value).strip()
                    for value in [
                        label,
                        *(partition.get("aliases") or []),
                        *(partition.get("positive_discriminators") or []),
                    ]
                    if str(value).strip()
                ))
                query = " ".join(terms[:5])[:700]
                identity = (axis_id, label.casefold())
                if not query or identity in seen_partition_queries:
                    continue
                seen_partition_queries.add(identity)
                classification_partition_queries.append(
                    {
                        "axis_id": axis_id,
                        "partition_id": str(partition.get("partition_id") or ""),
                        "label": label,
                        "query": query,
                    }
                )
        papers: list[dict[str, Any]] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "")
            summary = dict(summaries.get(paper_id) or {})
            lineage = str(summary.get("source_lineage_hash") or "")
            fingerprint_input = {
                "schema_version": 2,
                "fact_enrichment_contract_version": (
                    MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
                ),
                "topic": " ".join(topic.casefold().split()),
                "taxonomy_profile": project.taxonomy_profile,
                "paper_id": paper_id,
                "source_lineage_hash": lineage,
                "chunker_version": summary.get("chunker_version"),
                "embedding_profile": summary.get("embedding_profile"),
                "embedding_model": (
                    summary.get("embedding_model")
                    if summary.get("semantic") == "ready"
                    else ""
                ),
                "embedding_dimension": (
                    summary.get("embedding_dimension")
                    if summary.get("semantic") == "ready"
                    else 0
                ),
                "prompt_schema_version": 1,
                "routing_adjudicator_version": 1,
                "routing_axis_id": routing_axis_id,
                "routing_categories_fingerprint": routing_categories_fingerprint,
                "deterministic_routing_label": deterministic_routing_by_paper.get(
                    paper_id, ""
                ),
                "actual_model_id": resolve_model_tier(project.model_tier).model,
            }
            if topic_partitions or classification_axes:
                fingerprint_input.update(
                    {
                        "topic_partition_classifier_version": 2,
                        "topic_partitions": topic_partitions,
                        "classification_contract_fingerprint": (
                            classification_contract["fingerprint"]
                        ),
                    }
                )
            source_fingerprint = hashlib.sha256(
                json.dumps(
                    fingerprint_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            existing = dict(row.get("fact_enrichment") or {})
            if (
                not force
                and existing.get("source_fingerprint") == source_fingerprint
                and existing.get("status") in {"complete", "partial", "limited"}
            ):
                continue
            plans = build_question_query_plans(
                review_topic=topic,
                heading="",
                core_argument=topic,
                section_role="body",
            )
            candidates: dict[str, dict[str, Any]] = {}
            partition_candidates: dict[str, dict[str, Any]] = {}
            strict_question_hit_count = 0
            relaxed_question_hit_count = 0
            if (
                self.library_index is not None
                and self.library_index.enabled
                and summary.get("fulltext") == "ready"
            ):
                def add_question_hits(
                    hits: list[Any],
                    *,
                    question_id: str,
                    retrieval_pass: str,
                ) -> int:
                    added = 0
                    for hit in hits:
                        if hit.is_neighbor:
                            continue
                        added += 1
                        key = academic_evidence_key(
                            hit.paper_id, hit.chunk_id, hit.source_lineage_hash
                        )
                        candidate = candidates.setdefault(
                            key,
                            {
                                "evidence_key": key,
                                "paper_id": hit.paper_id,
                                "chunk_id": hit.chunk_id,
                                "page_start": hit.page_start,
                                "page_end": hit.page_end,
                                "section_path": list(hit.section_path),
                                "content_type": hit.content_type,
                                "content": hit.content,
                                "source_lineage_hash": hit.source_lineage_hash,
                                "question_ids": [],
                                "retrieval_passes": [],
                            },
                        )
                        if question_id not in candidate["question_ids"]:
                            candidate["question_ids"].append(question_id)
                        if retrieval_pass not in candidate["retrieval_passes"]:
                            candidate["retrieval_passes"].append(retrieval_pass)
                    return added

                for plan in plans:
                    question_id = str(plan.get("question_id") or "")
                    if question_id == "section_focus":
                        continue
                    strict_hits = self.library_index.retrieve(
                        principal,
                        str(plan.get("websearch_query") or ""),
                        allowed_papers=[paper_id],
                        top_k=2,
                        per_paper_limit=2,
                        include_neighbors=False,
                        term_groups=list(plan.get("term_groups") or []),
                        exact_phrases=list(plan.get("exact_phrases") or []),
                    )
                    strict_added = add_question_hits(
                        strict_hits,
                        question_id=question_id,
                        retrieval_pass="strict_topic_and_question",
                    )
                    strict_question_hit_count += strict_added
                    # Matrix papers have already passed Topic admission. If a
                    # strict same-chunk Topic+question query returns nothing,
                    # search only inside that admitted paper for the scientific
                    # question. Source-addressable excerpt validation remains
                    # unchanged at publish time, so this restores recall without
                    # weakening factual acceptance.
                    question_groups = list(
                        plan.get("question_term_groups") or []
                    )
                    if not strict_added and question_groups:
                        relaxed_parts: list[str] = []
                        for group in question_groups:
                            alternatives = [
                                f'"{term}"' if " " in str(term) else str(term)
                                for term in group
                                if str(term).strip()
                            ]
                            if alternatives:
                                relaxed_parts.append(
                                    "(" + " OR ".join(alternatives) + ")"
                                )
                        relaxed_query = " ".join(relaxed_parts)
                        if relaxed_query:
                            relaxed_added = add_question_hits(
                                self.library_index.retrieve(
                                    principal,
                                    relaxed_query,
                                    allowed_papers=[paper_id],
                                    top_k=2,
                                    per_paper_limit=2,
                                    include_neighbors=False,
                                    term_groups=question_groups,
                                    exact_phrases=[
                                        str(term)
                                        for group in question_groups
                                        for term in group
                                        if " " in str(term)
                                    ],
                                ),
                                question_id=question_id,
                                retrieval_pass=(
                                    "admitted_paper_question_recovery"
                                ),
                            )
                            relaxed_question_hit_count += relaxed_added
                for partition_query in classification_partition_queries:
                    hits = self.library_index.retrieve(
                        principal,
                        partition_query["query"],
                        allowed_papers=[paper_id],
                        top_k=4,
                        per_paper_limit=4,
                        include_neighbors=True,
                    )
                    for hit in hits:
                        key = academic_evidence_key(
                            hit.paper_id, hit.chunk_id, hit.source_lineage_hash
                        )
                        partition_candidates.setdefault(
                            key,
                            {
                                "evidence_key": key,
                                "paper_id": hit.paper_id,
                                "chunk_id": hit.chunk_id,
                                "page_start": hit.page_start,
                                "page_end": hit.page_end,
                                "section_path": list(hit.section_path),
                                "content_type": hit.content_type,
                                "content": hit.content,
                                "source_lineage_hash": hit.source_lineage_hash,
                                "matched_partitions": [],
                                "retrieval_passes": [],
                            },
                        )
                        matched = partition_candidates[key]["matched_partitions"]
                        partition_identity = {
                            "axis_id": partition_query["axis_id"],
                            "partition_id": partition_query["partition_id"],
                            "label": partition_query["label"],
                        }
                        if partition_identity not in matched:
                            matched.append(partition_identity)
                        retrieval_pass = (
                            "classification_neighbor_context"
                            if hit.is_neighbor
                            else "classification_discriminator_search"
                        )
                        passes = partition_candidates[key]["retrieval_passes"]
                        if retrieval_pass not in passes:
                            passes.append(retrieval_pass)
            abstract = self._matrix_abstract(row)
            if abstract:
                abstract_lineage = lineage or hashlib.sha256(
                    abstract.encode("utf-8")
                ).hexdigest()
                abstract_key = academic_evidence_key(
                    paper_id, "abstract", abstract_lineage
                )
                abstract_candidate = {
                    "evidence_key": abstract_key,
                    "paper_id": paper_id,
                    "chunk_id": "abstract",
                    "page_start": None,
                    "page_end": None,
                    "section_path": ["Abstract"],
                    "content_type": "abstract",
                    "content": abstract,
                    "source_lineage_hash": abstract_lineage,
                    "question_ids": ["abstract_summary"],
                    "match_type": "abstract_only",
                }
                candidates.setdefault(
                    abstract_key,
                    abstract_candidate,
                )
                partition_candidates.setdefault(
                    abstract_key,
                    {
                        **abstract_candidate,
                        "matched_partitions": [],
                    },
                )
            papers.append(
                {
                    "paper_id": paper_id,
                    "title": str(row.get("title") or paper_id),
                    "abstract": abstract,
                    "index_summary": summary,
                    "source_fingerprint": source_fingerprint,
                    "taxonomy_profile": project.taxonomy_profile,
                    "deterministic_routing_label": deterministic_routing_by_paper.get(
                        paper_id, ""
                    ),
                    "retrieval_summary": {
                        "strict_question_hit_count": strict_question_hit_count,
                        "relaxed_question_hit_count": relaxed_question_hit_count,
                        "classification_query_count": len(
                            classification_partition_queries
                        ),
                        "mode": (
                            "hybrid_targeted"
                            if summary.get("semantic") == "ready"
                            else "lexical_targeted"
                        ),
                    },
                    "evidence_candidates": list(candidates.values()),
                    "partition_evidence_candidates": list(
                        partition_candidates.values()
                    ),
                }
            )
        return {
            "schema_version": 2,
            "fact_enrichment_contract_version": (
                MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
            ),
            "force_refresh": bool(force),
            "project_id": project_id,
            "review_topic": topic,
            "topic_partitions": topic_partitions,
            "classification_axes": classification_axes,
            "classification_contract": classification_contract,
            "classification_contract_version": CLASSIFICATION_CONTRACT_VERSION,
            "routing_axis_id": routing_axis_id,
            "routing_categories": routing_categories,
            "routing_adjudicator_version": 1,
            "taxonomy_profile": project.taxonomy_profile,
            "actual_model_id": resolve_model_tier(project.model_tier).model,
            "source_matrix_artifact_id": matrix_artifact.id,
            "expected_matrix_revision": state.revision,
            "paper_count": len(rows),
            "pending_paper_count": len(papers),
            "fulltext_candidate_paper_count": sum(
                1
                for paper in papers
                if any(
                    str(item.get("content_type") or "") != "abstract"
                    for item in paper.get("evidence_candidates") or []
                    if isinstance(item, dict)
                )
            ),
            "papers": papers,
        }

    def publish_matrix_enrichment(
        self,
        principal: Principal,
        project_id: str,
        payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        """Publish per-paper facts only if the Matrix and source lineage stayed current."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        if matrix_artifact.id != str(payload.get("source_matrix_artifact_id") or ""):
            raise WorkflowConflict(
                "Matrix changed while scientific facts were being extracted. Run enrichment again."
            )
        input_by_paper = {
            str(item.get("paper_id") or ""): item
            for item in payload.get("papers") or []
            if isinstance(item, dict) and item.get("paper_id")
        }
        built_by_paper = {
            str(item.get("paper_id") or ""): item
            for item in built.get("papers") or []
            if isinstance(item, dict) and item.get("paper_id")
        }
        if set(built_by_paper) != set(input_by_paper):
            raise WorkflowValidationError(
                "Matrix enrichment result does not match the pending paper set."
            )
        updated = deepcopy(matrix)
        declared_partitions = [
            _clean_topic_partition(value)
            for value in payload.get("topic_partitions") or []
            if _clean_topic_partition(value)
        ]
        routing_axis_id = str(payload.get("routing_axis_id") or "").strip()
        allowed_routing_labels = {
            str(item.get("label") or "").strip().casefold(): str(
                item.get("label") or ""
            ).strip()
            for item in payload.get("routing_categories") or []
            if isinstance(item, dict) and str(item.get("label") or "").strip()
        }
        for row in updated.get("rows") or []:
            if not isinstance(row, dict):
                continue
            paper_id = str(row.get("paper_id") or "")
            source = input_by_paper.get(paper_id)
            result = built_by_paper.get(paper_id)
            if source is None or result is None:
                continue
            candidates = {
                str(item.get("evidence_key") or ""): item
                for item in source.get("evidence_candidates") or []
                if isinstance(item, dict) and item.get("evidence_key")
            }
            partition_candidates = {
                str(item.get("evidence_key") or ""): item
                for item in [
                    *(source.get("partition_evidence_candidates") or []),
                    *(source.get("evidence_candidates") or []),
                ]
                if isinstance(item, dict) and item.get("evidence_key")
            }
            fact_candidates = {**partition_candidates, **candidates}
            facts = []
            for fact in result.get("facts") or []:
                if not isinstance(fact, dict):
                    continue
                raw_refs = [
                    ref for ref in fact.get("evidence_refs") or []
                    if isinstance(ref, dict)
                ]
                if not raw_refs or any(
                    str(ref.get("evidence_key") or "") not in fact_candidates
                    for ref in raw_refs
                ):
                    continue
                excerpt = " ".join(
                    str(fact.get("support_excerpt") or "").split()
                ).casefold()
                if not excerpt or not str(fact.get("value") or "").strip():
                    continue
                if not str(fact.get("evidence_ceiling") or "").strip():
                    continue
                if not all(
                    excerpt
                    in " ".join(
                        str(
                            fact_candidates[str(ref.get("evidence_key") or "")].get(
                                "content"
                            )
                            or ""
                        ).split()
                    ).casefold()
                    for ref in raw_refs
                ):
                    continue
                field_id = str(fact.get("field_id") or "").casefold()
                if not field_id or any(
                    field_id != "topic_partition"
                    and field_id
                    not in {
                        str(value).casefold()
                        for value in fact_candidates[
                            str(ref.get("evidence_key") or "")
                        ].get("question_ids") or []
                    }
                    for ref in raw_refs
                ):
                    continue
                assertion_ceiling = str(fact.get("assertion_ceiling") or "").strip()
                if not assertion_ceiling:
                    content_types = {
                        str(
                            fact_candidates[str(ref.get("evidence_key") or "")].get(
                                "content_type"
                            )
                            or "body"
                        ).casefold()
                        for ref in raw_refs
                    }
                    assertion_ceiling = (
                        "abstract_report_only"
                        if content_types == {"abstract"}
                        else "direct_source_report"
                    )
                facts.append(
                    {
                        **fact,
                        "assertion_ceiling": assertion_ceiling,
                        "evidence_refs": raw_refs,
                    }
                )
            raw_classification = result.get("topic_partition_classification")
            if not declared_partitions:
                partition_classification = {
                    "schema_version": 1,
                    "status": "not_requested",
                    "partition": "",
                    "confidence": 0.0,
                    "evidence_refs": [],
                }
            elif isinstance(raw_classification, dict):
                status_value = str(
                    raw_classification.get("status") or "insufficient_evidence"
                ).casefold()
                if status_value == "boundary":
                    status_value = "insufficient_evidence"
                canonical_partition = _canonical_declared_partition(
                    raw_classification.get("partition"), declared_partitions
                )
                try:
                    partition_confidence = max(
                        0.0,
                        min(
                            1.0,
                            float(raw_classification.get("confidence") or 0),
                        ),
                    )
                except (TypeError, ValueError):
                    partition_confidence = 0.0
                partition_refs = [
                    dict(ref)
                    for ref in raw_classification.get("evidence_refs") or []
                    if isinstance(ref, dict)
                    and str(ref.get("evidence_key") or "") in partition_candidates
                ]
                support_excerpt = " ".join(
                    str(raw_classification.get("support_excerpt") or "").split()
                )
                excerpt_valid = bool(
                    support_excerpt
                    and partition_refs
                    and all(
                        support_excerpt.casefold()
                        in " ".join(
                            str(
                                partition_candidates[
                                    str(ref.get("evidence_key") or "")
                                ].get("content")
                                or ""
                            ).split()
                        ).casefold()
                        for ref in partition_refs
                    )
                )
                classified = bool(
                    status_value == "classified"
                    and canonical_partition
                    and partition_confidence >= 0.75
                    and excerpt_valid
                )
                unresolved_status = (
                    status_value
                    if status_value
                    in {"insufficient_evidence", "cross_category", "out_of_scope"}
                    else "insufficient_evidence"
                )
                if unresolved_status in {"cross_category", "out_of_scope"} and not excerpt_valid:
                    unresolved_status = "insufficient_evidence"
                partition_classification = {
                    "schema_version": 1,
                    "status": "classified" if classified else unresolved_status,
                    "partition": canonical_partition if classified else "",
                    "candidate_partition": (
                        canonical_partition
                        if canonical_partition and not classified
                        else ""
                    ),
                    "confidence": round(partition_confidence, 4),
                    "rationale": str(
                        raw_classification.get("rationale") or ""
                    )[:800],
                    "boundary_reason": (
                        ""
                        if classified
                        else str(
                            raw_classification.get("boundary_reason")
                            or "The source-bound classification did not meet the evidence and confidence requirements."
                        )[:800]
                    ),
                    "classification_reason": (
                        ""
                        if classified
                        else str(
                            raw_classification.get("classification_reason")
                            or raw_classification.get("boundary_reason")
                            or "The source-bound classification did not meet the evidence and confidence requirements."
                        )[:800]
                    ),
                    "support_excerpt": support_excerpt if excerpt_valid else "",
                    "evidence_ceiling": str(
                        raw_classification.get("evidence_ceiling")
                        or "Do not infer a contrasting partition from information absent in the cited passage."
                    )[:600],
                    "evidence_refs": partition_refs if excerpt_valid else [],
                    "review_status": (
                        "not_required" if classified else "needs_review"
                    ),
                    "extraction_method": "model_classified_from_bounded_source",
                }
            else:
                partition_classification = {
                    "schema_version": 1,
                    "status": "insufficient_evidence",
                    "partition": "",
                    "confidence": 0.0,
                    "evidence_refs": [],
                    "boundary_reason": "The model returned no valid Topic-partition classification.",
                    "review_status": "needs_review",
                    "extraction_method": "model_classification_missing",
                }
            axis_contract = {
                str(axis.get("axis_id") or ""): axis
                for axis in payload.get("classification_axes") or []
                if isinstance(axis, dict) and str(axis.get("axis_id") or "")
            }
            allowed_partitions = {
                axis_id: {
                    str(partition.get("partition_id") or "")
                    for partition in axis.get("partitions") or []
                    if isinstance(partition, dict)
                    and str(partition.get("partition_id") or "")
                }
                for axis_id, axis in axis_contract.items()
            }
            facts_by_id = {
                str(fact.get("fact_id") or ""): fact
                for fact in facts
                if str(fact.get("fact_id") or "")
            }
            evidence_backed_tags: dict[str, list[dict[str, Any]]] = {}
            raw_tags = result.get("evidence_backed_tags")
            if isinstance(raw_tags, dict):
                for axis_id, raw_values in raw_tags.items():
                    axis_id = str(axis_id or "")
                    if axis_id not in axis_contract or not isinstance(raw_values, list):
                        continue
                    for raw_tag in raw_values:
                        if not isinstance(raw_tag, dict):
                            continue
                        partition_id = str(raw_tag.get("partition_id") or "")
                        fact_ids = list(
                            dict.fromkeys(
                                str(value)
                                for value in raw_tag.get("fact_ids") or []
                                if str(value)
                            )
                        )
                        if (
                            partition_id not in allowed_partitions.get(axis_id, set())
                            or not fact_ids
                            or any(
                                fact_id not in facts_by_id
                                or str(facts_by_id[fact_id].get("field_id") or "")
                                != "topic_partition"
                                for fact_id in fact_ids
                            )
                        ):
                            continue
                        evidence_refs = [
                            dict(ref)
                            for ref in raw_tag.get("evidence_refs") or []
                            if isinstance(ref, dict)
                            and str(ref.get("evidence_key") or "") in partition_candidates
                        ]
                        if not evidence_refs:
                            continue
                        try:
                            tag_confidence = max(
                                0.0,
                                min(1.0, float(raw_tag.get("confidence") or 0)),
                            )
                        except (TypeError, ValueError):
                            tag_confidence = 0.0
                        evidence_backed_tags.setdefault(axis_id, []).append(
                            {
                                "axis_label": str(
                                    axis_contract[axis_id].get("label") or ""
                                )[:120],
                                "axis_role": str(
                                    axis_contract[axis_id].get("axis_role")
                                    or "comparison_dimension"
                                )[:80],
                                "partition_id": partition_id,
                                "partition_label": str(raw_tag.get("partition_label") or "")[:120],
                                "relation_to_paper": str(
                                    raw_tag.get("relation_to_paper") or "primary_contribution"
                                ),
                                "fact_ids": fact_ids,
                                "evidence_refs": evidence_refs,
                                "confidence": tag_confidence,
                                "assertion_ceiling": str(
                                    raw_tag.get("assertion_ceiling")
                                    or "direct_source_report"
                                ),
                            }
                        )
            classification_outcomes: list[dict[str, Any]] = []
            for raw_outcome in result.get("classification_outcomes") or []:
                if not isinstance(raw_outcome, dict):
                    continue
                axis_id = str(raw_outcome.get("axis_id") or "")
                if axis_id not in axis_contract or axis_id in evidence_backed_tags:
                    continue
                outcome_status = str(
                    raw_outcome.get("status") or "insufficient_evidence"
                ).casefold()
                if outcome_status not in {
                    "insufficient_evidence",
                    "cross_category",
                    "out_of_scope",
                }:
                    outcome_status = "insufficient_evidence"
                refs = [
                    dict(ref)
                    for ref in raw_outcome.get("evidence_refs") or []
                    if isinstance(ref, dict)
                    and str(ref.get("evidence_key") or "") in partition_candidates
                ]
                if outcome_status in {"cross_category", "out_of_scope"} and not refs:
                    outcome_status = "insufficient_evidence"
                classification_outcomes.append(
                    {
                        "axis_id": axis_id,
                        "axis_role": str(
                            axis_contract[axis_id].get("axis_role") or "comparison_dimension"
                        )[:80],
                        "status": outcome_status,
                        "reason": str(raw_outcome.get("reason") or "")[:800],
                        "support_excerpt": str(
                            raw_outcome.get("support_excerpt") or ""
                        )[:1600]
                        if refs
                        else "",
                        "evidence_refs": refs,
                        "resolution": str(
                            raw_outcome.get("resolution")
                            or "auto_route_from_positive_evidence_only"
                        )[:120],
                        "user_action_required": bool(
                            raw_outcome.get("user_action_required", False)
                        ),
                    }
                )
            raw_routing = result.get("routing_recommendation")
            if not isinstance(raw_routing, dict):
                raw_routing = {}
            requested_routing_label = str(raw_routing.get("label") or "").strip()
            routing_label = allowed_routing_labels.get(
                requested_routing_label.casefold(), ""
            )
            try:
                routing_confidence = max(
                    0.0, min(1.0, float(raw_routing.get("confidence") or 0))
                )
            except (TypeError, ValueError):
                routing_confidence = 0.0
            routing_refs = [
                dict(ref)
                for ref in raw_routing.get("evidence_refs") or []
                if isinstance(ref, dict)
                and str(ref.get("evidence_key") or "") in fact_candidates
            ]
            routing_excerpt = " ".join(
                str(raw_routing.get("support_excerpt") or "").split()
            )
            routing_excerpt_valid = bool(
                routing_excerpt
                and routing_refs
                and all(
                    routing_excerpt.casefold()
                    in " ".join(
                        str(
                            fact_candidates[
                                str(ref.get("evidence_key") or "")
                            ].get("content")
                            or ""
                        ).split()
                    ).casefold()
                    for ref in routing_refs
                )
            )
            routing_classified = bool(
                routing_axis_id
                and str(raw_routing.get("axis_id") or "") == routing_axis_id
                and str(raw_routing.get("status") or "").casefold()
                == "classified"
                and routing_label
                and routing_confidence >= 0.75
                and routing_excerpt_valid
            )
            formal_route_available = bool(
                routing_axis_id in evidence_backed_tags
                or str(raw_routing.get("status") or "").casefold()
                == "formal_axis_route_available"
            )
            deterministic_route_available = bool(
                str(raw_routing.get("status") or "").casefold()
                == "deterministic_route_available"
                and routing_label
            )
            routing_recommendation = {
                "schema_version": 1,
                "axis_id": routing_axis_id,
                "status": (
                    "classified"
                    if routing_classified
                    else "deterministic_route_available"
                    if deterministic_route_available
                    else "formal_axis_route_available"
                    if formal_route_available
                    else "insufficient_evidence"
                ),
                "label": (
                    routing_label
                    if routing_classified or deterministic_route_available
                    else ""
                ),
                "candidate_label": (
                    routing_label
                    if routing_label and not routing_classified
                    else ""
                ),
                "confidence": round(routing_confidence, 4),
                "rationale": str(raw_routing.get("rationale") or "")[:800],
                "reason": (
                    ""
                    if routing_classified
                    or deterministic_route_available
                    or formal_route_available
                    else str(
                        raw_routing.get("reason")
                        or "The bounded routing adjudicator found no supported publication category."
                    )[:800]
                ),
                "support_excerpt": (
                    routing_excerpt if routing_classified else ""
                ),
                "evidence_ceiling": str(
                    raw_routing.get("evidence_ceiling")
                    or "Do not extend this routing decision beyond the cited study design."
                )[:600],
                "evidence_refs": routing_refs if routing_classified else [],
                "review_status": (
                    "not_required"
                    if routing_classified
                    or deterministic_route_available
                    or formal_route_available
                    else "auto_unresolved"
                ),
                "extraction_method": str(
                    raw_routing.get("extraction_method")
                    or "model_routing_not_completed"
                )[:120],
            }
            status = str(result.get("status") or "failed")
            if result.get("facts") and len(facts) < len(result.get("facts") or []):
                status = "partial" if facts else "failed"
            row["scientific_facts"] = facts
            row["topic_partition_classification"] = partition_classification
            row["evidence_backed_tags"] = evidence_backed_tags
            row["classification_outcomes"] = classification_outcomes
            row["routing_recommendation"] = routing_recommendation
            row["comparison_evidence"] = {
                field_id: [
                    dict(fact)
                    for fact in facts
                    if str(fact.get("field_id") or "") == field_id
                ]
                for field_id in COMPARISON_FIELD_IDS
            }
            review_status = str(result.get("review_status") or "needs_review")
            if review_status not in {
                "not_required",
                "auto_resolved",
                "needs_review",
                "human_checked",
            }:
                review_status = "needs_review"
            row["fact_enrichment"] = {
                "schema_version": 2,
                "contract_version": int(
                    payload.get("fact_enrichment_contract_version")
                    or MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
                ),
                "status": status,
                "review_status": review_status,
                "source_fingerprint": str(source.get("source_fingerprint") or ""),
                "source_lineage_hash": str(
                    (source.get("index_summary") or {}).get("source_lineage_hash") or ""
                ),
                "fact_count": len(facts),
                "failed_fields": list(result.get("failed_fields") or []),
                "error": str(result.get("error") or "")[:1000],
                "automatic_resolution": deepcopy(
                    result.get("automatic_resolution") or {}
                ),
                "updated_at": utc_now().isoformat(),
            }
        classification_axes = [
            deepcopy(axis)
            for axis in payload.get("classification_axes") or updated.get("classification_axes") or []
            if isinstance(axis, dict) and str(axis.get("axis_id") or "")
        ]
        axis_coverage: dict[str, dict[str, Any]] = {}
        for axis in classification_axes:
            axis_id = str(axis.get("axis_id") or "")
            partition_counts: dict[str, int] = {}
            paper_ids: set[str] = set()
            for matrix_row in updated.get("rows") or []:
                if not isinstance(matrix_row, dict):
                    continue
                values = (matrix_row.get("evidence_backed_tags") or {}).get(axis_id) or []
                if not values:
                    continue
                paper_id = str(matrix_row.get("paper_id") or "")
                if paper_id:
                    paper_ids.add(paper_id)
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    partition_id = str(value.get("partition_id") or "")
                    if partition_id:
                        partition_counts[partition_id] = partition_counts.get(partition_id, 0) + 1
            axis_coverage[axis_id] = {
                "paper_ids": sorted(paper_ids),
                "paper_count": len(paper_ids),
                "partition_counts": partition_counts,
            }
            axis["evidence_coverage"] = deepcopy(axis_coverage[axis_id])
            if paper_ids:
                axis["role_status"] = "evidence_confirmed"
            elif str(axis.get("source_type") or "") != "explicit_topic":
                axis["role_status"] = "provisional"

        explicit_primary = next(
            (
                axis
                for axis in classification_axes
                if str(axis.get("source_type") or "") == "explicit_topic"
                and str(axis.get("axis_role") or "") == "primary_organization"
            ),
            None,
        )
        recommended_primary = explicit_primary
        if recommended_primary is None and classification_axes:
            recommended_primary = max(
                classification_axes,
                key=lambda axis: (
                    int(
                        (axis_coverage.get(str(axis.get("axis_id") or "")) or {}).get(
                            "paper_count"
                        )
                        or 0
                    ),
                    str(axis.get("axis_role") or "") == "primary_organization",
                ),
            )
            for axis in classification_axes:
                if axis is recommended_primary:
                    axis["axis_role"] = "primary_organization"
                    axis["heading_requirement"] = "primary_heading"
                elif str(axis.get("axis_role") or "") == "primary_organization":
                    axis["axis_role"] = "comparison_dimension"
                    axis["heading_requirement"] = "comparison_only"
        updated["classification_axes"] = classification_axes
        updated["classification_recommendation"] = {
            "schema_version": 1,
            "source": (
                "explicit_topic_with_matrix_evidence"
                if explicit_primary is not None
                else "system_recommended_from_selected_matrix_evidence"
            ),
            "primary_axis_id": str(
                (recommended_primary or {}).get("axis_id") or ""
            ),
            "primary_axis_label": str(
                (recommended_primary or {}).get("label") or ""
            ),
            "requires_existing_blueprint_confirmation": True,
            "axis_coverage": axis_coverage,
            "updated_at": utc_now().isoformat(),
        }
        updated_classification_contract = canonical_classification_contract(
            classification_axes,
            primary_axis_hint=str(
                (recommended_primary or {}).get("axis_id") or ""
            ),
            source=(
                "explicit_topic_with_matrix_evidence"
                if explicit_primary is not None
                else "matrix_evidence_recommendation"
            ),
        )
        updated["classification_contract"] = updated_classification_contract
        updated["classification_contract_version"] = (
            CLASSIFICATION_CONTRACT_VERSION
        )
        published_statuses = [
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            for row in updated.get("rows") or []
            if isinstance(row, dict)
        ]
        updated["fact_enrichment_summary"] = {
            "schema_version": 2,
            "contract_version": int(
                payload.get("fact_enrichment_contract_version")
                or MATRIX_FACT_ENRICHMENT_CONTRACT_VERSION
            ),
            "source_matrix_artifact_id": matrix_artifact.id,
            "complete_count": published_statuses.count("complete"),
            "partial_count": published_statuses.count("partial"),
            "limited_count": published_statuses.count("limited"),
            "failed_count": published_statuses.count("failed"),
            "pending_count": published_statuses.count("pending"),
            "needs_review_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str((row.get("fact_enrichment") or {}).get("review_status") or "")
                == "needs_review"
            ),
            "topic_partition_classified_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str(
                    (row.get("topic_partition_classification") or {}).get(
                        "status"
                    )
                    or ""
                )
                == "classified"
            ),
            "topic_partition_insufficient_evidence_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str(
                    (row.get("topic_partition_classification") or {}).get(
                        "status"
                    )
                    or ""
                )
                == "insufficient_evidence"
            ),
            "topic_partition_cross_category_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str(
                    (row.get("topic_partition_classification") or {}).get("status")
                    or ""
                )
                == "cross_category"
            ),
            "topic_partition_out_of_scope_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict)
                and str(
                    (row.get("topic_partition_classification") or {}).get("status")
                    or ""
                )
                == "out_of_scope"
            ),
            "evidence_backed_tag_paper_count": sum(
                1
                for row in updated.get("rows") or []
                if isinstance(row, dict) and bool(row.get("evidence_backed_tags"))
            ),
            "updated_at": utc_now().isoformat(),
        }
        updated["comparison_schema"] = {
            "schema_version": 1,
            "field_ids": list(COMPARISON_FIELD_IDS),
            "missing_value_policy": "keep_empty_and_do_not_infer",
            "source": "source_addressable_scientific_facts",
        }
        outline_compatible_ids = [
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        ]
        if matrix_artifact.id not in outline_compatible_ids:
            outline_compatible_ids.append(matrix_artifact.id)
        updated["outline_compatible_matrix_artifact_ids"] = outline_compatible_ids[-20:]

        def evidence_state_by_paper(document: dict[str, Any]) -> dict[str, Any]:
            return {
                str(row.get("paper_id") or ""): {
                    "scientific_facts": row.get("scientific_facts") or [],
                    "topic_partition_classification": row.get(
                        "topic_partition_classification"
                    )
                    or {},
                    "evidence_backed_tags": row.get("evidence_backed_tags") or {},
                    "classification_outcomes": row.get("classification_outcomes") or [],
                    "routing_recommendation": row.get("routing_recommendation") or {},
                    "fact_status": str(
                        (row.get("fact_enrichment") or {}).get("status") or "pending"
                    ),
                }
                for row in document.get("rows") or []
                if isinstance(row, dict) and str(row.get("paper_id") or "")
            }

        previous_evidence_state = evidence_state_by_paper(matrix)
        next_evidence_state = evidence_state_by_paper(updated)
        changed_paper_ids = sorted(
            paper_id
            for paper_id in set(previous_evidence_state) | set(next_evidence_state)
            if previous_evidence_state.get(paper_id) != next_evidence_state.get(paper_id)
        )
        def stable_classification_contract(document: dict[str, Any]) -> dict[str, Any]:
            contract = classification_contract_from_document(
                document,
                primary_axis_hint=str(
                    (document.get("classification_recommendation") or {}).get(
                        "primary_axis_id"
                    )
                    or ""
                ),
                source="matrix_contract_comparison",
            )
            return {
                "contract_version": contract["contract_version"],
                "fingerprint": contract["fingerprint"],
            }

        classification_contract_changed = bool(
            stable_classification_contract(matrix)
            != stable_classification_contract(updated)
        )

        def enrichment_cache_state(document: dict[str, Any]) -> dict[str, Any]:
            return {
                str(row.get("paper_id") or ""): {
                    "source_fingerprint": str(
                        (row.get("fact_enrichment") or {}).get("source_fingerprint") or ""
                    ),
                    "source_lineage_hash": str(
                        (row.get("fact_enrichment") or {}).get("source_lineage_hash") or ""
                    ),
                    "failed_fields": list(
                        (row.get("fact_enrichment") or {}).get("failed_fields") or []
                    ),
                }
                for row in document.get("rows") or []
                if isinstance(row, dict) and str(row.get("paper_id") or "")
            }

        previous_cache_state = enrichment_cache_state(matrix)
        next_cache_state = enrichment_cache_state(updated)
        refreshed_paper_ids = sorted(
            paper_id
            for paper_id in set(previous_cache_state) | set(next_cache_state)
            if previous_cache_state.get(paper_id) != next_cache_state.get(paper_id)
        )
        updated["matrix_change_set"] = {
            "schema_version": 1,
            "operation": "scientific_fact_refresh",
            "changed_paper_ids": changed_paper_ids,
            "changed_paper_count": len(changed_paper_ids),
            "classification_contract_changed": classification_contract_changed,
            "refreshed_paper_ids": refreshed_paper_ids,
            "dependency_policy": "invalidate_only_when_evidence_state_changed",
            "updated_at": utc_now().isoformat(),
        }
        if (
            not changed_paper_ids
            and not refreshed_paper_ids
            and not classification_contract_changed
        ):
            current_state = self.repository.get_stage_state(
                principal.user_id, project_id, "matrix"
            )
            return {
                "project_id": project_id,
                "matrix_artifact_id": matrix_artifact.id,
                "matrix_revision": current_state.revision if current_state else 0,
                "fact_enrichment_summary": matrix.get("fact_enrichment_summary")
                or updated["fact_enrichment_summary"],
                "changed_paper_ids": [],
                "refreshed_paper_ids": [],
                "classification_contract_changed": False,
                "unchanged": True,
            }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={
                    "source_matrix_artifact_id": matrix_artifact.id,
                    "source_fingerprints": {
                        paper_id: item.get("source_fingerprint")
                        for paper_id, item in input_by_paper.items()
                    },
                },
            )
            state = None
            for attempt in range(3):
                current_matrix, current_matrix_artifact = self._matrix(
                    principal, project_id
                )
                if current_matrix_artifact.id != matrix_artifact.id:
                    raise WorkflowConflict(
                        "Matrix content changed while scientific facts were being published. Run enrichment again."
                    )
                current_state = self.repository.get_stage_state(
                    principal.user_id, project_id, "matrix"
                )
                expected_revision = current_state.revision if current_state else 0
                try:
                    state = self.repository.promote_stage_artifacts_atomically(
                        principal.user_id,
                        project_id,
                        "matrix",
                        artifact_ids={
                            MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id
                        },
                        run_id=run.id,
                        expected_revision=expected_revision,
                        status="review",
                        invalidate_stages=(
                            (
                                "blueprint",
                                "sections",
                                "figure-review",
                                "figures",
                                "draft",
                                "final",
                            )
                            if changed_paper_ids or classification_contract_changed
                            else ()
                        ),
                        expected_current_artifacts={
                            MATRIX_LOGICAL_NAME: matrix_artifact.id
                        },
                    )
                    break
                except WorkflowConflict:
                    if attempt == 2:
                        raise
            if state is None:  # pragma: no cover - defensive invariant
                raise WorkflowConflict("Scientific facts could not be published.")
        return {
            "project_id": project_id,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "fact_enrichment_summary": updated["fact_enrichment_summary"],
            "changed_paper_ids": changed_paper_ids,
            "refreshed_paper_ids": refreshed_paper_ids,
            "classification_contract_changed": classification_contract_changed,
            "unchanged": False,
        }

    def confirm_matrix_limited_mode(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        """Let the user continue only after every automatic fact extraction failed."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        statuses = [
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            for row in rows
        ]
        if not rows or any(status != "failed" for status in statuses):
            raise WorkflowConflict(
                "Limited mode is available only when every Matrix fact extraction failed."
            )
        summary = {
            **dict(matrix.get("fact_enrichment_summary") or {}),
            "limited_mode_confirmed": True,
            "limited_mode_confirmed_at": utc_now().isoformat(),
            "limited_mode_reason": "all_scientific_fact_extractions_failed",
        }
        updated = {**deepcopy(matrix), "fact_enrichment_summary": summary}
        outline_compatible_ids = [
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        ]
        if matrix_artifact.id not in outline_compatible_ids:
            outline_compatible_ids.append(matrix_artifact.id)
        updated["outline_compatible_matrix_artifact_ids"] = outline_compatible_ids[-20:]
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={
                    "operation": "confirm-limited-mode",
                    "source_matrix_artifact_id": matrix_artifact.id,
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint", "sections", "figure-review", "figures", "draft", "final"
                ),
                expected_current_artifacts={MATRIX_LOGICAL_NAME: matrix_artifact.id},
            )
        return {
            "project_id": project_id,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "limited_mode_confirmed": True,
        }

    @staticmethod
    def _tag_value(value: Any) -> str:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        if isinstance(value, (list, tuple)):
            value = next(
                (
                    item
                    for item in value
                    if str(item or "").strip()
                    and str(item).strip().casefold()
                    not in {"not specified", "none", "unknown"}
                ),
                "",
            )
        normalized = str(value or "").strip()
        return (
            ""
            if normalized.casefold()
            in {"not specified", "none", "unknown", "n/a"}
            else normalized
        )

    def _outline_sources(
        self,
        principal: Principal,
        rows: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        paper_ids = _paper_ids(rows)
        if not paper_ids:
            return {}, {}
        with database_session(self.repository.session_factory) as session:
            records = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == uuid.UUID(principal.user_id),
                    LibraryPaper.paper_id.in_(tuple(paper_ids)),
                    LibraryPaper.status == "active",
                    LibraryPaper.deleted_at.is_(None),
                )
            ).all()
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        rows_by_id = {str(row.get("paper_id") or ""): row for row in rows}
        for record in records:
            metadata = (
                record.metadata_json if isinstance(record.metadata_json, dict) else {}
            )
            tags = verified_structured_tags(metadata)
            row = rows_by_id.get(record.paper_id) or {}
            # Organization precedence is separate from Claim eligibility:
            # verified Library metadata < formal Matrix fact classifications
            # < explicit human project tags. Stage 02 retrieval hints and
            # legacy automatic screening tags never organize the outline.
            project_tags = row.get("project_tags")
            formal_tags = row.get("evidence_backed_tags") or {}
            if isinstance(formal_tags, dict):
                for axis_id, values in formal_tags.items():
                    labels = [
                        str(value.get("partition_label") or "").strip()
                        for value in values or []
                        if isinstance(value, dict)
                        and str(value.get("partition_label") or "").strip()
                    ]
                    if labels:
                        tags[str(axis_id)] = labels
                        axis_label = next(
                            (
                                str(value.get("axis_label") or "").strip()
                                for value in values or []
                                if isinstance(value, dict)
                                and str(value.get("axis_label") or "").strip()
                            ),
                            "",
                        )
                        if axis_label:
                            tags[axis_label] = labels
            routing = row.get("routing_recommendation")
            if isinstance(routing, dict):
                routing_axis = str(routing.get("axis_id") or "").strip()
                routing_label = str(routing.get("label") or "").strip()
                if (
                    routing_axis
                    and routing_label
                    and str(routing.get("status") or "") == "classified"
                    and routing_axis not in tags
                    and bool(routing.get("evidence_refs"))
                ):
                    # This bounded Agent recommendation is a routing aid only.
                    # It does not become Claim evidence or replace a stronger
                    # formal axis assignment.
                    tags[routing_axis] = [routing_label]
            human_tags = row.get("human_confirmed_tags")
            if isinstance(human_tags, dict) and human_tags:
                tags.update(human_tags)
            elif row.get("project_tag_review_status") == "confirmed" and isinstance(
                project_tags, dict
            ):
                tags.update(project_tags)
            tags_by_paper[record.paper_id] = tags
            scientific_facts = [
                item
                for item in row.get("scientific_facts") or []
                if isinstance(item, dict)
                and str(item.get("field_id") or "") != "abstract_summary"
            ]
            # Route from the paper's extracted scientific object before title
            # words.  Product names in titles are otherwise easily mistaken
            # for the substrate/precursor used by the study.
            fact_priority = {
                "object_input": 0,
                "document_scope": 1,
                "transformation": 2,
                "method_family": 3,
            }
            fact_parts = [
                str(item.get("value") or "").strip()
                for item in sorted(
                    scientific_facts,
                    key=lambda item: fact_priority.get(
                        str(item.get("field_id") or ""), 10
                    ),
                )
                if str(item.get("value") or "").strip()
            ]
            parts = [
                " ".join(fact_parts),
                row.get("title"),
                " ".join(str(item) for item in (row.get("keywords") or [])),
                record.title,
                " ".join(str(item) for item in (record.keywords_json or [])),
                row.get("abstract"),
                row.get("main_content"),
            ]
            text_by_paper[record.paper_id] = " ".join(
                str(part) for part in parts if str(part or "").strip()
            ).casefold()
        return tags_by_paper, text_by_paper

    @staticmethod
    def _taxonomy_match_text(value: Any) -> str:
        """Normalize harmless typesetting punctuation before phrase matching.

        Chemical titles often wrap a substituent name in parentheses, as in
        ``(allenylmethyl)silanes``.  Removing grouping brackets from both the
        evidence text and taxonomy terms keeps those typography variants from
        becoming artificial routing failures while preserving other chemical
        punctuation used by the rules.
        """

        normalized = str(value or "").strip().casefold()
        return re.sub(r"[()\[\]{}]", "", normalized)

    @staticmethod
    def _semantic_outline_groups(
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        try:
            topic_text = " ".join(
                text for text in text_by_paper.values() if str(text or "").strip()
            )
            rules = [
                (label, aliases)
                for label, category, aliases in load_taxonomy_rules(
                    Path.cwd(),
                    profile=taxonomy_profile,
                    topic_text=topic_text,
                )
                if category == tag_key
            ]
        except TaxonomyConfigurationError:
            return {}
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            text = PlanningService._taxonomy_match_text(
                text_by_paper.get(paper_id, "")
            )
            ranked: list[tuple[int, int, str]] = []
            for index, (label, aliases) in enumerate(rules):
                score = 0
                for term in (label, *aliases):
                    normalized = PlanningService._taxonomy_match_text(term)
                    if not normalized:
                        continue
                    pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
                    match = re.search(pattern, text)
                    if match:
                        # _outline_sources puts source-addressable scientific
                        # facts first, followed by title/keywords and abstract.
                        # Prefer the extracted study object over product words
                        # and related-work mentions later in the source.
                        score = max(
                            score,
                            100_000
                            - min(match.start(), 99_999)
                            + len(normalized.split()) * 10
                            + len(normalized),
                        )
                ranked.append((score, -index, label))
            best = max(ranked, default=(0, 0, ""))
            if best[0] > 0:
                groups.setdefault(best[2], []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups[ROUTING_REQUIRED_LABEL] = other
        return groups

    def _outline_groups(
        self,
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        tag_key: str,
        taxonomy_profile: str,
    ) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        other: list[str] = []
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            label = self._tag_value(
                (tags_by_paper.get(paper_id) or {}).get(tag_key)
            )
            if label:
                groups.setdefault(label, []).append(paper_id)
            else:
                other.append(paper_id)
        if other:
            groups[ROUTING_REQUIRED_LABEL] = other
        if not other:
            return groups

        unresolved_set = set(other)
        unresolved_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() in unresolved_set
        ]
        semantic = self._semantic_outline_groups(
            unresolved_rows,
            text_by_paper,
            tag_key=tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        repaired = {
            label: list(paper_ids)
            for label, paper_ids in groups.items()
            if label != ROUTING_REQUIRED_LABEL and paper_ids
        }
        for label, paper_ids in semantic.items():
            if label == ROUTING_REQUIRED_LABEL or not paper_ids:
                continue
            bucket = repaired.setdefault(label, [])
            bucket.extend(paper_id for paper_id in paper_ids if paper_id not in bucket)
        still_unresolved = list(semantic.get(ROUTING_REQUIRED_LABEL) or [])
        if still_unresolved:
            # Keep unresolved classification as workflow state. It must not be
            # converted into a reader-facing catch-all or "boundary" chapter.
            repaired[ROUTING_REQUIRED_LABEL] = still_unresolved
        return repaired or groups

    def _auto_repair_generated_routing_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
        tag_key_override: str = "",
        axis_label_override: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve system-created routing placeholders before Blueprint build.

        User-authored catch-all sections remain subject to the normal academic
        gate.  Only the exact placeholder emitted by older built-in outline
        versions is repaired automatically. Taxonomy matches are merged into
        an existing same-title section when possible; otherwise a defensible
        category is inserted before the conclusion. Truly unresolved primary
        studies remain explicit Matrix routing state instead of being relabeled
        as Introduction context or exposed as a reader-facing ``Other`` section.
        """

        style = str(outline_style or "").casefold()
        definition = OUTLINE_STYLES.get(style)
        if definition is None:
            return sections, []
        routing_tag_key = str(tag_key_override or definition["tag_key"])
        routing_axis_label = str(axis_label_override or definition["axis"])

        repaired = deepcopy(sections)
        repairable_titles = {
            ROUTING_REQUIRED_LABEL.casefold(),
            CROSS_CATEGORY_BOUNDARY_LABEL.casefold(),
        }
        placeholder_indexes = [
            index
            for index, section in enumerate(repaired)
            if str(section.get("section_role") or "body").casefold() == "body"
            and str(section.get("title") or "").strip().casefold()
            in repairable_titles
        ]
        if not placeholder_indexes:
            return repaired, []

        unresolved_ids = list(
            dict.fromkeys(
                paper_id
                for index in placeholder_indexes
                for paper_id in (repaired[index].get("paper_ids") or [])
            )
        )
        unresolved_set = set(unresolved_ids)
        unresolved_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() in unresolved_set
        ]
        semantic = self._semantic_outline_groups(
            unresolved_rows,
            text_by_paper,
            tag_key=routing_tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        grouped: dict[str, list[str]] = {
            label: list(dict.fromkeys(paper_ids))
            for label, paper_ids in semantic.items()
            if label != ROUTING_REQUIRED_LABEL and paper_ids
        }
        routed = {paper_id for paper_ids in grouped.values() for paper_id in paper_ids}
        still_unresolved = [paper_id for paper_id in unresolved_ids if paper_id not in routed]

        repaired = [
            section
            for index, section in enumerate(repaired)
            if index not in placeholder_indexes
        ]
        existing_by_title = {
            str(section.get("title") or "").strip().casefold(): section
            for section in repaired
            if str(section.get("section_role") or "body").casefold() == "body"
        }
        insert_at = next(
            (
                index
                for index, section in enumerate(repaired)
                if str(section.get("section_role") or "").casefold() == "conclusion"
            ),
            len(repaired),
        )
        adjustments: list[dict[str, Any]] = []
        source_titles = list(
            dict.fromkeys(
                str(sections[index].get("title") or ROUTING_REQUIRED_LABEL)
                for index in placeholder_indexes
            )
        )
        if still_unresolved:
            adjustments.append(
                {
                    "source_section": ", ".join(source_titles),
                    "target_section": "Matrix routing unresolved",
                    "paper_ids": list(still_unresolved),
                    "method": "unresolved_classification_retained",
                    "created_section": False,
                }
            )
        for label, paper_ids in grouped.items():
            public_label = publication_section_title("", label)
            target = existing_by_title.get(public_label.casefold())
            created = target is None
            if target is None:
                target = {
                    "title": public_label,
                    "paper_ids": [],
                    "context_paper_ids": [],
                    "section_role": "body",
                    "purpose": (
                        f"compare the selected papers within this {routing_axis_label} "
                        "category and state its evidence boundaries."
                    ),
                    "notes": "Automatically routed from a system-generated placeholder.",
                }
                if label == CROSS_CATEGORY_BOUNDARY_LABEL:
                    target["boundary_rationale"] = (
                        "The available source evidence does not support assigning these papers "
                        "to one primary-axis category; retain them for explicit cross-category "
                        "comparison until a narrower evidence-backed route is available."
                    )
                repaired.insert(insert_at, target)
                insert_at += 1
                existing_by_title[public_label.casefold()] = target
            bucket = target.setdefault("paper_ids", [])
            bucket.extend(paper_id for paper_id in paper_ids if paper_id not in bucket)
            adjustments.append(
                {
                    "source_section": ", ".join(source_titles),
                    "target_section": public_label,
                    "paper_ids": list(paper_ids),
                    "method": "taxonomy_evidence_match",
                    "created_section": created,
                }
            )
        return repaired, adjustments

    @classmethod
    def _contextual_outline_paper_ids(
        cls,
        rows: list[dict[str, Any]],
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
    ) -> list[str]:
        """Identify sources that frame the field but are not primary studies.

        These papers remain available to the introduction as contextual
        evidence.  They are not forced into a body taxonomy where a review or
        perspective would create an artificial catch-all category.
        """

        contextual: list[str] = []
        scope_terms = (
            "review",
            "comprehensive review",
            "account",
            "perspective",
            "book",
            "book chapter",
        )
        context_pattern = re.compile(
            r"\b(?:this|the present) review\b|\bwe review\b|"
            r"\breview (?:will|article|paper)\b|\bcomprehensive review\b|"
            r"\bperspective (?:on|article)\b|"
            r"\b(?:this|the|an) account\b|"
            r"\baccount (?:of|on|surveys|reviews|concerns|summarizes)\b",
            re.I,
        )
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            if not paper_id:
                continue
            document_scope = cls._tag_value(
                (tags_by_paper.get(paper_id) or {}).get("document_scope")
            ).casefold()
            text = text_by_paper.get(paper_id, "")
            if any(term in document_scope for term in scope_terms) or context_pattern.search(text):
                contextual.append(paper_id)
        return contextual

    @staticmethod
    def _sanitize_generated_outline_titles(
        sections: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep classification diagnostics out of reader-facing headings."""

        repaired = deepcopy(sections)
        adjustments: list[dict[str, Any]] = []
        for section in repaired:
            if str(section.get("section_role") or "body").casefold() != "body":
                continue
            original = str(section.get("title") or "").strip()
            public = sanitize_internal_section_title(
                original,
                topic_partition=section.get("topic_partition"),
            )
            if not public or public == original:
                continue
            section["title"] = public
            adjustments.append(
                {
                    "source_section": original,
                    "target_section": public,
                    "paper_ids": list(section.get("paper_ids") or []),
                    "method": "publication_title_sanitization",
                    "created_section": False,
                }
            )
        return repaired, adjustments

    def _realign_generated_body_sections(
        self,
        sections: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        text_by_paper: dict[str, str],
        *,
        outline_style: str,
        taxonomy_profile: str,
        tag_key_override: str = "",
        axis_label_override: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Realign a system outline when scientific facts contradict old routing.

        Saved generated outlines can predate fact extraction or taxonomy fixes.
        This pass is intentionally disabled for manually edited outlines.  It
        preserves roles and ordering while moving each paper to the category
        supported by its source-addressable study object.
        """

        definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
        if definition is None:
            return deepcopy(sections), []
        routing_tag_key = str(tag_key_override or definition["tag_key"])
        routing_axis_label = str(axis_label_override or definition["axis"])
        body_paper_ids = list(
            dict.fromkeys(
                str(paper_id)
                for section in sections
                if str(section.get("section_role") or "body").casefold() == "body"
                for paper_id in section.get("paper_ids") or []
                if str(paper_id or "").strip()
            )
        )
        body_set = set(body_paper_ids)
        fact_evidence_ids = {
            str(row.get("paper_id") or "")
            for row in rows
            if str(row.get("paper_id") or "") in body_set
            and any(
                isinstance(fact, dict)
                and str(fact.get("field_id") or "") != "abstract_summary"
                and str(fact.get("value") or "").strip()
                for fact in row.get("scientific_facts") or []
            )
        }
        if not fact_evidence_ids:
            return deepcopy(sections), []
        semantic = self._semantic_outline_groups(
            [
                row
                for row in rows
                if str(row.get("paper_id") or "").strip() in fact_evidence_ids
            ],
            text_by_paper,
            tag_key=routing_tag_key,
            taxonomy_profile=taxonomy_profile,
        )
        target_by_paper: dict[str, str] = {}
        for label, paper_ids in semantic.items():
            if label == ROUTING_REQUIRED_LABEL:
                continue
            target = label
            for paper_id in paper_ids:
                target_by_paper.setdefault(str(paper_id), target)

        repaired = deepcopy(sections)
        body_snapshot = [
            (section, list(section.get("paper_ids") or []))
            for section in repaired
            if str(section.get("section_role") or "body").casefold() == "body"
        ]
        existing_by_title = {
            str(section.get("title") or "").strip().casefold(): section
            for section, _paper_ids_snapshot in body_snapshot
        }
        insert_at = next(
            (
                index
                for index, section in enumerate(repaired)
                if infer_section_role(
                    section.get("title"), section.get("section_role")
                )
                == "conclusion"
            ),
            len(repaired),
        )
        adjustments: list[dict[str, Any]] = []
        for source, source_papers in body_snapshot:
            source_title = str(source.get("title") or "").strip()
            source_partition = str(source.get("topic_partition") or "").strip()
            for paper_id in source_papers:
                target_category = target_by_paper.get(str(paper_id))
                if not target_category:
                    continue
                display_category = target_category
                target_title = publication_section_title(
                    _capitalize_outline_heading(source_partition),
                    (
                        _capitalize_outline_heading(display_category)
                        if source_partition
                        else display_category
                    ),
                )
                if not target_title or target_title.casefold() == source_title.casefold():
                    continue
                source["paper_ids"] = [
                    current
                    for current in source.get("paper_ids") or []
                    if str(current) != str(paper_id)
                ]
                target = existing_by_title.get(target_title.casefold())
                created = target is None
                if target is None:
                    target = {
                        "title": target_title,
                        "paper_ids": [],
                        "context_paper_ids": [],
                        "section_role": "body",
                        "topic_partition": source_partition,
                        "purpose": (
                            f"compare the selected papers within this {routing_axis_label} "
                            "category and state its evidence boundaries."
                        ),
                        "notes": "Automatically realigned from source-addressable scientific facts.",
                    }
                    repaired.insert(insert_at, target)
                    insert_at += 1
                    existing_by_title[target_title.casefold()] = target
                if paper_id not in target["paper_ids"]:
                    target["paper_ids"].append(paper_id)
                adjustments.append(
                    {
                        "source_section": source_title,
                        "target_section": target_title,
                        "paper_ids": [paper_id],
                        "method": "scientific_object_reassignment",
                        "created_section": created,
                    }
                )
        repaired = [
            section
            for section in repaired
            if str(section.get("section_role") or "body").casefold() != "body"
            or bool(section.get("paper_ids"))
        ]
        return repaired, adjustments

    def _topic_outline_document(
        self,
        rows: list[dict[str, Any]],
        *,
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        taxonomy_profile: str,
        intent: dict[str, Any],
    ) -> str:
        """Build a Matrix-grounded outline from explicit Topic organization.

        The Topic controls the hierarchy, while Matrix evidence controls paper
        placement. This prevents the recommendation from becoming a decorative
        restatement of the prompt or assigning papers by prompt words alone.
        """

        primary_axis = str(intent.get("primary_axis") or "reaction_type")
        secondary_axes = [
            str(axis)
            for axis in intent.get("secondary_axes") or []
            if str(axis) and str(axis) != primary_axis
        ]
        partitions = [
            _clean_topic_partition(label)
            for label in (
                intent.get("required_partitions")
                or intent.get("partitions")
                or []
            )
            if _clean_topic_partition(label)
        ]
        comparison_dimensions = [
            str(value).strip()
            for value in (
                intent.get("comparison_dimensions")
                or intent.get("named_systems")
                or []
            )
            if str(value).strip()
        ]
        focus_dimensions = [
            str(value).strip()
            for value in (
                intent.get("focus_dimensions")
                or intent.get("outcome_dimensions")
                or intent.get("requested_outcomes")
                or []
            )
            if str(value).strip()
        ]
        contextual_paper_ids = self._contextual_outline_paper_ids(
            rows,
            tags_by_paper,
            text_by_paper,
        )
        for row in rows:
            paper_id = str(row.get("paper_id") or "").strip()
            positively_out_of_scope = any(
                isinstance(outcome, dict)
                and str(outcome.get("status") or "") == "out_of_scope"
                and bool(outcome.get("evidence_refs"))
                for outcome in row.get("classification_outcomes") or []
            ) or (
                str(
                    (row.get("topic_partition_classification") or {}).get("status")
                    or ""
                )
                == "out_of_scope"
                and bool(
                    (row.get("topic_partition_classification") or {}).get(
                        "evidence_refs"
                    )
                )
            )
            if paper_id and positively_out_of_scope and paper_id not in contextual_paper_ids:
                contextual_paper_ids.append(paper_id)
        contextual_set = set(contextual_paper_ids)
        analytical_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() not in contextual_set
        ]
        partitioned_rows: dict[str, list[dict[str, Any]]] = {}
        if partitions:
            for row in analytical_rows:
                paper_id = str(row.get("paper_id") or "").strip()
                label = _topic_partition_for_row(
                    row,
                    partitions,
                    text_by_paper.get(paper_id, ""),
                )
                partitioned_rows.setdefault(label, []).append(row)
        else:
            partitioned_rows[""] = analytical_rows

        primary_label = str(
            intent.get("primary_axis_label")
            or (TOPIC_AXIS_LABELS.get(primary_axis) or {}).get("en")
            or primary_axis.replace("_", " ")
        )
        secondary_axis_labels = dict(intent.get("secondary_axis_labels") or {})
        secondary_labels = [
            str(
                secondary_axis_labels.get(axis)
                or (TOPIC_AXIS_LABELS.get(axis) or {}).get("en")
                or axis.replace("_", " ")
            )
            for axis in secondary_axes
        ]
        structure_description = primary_label
        if secondary_labels:
            structure_description += ", with " + ", ".join(secondary_labels) + " as secondary axes"
        if partitions:
            structure_description += "; separate " + " versus ".join(partitions)
        if focus_dimensions:
            structure_description += "; cover " + ", ".join(focus_dimensions)
        ordered_partitions = [
            *[label for label in partitions if label in partitioned_rows],
            *[label for label in partitioned_rows if label not in partitions],
        ]
        groups_by_partition: dict[str, dict[str, list[str]]] = {}
        for partition in ordered_partitions:
            groups = self._outline_groups(
                partitioned_rows.get(partition) or [],
                tags_by_paper,
                text_by_paper,
                tag_key=primary_axis,
                taxonomy_profile=taxonomy_profile,
            )
            # An unresolved primary-study route is not evidence that the paper
            # is a review or an Introduction-only source. Keep its Matrix
            # routing status explicit instead of silently changing its role.
            groups.pop(ROUTING_REQUIRED_LABEL, None)
            groups_by_partition[partition] = groups
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: Topic-guided ({structure_description}).",
            (
                "This system-recommended organization is grounded in the selected Matrix evidence and remains editable before Blueprint generation."
                if intent.get("system_recommended")
                else "This recommendation implements the explicit organization instructions in the user Topic and remains editable before Blueprint generation."
            ),
            "",
            "## Introduction",
            "Section role: introduction",
            "Purpose: define the review terminology, scope, explicit focus dimensions, and the evidence basis for the Topic-requested organization.",
        ]
        if focus_dimensions:
            lines.append(
                "Focus dimensions: " + ", ".join(focus_dimensions) + "."
            )
        if contextual_paper_ids:
            lines.extend(
                [
                    f"Context papers: {', '.join(contextual_paper_ids)}.",
                    "Notes: Use field-level sources for terminology and historical framing, not as primary body evidence.",
                ]
            )
        lines.append("")

        body_index = 0
        for partition in ordered_partitions:
            partition_rows = partitioned_rows.get(partition) or []
            groups = groups_by_partition.get(partition) or {}
            for group, paper_ids in groups.items():
                if not paper_ids:
                    continue
                body_index += 1
                partition_label = (
                    _capitalize_outline_heading(partition) if partition else ""
                )
                group_title = _capitalize_outline_heading(group)
                title = publication_section_title(partition_label, group_title)
                purpose_parts = [
                    f"compare the selected evidence within this {primary_label} category"
                ]
                if secondary_labels:
                    purpose_parts.append(
                        "compare " + ", ".join(secondary_labels) + " within the category"
                    )
                if comparison_dimensions:
                    purpose_parts.append(
                        "track the explicitly named comparison examples where supported: "
                        + ", ".join(comparison_dimensions)
                    )
                if focus_dimensions:
                    purpose_parts.append(
                        "cover the requested focus dimensions where supported: "
                        + ", ".join(focus_dimensions)
                    )
                paper_id_set = set(paper_ids)
                group_rows = [
                    row
                    for row in partition_rows
                    if str(row.get("paper_id") or "") in paper_id_set
                ]
                represented_secondary: dict[str, list[str]] = {}
                for secondary_axis in secondary_axes:
                    secondary_groups = self._outline_groups(
                        group_rows,
                        tags_by_paper,
                        text_by_paper,
                        tag_key=secondary_axis,
                        taxonomy_profile=taxonomy_profile,
                    )
                    represented_secondary[secondary_axis] = [
                        label
                        for label, assigned in secondary_groups.items()
                        if assigned and label != ROUTING_REQUIRED_LABEL
                    ]
                lines.extend(
                    [
                        f"## {body_index}. {title}",
                        "Section role: body",
                        f"Assigned papers: {', '.join(paper_ids)}.",
                        *(
                            [f"Topic partition: {partition}."]
                            if partition in partitions
                            else []
                        ),
                        "Purpose: " + "; ".join(purpose_parts) + ".",
                    ]
                )
                notes: list[str] = []
                for secondary_axis, represented in represented_secondary.items():
                    if represented:
                        notes.append(
                            str(
                                secondary_axis_labels.get(secondary_axis)
                                or (TOPIC_AXIS_LABELS.get(secondary_axis) or {}).get("en")
                                or secondary_axis.replace("_", " ")
                            ).capitalize()
                            + " represented by assigned evidence: "
                            + ", ".join(represented)
                            + "."
                        )
                if notes:
                    normalized_notes = " ".join(
                        note.removeprefix("Notes: ").strip() for note in notes
                    )
                    lines.append(f"Notes: {normalized_notes}")
                lines.append("")
        lines.extend(
            [
                "## Cross-regime comparison, limitations, and outlook",
                "Section role: conclusion",
                "Purpose: compare the primary categories, secondary axes, explicit focus dimensions, evidence boundaries, limitations, and future directions across the Topic-requested partitions.",
                "",
            ]
        )
        return "\n".join(lines)

    def _outline_document(
        self,
        style: str,
        rows: list[dict[str, Any]],
        *,
        tags_by_paper: dict[str, dict[str, Any]],
        text_by_paper: dict[str, str],
        taxonomy_profile: str,
    ) -> str:
        definition = OUTLINE_STYLES[style]
        contextual_paper_ids = self._contextual_outline_paper_ids(
            rows,
            tags_by_paper,
            text_by_paper,
        )
        contextual_set = set(contextual_paper_ids)
        analytical_rows = [
            row
            for row in rows
            if str(row.get("paper_id") or "").strip() not in contextual_set
        ]
        groups = self._outline_groups(
            analytical_rows,
            tags_by_paper,
            text_by_paper,
            tag_key=definition["tag_key"],
            taxonomy_profile=taxonomy_profile,
        )
        # Only document-scope evidence may create an Introduction context
        # source. Failed taxonomy routing remains an explicit Matrix state.
        groups.pop(ROUTING_REQUIRED_LABEL, None)
        lines = [
            "# Selected Outline",
            "",
            f"Primary structure: {definition['en']}.",
            "This working outline remains fully editable before Blueprint generation.",
            "",
            "## Introduction",
            "Section role: introduction",
            f"Purpose: {definition['introduction']}.",
        ]
        if contextual_paper_ids:
            lines.extend(
                [
                    f"Context papers: {', '.join(contextual_paper_ids)}.",
                    "Notes: Use these field-level sources for scope and historical framing; do not treat them as primary body evidence.",
                ]
            )
        lines.append("")
        for index, (label, paper_ids) in enumerate(groups.items(), start=1):
            public_label = publication_section_title(
                "", _capitalize_outline_heading(label)
            )
            block = [
                f"## {index}. {public_label}",
                "Section role: body",
                f"Assigned papers: {', '.join(paper_ids)}.",
                f"Purpose: compare the selected papers within this {definition['axis']} category.",
            ]
            lines.extend([*block, ""])
        lines.extend(
            [
                "## Cross-category comparison and conclusion",
                "Section role: conclusion",
                "Purpose: compare the main systems, outcomes, evidence boundaries, limitations, and future directions.",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _validate_outline(markdown: str, matrix_ids: set[str]) -> str:
        text = str(markdown or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise WorkflowValidationError("Outline Markdown must not be empty.")
        if len(text) > 250_000:
            raise WorkflowValidationError("Outline Markdown exceeds 250,000 characters.")
        sections = _outline_sections(text)
        if not sections:
            raise WorkflowValidationError(
                "Outline Markdown needs at least one level-2 heading (##)."
            )
        missing = [
            section["title"]
            for section in sections
            if not section["paper_ids"]
            and section.get("section_role")
            not in {"introduction", "conclusion", "references"}
        ]
        if missing:
            raise WorkflowValidationError(
                "Every major section must assign at least one paper.",
                details={"sections": missing},
            )
        unknown = sorted(
            {
                paper_id
                for section in sections
                for paper_id in [
                    *section["paper_ids"],
                    *section.get("context_paper_ids", []),
                ]
                if paper_id not in matrix_ids
            }
        )
        if unknown:
            raise WorkflowValidationError(
                "Outline paper assignments must resolve to the current Matrix.",
                details={"paper_ids": unknown},
            )
        return text + "\n"

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix, bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME, required=False
        )
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        blueprint, blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        rows = matrix["rows"]
        project = self._owned_project(principal, project_id)
        tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
        review_topic = str(
            matrix.get("review_topic") or (discovery or {}).get("topic") or ""
        )
        topic_intent = _topic_outline_intent(
            review_topic,
            discovery,
            list(matrix.get("classification_axes") or []),
        )
        selected_ids: list[str] = []
        for group in (discovery or {}).get("results") or []:
            if not isinstance(group, dict) or group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                if (
                    isinstance(row, dict)
                    and row.get("selected_for_matrix")
                    and str(row.get("role") or "") != "excluded"
                ):
                    paper_id = str(row.get("paper_id") or "")
                    if paper_id and paper_id not in selected_ids:
                        selected_ids.append(paper_id)
        matrix_sync = dict(matrix.get("sync") or {})
        selection_current = bool(
            selected_ids
            and set(_paper_ids(rows)) == set(selected_ids)
        )
        generated = [
            {
                "candidate_id": style,
                "outline_style": style,
                "labels": {"en": definition["en"], "zh": definition["zh"]},
                "outline_md": self._outline_document(
                    style,
                    rows,
                    tags_by_paper=tags_by_paper,
                    text_by_paper=text_by_paper,
                    taxonomy_profile=project.taxonomy_profile,
                ),
                "source": "builtin",
            }
            for style, definition in OUTLINE_STYLES.items()
            if style != TOPIC_GUIDED_STYLE
        ]
        if topic_intent.get("available"):
            generated.insert(
                0,
                {
                    "candidate_id": TOPIC_GUIDED_STYLE,
                    "outline_style": TOPIC_GUIDED_STYLE,
                    "labels": {
                        "en": "Recommended from your Topic",
                        "zh": "根据你的 Topic 推荐",
                    },
                    "outline_md": self._topic_outline_document(
                        rows,
                        tags_by_paper=tags_by_paper,
                        text_by_paper=text_by_paper,
                        taxonomy_profile=project.taxonomy_profile,
                        intent=topic_intent,
                    ),
                    "source": "topic",
                    "topic_outline_intent": topic_intent,
                },
            )
        if outline_artifact is not None and outline is not None:
            generated.append(
                {
                    "candidate_id": "saved-current",
                    "outline_style": outline.get("outline_style", "custom"),
                    "labels": {"en": "Saved outline", "zh": "已保存大纲"},
                    "outline_md": _sanitize_outline_markdown_headings(
                        outline.get("outline_md")
                    ),
                    "source": "saved",
                    "artifact_id": outline_artifact.id,
                }
            )
        all_reference_candidates = list((references or {}).get("candidates") or [])
        reference_candidates = [
            candidate
            for candidate in all_reference_candidates
            if self._reference_candidate_is_isolated(candidate)
        ]
        outline_compatible_ids = {
            str(artifact_id)
            for artifact_id in matrix.get("outline_compatible_matrix_artifact_ids") or []
            if str(artifact_id)
        }
        # Backward compatibility for enrichment artifacts created before the
        # explicit compatibility lineage was introduced.
        enrichment_source_id = str(
            (matrix.get("fact_enrichment_summary") or {}).get(
                "source_matrix_artifact_id"
            )
            or ""
        )
        if enrichment_source_id:
            outline_compatible_ids.add(enrichment_source_id)
        outline_source_id = str((outline or {}).get("source_matrix_artifact_id") or "")
        outline_current = bool(
            outline is not None
            and outline_artifact is not None
            and (
                outline_source_id == matrix_artifact.id
                or outline_source_id in outline_compatible_ids
            )
        )
        blueprint_current = bool(
            blueprint is not None
            and blueprint_artifact is not None
            and outline_artifact is not None
            and outline_current
            and str(blueprint.get("source_matrix_artifact_id") or "")
            == matrix_artifact.id
            and str(blueprint.get("source_outline_artifact_id") or "")
            == outline_artifact.id
            and blueprint_state is not None
            and blueprint_state.status != "stale"
        )
        public_outline = deepcopy(outline) if isinstance(outline, dict) else None
        if public_outline is not None:
            public_outline["outline_md"] = _sanitize_outline_markdown_headings(
                public_outline.get("outline_md")
            )
        public_blueprint = (
            deepcopy(blueprint) if isinstance(blueprint, dict) else None
        )
        if public_blueprint is not None:
            for section in public_blueprint.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                section["title"] = sanitize_internal_section_title(
                    section.get("title"),
                    topic_partition=section.get("topic_partition"),
                )
            public_blueprint["section_writing_plan_md"] = (
                _sanitize_outline_markdown_headings(
                    public_blueprint.get("section_writing_plan_md")
                )
            )
        scope_contract = dict((outline or {}).get("scope_contract") or {})
        scope_report = dict((outline or {}).get("scope_diagnostics") or {})
        coverage_report = dict((outline or {}).get("coverage_diagnostics") or {})
        basis = dict((outline or {}).get("classification_basis") or {})
        public_classification_contract = dict(
            (blueprint or {}).get("classification_contract")
            or (outline or {}).get("classification_contract")
            or matrix.get("classification_contract")
            or {}
        )
        outline_diagnostics = dict((outline or {}).get("taxonomy_diagnostics") or {})
        enrichment_jobs = self.repository.list_project_jobs(
            principal.user_id, project_id, job_type="matrix.enrich", limit=20
        )
        enrichment_counts = {
            status: sum(
                1
                for row in rows
                if str((row.get("fact_enrichment") or {}).get("status") or "pending")
                == status
            )
            for status in ("pending", "complete", "partial", "limited", "failed")
        }
        enrichment_summary = dict(matrix.get("fact_enrichment_summary") or {})
        all_enrichment_failed = bool(rows) and enrichment_counts["failed"] == len(rows)
        latest_enrichment_job = enrichment_jobs[0] if enrichment_jobs else None
        failed_publish_with_pending_rows = bool(
            enrichment_counts["pending"]
            and latest_enrichment_job is not None
            and latest_enrichment_job.status in {"failed", "cancelled", "interrupted"}
        )
        return {
            "project_id": project_id,
            "topic": review_topic,
            "literature_matrix": matrix,
            "matrix_artifact_id": matrix_artifact.id,
            "bibliography_metadata_artifact_ids": bibliography_metadata_artifact_ids,
            "matrix_revision": matrix_state.revision if matrix_state else 0,
            "matrix_sync": {**matrix_sync, "selection_current": selection_current},
            "matrix_enrichment": {
                "summary": enrichment_summary,
                "counts": enrichment_counts,
                "jobs": [_planning_job_payload(job) for job in enrichment_jobs],
                "all_failed": all_enrichment_failed,
                "failed_publish_with_pending_rows": failed_publish_with_pending_rows,
                "limited_mode_confirmed": bool(
                    enrichment_summary.get("limited_mode_confirmed")
                ),
                "planning_blocked": bool(
                    (
                        all_enrichment_failed
                        and not enrichment_summary.get("limited_mode_confirmed")
                    )
                    or failed_publish_with_pending_rows
                ),
            },
            "discovery_selection": {
                "selected_paper_count": len(selected_ids),
                "selected_paper_ids": selected_ids,
                "selection_current": selection_current,
            },
            "selected_outline_md": str(
                (public_outline or {}).get("outline_md") or ""
            ),
            "outline_selection": (
                {**public_outline, "artifact_id": outline_artifact.id}
                if public_outline is not None and outline_artifact is not None
                else None
            ),
            "outline_current": outline_current,
            "scope_contract": scope_contract,
            "scope_diagnostics": dict(
                (blueprint or {}).get("scope_diagnostics")
                or scope_report
            ),
            "coverage_diagnostics": dict(
                (blueprint or {}).get("coverage_diagnostics")
                or coverage_report
            ),
            "classification_basis": basis,
            "classification_contract": public_classification_contract,
            "taxonomy_diagnostics": dict(
                (blueprint or {}).get("taxonomy_diagnostics")
                or outline_diagnostics
            ),
            "outline_candidates": generated + reference_candidates,
            "reference_outline_candidates": reference_candidates,
            "legacy_reference_outline_count": len(all_reference_candidates)
            - len(reference_candidates),
            "section_blueprint": public_blueprint,
            "blueprint_artifact_id": blueprint_artifact.id if blueprint_artifact else None,
            "blueprint_revision": blueprint_state.revision if blueprint_state else 0,
            "blueprint_current": blueprint_current,
            "section_writing_plan_md": str(
                (public_blueprint or {}).get("section_writing_plan_md") or ""
            ),
            "workspace": {
                "active_stage": "planning",
                "tabs": [
                    {
                        "id": "matrix",
                        "labels": {"en": "Literature Matrix", "zh": "文献矩阵"},
                    },
                    {
                        "id": "blueprint",
                        "labels": {"en": "Blueprint", "zh": "章节蓝图"},
                    },
                ],
            },
        }

    def update_matrix_row(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        *,
        revision: int,
        main_content: str | None,
        most_relevant_figure: dict[str, Any] | None,
        scientific_facts: list[dict[str, Any]] | None,
        mark_complete: bool,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        updated = deepcopy(matrix)
        row = next(
            (
                item
                for item in updated["rows"]
                if isinstance(item, dict) and str(item.get("paper_id")) == paper_id
            ),
            None,
        )
        if row is None:
            raise WorkflowNotFound("Matrix paper was not found.")
        if main_content is not None:
            row["main_content"] = str(main_content).strip()
        if most_relevant_figure is not None:
            row["most_relevant_figure"] = dict(most_relevant_figure)
        if scientific_facts is not None:
            existing_facts = {
                str(item.get("fact_id") or ""): item
                for item in row.get("scientific_facts") or []
                if isinstance(item, dict) and item.get("fact_id")
            }
            submitted_ids = {
                str(item.get("fact_id") or "")
                for item in scientific_facts
                if isinstance(item, dict) and item.get("fact_id")
            }
            if submitted_ids != set(existing_facts):
                raise WorkflowValidationError(
                    "Matrix fact edits must preserve the current source-addressable fact set."
                )
            revised_facts = []
            for submitted in scientific_facts:
                fact_id = str(submitted.get("fact_id") or "")
                current = existing_facts[fact_id]
                value = " ".join(str(submitted.get("value") or "").split()).strip()
                ceiling = " ".join(
                    str(submitted.get("evidence_ceiling") or "").split()
                ).strip()
                if not value or len(value) > 4000 or len(ceiling) > 2000:
                    raise WorkflowValidationError(
                        "A Matrix fact edit has an invalid value or evidence ceiling."
                    )
                revised_facts.append(
                    {
                        **current,
                        "value": value,
                        "evidence_ceiling": ceiling
                        or str(current.get("evidence_ceiling") or ""),
                        "human_checked": True,
                        "review_status": "human_edited",
                        "human_edited_at": utc_now().isoformat(),
                    }
                )
            row["scientific_facts"] = revised_facts
        if mark_complete and len(re.sub(r"\s+", "", str(row.get("main_content") or ""))) < 300:
            raise WorkflowConflict(
                "Add at least 300 characters of full-paper reading notes before marking this paper complete."
            )
        row["matrix_status"] = (
            "full_reading_complete" if mark_complete else "needs_full_reading"
        )
        updated.pop("outline_compatible_matrix_artifact_ids", None)
        updated["updated_at"] = utc_now().isoformat()
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(updated), "json")},
                input_snapshot={"paper_id": paper_id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "row": row,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def save_outline(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        outline_style: str,
        outline_md: str | None,
        manual: bool,
        scope_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix, _bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        project = self._owned_project(principal, project_id)
        rows = matrix["rows"]
        matrix_ids = set(_paper_ids(rows))
        style = str(outline_style or "").strip().casefold()
        discovery, _discovery_artifact = self._read_json(
            principal,
            project_id,
            DISCOVERY_LOGICAL_NAME,
            required=False,
        )
        review_topic = str(
            matrix.get("review_topic") or (discovery or {}).get("topic") or ""
        )
        topic_intent = _topic_outline_intent(
            review_topic,
            discovery,
            list(matrix.get("classification_axes") or []),
        )
        topic_text_by_paper: dict[str, str] = {}
        if style == "custom" and not manual:
            markdown = ""
            complete = False
        elif style.startswith("reference:") and not manual:
            references, _artifact = self._read_json(
                principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
            )
            candidate_id = style.removeprefix("reference:")
            candidate = next(
                (
                    item
                    for item in (references or {}).get("candidates") or []
                    if str(item.get("candidate_id")) == candidate_id
                ),
                None,
            )
            if not isinstance(candidate, dict):
                raise WorkflowNotFound("Reference outline candidate was not found.")
            if not self._reference_candidate_is_isolated(candidate):
                raise WorkflowConflict(
                    "This legacy reference outline did not pass content isolation. Upload the reference again to learn format only."
                )
            markdown = self._validate_outline(
                str(candidate.get("outline_md") or ""), matrix_ids
            )
            complete = True
        elif manual:
            if style != "custom" and style not in OUTLINE_STYLES and not style.startswith("reference:"):
                raise WorkflowValidationError("Unknown outline style.")
            markdown = self._validate_outline(str(outline_md or ""), matrix_ids)
            complete = True
        elif style == TOPIC_GUIDED_STYLE:
            if not topic_intent.get("available"):
                raise WorkflowValidationError(
                    "The Topic does not contain a usable organization instruction."
                )
            tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
            topic_text_by_paper = text_by_paper
            markdown = self._validate_outline(
                self._topic_outline_document(
                    rows,
                    tags_by_paper=tags_by_paper,
                    text_by_paper=text_by_paper,
                    taxonomy_profile=project.taxonomy_profile,
                    intent=topic_intent,
                ),
                matrix_ids,
            )
            complete = True
        else:
            if style not in OUTLINE_STYLES:
                raise WorkflowValidationError("Unknown outline style.")
            tags_by_paper, text_by_paper = self._outline_sources(principal, rows)
            markdown = self._outline_document(
                style,
                rows,
                tags_by_paper=tags_by_paper,
                text_by_paper=text_by_paper,
                taxonomy_profile=project.taxonomy_profile,
            )
            complete = True
        current_outline, current_outline_artifact = self._read_json(
            principal,
            project_id,
            OUTLINE_LOGICAL_NAME,
            required=False,
        )
        parsed_sections = _outline_sections(markdown) if complete else []
        previous_scope = (
            (current_outline or {}).get("scope_contract")
            if isinstance(current_outline, dict)
            else None
        )
        previous_style = str((current_outline or {}).get("outline_style") or "")
        scope_input: dict[str, Any] | None = None
        if isinstance(scope_contract, dict):
            scope_input = {**scope_contract, "source": "user_edited"}
        elif (
            isinstance(previous_scope, dict)
            and previous_scope.get("source") == "user_edited"
            and previous_style == style
        ):
            scope_input = previous_scope
        scope_seed = dict(scope_input or {})
        scope_seed.setdefault(
            "coverage_mode", str(matrix.get("coverage_mode") or "local_bounded")
        )
        discovery_coverage = matrix.get("coverage_diagnostics")
        if isinstance(discovery_coverage, dict):
            scope_seed.setdefault(
                "discovery_coverage_diagnostics", deepcopy(discovery_coverage)
            )
        scope_style = (
            "reaction"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "reaction_type"
            else "catalyst"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "catalyst_or_method"
            else "substrate"
            if style == TOPIC_GUIDED_STYLE
            and topic_intent.get("primary_axis") == "substrate"
            else style
        )
        scope = derive_scope_contract(
            matrix.get("review_topic"),
            scope_style,
            rows,
            current=scope_seed,
        )
        if style == TOPIC_GUIDED_STYLE:
            scope["primary_navigation_axis"] = str(
                topic_intent.get("primary_axis") or "reaction_type"
            )
            scope["secondary_axes"] = list(topic_intent.get("secondary_axes") or [])
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, rows)
        basis = classification_basis(scope_style)
        if style == TOPIC_GUIDED_STYLE:
            required_partitions = list(
                topic_intent.get("required_partitions")
                or topic_intent.get("partitions")
                or []
            )
            if not topic_text_by_paper:
                _topic_tags, topic_text_by_paper = self._outline_sources(
                    principal, rows
                )
            represented_partitions = {
                label
                for row in rows
                if (
                    label := _topic_partition_for_row(
                        row,
                        required_partitions,
                        topic_text_by_paper.get(
                            str(row.get("paper_id") or ""), ""
                        ),
                    )
                )
                in required_partitions
            }
            partition_coverage_boundaries = {
                partition: {
                    "reason": (
                        "No selected Matrix paper currently has a source-supported, "
                        "high-confidence route to this independently requested partition."
                    ),
                    "source": "matrix_evidence_partition_classifier",
                }
                for partition in required_partitions
                if partition not in represented_partitions
            }
            basis.update(
                {
                    "primary_axis": scope["primary_navigation_axis"],
                    "overview_axis": scope["primary_navigation_axis"],
                    "orthogonal_axes": list(topic_intent.get("secondary_axes") or []),
                    "overview_secondary_axes": list(
                        topic_intent.get("secondary_axes") or []
                    ),
                    "topic_partitions": required_partitions,
                    "required_outline_partitions": required_partitions,
                    "topic_partition_coverage_boundaries": partition_coverage_boundaries,
                    "topic_comparison_dimensions": list(
                        topic_intent.get("comparison_dimensions")
                        or topic_intent.get("named_systems")
                        or []
                    ),
                    "topic_axis_examples": dict(
                        topic_intent.get("axis_examples") or {}
                    ),
                    "topic_outcome_dimensions": list(
                        topic_intent.get("focus_dimensions")
                        or topic_intent.get("outcome_dimensions")
                        or topic_intent.get("requested_outcomes")
                        or []
                    ),
                    "topic_focus_dimensions": list(
                        topic_intent.get("focus_dimensions")
                        or topic_intent.get("outcome_dimensions")
                        or topic_intent.get("requested_outcomes")
                        or []
                    ),
                    "partition_trace_policy": str(
                        topic_intent.get("partition_trace_policy")
                        or "source_bounded_model_or_section_contract"
                    ),
                    "boundary_policy": "explicit_rationale_allowed",
                    "source": "explicit_user_topic",
                }
            )
        selected_axis_contract = canonical_classification_contract(
            (
                (topic_intent.get("classification_contract") or {}).get("axes")
                if style == TOPIC_GUIDED_STYLE
                and isinstance(topic_intent.get("classification_contract"), dict)
                else matrix.get("classification_axes") or []
            ),
            primary_axis_hint=str(
                scope.get("primary_navigation_axis")
                or basis.get("primary_axis")
                or ""
            ),
            source="selected_outline",
        )
        basis = _basis_with_axis_contract(basis, selected_axis_contract)
        diagnostics = taxonomy_diagnostics(
            parsed_sections,
            _paper_ids(rows),
            classification_contract=basis,
        )
        payload = {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "outline_style": style,
            "outline_md": markdown,
            "outline_complete": complete,
            "selection_source": (
                "manual"
                if manual
                else "custom_draft"
                if not complete
                else "topic_recommendation"
                if style == TOPIC_GUIDED_STYLE
                else "template"
            ),
            "manually_edited": bool(manual),
            "source_matrix_artifact_id": matrix_artifact.id,
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "classification_contract": selected_axis_contract,
            "taxonomy_diagnostics": diagnostics,
            "topic_outline_intent": (
                topic_intent if style == TOPIC_GUIDED_STYLE else None
            ),
            "saved_at": utc_now().isoformat(),
        }
        current_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if (
            current_outline_artifact is not None
            and isinstance(current_outline, dict)
            and current_state is not None
            and current_state.revision == int(revision)
            and str(current_outline.get("outline_style") or "") == style
            and str(current_outline.get("outline_md") or "") == markdown
            and bool(current_outline.get("outline_complete")) == complete
            and str(current_outline.get("source_matrix_artifact_id") or "")
            == matrix_artifact.id
            and current_outline.get("scope_contract") == scope
            and current_outline.get("scope_diagnostics") == scope_report
            and current_outline.get("coverage_diagnostics") == coverage_report
            and current_outline.get("classification_basis") == basis
            and current_outline.get("classification_contract")
            == selected_axis_contract
            and current_outline.get("taxonomy_diagnostics") == diagnostics
            and current_outline.get("topic_outline_intent")
            == payload.get("topic_outline_intent")
        ):
            return {
                "project_id": project_id,
                "outline_style": style,
                "selected_outline_md": markdown,
                "outline_complete": complete,
                "blueprint_pending": complete,
                "scope_contract": scope,
                "scope_diagnostics": scope_report,
                "coverage_diagnostics": coverage_report,
                "classification_basis": basis,
                "classification_contract": selected_axis_contract,
                "taxonomy_diagnostics": diagnostics,
                "outline_artifact_id": current_outline_artifact.id,
                "matrix_revision": current_state.revision,
                "unchanged": True,
            }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={OUTLINE_LOGICAL_NAME: (_json_bytes(payload), "json")},
                input_snapshot={"outline_style": style, "manual": manual},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={OUTLINE_LOGICAL_NAME: published[OUTLINE_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "blueprint",
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
            )
        return {
            "project_id": project_id,
            "outline_style": style,
            "selected_outline_md": markdown,
            "outline_complete": complete,
            "blueprint_pending": complete,
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "classification_contract": selected_axis_contract,
            "taxonomy_diagnostics": diagnostics,
            "outline_artifact_id": published[OUTLINE_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
        }

    def _analyze_reference_document(
        self,
        principal: Principal,
        project_id: str,
        *,
        candidate_id: str,
        safe_name: str,
        raw: bytes,
        matrix: dict[str, Any],
    ) -> dict[str, Any]:
        if self.scientific_runner is None:
            raise WorkflowValidationError(
                "Reference-format analysis is unavailable in this deployment."
            )
        environment: dict[str, str] = {}
        gateway_context: SimpleNamespace | None = None
        if self.model_gateway is not None:
            gateway_context = self._begin_reference_gateway_job(
                principal, project_id, candidate_id
            )
            gateway_normal, gateway_secrets = self.model_gateway.environment_for_job(
                gateway_context
            )
            environment = {**gateway_normal, **gateway_secrets}
        elif self.provider_settings is not None:
            try:
                environment = self.provider_settings.runtime_environment(
                    principal,
                    provider_kinds=(ProviderKind.TEXT,),
                )
            except ProviderSettingsError as exc:
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                ) from exc
            if not environment.get("OPENAI_API_KEY"):
                raise WorkflowValidationError(
                    "Configure and enable the text provider before analyzing a reference review."
                )
        script = (
            self.root
            / "skills"
            / "review-reference-outline-template"
            / "scripts"
            / "analyze_reference_review.py"
        )
        if not script.is_file():
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis skill is not installed.",
                )
            raise WorkflowValidationError(
                "The reference-format analysis skill is not installed."
            )
        staging_parent = self.artifacts.workspace_manager.trusted_user_directory(
            principal.user_id,
            ".review-writer",
            "reference-outline-analysis",
        )
        with tempfile.TemporaryDirectory(
            prefix=f"{candidate_id}-", dir=staging_parent
        ) as temporary:
            staging = Path(temporary).resolve()
            source = staging / safe_name
            matrix_path = staging / "literature_matrix.json"
            output_path = staging / "candidate.json"
            source.write_bytes(raw)
            matrix_path.write_text(
                json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            normal_environment = {
                key: value
                for key, value in environment.items()
                if not SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            secret_environment = {
                key: value
                for key, value in environment.items()
                if SENSITIVE_ENVIRONMENT_KEY.search(key)
            }
            try:
                self.scientific_runner.run(
                    (
                        sys.executable,
                        str(script),
                        "--input",
                        str(source),
                        "--matrix",
                        str(matrix_path),
                        "--output",
                        str(output_path),
                        "--project-id",
                        project_id,
                        "--candidate-id",
                        candidate_id,
                    ),
                    cwd=self.root,
                    staging_directory=staging,
                    expected_outputs=("candidate.json",),
                    env=normal_environment,
                    secret_env=secret_environment,
                    timeout_seconds=900,
                )
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if gateway_context is not None:
                    self._finish_reference_gateway_job(
                        gateway_context.job_id,
                        succeeded=False,
                        error_message=str(exc),
                    )
                raise WorkflowConflict(
                    "Reference-format analysis returned an unreadable result."
                ) from exc
            except Exception as exc:
                if gateway_context is not None:
                    self._finish_reference_gateway_job(
                        gateway_context.job_id,
                        succeeded=False,
                        error_message=str(exc),
                    )
                raise
        if not isinstance(result, dict):
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis returned a non-object result.",
                )
            raise WorkflowConflict(
                "Reference-format analysis returned an invalid result."
            )
        if not self._reference_candidate_is_isolated(result):
            if gateway_context is not None:
                self._finish_reference_gateway_job(
                    gateway_context.job_id,
                    succeeded=False,
                    error_message="Reference analysis failed the content-isolation gate.",
                )
            raise WorkflowConflict(
                "Reference analysis failed the content-isolation gate; the uploaded review was not added."
            )
        if gateway_context is not None:
            self._finish_reference_gateway_job(
                gateway_context.job_id,
                succeeded=True,
            )
        return result

    def register_reference(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        filename: str,
        content_base64: str,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        self._matrix(principal, project_id)
        safe_name = Path(str(filename or "")).name
        suffix = Path(safe_name).suffix.casefold()
        if suffix not in {".pdf", ".docx", ".md", ".txt"}:
            raise WorkflowValidationError(
                "Upload a PDF, DOCX, Markdown, or text review document."
            )
        try:
            raw = base64.b64decode(str(content_base64 or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkflowValidationError("Reference content is not valid base64.") from exc
        if not raw:
            raise WorkflowValidationError("Uploaded reference file is empty.")
        if len(raw) > 30 * 1024 * 1024:
            raise WorkflowValidationError("Uploaded reference file exceeds 30 MB.")
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        matrix_ids = _paper_ids(matrix["rows"])
        candidate_id = f"reference-{uuid.uuid4().hex[:12]}"
        analysis = self._analyze_reference_document(
            principal,
            project_id,
            candidate_id=candidate_id,
            safe_name=safe_name,
            raw=raw,
            matrix=matrix,
        )
        outline_text = self._validate_outline(
            str(analysis.get("outline_md") or ""), set(matrix_ids)
        )
        analysis_mode = str(analysis.get("analysis_mode") or "")
        references, _references_artifact = self._read_json(
            principal, project_id, REFERENCE_INDEX_LOGICAL_NAME, required=False
        )
        index = deepcopy(references or {"project_id": project_id, "candidates": []})
        source_logical = f"planning/references/{candidate_id}/{safe_name}"
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={
                    source_logical: (raw, suffix.lstrip(".")),
                },
                input_snapshot={"filename": safe_name},
            )
            candidate = {
                "candidate_id": candidate_id,
                "outline_style": f"reference:{candidate_id}",
                "labels": {"en": safe_name, "zh": f"参考大纲：{safe_name}"},
                "outline_md": outline_text,
                "source": "reference",
                "source_name": safe_name,
                "source_artifact_id": published[source_logical].id,
                "analysis_mode": analysis_mode,
                "content_source": "current_matrix_only",
                "reference_content_reused": False,
                "content_firewall": deepcopy(analysis.get("content_firewall") or {}),
                "reference_structure_metrics": deepcopy(
                    analysis.get("reference_structure_metrics") or {}
                ),
                "writing_style": deepcopy(analysis.get("writing_style") or {}),
                "created_at": utc_now().isoformat(),
            }
            index["candidates"] = [*(index.get("candidates") or []), candidate]
            index_published, index_run = self._publish_files(
                principal,
                project_id,
                stage_id="matrix",
                files={REFERENCE_INDEX_LOGICAL_NAME: (_json_bytes(index), "json")},
                input_snapshot={"source_artifact_id": published[source_logical].id},
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "matrix",
                artifact_ids={
                    REFERENCE_INDEX_LOGICAL_NAME: index_published[
                        REFERENCE_INDEX_LOGICAL_NAME
                    ].id
                },
                run_id=index_run.id,
                expected_revision=revision,
                status="review",
            )
        return {
            "project_id": project_id,
            "candidate": candidate,
            "matrix_revision": state.revision,
        }

    def generate_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        previous_blueprint, previous_blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False
        )
        previous_blueprint_state = self.repository.get_stage_state(
            principal.user_id, project_id, "blueprint"
        )
        matrix, matrix_artifact = self._matrix(principal, project_id)
        matrix, bibliography_metadata_artifact_ids = self._with_current_bibliography(
            principal, matrix
        )
        matrix_rows = [
            row for row in matrix.get("rows") or [] if isinstance(row, dict)
        ]
        all_fact_extraction_failed = bool(matrix_rows) and all(
            str((row.get("fact_enrichment") or {}).get("status") or "pending")
            == "failed"
            for row in matrix_rows
        )
        if all_fact_extraction_failed and not bool(
            (matrix.get("fact_enrichment_summary") or {}).get(
                "limited_mode_confirmed"
            )
        ):
            raise WorkflowConflict(
                "Every Matrix fact extraction failed. Retry extraction or explicitly continue in limited mode."
            )
        project = self._owned_project(principal, project_id)
        discovery, _discovery_artifact = self._read_json(
            principal, project_id, DISCOVERY_LOGICAL_NAME, required=False
        )
        outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if not outline.get("outline_complete") or not str(outline.get("outline_md") or "").strip():
            raise WorkflowConflict(
                "The selected outline is blank or incomplete. Edit and save it before Blueprint generation."
            )
        matrix_ids = set(_paper_ids(matrix["rows"]))
        parsed = _outline_sections(str(outline["outline_md"]))
        matrix_order = _paper_ids(matrix["rows"])
        auto_routing_adjustments: list[dict[str, Any]] = []
        resolved_outline_md = str(outline["outline_md"])
        outline_style = str(outline.get("outline_style") or "")
        routing_style = outline_style
        routing_tag_key = ""
        routing_axis_label = ""
        if outline_style == TOPIC_GUIDED_STYLE:
            primary_axis = str(
                (outline.get("topic_outline_intent") or {}).get("primary_axis")
                or ""
            )
            if primary_axis in TOPIC_AXIS_LABELS:
                routing_tag_key = primary_axis
                routing_axis_label = TOPIC_AXIS_LABELS[primary_axis]["en"]
        tags_by_paper: dict[str, dict[str, Any]] = {}
        text_by_paper: dict[str, str] = {}
        if not bool(outline.get("manually_edited")):
            tags_by_paper, text_by_paper = self._outline_sources(
                principal, matrix["rows"]
            )
            parsed, auto_routing_adjustments = (
                self._auto_repair_generated_routing_sections(
                    parsed,
                    matrix["rows"],
                    text_by_paper,
                    outline_style=routing_style,
                    taxonomy_profile=project.taxonomy_profile,
                    tag_key_override=routing_tag_key,
                    axis_label_override=routing_axis_label,
                )
            )
            parsed, evidence_realignments = self._realign_generated_body_sections(
                parsed,
                matrix_rows,
                text_by_paper,
                outline_style=routing_style,
                taxonomy_profile=project.taxonomy_profile,
                tag_key_override=routing_tag_key,
                axis_label_override=routing_axis_label,
            )
            auto_routing_adjustments.extend(evidence_realignments)
            parsed, title_adjustments = self._sanitize_generated_outline_titles(
                parsed
            )
            auto_routing_adjustments.extend(title_adjustments)
            contextual_ids = self._contextual_outline_paper_ids(
                matrix_rows, tags_by_paper, text_by_paper
            )
            if contextual_ids:
                contextual_set = set(contextual_ids)
                introduction = next(
                    (
                        section
                        for section in parsed
                        if infer_section_role(
                            section.get("title"), section.get("section_role")
                        )
                        == "introduction"
                    ),
                    None,
                )
                if introduction is not None:
                    intro_context = introduction.setdefault("context_paper_ids", [])
                    intro_context.extend(
                        paper_id
                        for paper_id in contextual_ids
                        if paper_id not in intro_context
                    )
                for section in parsed:
                    role = infer_section_role(
                        section.get("title"), section.get("section_role")
                    )
                    if role != "body":
                        continue
                    before = list(section.get("paper_ids") or [])
                    removed = [
                        paper_id for paper_id in before if paper_id in contextual_set
                    ]
                    if not removed:
                        continue
                    section["paper_ids"] = [
                        paper_id for paper_id in before if paper_id not in contextual_set
                    ]
                    auto_routing_adjustments.append(
                        {
                            "source_section": str(section.get("title") or ""),
                            "target_section": "Introduction (context evidence)",
                            "paper_ids": removed,
                            "method": "contextual_source_detection",
                            "created_section": False,
                        }
                    )
            if auto_routing_adjustments:
                resolved_outline_md = _outline_markdown_from_sections(
                    parsed,
                    outline_style=outline_style,
                    automatically_adjusted=True,
                )
        prepared = []
        for index, section in enumerate(parsed, start=1):
            role = infer_section_role(
                section.get("title"), section.get("section_role")
            )
            if role == "references":
                continue
            assigned = list(dict.fromkeys(section["paper_ids"]))
            if (
                not bool(outline.get("manually_edited"))
                and role == "body"
                and not assigned
            ):
                continue
            unknown = sorted(set(assigned) - matrix_ids)
            if unknown:
                raise WorkflowConflict(
                    "The selected outline refers to papers missing from the current Matrix.",
                    details={"paper_ids": unknown},
                )
            prepared.append(
                {
                    **section,
                    "section_id": f"S{len(prepared) + 1:02d}",
                    "section_role": role,
                    "paper_ids": assigned,
                    "context_paper_ids": list(
                        dict.fromkeys(section.get("context_paper_ids") or [])
                    ),
                }
            )

        scope = derive_scope_contract(
            matrix.get("review_topic") or (discovery or {}).get("topic"),
            outline.get("outline_style"),
            matrix["rows"],
            current=outline.get("scope_contract")
            if isinstance(outline.get("scope_contract"), dict)
            else None,
        )
        time_span = (
            scope.get("time_span") if isinstance(scope.get("time_span"), dict) else {}
        )
        scope_year_from = _publication_year(time_span.get("from"))
        scope_year_to = _publication_year(time_span.get("to"))
        if scope_year_from is not None and scope_year_to is not None:
            if scope_year_from > scope_year_to:
                scope_year_from, scope_year_to = scope_year_to, scope_year_from
            rows_by_scope_id = {
                str(row.get("paper_id") or ""): row
                for row in matrix_rows
                if str(row.get("paper_id") or "")
            }
            for section in prepared:
                if str(section.get("section_role") or "body") != "body":
                    continue
                assigned = list(section.get("paper_ids") or [])
                outside = [
                    paper_id
                    for paper_id in assigned
                    if (
                        (year := _matrix_publication_year(
                            rows_by_scope_id.get(paper_id) or {}
                        ))
                        is not None
                        and not (scope_year_from <= year <= scope_year_to)
                    )
                ]
                if not outside:
                    continue
                section["paper_ids"] = [
                    paper_id for paper_id in assigned if paper_id not in set(outside)
                ]
                section["context_paper_ids"] = list(
                    dict.fromkeys(
                        [*(section.get("context_paper_ids") or []), *outside]
                    )
                )
                auto_routing_adjustments.append(
                    {
                        "source_section": str(section.get("title") or ""),
                        "target_section": f"Context evidence outside {scope_year_from}–{scope_year_to}",
                        "paper_ids": outside,
                        "method": "explicit_time_scope_role_downgrade",
                        "created_section": False,
                    }
                )
            if not bool(outline.get("manually_edited")):
                prepared = [
                    section
                    for section in prepared
                    if section.get("section_role") != "body"
                    or section.get("paper_ids")
                    or section.get("context_paper_ids")
                ]
                resolved_outline_md = _outline_markdown_from_sections(
                    prepared,
                    outline_style=outline_style,
                    automatically_adjusted=bool(auto_routing_adjustments),
                )

        normalized, primary_owner = assign_primary_paper_sections(
            prepared, matrix_order
        )
        body_primary_papers = list(
            dict.fromkeys(
                paper_id
                for section in normalized
                if section.get("section_role") == "body"
                for paper_id in section.get("primary_papers") or []
            )
        )
        rows_by_id = {
            str(row.get("paper_id") or ""): row for row in matrix_rows
        }
        index_summaries = (
            self.library_index.summaries(principal, matrix_order)
            if self.library_index is not None and self.library_index.enabled
            else {}
        )

        def evidence_readiness(
            role: str, primary_papers: list[str], context_papers: list[str]
        ) -> dict[str, Any]:
            if role in {"introduction", "conclusion"}:
                return {
                    "status": "synthesis",
                    "writeable_primary_papers": [],
                    "context_only_primary_papers": [],
                    "unresolved_primary_papers": [],
                    "context_papers": list(context_papers),
                }
            writeable: list[str] = []
            context_only: list[str] = []
            unresolved: list[str] = []
            for paper_id in primary_papers:
                row = rows_by_id.get(paper_id) or {}
                summary = index_summaries.get(paper_id) or {}
                has_fulltext = (
                    summary.get("fulltext") == "ready"
                    and int(summary.get("chunk_count") or 0) > 0
                )
                has_source_fact = any(
                    isinstance(fact, dict)
                    and str(fact.get("field_id") or "") != "abstract_summary"
                    and str(fact.get("value") or "").strip()
                    and bool(fact.get("evidence_refs"))
                    for fact in row.get("scientific_facts") or []
                )
                if has_fulltext or has_source_fact:
                    writeable.append(paper_id)
                elif self._matrix_abstract(row) or str(
                    (row.get("fact_enrichment") or {}).get("status") or ""
                ) == "limited":
                    context_only.append(paper_id)
                else:
                    unresolved.append(paper_id)
            status = (
                "ready"
                if primary_papers and len(writeable) == len(primary_papers)
                else "partial"
                if writeable or context_only
                else "insufficient"
            )
            return {
                "status": status,
                "assigned_primary_count": len(primary_papers),
                "writeable_primary_count": len(writeable),
                "context_only_primary_count": len(context_only),
                "unresolved_primary_count": len(unresolved),
                "writeable_primary_papers": writeable,
                "context_only_primary_papers": context_only,
                "unresolved_primary_papers": unresolved,
                "context_papers": list(context_papers),
            }

        sections: list[dict[str, Any]] = []
        for section in normalized:
            role = section["section_role"]
            primary = list(section["primary_papers"])
            supporting = list(section["supporting_papers"])
            context_papers = list(section.get("context_paper_ids") or [])
            if role == "conclusion":
                # A conclusion synthesizes the completed body arguments.  Give
                # it access to every body-owned paper so citations inherited
                # from those evidence-bound syntheses remain valid.
                supporting = list(body_primary_papers)
            if role == "introduction":
                thesis = (
                    str(section.get("purpose") or "").strip()
                    or "Define the review scope, organizing question, and evidence landscape "
                    "without repeating paper-level results from the body sections."
                )
                problem = "What problem, scope, and organizing logic does this review establish?"
                claim = (
                    "Frame the field and its evidence boundaries with brief representative "
                    "citations; reserve detailed study descriptions for their primary sections."
                )
                figure_need = "None unless an overview figure materially clarifies the review scope."
                target_words = 900
            elif role == "conclusion":
                thesis = (
                    str(section.get("purpose") or "").strip()
                    or "Synthesize cross-section findings, limitations, and future directions "
                    "without replaying individual paper summaries."
                )
                problem = "What conclusions hold across sections, and where do important limits remain?"
                claim = (
                    "Compare the body-section conclusions and cite prior evidence concisely; "
                    "do not restate full methods, conditions, or paper-by-paper results."
                )
                figure_need = "None unless a cross-section synthesis figure adds new comparative value."
                target_words = 900
            else:
                thesis = str(section.get("purpose") or "").strip() or f"Synthesize evidence for {section['title']}."
                problem = f"What does the current evidence establish about {section['title']}?"
                if primary:
                    claim = (
                        f"Develop claim-centered synthesis from {len(primary)} primary papers, "
                        "comparing convergent evidence, differences, and limitations."
                    )
                else:
                    claim = (
                        "Develop a cross-cutting comparison from previously introduced evidence "
                        "without repeating full study descriptions."
                    )
                figure_need = f"Support the comparison in {section['title']} where source evidence permits."
                target_words = max(700, 350 * max(1, len(primary)))
            sections.append(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "section_role": role,
                    "topic_partition": str(
                        section.get("topic_partition") or ""
                    ).strip(),
                    "boundary_rationale": str(
                        section.get("boundary_rationale") or ""
                    ).strip(),
                    "section_thesis": thesis,
                    "review_problem": problem,
                    "major_papers": primary,
                    "primary_papers": primary,
                    "supporting_papers": supporting,
                    "context_papers": context_papers,
                    "review_claims": [{"claim": claim}],
                    "figure_or_table_needs": [
                        {
                            "type": "Figure or table",
                            "purpose": figure_need,
                            "candidate_papers": primary[:3],
                        }
                    ],
                    "avoid_patterns": [
                        "Do not infer unsupported conditions or mechanisms.",
                        "Do not repeat a paper-level description already owned by another section.",
                        "Do not organize prose as one title or one summary block per paper.",
                    ],
                    "section_transition": "Connect this evidence to the next comparison axis.",
                    "target_words": target_words,
                    "evidence_readiness": evidence_readiness(
                        role, primary, context_papers
                    ),
                }
            )
            sections[-1]["academic_contract"] = section_academic_contract(sections[-1])
            sections[-1]["synthesis_requirements"] = synthesis_requirements(
                sections[-1], taxonomy_profile=project.taxonomy_profile
            )
        if not sections:
            raise WorkflowValidationError("The selected outline contains no usable sections.")
        contextual_paper_ids = list(
            dict.fromkeys(
                paper_id
                for section in sections
                for paper_id in section.get("context_papers") or []
            )
        )
        scope_report = scope_diagnostics(scope)
        coverage_report = coverage_diagnostics(scope, matrix["rows"])
        basis = dict(
            outline.get("classification_basis")
            or classification_basis(outline.get("outline_style"))
        )
        selected_axis_contract = classification_contract_from_document(
            outline,
            primary_axis_hint=str(basis.get("primary_axis") or ""),
            source="blueprint_from_selected_outline",
        )
        basis = _basis_with_axis_contract(basis, selected_axis_contract)
        diagnostics = taxonomy_diagnostics(
            sections,
            matrix_order,
            classification_contract=basis,
        )
        restructure_reasons: list[str] = []
        if auto_routing_adjustments:
            restructure_reasons.append("evidence_based_paper_routing_changed")
        if any(
            str((section.get("evidence_readiness") or {}).get("status") or "")
            in {"partial", "insufficient"}
            for section in sections
            if str(section.get("section_role") or "body") == "body"
        ):
            restructure_reasons.append("section_evidence_distribution_is_uneven")
        for issue in diagnostics.get("issues") or []:
            if isinstance(issue, dict) and issue.get("rule_id"):
                restructure_reasons.append(str(issue["rule_id"]))
        restructure_record = _blueprint_restructure_record(
            previous_blueprint,
            sections,
            previous_artifact_id=(
                previous_blueprint_artifact.id if previous_blueprint_artifact else ""
            ),
            trigger_reasons=restructure_reasons,
        )
        current_sections_artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, "sections/section_drafts.json"
        )
        current_draft_artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, "draft/manuscript.md"
        )
        has_manual_draft = bool(
            current_draft_artifact
            and (
                current_draft_artifact.metadata.get("unverified_manual_paragraph_ids")
                or str(current_draft_artifact.metadata.get("operation") or "")
                == "full-edit"
                or str(current_draft_artifact.metadata.get("operation") or "").startswith(
                    "paragraph-edit:"
                )
            )
        )
        safe_auto_apply = bool(
            restructure_record["is_restructure"]
            and previous_blueprint_state is not None
            and previous_blueprint_state.status == "approved"
            and current_sections_artifact is None
            and current_draft_artifact is None
            and not bool(outline.get("manually_edited"))
        )
        restructure_record.update(
            {
                "application_mode": (
                    "auto_applied_before_section_generation"
                    if safe_auto_apply
                    else "candidate_requires_existing_blueprint_confirmation"
                    if restructure_record["is_restructure"]
                    else "not_applicable"
                ),
                "downstream_sections_present": current_sections_artifact is not None,
                "manual_draft_content_present": has_manual_draft,
            }
        )
        matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        if matrix_state is None:
            raise WorkflowConflict("The current Matrix stage state is missing.")
        blueprint = {
            "schema_version": ACADEMIC_SCHEMA_VERSION,
            "project_id": project_id,
            "review_topic": str(
                matrix.get("review_topic") or (discovery or {}).get("topic") or ""
            ),
            "outline_style": outline.get("outline_style"),
            "scope_contract": scope,
            "scope_diagnostics": scope_report,
            "coverage_diagnostics": coverage_report,
            "classification_basis": basis,
            "classification_contract": selected_axis_contract,
            "classification_contract_lineage": {
                "matrix_fingerprint": str(
                    classification_contract_from_document(
                        matrix,
                        primary_axis_hint=str(
                            (matrix.get("classification_recommendation") or {}).get(
                                "primary_axis_id"
                            )
                            or ""
                        ),
                        source="blueprint_matrix_input",
                    ).get("fingerprint")
                    or ""
                ),
                "outline_fingerprint": str(
                    selected_axis_contract.get("fingerprint") or ""
                ),
                "effective_fingerprint": str(
                    selected_axis_contract.get("fingerprint") or ""
                ),
                "status": "selected_outline_contract_applied",
            },
            "taxonomy_profile": project.taxonomy_profile,
            "taxonomy_diagnostics": diagnostics,
            "source_matrix_artifact_id": matrix_artifact.id,
            "source_outline_artifact_id": outline_artifact.id,
            "source_bibliography_metadata_artifact_ids": bibliography_metadata_artifact_ids,
            "resolved_outline_md": resolved_outline_md,
            "auto_routing_adjustments": auto_routing_adjustments,
            "restructure_record": restructure_record,
            "rule_pack": "general",
            "rule_pack_path": "references/rule_packs/general",
            "generated_at": utc_now().isoformat(),
            "paper_assignment_policy": {
                "mode": "single_primary_section_with_supporting_cross_references",
                "primary_section_by_paper": primary_owner,
                "introduction_and_conclusion_are_synthesis_only": True,
                "contextual_papers": contextual_paper_ids,
                "contextual_paper_policy": (
                    "Field-level reviews and perspectives may frame scope and history, "
                    "but do not substitute for primary body evidence."
                ),
            },
            "sections": sections,
            "synthesis_requirements": [
                {
                    "section_id": section["section_id"],
                    "components": section["synthesis_requirements"],
                }
                for section in sections
            ],
            "section_writing_plan_md": "# Section Writing Plan\n\n"
            + "\n".join(
                f"- {section['section_id']} {section['title']}: "
                f"{len(section['primary_papers'])} primary, "
                f"{len(section['supporting_papers'])} supporting papers."
                for section in sections
            )
            + "\n",
        }
        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="blueprint",
                files={BLUEPRINT_LOGICAL_NAME: (_json_bytes(blueprint), "json")},
                input_snapshot={
                    "matrix_artifact_id": matrix_artifact.id,
                    "outline_artifact_id": outline_artifact.id,
                    "bibliography_metadata_artifact_ids": bibliography_metadata_artifact_ids,
                    "classification_contract_fingerprint": str(
                        selected_axis_contract.get("fingerprint") or ""
                    ),
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "blueprint",
                artifact_ids={BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id},
                run_id=run.id,
                expected_revision=revision,
                status="approved" if safe_auto_apply else "review",
                invalidate_stages=(
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
                approve_stages={"matrix": matrix_state.revision},
            )
        return {
            "project_id": project_id,
            "section_blueprint": blueprint,
            "blueprint_artifact_id": published[BLUEPRINT_LOGICAL_NAME].id,
            "blueprint_revision": state.revision,
            "matrix_revision": matrix_state.revision + 1,
            "auto_applied": safe_auto_apply,
            "restructure_record": restructure_record,
        }

    def confirm_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        blueprint, _blueprint_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        _matrix, matrix_artifact = self._matrix(principal, project_id)
        _outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if (
            blueprint.get("source_matrix_artifact_id") != matrix_artifact.id
            or blueprint.get("source_outline_artifact_id") != outline_artifact.id
        ):
            raise WorkflowConflict(
                "Blueprint is out of date. Regenerate it from the current Matrix and outline."
            )
        diagnostics = blueprint.get("taxonomy_diagnostics")
        scope_report = blueprint.get("scope_diagnostics")
        blocking_issues = []
        if isinstance(scope_report, dict) and not scope_report.get("can_confirm", False):
            blocking_issues.extend(scope_report.get("issues") or [])
        if isinstance(diagnostics, dict) and not diagnostics.get("can_confirm", False):
            blocking_issues.extend(diagnostics.get("issues") or [])
        if blocking_issues:
            raise WorkflowConflict(
                "Blueprint cannot be confirmed until Scope and taxonomy blockers are resolved in the existing planning page.",
                details={"issues": blocking_issues},
            )
        state = self.repository.compare_and_set_stage(
            principal.user_id,
            project_id,
            "blueprint",
            int(revision),
            status="approved",
        )
        return {
            "project_id": project_id,
            "revision": state.revision,
            "status": state.status,
            "next_stage": "sections",
            "next_path": f"/sections?project={project_id}",
        }

    def restore_blueprint(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        artifact_id: str,
    ) -> dict[str, Any]:
        """Publish an older Blueprint as a new reviewable version.

        Immutable artifacts are never made current directly: restoring creates
        a new version with an explicit lineage record, then invalidates only
        the downstream products that depended on the replaced Blueprint.
        """

        principal.require(Permission.PROJECT_WRITE)
        current_blueprint, current_artifact = self._read_json(
            principal, project_id, BLUEPRINT_LOGICAL_NAME
        )
        if current_artifact is None:
            raise WorkflowNotFound("The current Blueprint was not found.")
        if str(current_artifact.id) == str(artifact_id):
            raise WorkflowValidationError(
                "The selected Blueprint version is already current."
            )

        resolved = self.artifacts.resolve_owned_artifact(
            principal.user_id, artifact_id
        )
        if (
            str(resolved.artifact.project_id) != str(project_id)
            or resolved.artifact.logical_name != BLUEPRINT_LOGICAL_NAME
        ):
            raise WorkflowNotFound("Blueprint version not found.")
        try:
            restored_source = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The selected Blueprint version is unreadable.") from exc
        if not isinstance(restored_source, dict):
            raise WorkflowConflict("The selected Blueprint version is invalid.")

        _matrix, matrix_artifact = self._matrix(principal, project_id)
        _outline, outline_artifact = self._read_json(
            principal, project_id, OUTLINE_LOGICAL_NAME
        )
        if (
            str(restored_source.get("source_matrix_artifact_id") or "")
            != str(matrix_artifact.id)
            or str(restored_source.get("source_outline_artifact_id") or "")
            != str(outline_artifact.id)
        ):
            raise WorkflowConflict(
                "This Blueprint version belongs to an older Matrix or outline and cannot be restored directly. Regenerate a Blueprint from the current planning inputs instead."
            )

        restored = deepcopy(restored_source)
        restored["restructure_record"] = {
            "is_restructure": True,
            "application_mode": "restored_candidate_requires_confirmation",
            "previous_blueprint_artifact_id": current_artifact.id,
            "restored_from_artifact_id": resolved.artifact.id,
            "trigger_reasons": ["user_restored_previous_blueprint"],
            "section_mapping": _blueprint_restructure_record(
                current_blueprint,
                [
                    item
                    for item in restored.get("sections") or []
                    if isinstance(item, dict)
                ],
                previous_artifact_id=current_artifact.id,
                trigger_reasons=["user_restored_previous_blueprint"],
            ).get("section_mapping", []),
            "rollback_supported": True,
            "created_at": utc_now().isoformat(),
        }
        restored["restored_at"] = utc_now().isoformat()

        with self._write_lock:
            published, run = self._publish_files(
                principal,
                project_id,
                stage_id="blueprint",
                files={BLUEPRINT_LOGICAL_NAME: (_json_bytes(restored), "json")},
                input_snapshot={
                    "operation": "restore_blueprint",
                    "restored_from_artifact_id": resolved.artifact.id,
                    "replaced_artifact_id": current_artifact.id,
                },
            )
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id,
                project_id,
                "blueprint",
                artifact_ids={
                    BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id
                },
                run_id=run.id,
                expected_revision=revision,
                status="review",
                invalidate_stages=(
                    "sections",
                    "figure-review",
                    "figures",
                    "draft",
                    "final",
                ),
                expected_current_artifacts={
                    BLUEPRINT_LOGICAL_NAME: current_artifact.id
                },
            )
        return {
            "project_id": project_id,
            "section_blueprint": restored,
            "blueprint_artifact_id": published[BLUEPRINT_LOGICAL_NAME].id,
            "blueprint_revision": state.revision,
            "status": state.status,
            "restored_from_artifact_id": resolved.artifact.id,
        }
