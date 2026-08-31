"""Native Discovery review, explicit selection, and Matrix handoff."""

from __future__ import annotations

import json
import hashlib
import math
import re
import threading
import uuid
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import Project, database_session, utc_now
from review_writer_api.domain_services.base import OwnedProjectService
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.workflow_models import LibraryPaper
from review_writer_core.metadata_fields import unwrap_metadata_value
from review_writer_core.paper_sources.normalize import normalize_doi, normalize_title
from review_writer_core.classification_axes import (
    CLASSIFICATION_CONTRACT_VERSION,
    canonical_classification_contract,
)
from review_writer_core.atomic_io import atomic_write_json


DISCOVERY_LOGICAL_NAME = "discovery/review.json"
MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
MUTABLE_ROLES = {
    "core_candidate",
    "supporting_candidate",
    "background",
    "uncertain",
    "excluded",
}
MATRIX_CLASSIFICATION_POLICY_VERSION = 5
TOPIC_CANDIDATE_POOL_KEY = "__topic_candidates_pending_evidence__"
LEGACY_DISCOVERY_TAG_FIELDS = {
    "base_tags",
    "base_tags_verified",
    "project_tag_assessment",
    "confirmed_project_tags",
    "tag_review_status",
    "screening_classification",
    "provisional_screening_tags",
}
LEGACY_MATRIX_TAG_FIELDS = {
    "base_tags",
    "base_tags_verified",
    "project_tags",
    "project_tag_review_status",
    "project_tag_topic_fingerprint",
    "human_confirmed_tags",
    "provisional_screening_tags",
    "screening_classification_status",
}


class DiscoverySelectionNotInLibrary(WorkflowValidationError):
    code = "DISCOVERY_SELECTION_NOT_IN_LIBRARY"


def _candidate_id(row: dict[str, Any], *, external: bool) -> str:
    if external:
        return str(
            row.get("candidate_id")
            or row.get("doi")
            or row.get("url")
            or f"{row.get('title', '')}|{row.get('year', '')}"
        ).strip()
    return str(row.get("paper_id") or "").strip()


def _selected(row: dict[str, Any]) -> bool:
    return bool(row.get("selected_for_matrix")) and str(row.get("role") or "") != "excluded"


def _publication_year(value: Any) -> int | None:
    raw = unwrap_metadata_value(value)
    match = re.search(r"(?:18|19|20|21)\d{2}", str(raw or ""))
    return int(match.group(0)) if match else None


def discovery_coverage_diagnostics(review: dict[str, Any]) -> dict[str, Any]:
    """Describe observable local-result gaps without claiming global recall."""

    unique: dict[str, dict[str, Any]] = {}
    empty_groups: list[str] = []
    for group in review.get("results") or []:
        if not isinstance(group, dict) or group.get("keep") is False:
            continue
        local = [row for row in group.get("local_results") or [] if isinstance(row, dict)]
        if not local:
            empty_groups.append(str(group.get("keyword") or "").strip())
        for row in local:
            paper_id = _candidate_id(row, external=False)
            if paper_id:
                unique.setdefault(paper_id, row)
    years = [
        year
        for row in unique.values()
        if (year := _publication_year(row.get("first_publication_date") or row.get("year")))
        is not None
    ]
    year_distribution = Counter(str(year) for year in years)
    query_plan = review.get("query_plan") if isinstance(review.get("query_plan"), dict) else {}
    filters = review.get("filters") if isinstance(review.get("filters"), dict) else {}
    if not filters and isinstance(query_plan.get("filters"), dict):
        filters = query_plan["filters"]
    year_from = _publication_year(filters.get("year_from"))
    year_to = _publication_year(filters.get("year_to"))
    if year_from is None or year_to is None:
        topic_range = re.search(
            r"(?<!\d)((?:18|19|20|21)\d{2})\s*(?:-|–|—|to|至)\s*((?:18|19|20|21)\d{2})(?!\d)",
            str(review.get("topic") or ""),
            re.I,
        )
        if topic_range:
            year_from = int(topic_range.group(1))
            year_to = int(topic_range.group(2))
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    missing_years: list[int] = []
    if year_from is not None and year_to is not None and year_to - year_from <= 40:
        observed = set(years)
        missing_years = [year for year in range(year_from, year_to + 1) if year not in observed]
    external = review.get("external_search") if isinstance(review.get("external_search"), dict) else {}
    requested_sources = [
        str(value)
        for value in external.get("requested_sources") or []
        if str(value).strip()
    ]
    source_statuses = (
        external.get("source_statuses")
        if isinstance(external.get("source_statuses"), dict)
        else {}
    )
    completed_sources = [
        str(name)
        for name, status in source_statuses.items()
        if isinstance(status, dict)
        and str(status.get("status") or "") == "completed"
    ]
    for value in external.get("sources_used") or []:
        source = str(value).strip()
        if source and source not in completed_sources:
            completed_sources.append(source)
    reason_codes: list[str] = []
    if len(unique) < 10:
        reason_codes.append("coverage.local_candidate_pool_small")
    unknown_year_count = len(unique) - len(years)
    if unknown_year_count >= max(2, (len(unique) + 3) // 4):
        reason_codes.append("coverage.publication_years_unknown")
    span = (year_to - year_from + 1) if year_from is not None and year_to is not None else 0
    if span and len(missing_years) >= max(2, (span + 2) // 3):
        reason_codes.append("coverage.explicit_year_range_sparse")
    if empty_groups:
        reason_codes.append("coverage.query_groups_empty")
    completion_state = str(external.get("completion_state") or "disabled")
    online_search_suggested = bool(
        reason_codes
        and (
            not completed_sources
            or completion_state in {"disabled", "failed", "partial"}
        )
    )
    return {
        "schema_version": 1,
        "coverage_mode": "multi_source" if completed_sources else "local_bounded",
        "coverage_claim": "selected_corpus_only",
        "candidate_paper_count": len(unique),
        "year_distribution": dict(sorted(year_distribution.items())),
        "year_unknown_count": unknown_year_count,
        "declared_year_from": year_from,
        "declared_year_to": year_to,
        "missing_years": missing_years,
        "empty_query_groups": [value for value in empty_groups if value],
        "requested_online_sources": requested_sources,
        "completed_online_sources": completed_sources,
        "online_search_completion_state": completion_state,
        "online_search_suggested": online_search_suggested,
        "reason_codes": reason_codes,
        "limitations": [
            "The diagnosis describes only the observed local results and configured query plan.",
            "It does not estimate an unknowable global recall percentage.",
        ],
    }


def discovery_search_record(review: dict[str, Any]) -> dict[str, Any]:
    """Build a reproducible record from observed Discovery execution facts."""

    groups = [
        group
        for group in review.get("results") or []
        if isinstance(group, dict) and group.get("keep") is not False
    ]
    local_rows = [
        row
        for group in groups
        for row in group.get("local_results") or []
        if isinstance(row, dict)
    ]
    external_rows = [
        row
        for group in groups
        for row in group.get("web_results") or []
        if isinstance(row, dict)
    ]
    external = (
        review.get("external_search")
        if isinstance(review.get("external_search"), dict)
        else {}
    )
    statuses = (
        external.get("source_statuses")
        if isinstance(external.get("source_statuses"), dict)
        else {}
    )
    executed_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and (
            int(row.get("completed_queries") or 0) > 0
            or str(row.get("status") or "") == "completed"
        )
    ]
    for source in external.get("executed_sources") or []:
        normalized = str(source).strip()
        if normalized and normalized not in executed_sources:
            executed_sources.append(normalized)
    successful_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and str(row.get("status") or "") == "completed"
    ]
    for source in external.get("successful_sources") or []:
        normalized = str(source).strip()
        if normalized and normalized not in successful_sources:
            successful_sources.append(normalized)
    enabled_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and str(row.get("status") or "") not in {"disabled", "not_configured"}
    ]
    failed_sources = [
        str(name)
        for name, row in statuses.items()
        if isinstance(row, dict)
        and str(row.get("status") or "") in {"failed", "error", "partial"}
    ]
    query_log = [
        dict(row)
        for row in external.get("query_log") or []
        if isinstance(row, dict)
    ]
    if not query_log:
        query_log = [
            {
                "query_group": str(group.get("keyword") or "").strip(),
                "query": str(group.get("query") or group.get("keyword") or "").strip(),
                "status": "record_recovered_from_saved_results",
            }
            for group in groups
            if str(group.get("keyword") or "").strip()
        ]
    local_ids = {
        _candidate_id(row, external=False)
        for row in local_rows
        if _candidate_id(row, external=False)
    }
    external_ids = {
        _candidate_id(row, external=True)
        for row in external_rows
        if _candidate_id(row, external=True)
    }
    selected_ids = {
        _candidate_id(row, external=False) for row in local_rows if _selected(row)
    }
    return {
        "schema_version": 1,
        "retrieved_at": str(
            external.get("completed_at")
            or review.get("searched_at")
            or review.get("generated_at")
            or review.get("updated_at")
            or ""
        ),
        "requested_sources": [
            str(value)
            for value in external.get("requested_sources") or []
            if str(value).strip()
        ],
        "enabled_sources": enabled_sources,
        "executed_sources": executed_sources,
        "successful_sources": successful_sources,
        "failed_sources": failed_sources,
        "contributing_sources": [
            str(value)
            for value in external.get("sources_used") or []
            if str(value).strip()
        ],
        "source_statuses": deepcopy(statuses),
        "completion_state": str(external.get("completion_state") or "disabled"),
        "query_log": query_log,
        "initial_local_hit_count": len(local_rows),
        "unique_local_candidate_count": len(local_ids),
        "initial_external_hit_count": len(external_rows),
        "unique_external_candidate_count": len(external_ids),
        "selected_matrix_candidate_count": len(selected_ids),
        "explicitly_excluded_local_count": len(
            {
                _candidate_id(row, external=False)
                for row in local_rows
                if str(row.get("role") or "") == "excluded"
            }
        ),
        "deduplication_basis": "stable local Paper ID; external DOI/URL/source identity",
        "citation_tracking": "not_performed",
        "structure_search": "not_performed",
    }


def normalize_review(payload: dict[str, Any]) -> dict[str, Any]:
    review = deepcopy(payload)
    results = review.get("results")
    if not isinstance(results, list):
        raise WorkflowValidationError("Discovery results must be a list.")
    local_selected: dict[str, bool] = {}
    for group in results:
        if not isinstance(group, dict) or not str(group.get("keyword") or "").strip():
            raise WorkflowValidationError("Every Discovery group needs a keyword.")
        group["keep"] = group.get("keep") is not False
        for row in group.get("local_results") or []:
            if not isinstance(row, dict):
                raise WorkflowValidationError("Discovery candidates must be objects.")
            paper_id = _candidate_id(row, external=False)
            if not paper_id:
                raise WorkflowValidationError("Local Discovery candidates need paper_id.")
            role = str(row.get("role") or "uncertain")
            row["role"] = role if role in MUTABLE_ROLES else "uncertain"
            chosen = bool(row.get("selected_for_matrix")) and row["role"] != "excluded"
            local_selected[paper_id] = local_selected.get(paper_id, False) or chosen
            for field in LEGACY_DISCOVERY_TAG_FIELDS:
                row.pop(field, None)
        for row in group.get("web_results") or []:
            if not isinstance(row, dict) or not _candidate_id(row, external=True):
                raise WorkflowValidationError("External candidates need a stable source identity.")
            # External candidates have no canonical, user-owned Library paper.
            # They must be downloaded, parsed and indexed before Matrix use.
            row["selected_for_matrix"] = False
    for group in results:
        for row in group.get("local_results") or []:
            paper_id = _candidate_id(row, external=False)
            row["selected_for_matrix"] = local_selected[paper_id]
    review["selection_mode"] = "explicit"
    review["coverage_diagnostics"] = discovery_coverage_diagnostics(review)
    review["search_record"] = discovery_search_record(review)
    review["coverage_mode"] = review["coverage_diagnostics"]["coverage_mode"]
    return review


def statistics(review: dict[str, Any]) -> dict[str, int]:
    groups = [group for group in review.get("results") or [] if group.get("keep") is not False]
    local_rows = [row for group in groups for row in group.get("local_results") or []]
    external_rows = [row for group in groups for row in group.get("web_results") or []]
    local_ids = {_candidate_id(row, external=False) for row in local_rows}
    selected_ids = {_candidate_id(row, external=False) for row in local_rows if _selected(row)}
    external_ids = {_candidate_id(row, external=True) for row in external_rows}
    categories = {
        str(group.get("category") or "unclassified") for group in groups
    }
    return {
        "candidate_count": len(local_ids),
        "keyword_hit_count": len(local_rows),
        "selected_count": len(selected_ids),
        "keyword_group_count": len(groups),
        "external_candidate_count": len(external_ids),
        "category_count": len(categories),
        "unclassified_keyword_group_count": sum(
            1 for group in groups if str(group.get("category") or "unclassified") == "unclassified"
        ),
    }


class DiscoveryService(OwnedProjectService):
    def __init__(
        self,
        repository: WorkflowRepository,
        artifacts: ArtifactService,
        library_index: Any | None = None,
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.library_index = library_index
        self._write_lock = threading.RLock()

    @staticmethod
    def _paper_metadata_value(paper: LibraryPaper, key: str, default: Any = None) -> Any:
        metadata = paper.metadata_json if isinstance(paper.metadata_json, dict) else {}
        value = unwrap_metadata_value(metadata.get(key))
        return default if value in (None, "") else value

    def _active_library_catalog(self, principal: Principal) -> dict[str, LibraryPaper]:
        principal.require(Permission.PROJECT_READ)
        with database_session(self.repository.session_factory) as session:
            rows = list(
                session.scalars(
                    select(LibraryPaper).where(
                        LibraryPaper.user_id == uuid.UUID(principal.user_id),
                        LibraryPaper.deleted_at.is_(None),
                        LibraryPaper.status == "active",
                    )
                )
            )
        return {row.paper_id: row for row in rows}

    def _local_candidate_from_catalog(
        self,
        paper: LibraryPaper,
        *,
        score: float = 0.0,
        reason: str = "Recovered by the Discovery hybrid retrieval layer",
    ) -> dict[str, Any]:
        metadata = paper.metadata_json if isinstance(paper.metadata_json, dict) else {}
        source_paths = metadata.get("source_paths") or {}
        return {
            "paper_id": paper.paper_id,
            "title": self._paper_metadata_value(paper, "title", paper.title) or paper.paper_id,
            "authors": self._paper_metadata_value(paper, "authors", paper.authors_json) or [],
            "year": self._paper_metadata_value(paper, "year", None),
            "first_publication_date": self._paper_metadata_value(
                paper, "first_publication_date", None
            ),
            "journal": self._paper_metadata_value(paper, "journal", "") or "",
            "doi": self._paper_metadata_value(paper, "doi", "") or "",
            "score": round(float(score), 6),
            "raw_score": round(float(score), 6),
            "reason": reason,
            "role": "uncertain",
            "keep": True,
            "selected_for_matrix": False,
            "source_paths": source_paths if isinstance(source_paths, dict) else {},
        }

    @staticmethod
    def _semantic_queries(review: dict[str, Any]) -> list[dict[str, Any]]:
        plan = review.get("query_plan") if isinstance(review.get("query_plan"), dict) else {}
        queries = [
            dict(item)
            for item in plan.get("semantic_queries") or []
            if isinstance(item, dict)
            and str(item.get("query_id") or "").strip()
            and str(item.get("query") or "").strip()
        ]
        if queries:
            has_declared_axis_queries = any(
                str(item.get("kind") or "") == "topic_partition"
                and str(item.get("axis_id") or "").strip()
                and str(item.get("partition_id") or "").strip()
                for item in queries
            )
            normalized: list[dict[str, Any]] = []
            for item in queries:
                if (
                    has_declared_axis_queries
                    and str(item.get("kind") or "") == "topic_partition"
                    and not str(item.get("axis_id") or "").strip()
                ):
                    continue
                if str(item.get("kind") or "") == "topic_partition":
                    groups = item.get("lexical_term_groups")
                    discriminator_terms = (
                        groups[-1]
                        if isinstance(groups, list)
                        and groups
                        and isinstance(groups[-1], list)
                        else [item.get("source_surface") or item.get("label") or ""]
                    )
                    discriminator_terms = list(
                        dict.fromkeys(
                            " ".join(str(value or "").split())
                            for value in discriminator_terms
                            if " ".join(str(value or "").split())
                        )
                    )[:12]
                    if not discriminator_terms:
                        continue
                    item["query"] = " ; ".join(discriminator_terms)
                    item["lexical_term_groups"] = [discriminator_terms]
                    item["admission_query_id"] = "topic_core"
                elif str(item.get("kind") or "") == "topic_core":
                    groups = item.get("lexical_term_groups")
                    legacy_terms = (
                        groups[0]
                        if isinstance(groups, list)
                        and len(groups) == 1
                        and isinstance(groups[0], list)
                        and len(groups[0]) > 1
                        else []
                    )
                    if legacy_terms:
                        for index, term in enumerate(legacy_terms[:4], start=1):
                            clean = " ".join(str(term or "").split())
                            if not clean:
                                continue
                            split_item = dict(item)
                            split_item["query_id"] = (
                                "topic_core"
                                if index == 1
                                else f"topic_core_{index:02d}"
                            )
                            split_item["label"] = (
                                "Core topic"
                                if index == 1
                                else f"Core topic {index}"
                            )
                            split_item["query"] = clean
                            split_item["lexical_term_groups"] = [[clean]]
                            normalized.append(split_item)
                        continue
                normalized.append(item)
            return normalized[:16]
        # Old and test artifacts may predate semantic_queries.  Build a compact
        # query from validated group labels rather than embedding the raw Topic.
        labels = [
            " ".join(str(group.get("keyword") or "").split())
            for group in review.get("results") or []
            if isinstance(group, dict) and str(group.get("keyword") or "").strip()
        ]
        labels = list(dict.fromkeys(labels))[:12]
        if not labels:
            return []
        core_queries = [
            {
                "query_id": "topic_core" if index == 1 else f"topic_core_{index:02d}",
                "kind": "topic_core",
                "label": "Core topic" if index == 1 else f"Core topic {index}",
                "query": label,
                "lexical_term_groups": [[label]],
            }
            for index, label in enumerate(labels[:4], start=1)
        ]
        return [
            *core_queries,
            *[
                {
                    "query_id": f"partition_{index:02d}",
                    "kind": "topic_partition",
                    "label": label,
                    "query": label,
                    "source_surface": label,
                    "lexical_term_groups": [[label]],
                    "admission_query_id": "topic_core",
                }
                for index, label in enumerate(labels, start=1)
            ],
        ][:16]

    @staticmethod
    def _partition_assignments(
        signal: dict[str, Any],
        query_by_id: dict[str, dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Separate lexical and semantic *retrieval* hints.

        Semantic similarity may admit a paper to the Topic candidate pool, but
        neither it nor a lexical chunk hit proves that the matching expression
        describes the paper's own contribution. Concrete scientific facets are
        assigned only after selection, from source-addressable Matrix facts.
        """

        matches = (
            signal.get("query_matches")
            if isinstance(signal.get("query_matches"), dict)
            else {}
        )
        lexical_candidates: list[str] = []
        semantic_candidates: list[str] = []
        for query_id, query in query_by_id.items():
            if str(query.get("kind") or "") != "topic_partition":
                continue
            match = matches.get(query_id) if isinstance(matches.get(query_id), dict) else {}
            lexical_count = int(match.get("lexical_chunk_count") or 0)
            semantic_count = int(match.get("semantic_chunk_count") or 0)
            if lexical_count > 0:
                lexical_candidates.append(query_id)
            elif semantic_count > 0:
                semantic_candidates.append(query_id)
        return lexical_candidates, semantic_candidates

    @staticmethod
    def _screening_text_supports_partition(
        text: str,
        query: dict[str, Any],
    ) -> bool:
        """Return whether title/abstract text explicitly supports a facet.

        This is intentionally lexical.  External records have no trusted full
        text yet, so semantic similarity remains a recall hint until the PDF is
        downloaded, parsed and indexed.
        """

        normalized_text = " ".join(str(text or "").casefold().split())
        groups = query.get("lexical_term_groups")
        terms = (
            groups[-1]
            if isinstance(groups, list) and groups and isinstance(groups[-1], list)
            else [query.get("source_surface") or query.get("label") or ""]
        )
        for raw_term in terms:
            term = " ".join(str(raw_term or "").casefold().split())
            if not term:
                continue
            if re.search(
                r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])",
                normalized_text,
                re.I,
            ):
                return True
            tokens = re.findall(r"[a-z0-9][a-z0-9'′-]*", term, re.I)
            tokens = [token for token in tokens if len(token) >= 2]
            if tokens and all(
                re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", normalized_text, re.I)
                for token in tokens
            ):
                return True
        return False

    @staticmethod
    def _pending_candidate_group(review: dict[str, Any]) -> dict[str, Any]:
        results = review.setdefault("results", [])
        for group in results:
            if (
                isinstance(group, dict)
                and str(group.get("system_group") or "") == TOPIC_CANDIDATE_POOL_KEY
            ):
                return group
        group = {
            "keyword": "Hybrid-retrieved Topic candidates",
            "category": "unclassified",
            "system_group": TOPIC_CANDIDATE_POOL_KEY,
            "classification_status": "deferred_to_matrix",
            "classification_stage": "matrix_after_selection",
            "keep": True,
            "local_results": [],
            "web_results": [],
        }
        results.append(group)
        return group

    @staticmethod
    def _cosine(left: list[Any], right: list[Any]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        left_values = [float(value) for value in left]
        right_values = [float(value) for value in right]
        denominator = math.sqrt(sum(value * value for value in left_values)) * math.sqrt(
            sum(value * value for value in right_values)
        )
        if denominator <= 0:
            return 0.0
        return sum(a * b for a, b in zip(left_values, right_values)) / denominator

    @staticmethod
    def _external_access_status(row: dict[str, Any]) -> str:
        if str(row.get("resolved_paper_id") or "").strip():
            return "downloaded_to_library"
        open_access = row.get("open_access")
        is_oa = (
            open_access.get("is_oa")
            if isinstance(open_access, dict)
            else bool(open_access) if open_access is not None else None
        )
        if str(row.get("pdf_url") or "").strip() or is_oa is True:
            return "open_access_downloadable"
        if is_oa is False and (
            str(row.get("landing_url") or row.get("url") or "").strip()
            or normalize_doi(row.get("doi"))
        ):
            return "institution_required"
        if not str(row.get("title") or "").strip():
            return "metadata_only"
        return "access_unknown"

    def _rerank_external_candidates(
        self,
        principal: Principal,
        review: dict[str, Any],
        queries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unique: dict[str, dict[str, Any]] = {}
        for group in review.get("results") or []:
            for row in group.get("web_results") or []:
                if not isinstance(row, dict):
                    continue
                identity = _candidate_id(row, external=True)
                if identity:
                    unique.setdefault(identity, row)
                row["selected_for_matrix"] = False
                row["access_status"] = self._external_access_status(row)
        if not unique or not queries or self.library_index is None:
            return {"status": "not_applicable" if not unique else "unavailable"}

        query_texts = [str(item.get("query") or "") for item in queries]
        admission_query_indexes = [
            index
            for index, query in enumerate(queries)
            if str(query.get("kind") or "") != "topic_partition"
        ]
        if not admission_query_indexes:
            return {
                "status": "degraded",
                "reason": "No Topic admission query was available for external screening.",
            }
        candidate_ids: list[str] = []
        candidate_texts: list[str] = []
        candidate_text_by_id: dict[str, str] = {}
        for identity, row in unique.items():
            title = " ".join(str(row.get("title") or "").split())
            abstract = " ".join(str(row.get("abstract") or "").split())[:6000]
            if not title and not abstract:
                row["semantic_screening"] = {"status": "metadata_insufficient"}
                continue
            candidate_ids.append(identity)
            screening_text = f"Title: {title}\nAbstract: {abstract}"
            candidate_texts.append(screening_text)
            candidate_text_by_id[identity] = screening_text
        if not candidate_texts:
            return {"status": "metadata_insufficient"}
        digest = hashlib.sha256(
            json.dumps([query_texts, candidate_ids], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        embedded = self.library_index.embed_screening_texts(
            principal,
            [*query_texts, *candidate_texts],
            request_key=f"discovery-external:{digest[:32]}",
        )
        vectors = embedded.get("embeddings")
        if embedded.get("status") != "ready" or not isinstance(vectors, list):
            for row in unique.values():
                row["semantic_screening"] = {
                    "status": "degraded",
                    "reason": str(embedded.get("error") or "Embedding service unavailable"),
                }
            return {
                "status": "degraded",
                "reason": str(embedded.get("error") or "Embedding service unavailable"),
            }
        query_vectors = vectors[: len(query_texts)]
        document_vectors = vectors[len(query_texts) :]
        threshold = float(getattr(self.library_index.tuning, "semantic_min_similarity", 0.35))
        best_by_id: dict[str, float] = {}
        for identity, vector in zip(candidate_ids, document_vectors):
            similarities = [self._cosine(vector, query_vector) for query_vector in query_vectors]
            matched = [
                str(queries[index].get("query_id") or "")
                for index, similarity in enumerate(similarities)
                if similarity >= threshold
            ]
            evidence_backed_partitions = [
                str(query.get("query_id") or "")
                for query in queries
                if str(query.get("kind") or "") == "topic_partition"
                and self._screening_text_supports_partition(
                    candidate_text_by_id.get(identity, ""), query
                )
            ]
            semantic_partition_candidates = [
                query_id
                for query_id in matched
                if query_id != "topic_core"
                and query_id not in evidence_backed_partitions
            ]
            admission_similarities = [
                similarities[index]
                for index in admission_query_indexes
                if index < len(similarities)
            ]
            # Partition similarity is a review hint only. It must not admit or
            # rank an external record as relevant to the whole Topic.
            best = max(admission_similarities, default=0.0)
            best_by_id[identity] = best
            row = unique[identity]
            row["semantic_screening"] = {
                "status": "ready",
                "best_similarity": round(best, 6),
                "matched_query_ids": matched,
                "title_only": not bool(str(row.get("abstract") or "").strip()),
                "embedding_model": str(embedded.get("model") or ""),
                "embedding_dimension": int(embedded.get("dimension") or 0),
            }
            row["matched_query_ids"] = matched
            row["matched_partitions"] = evidence_backed_partitions
            row["semantic_partition_candidates"] = semantic_partition_candidates
            channels = ["title_abstract_lexical"]
            if matched:
                channels.append("title_abstract_semantic")
            row["retrieval_channels"] = channels
            row["recommendation_status"] = (
                "recommended"
                if best >= max(threshold + 0.12, 0.55) and not row["semantic_screening"]["title_only"]
                else "review"
            )
        lexical_rank = {
            identity: rank
            for rank, (identity, _row) in enumerate(
                sorted(
                    unique.items(),
                    key=lambda item: (-float(item[1].get("score") or 0), item[0]),
                ),
                start=1,
            )
        }
        semantic_rank = {
            identity: rank
            for rank, identity in enumerate(
                sorted(best_by_id, key=lambda value: (-best_by_id[value], value)),
                start=1,
            )
        }
        constant = float(getattr(self.library_index.tuning, "rrf_constant", 60))
        for identity, row in unique.items():
            row["external_rrf_score"] = round(
                1.0 / (constant + lexical_rank[identity])
                + (
                    0.8 / (constant + semantic_rank[identity])
                    if identity in semantic_rank
                    else 0.0
                ),
                8,
            )
        return {
            "status": "ready",
            "candidate_count": len(candidate_ids),
            "embedding_model": str(embedded.get("model") or ""),
            "embedding_dimension": int(embedded.get("dimension") or 0),
        }

    def enrich_hybrid(
        self,
        principal: Principal,
        project_id: str,
        built: dict[str, Any],
    ) -> dict[str, Any]:
        """Add paper-level lexical/semantic screening to a Discovery build."""

        self._owned_project(principal, project_id)
        review = deepcopy(built)
        if not isinstance(review.get("results"), list):
            return review
        queries = self._semantic_queries(review)
        catalog = self._active_library_catalog(principal)
        filters = (
            (review.get("query_plan") or {}).get("filters")
            if isinstance(review.get("query_plan"), dict)
            else {}
        ) or review.get("filters") or {}
        year_from = filters.get("year_from") if isinstance(filters, dict) else None
        year_to = filters.get("year_to") if isinstance(filters, dict) else None
        allowed: list[str] = []
        for paper_id, paper in catalog.items():
            year = _publication_year(self._paper_metadata_value(paper, "year", None))
            if type(year_from) is int and (year is None or year < year_from):
                continue
            if type(year_to) is int and (year is None or year > year_to):
                continue
            allowed.append(paper_id)

        relevance = {
            "status": "unavailable",
            "semantic_status": "unavailable",
            "semantic_reason": "library_index_unavailable",
            "papers": {},
        }
        if self.library_index is not None and getattr(self.library_index, "enabled", False):
            try:
                embedding_backfill = {}
                if bool(getattr(self.library_index, "vector_enabled", False)):
                    embedding_backfill = self.library_index.ensure_embeddings(
                        principal, allowed
                    )
                relevance = self.library_index.retrieve_paper_relevance(
                    principal,
                    queries,
                    allowed,
                    per_paper_chunk_limit=3,
                )
                relevance["embedding_backfill"] = embedding_backfill
            except Exception as exc:
                relevance = {
                    "status": "degraded",
                    "semantic_status": "degraded",
                    "semantic_reason": f"{type(exc).__name__}: {exc}"[:500],
                    "papers": {},
                }

        groups = [group for group in review.get("results") or [] if isinstance(group, dict)]
        local_rows_by_paper: dict[str, list[dict[str, Any]]] = {}
        deterministic_score: dict[str, float] = {}
        for group in groups:
            for row in group.get("local_results") or []:
                if not isinstance(row, dict):
                    continue
                paper_id = _candidate_id(row, external=False)
                if not paper_id:
                    continue
                local_rows_by_paper.setdefault(paper_id, []).append(row)
                deterministic_score[paper_id] = max(
                    deterministic_score.get(paper_id, 0.0),
                    float(row.get("score") or row.get("raw_score") or 0),
                )
        metadata_rank = {
            paper_id: rank
            for rank, paper_id in enumerate(
                sorted(
                    deterministic_score,
                    key=lambda value: (-deterministic_score[value], value),
                ),
                start=1,
            )
        }
        paper_signals = relevance.get("papers") if isinstance(relevance.get("papers"), dict) else {}
        index_statuses = (
            relevance.get("index_statuses")
            if isinstance(relevance.get("index_statuses"), dict)
            else {}
        )
        constant = float(relevance.get("rrf_constant") or 60)
        combined_scores: dict[str, float] = {}
        for paper_id in set(paper_signals) | set(metadata_rank):
            signal = dict(paper_signals.get(paper_id) or {})
            score = float(signal.get("rrf_score") or 0)
            if paper_id in metadata_rank:
                score += 1.0 / (constant + metadata_rank[paper_id])
            combined_scores[paper_id] = score
        max_score = max(combined_scores.values(), default=1.0) or 1.0

        query_by_id = {str(item.get("query_id") or ""): item for item in queries}
        for paper_id, score in combined_scores.items():
            paper = catalog.get(paper_id)
            if paper is None:
                continue
            signal = deepcopy(paper_signals.get(paper_id) or {})
            lexical_partition_candidates, semantic_partition_candidates = (
                self._partition_assignments(signal, query_by_id)
            )
            normalized_score = score / max_score
            rows = local_rows_by_paper.get(paper_id, [])
            if not rows and signal:
                # Hybrid retrieval may recover papers omitted by the metadata
                # candidate set. Keep them in a neutral Topic candidate pool;
                # query-level facet hits remain retrieval hints until selected
                # papers are classified from Matrix scientific facts.
                pending_group = self._pending_candidate_group(review)
                if pending_group not in groups:
                    groups.append(pending_group)
                row = self._local_candidate_from_catalog(
                    paper,
                    score=normalized_score,
                    reason=(
                        "Recovered as a Topic-related candidate by hybrid retrieval; "
                        "formal partition classification is deferred to Matrix facts"
                    ),
                )
                row["classification_status"] = "deferred_to_matrix"
                row["classification_stage"] = "matrix_after_selection"
                pending_group.setdefault("local_results", []).append(row)
                rows.append(row)
                if rows:
                    local_rows_by_paper[paper_id] = rows
            if not rows:
                continue
            channels = list(signal.get("retrieval_channels") or [])
            if paper_id in metadata_rank:
                channels.insert(0, "metadata_rules")
            channels = list(dict.fromkeys(channels))
            strong_query = any(
                int(match.get("lexical_chunk_count") or 0) > 0
                and int(match.get("semantic_chunk_count") or 0) > 0
                for query_id, match in (signal.get("query_matches") or {}).items()
                if isinstance(match, dict)
                and str((query_by_id.get(str(query_id)) or {}).get("kind") or "")
                != "topic_partition"
            )
            deterministic_recommended = any(
                str(candidate.get("role") or "")
                in {"core_candidate", "supporting_candidate"}
                for candidate in rows
            )
            recommendation = (
                "recommended"
                if paper_id in metadata_rank
                and (deterministic_recommended or strong_query)
                else "review"
            )
            for row in rows:
                row["hybrid_score"] = round(normalized_score, 6)
                row["retrieval_signals"] = {
                    "metadata_rank": metadata_rank.get(paper_id),
                    "paper_rrf_score": round(score, 8),
                    "query_matches": deepcopy(signal.get("query_matches") or {}),
                }
                row["matched_query_ids"] = list(signal.get("matched_query_ids") or [])
                row["lexical_partition_candidates"] = list(
                    lexical_partition_candidates
                )
                row["matched_partitions"] = []
                row["classification_status"] = "deferred_to_matrix"
                row["classification_stage"] = "matrix_after_selection"
                row["semantic_partition_candidates"] = list(
                    semantic_partition_candidates
                )
                row["retrieval_channels"] = channels
                row["screening_chunks"] = deepcopy(signal.get("top_chunks") or [])[:3]
                row["semantic_index_status"] = str(
                    signal.get("semantic_index_status")
                    or (index_statuses.get(paper_id) or {}).get("semantic")
                    or "not_indexed"
                )
                row["recommendation_status"] = recommendation

        # Canonicalize candidates already present in this user's Library.
        doi_to_paper: dict[str, str] = {}
        title_to_paper: dict[str, str] = {}
        for paper_id, paper in catalog.items():
            doi = normalize_doi(self._paper_metadata_value(paper, "doi", ""))
            title = normalize_title(self._paper_metadata_value(paper, "title", paper.title))
            if doi:
                doi_to_paper[doi] = paper_id
            if len(title) >= 20:
                title_to_paper[title] = paper_id
        for group in groups:
            retained: list[dict[str, Any]] = []
            for external in group.get("web_results") or []:
                if not isinstance(external, dict):
                    continue
                paper_id = doi_to_paper.get(normalize_doi(external.get("doi")))
                if not paper_id:
                    paper_id = title_to_paper.get(normalize_title(external.get("title")))
                if not paper_id or paper_id not in catalog:
                    retained.append(external)
                    continue
                candidates = [
                    row
                    for row in group.get("local_results") or []
                    if isinstance(row, dict) and _candidate_id(row, external=False) == paper_id
                ]
                if not candidates:
                    local = self._local_candidate_from_catalog(
                        catalog[paper_id],
                        reason="External source matched an existing Library paper",
                    )
                    group.setdefault("local_results", []).append(local)
                    candidates = [local]
                for local in candidates:
                    local["external_sources"] = deepcopy(external.get("sources") or [external.get("source")])
                    local["access_status"] = "downloaded_to_library"
            group["web_results"] = retained

        review["results"] = [
            group
            for group in review.get("results") or []
            if not (
                isinstance(group, dict)
                and str(group.get("system_group") or "") == TOPIC_CANDIDATE_POOL_KEY
                and not (group.get("local_results") or group.get("web_results"))
            )
        ]
        external_screening = self._rerank_external_candidates(principal, review, queries)
        review["hybrid_retrieval"] = {
            "schema_version": 1,
            "stage_boundary": "retrieval_and_selection_only",
            "matrix_facts_used": False,
            "status": str(relevance.get("status") or "unavailable"),
            "semantic_status": str(relevance.get("semantic_status") or "unavailable"),
            "semantic_reason": str(relevance.get("semantic_reason") or "")[:500],
            "semantic_indexed_paper_count": int(relevance.get("semantic_indexed_paper_count") or 0),
            "embedding_backfill": deepcopy(relevance.get("embedding_backfill") or {}),
            "library_paper_count": len(allowed),
            "embedding_model": str(relevance.get("embedding_model") or external_screening.get("embedding_model") or ""),
            "embedding_dimension": int(relevance.get("embedding_dimension") or external_screening.get("embedding_dimension") or 0),
            "external_screening": external_screening,
            "partition_assignment_policy": "retrieval_hints_only_then_matrix_fact_classification",
        }
        return review

    def refresh_external_candidate(
        self,
        principal: Principal,
        project_id: str,
        *,
        candidate_id: str,
        paper_id: str,
        source_revision: int | None = None,
    ) -> dict[str, Any]:
        """Publish a reviewable Discovery revision after acquisition/indexing."""

        self._owned_project(principal, project_id)
        catalog = self._active_library_catalog(principal)
        paper = catalog.get(str(paper_id or "").strip())
        if paper is None:
            raise WorkflowNotFound("Downloaded Library paper not found.")
        wanted_candidate_id = str(candidate_id or "").strip()
        paper_doi = normalize_doi(self._paper_metadata_value(paper, "doi", ""))
        paper_title = normalize_title(
            self._paper_metadata_value(paper, "title", paper.title)
        )

        for attempt in range(2):
            current, artifact = self._read_current(
                principal, project_id, DISCOVERY_LOGICAL_NAME
            )
            state = self.repository.get_stage_state(
                principal.user_id, project_id, "discovery"
            )
            revision = state.revision if state else 0
            found = False
            changed = deepcopy(current)
            for group in changed.get("results") or []:
                if not isinstance(group, dict):
                    continue
                retained: list[dict[str, Any]] = []
                matched_external: list[dict[str, Any]] = []
                for row in group.get("web_results") or []:
                    if not isinstance(row, dict):
                        continue
                    same = bool(
                        wanted_candidate_id
                        and _candidate_id(row, external=True) == wanted_candidate_id
                    )
                    if not same and paper_doi:
                        same = normalize_doi(row.get("doi")) == paper_doi
                    if not same and len(paper_title) >= 20:
                        same = normalize_title(row.get("title")) == paper_title
                    if same:
                        found = True
                        matched_external.append(row)
                    else:
                        retained.append(row)
                if not matched_external:
                    continue
                group["web_results"] = retained
                local_rows = [
                    row
                    for row in group.get("local_results") or []
                    if isinstance(row, dict)
                    and _candidate_id(row, external=False) == paper.paper_id
                ]
                if not local_rows:
                    local = self._local_candidate_from_catalog(
                        paper,
                        reason="Downloaded external candidate was parsed and indexed in Library",
                    )
                    group.setdefault("local_results", []).append(local)
                    local_rows = [local]
                for local in local_rows:
                    local["selected_for_matrix"] = False
                    local["access_status"] = "downloaded_to_library"
                    local["acquired_from_candidate_id"] = wanted_candidate_id
                    local["external_sources"] = list(
                        dict.fromkeys(
                            str(value)
                            for external in matched_external
                            for value in (
                                external.get("sources")
                                if isinstance(external.get("sources"), list)
                                else [external.get("source")]
                            )
                            if str(value).strip()
                        )
                    )
            if not found:
                return {
                    "status": "no_change",
                    "reason": "The current Discovery revision no longer contains this external candidate.",
                    "project_id": project_id,
                    "paper_id": paper.paper_id,
                    "current_revision": revision,
                    "source_revision": source_revision,
                }
            changed["candidate_refresh"] = {
                "schema_version": 1,
                "candidate_id": wanted_candidate_id,
                "paper_id": paper.paper_id,
                "source_revision": source_revision,
                "source_artifact_id": artifact.id,
                "refreshed_at": utc_now().isoformat(),
                "matrix_modified": False,
            }
            changed = self.enrich_hybrid(principal, project_id, changed)
            review = normalize_review(changed)
            try:
                published = self._publish_review(
                    principal,
                    project_id,
                    review,
                    expected_revision=revision,
                    status_value="review",
                )
                return {
                    "status": "refreshed",
                    "project_id": project_id,
                    "paper_id": paper.paper_id,
                    "candidate_id": wanted_candidate_id,
                    "revision": published["revision"],
                    "artifact_id": published["artifact_id"],
                    "matrix_modified": False,
                }
            except WorkflowConflict:
                if attempt == 0:
                    continue
                raise
        raise WorkflowConflict("Discovery candidate refresh conflicted repeatedly.")

    def _write_json_artifact(
        self,
        principal: Principal,
        project_id: str,
        *,
        stage_id: str,
        logical_name: str,
        payload: dict[str, Any],
        make_current: bool = True,
    ):
        run = self.repository.create_stage_run(
            principal.user_id,
            project_id,
            stage_id,
            status="succeeded",
            input_snapshot={"logical_name": logical_name},
        )
        staging = self.artifacts.stage_run_directory(principal.user_id, project_id, run.id)
        filename = Path(logical_name).name
        atomic_write_json(staging / filename, payload)
        return self.artifacts.publish(
            principal.user_id,
            project_id,
            run.id,
            filename,
            logical_name=logical_name,
            artifact_type="json",
            producer_stage=stage_id,
            make_current=make_current,
        ), run

    def _read_current(self, principal: Principal, project_id: str, logical_name: str) -> tuple[dict[str, Any], Any]:
        self._owned_project(principal, project_id)
        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            raise WorkflowNotFound("Discovery review not found.")
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact.id)
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict("The current Discovery artifact is unreadable.") from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict("The current Discovery artifact is invalid.")
        return payload, artifact

    def _read_optional_current_json(
        self, principal: Principal, project_id: str, logical_name: str
    ) -> tuple[dict[str, Any] | None, Any | None]:
        """Read an optional published JSON artifact without changing its pointer."""

        artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, logical_name
        )
        if artifact is None:
            return None, None
        resolved = self.artifacts.resolve_owned_artifact(
            principal.user_id, artifact.id
        )
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowConflict(
                f"The current {logical_name} artifact is unreadable."
            ) from exc
        if not isinstance(payload, dict):
            raise WorkflowConflict(
                f"The current {logical_name} artifact is invalid."
            )
        return payload, artifact

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        payload, artifact = self._read_current(principal, project_id, DISCOVERY_LOGICAL_NAME)
        state = self.repository.get_stage_state(principal.user_id, project_id, "discovery")
        matrix_artifact = self.repository.get_current_artifact(
            principal.user_id, project_id, MATRIX_LOGICAL_NAME
        )
        review = normalize_review(payload)
        return {
            **review,
            "project_id": project_id,
            "artifact_id": artifact.id,
            "revision": state.revision if state else 0,
            "status": state.status if state else "pending",
            "has_published_matrix": matrix_artifact is not None,
            "statistics": statistics(review),
            "selected_paper_ids": self.selected_paper_ids(review),
        }

    @staticmethod
    def selected_paper_ids(review: dict[str, Any]) -> list[str]:
        ranked: dict[str, tuple[float, int]] = {}
        order = 0
        for group in review.get("results") or []:
            if group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                if not _selected(row):
                    order += 1
                    continue
                paper_id = _candidate_id(row, external=False)
                score = float(row.get("score") or row.get("raw_score") or 0)
                previous = ranked.get(paper_id)
                if previous is None or score > previous[0]:
                    ranked[paper_id] = (score, order)
                order += 1
        return [key for key, _value in sorted(ranked.items(), key=lambda item: (-item[1][0], item[1][1]))]

    @staticmethod
    def _merge_mutable(current: dict[str, Any], proposed_results: list[Any]) -> dict[str, Any]:
        proposed = normalize_review({"results": proposed_results})
        proposed_groups = {
            str(group.get("keyword")): group for group in proposed["results"]
        }
        merged = deepcopy(current)
        if set(proposed_groups) != {
            str(group.get("keyword")) for group in merged.get("results") or []
        }:
            raise WorkflowValidationError("Discovery candidate groups cannot be added or removed while saving review.")
        proposed_local: dict[str, dict[str, Any]] = {}
        proposed_external: dict[str, dict[str, Any]] = {}
        for group in proposed["results"]:
            for row in group.get("local_results") or []:
                proposed_local[_candidate_id(row, external=False)] = row
            for row in group.get("web_results") or []:
                proposed_external[_candidate_id(row, external=True)] = row
        for group in merged.get("results") or []:
            source_group = proposed_groups[str(group.get("keyword"))]
            group["keep"] = source_group.get("keep") is not False
            for row in group.get("local_results") or []:
                identity = _candidate_id(row, external=False)
                if identity not in proposed_local:
                    raise WorkflowValidationError("Discovery candidates cannot be added or removed while saving review.")
                source = proposed_local[identity]
                role = str(source.get("role") or "uncertain")
                row["role"] = role if role in MUTABLE_ROLES else "uncertain"
                row["selected_for_matrix"] = bool(source.get("selected_for_matrix")) and row["role"] != "excluded"
            for row in group.get("web_results") or []:
                identity = _candidate_id(row, external=True)
                if identity not in proposed_external:
                    raise WorkflowValidationError("External candidates cannot be added or removed while saving review.")
                row["selected_for_matrix"] = False
        return normalize_review(merged)

    def _publish_review(
        self,
        principal: Principal,
        project_id: str,
        review: dict[str, Any],
        *,
        expected_revision: int,
        status_value: str = "review",
    ) -> dict[str, Any]:
        with self._write_lock:
            state = self.repository.get_stage_state(principal.user_id, project_id, "discovery")
            actual = state.revision if state else 0
            if actual != expected_revision:
                raise WorkflowConflict(
                    "Discovery changed since it was loaded.",
                    details={"expected_revision": expected_revision, "actual_revision": actual},
                )
            artifact, run = self._write_json_artifact(
                principal,
                project_id,
                stage_id="discovery",
                logical_name=DISCOVERY_LOGICAL_NAME,
                payload=review,
                make_current=False,
            )
            next_state = self.repository.save_discovery_atomically(
                principal.user_id,
                project_id,
                artifact_id=artifact.id,
                run_id=run.id,
                expected_revision=expected_revision,
                status=status_value,
            )
        return {
            **review,
            "artifact_id": artifact.id,
            "revision": next_state.revision,
            "statistics": statistics(review),
            "selected_paper_ids": self.selected_paper_ids(review),
        }

    def replace_from_job(
        self,
        principal: Principal,
        project_id: str,
        payload: dict[str, Any],
        built: dict[str, Any],
    ) -> dict[str, Any]:
        self._owned_project(principal, project_id)
        review = normalize_review(
            {**built, "project_id": project_id, "topic": payload["topic"]}
        )
        state = self.repository.get_stage_state(
            principal.user_id, project_id, "discovery"
        )
        artifact, run = self._write_json_artifact(
            principal,
            project_id,
            stage_id="discovery",
            logical_name=DISCOVERY_LOGICAL_NAME,
            payload=review,
            make_current=False,
        )
        next_state = self.repository.replace_discovery_atomically(
            principal.user_id,
            project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=state.revision if state else 0,
            topic=str(payload["topic"]),
        )
        external_search = deepcopy(review.get("external_search") or {})
        return {
            "artifact_id": artifact.id,
            "revision": next_state.revision,
            "statistics": statistics(review),
            "external_search": external_search,
            "completion_state": str(external_search.get("completion_state") or "complete"),
            "degraded": bool(external_search.get("degraded")),
            "source_errors": deepcopy(external_search.get("source_errors") or {}),
        }

    def save(
        self,
        principal: Principal,
        project_id: str,
        revision: int,
        results: list[Any],
        *,
        coverage_decision: str | None = None,
    ) -> dict[str, Any]:
        current, _artifact = self._read_current(principal, project_id, DISCOVERY_LOGICAL_NAME)
        merged = self._merge_mutable(current, results)
        if coverage_decision is not None:
            if coverage_decision != "keep_local":
                raise WorkflowValidationError("Unsupported coverage decision.")
            merged["coverage_decision"] = "keep_local"
            merged["coverage_decided_at"] = utc_now().isoformat()
        return self._publish_review(
            principal, project_id, merged, expected_revision=int(revision)
        )

    def select_one(
        self,
        principal: Principal,
        project_id: str,
        paper_id: str,
        selected: bool,
    ) -> dict[str, Any]:
        current = self.get(principal, project_id)
        found = False
        for group in current["results"]:
            for row in group.get("local_results") or []:
                if _candidate_id(row, external=False) == paper_id:
                    found = True
                    row["selected_for_matrix"] = bool(selected)
                    if selected and row.get("role") == "excluded":
                        row["role"] = "uncertain"
        if not found:
            raise WorkflowNotFound("Discovery candidate not found.")
        return self.save(principal, project_id, current["revision"], current["results"])

    def select_top(
        self, principal: Principal, project_id: str, count: int
    ) -> dict[str, Any]:
        if int(count) < 1:
            raise WorkflowValidationError("Top-N count must be positive.")
        current = self.get(principal, project_id)
        ranked: dict[str, tuple[float, int]] = {}
        order = 0
        for group in current["results"]:
            if group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                identity = _candidate_id(row, external=False)
                score = float(row.get("score") or row.get("raw_score") or 0)
                previous = ranked.get(identity)
                if previous is None or score > previous[0]:
                    ranked[identity] = (score, order)
                order += 1
        selected_ids = {
            identity
            for identity, _rank in sorted(
                ranked.items(), key=lambda item: (-item[1][0], item[1][1])
            )[: int(count)]
        }
        for group in current["results"]:
            for row in group.get("local_results") or []:
                chosen = _candidate_id(row, external=False) in selected_ids
                row["selected_for_matrix"] = chosen
                if chosen and row.get("role") == "excluded":
                    row["role"] = "uncertain"
        return self.save(principal, project_id, current["revision"], current["results"])

    def clear(self, principal: Principal, project_id: str) -> dict[str, Any]:
        current = self.get(principal, project_id)
        for group in current["results"]:
            for row in [
                *(group.get("local_results") or []),
                *(group.get("web_results") or []),
            ]:
                row["selected_for_matrix"] = False
        return self.save(principal, project_id, current["revision"], current["results"])

    def confirm(self, principal: Principal, project_id: str, revision: int) -> dict[str, Any]:
        current = self.get(principal, project_id)
        if current["revision"] != int(revision):
            raise WorkflowConflict(
                "Discovery changed since confirmation was opened.",
                details={"expected_revision": revision, "actual_revision": current["revision"]},
            )
        selected_ids = current["selected_paper_ids"]
        user_uuid = uuid.UUID(principal.user_id)
        with database_session(self.repository.session_factory) as session:
            catalog = session.scalars(
                select(LibraryPaper).where(
                    LibraryPaper.user_id == user_uuid,
                    LibraryPaper.paper_id.in_(selected_ids),
                    LibraryPaper.deleted_at.is_(None),
                    LibraryPaper.status == "active",
                )
            ).all()
        catalog_by_id = {row.paper_id: row for row in catalog}
        missing = [
            paper_id for paper_id in selected_ids if paper_id not in catalog_by_id
        ]
        if missing:
            raise DiscoverySelectionNotInLibrary(
                "Selected Discovery papers must belong to your active Library catalog.",
                details={"paper_ids": missing},
            )

        existing_matrix, existing_matrix_artifact = self._read_optional_current_json(
            principal, project_id, MATRIX_LOGICAL_NAME
        )
        existing_rows = (
            existing_matrix.get("rows")
            if isinstance(existing_matrix, dict)
            and isinstance(existing_matrix.get("rows"), list)
            else []
        )
        existing_ids = [
            str(row.get("paper_id") or "").strip()
            for row in existing_rows
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        ]
        current_topic = " ".join(str(current.get("topic") or "").split())
        matrix_topic = " ".join(
            str((existing_matrix or {}).get("review_topic") or "").split()
        )
        existing_matrix_state = self.repository.get_stage_state(
            principal.user_id, project_id, "matrix"
        )
        matrix_input_unchanged = bool(
            existing_matrix_artifact
            and existing_matrix_state
            and existing_matrix_state.status != "stale"
            and len(existing_ids) == len(selected_ids)
            and set(existing_ids) == set(selected_ids)
            and matrix_topic.casefold() == current_topic.casefold()
            and int((existing_matrix or {}).get("classification_policy_version") or 0)
            == MATRIX_CLASSIFICATION_POLICY_VERSION
            and str((existing_matrix or {}).get("coverage_mode") or "local_bounded")
            == str(current.get("coverage_mode") or "local_bounded")
        )

        if matrix_input_unchanged:
            with self._write_lock:
                discovery_state = (
                    self.repository.approve_discovery_without_matrix_change_atomically(
                        principal.user_id,
                        project_id,
                        expected_discovery_revision=current["revision"],
                        expected_matrix_artifact_id=existing_matrix_artifact.id,
                        topic=current_topic,
                    )
                )
            return {
                "discovery_revision": discovery_state.revision,
                "matrix_artifact_id": existing_matrix_artifact.id,
                "matrix_revision": existing_matrix_state.revision,
                "matrix": existing_matrix,
                "matrix_sync": {
                    "selected_paper_ids": sorted(selected_ids),
                    "selected_paper_count": len(selected_ids),
                    "synchronized_paper_count": len(existing_ids),
                    "selection_current": True,
                },
                "matrix_reused": True,
                "downstream_stale": False,
            }

        def metadata_value(row: LibraryPaper, key: str, default: Any) -> Any:
            value = row.metadata_json.get(key)
            return unwrap_metadata_value(value) if value is not None else default

        existing_by_id = {
            str(row.get("paper_id") or "").strip(): deepcopy(row)
            for row in existing_rows
            if isinstance(row, dict) and str(row.get("paper_id") or "").strip()
        }
        rows: list[dict[str, Any]] = []
        for paper_id in selected_ids:
            row = {
                **existing_by_id.get(paper_id, {}),
                "paper_id": paper_id,
                "title": metadata_value(
                    catalog_by_id[paper_id],
                    "title",
                    catalog_by_id[paper_id].title,
                )
                or paper_id,
                "authors": metadata_value(
                    catalog_by_id[paper_id],
                    "authors",
                    catalog_by_id[paper_id].authors_json,
                )
                or [],
                "keywords": metadata_value(
                    catalog_by_id[paper_id],
                    "keywords",
                    catalog_by_id[paper_id].keywords_json,
                )
                or [],
                "abstract": metadata_value(
                    catalog_by_id[paper_id],
                    "abstract",
                    "abstract unavailable or unreliable",
                )
                or "abstract unavailable or unreliable",
                "main_content": str(
                    existing_by_id.get(paper_id, {}).get("main_content") or ""
                ),
                "year": metadata_value(catalog_by_id[paper_id], "year", None),
                "first_publication_date": metadata_value(
                    catalog_by_id[paper_id], "first_publication_date", None
                ),
                "bibliographic_year": metadata_value(
                    catalog_by_id[paper_id], "bibliographic_year", None
                ),
                "publication_status": metadata_value(
                    catalog_by_id[paper_id], "publication_status", "unknown"
                ) or "unknown",
                "journal": metadata_value(catalog_by_id[paper_id], "journal", "") or "",
                "doi": metadata_value(catalog_by_id[paper_id], "doi", "") or "",
                "classification_stage": "matrix_after_selection",
                "matrix_status": str(
                    existing_by_id.get(paper_id, {}).get("matrix_status")
                    or "needs_full_reading"
                ),
                "scientific_facts": list(
                    existing_by_id.get(paper_id, {}).get("scientific_facts") or []
                ),
                "fact_enrichment": dict(
                    existing_by_id.get(paper_id, {}).get("fact_enrichment") or {
                        "schema_version": 1,
                        "status": "pending",
                        "fact_count": 0,
                    }
                ),
            }
            for field in LEGACY_MATRIX_TAG_FIELDS:
                row.pop(field, None)
            rows.append(row)
        source_query_plan = (
            dict(current.get("query_plan") or {})
            if isinstance(current.get("query_plan"), dict)
            else {}
        )
        source_axes = deepcopy(
            source_query_plan.get("classification_axes")
            or current.get("classification_axes")
            or []
        )
        group_by = list(source_query_plan.get("group_by") or current.get("group_by") or [])
        classification_contract = canonical_classification_contract(
            source_axes,
            primary_axis_hint=group_by[0] if group_by else "",
            source="confirmed_discovery_selection",
        )
        matrix = {
            "project_id": project_id,
            "review_topic": str(current.get("topic") or ""),
            "classification_policy_version": MATRIX_CLASSIFICATION_POLICY_VERSION,
            "coverage_mode": str(current.get("coverage_mode") or "local_bounded"),
            "coverage_diagnostics": deepcopy(
                current.get("coverage_diagnostics") or {}
            ),
            "external_search": deepcopy(current.get("external_search") or {}),
            "classification_axes": deepcopy(classification_contract["axes"]),
            "classification_contract": classification_contract,
            "classification_contract_version": CLASSIFICATION_CONTRACT_VERSION,
            "classification_stage": "matrix_after_selection",
            "classification_policy": "topic_intent_then_source_addressable_scientific_facts",
            "rows": rows,
            "sync": {
                "selected_paper_ids": sorted(selected_ids),
                "selected_paper_count": len(selected_ids),
                "synchronized_paper_count": len(rows),
                "synced_at": utc_now().isoformat(),
            },
        }
        with self._write_lock:
            matrix_state = self.repository.get_stage_state(
                principal.user_id, project_id, "matrix"
            )
            matrix_artifact, matrix_run = self._write_json_artifact(
                principal,
                project_id,
                stage_id="matrix",
                logical_name=MATRIX_LOGICAL_NAME,
                payload=matrix,
                make_current=False,
            )
            discovery_state, next_matrix = self.repository.confirm_discovery_atomically(
                principal.user_id,
                project_id,
                artifact_id=matrix_artifact.id,
                run_id=matrix_run.id,
                expected_discovery_revision=current["revision"],
                expected_matrix_revision=matrix_state.revision if matrix_state else 0,
                topic=current_topic,
            )
        return {
            "discovery_revision": discovery_state.revision,
            "matrix_artifact_id": matrix_artifact.id,
            "matrix_revision": next_matrix.revision,
            "matrix": matrix,
            "matrix_sync": {
                **matrix["sync"],
                "selection_current": len(selected_ids) == len(rows),
            },
            "matrix_reused": False,
            "downstream_stale": bool(existing_matrix_artifact),
        }
