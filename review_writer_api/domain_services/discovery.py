"""Native Discovery review, explicit selection, and Matrix handoff."""

from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from review_writer_api.artifact_service import ArtifactService
from review_writer_api.database import Project, database_session, utc_now
from review_writer_api.errors import WorkflowConflict, WorkflowNotFound, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_api.workflow_repository import WorkflowRepository
from review_writer_api.workflow_models import LibraryPaper
from review_writer_core.metadata_tags import (
    structured_tags_are_verified,
    verified_structured_tags,
)


DISCOVERY_LOGICAL_NAME = "discovery/review.json"
MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
MUTABLE_ROLES = {
    "core_candidate",
    "supporting_candidate",
    "background",
    "uncertain",
    "excluded",
}
PROJECT_TAG_KEYS = {
    "product",
    "substrate",
    "catalyst_or_method",
    "organometallic_partner",
    "ligand_or_chiral_source",
    "leaving_group",
    "reaction_type",
    "document_scope",
}
TAG_REVIEW_STATUSES = {"pending", "confirmed"}
MATRIX_TAG_POLICY_VERSION = 2


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


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _normalize_project_tags(value: Any, *, field: str) -> dict[str, list[str]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise WorkflowValidationError(f"{field} must be an object.")
    normalized: dict[str, list[str]] = {}
    for raw_category, raw_values in value.items():
        category = str(raw_category or "").strip()
        if category not in PROJECT_TAG_KEYS:
            raise WorkflowValidationError(
                f"{field} contains unsupported category {category!r}."
            )
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        clean: list[str] = []
        seen: set[str] = set()
        for raw in values:
            tag = " ".join(str(raw or "").split()).strip()
            if not tag or tag.casefold() in seen:
                continue
            if len(tag) > 200:
                raise WorkflowValidationError(f"{field} Tag values must be 200 characters or fewer.")
            seen.add(tag.casefold())
            clean.append(tag)
        if clean:
            normalized[category] = clean[:32]
    return normalized


def _normalize_tag_assessment(value: Any) -> dict[str, Any]:
    if value in (None, "") or value == {}:
        return {}
    if not isinstance(value, dict):
        raise WorkflowValidationError("project_tag_assessment must be an object.")
    assessment = deepcopy(value)
    assessment["suggested_tags"] = _normalize_project_tags(
        assessment.get("suggested_tags"), field="project_tag_assessment.suggested_tags"
    )
    evidence = assessment.get("evidence") or []
    if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
        raise WorkflowValidationError("project_tag_assessment.evidence must be a list of objects.")
    assessment["evidence"] = evidence[:100]
    return assessment


def normalize_review(payload: dict[str, Any]) -> dict[str, Any]:
    review = deepcopy(payload)
    results = review.get("results")
    if not isinstance(results, list):
        raise WorkflowValidationError("Discovery results must be a list.")
    local_selected: dict[str, bool] = {}
    local_tag_reviews: dict[str, tuple[dict[str, list[str]], str]] = {}
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
            row["base_tags"] = dict(row.get("base_tags") or {}) if isinstance(row.get("base_tags") or {}, dict) else {}
            row["base_tags_verified"] = bool(row.get("base_tags_verified"))
            row["project_tag_assessment"] = _normalize_tag_assessment(
                row.get("project_tag_assessment")
            )
            confirmed_tags = _normalize_project_tags(
                row.get("confirmed_project_tags"), field="confirmed_project_tags"
            )
            review_status = str(row.get("tag_review_status") or "pending").strip()
            if review_status not in TAG_REVIEW_STATUSES:
                review_status = "pending"
            state = (confirmed_tags, review_status)
            previous = local_tag_reviews.get(paper_id)
            if previous is not None and previous != state:
                raise WorkflowValidationError(
                    "Duplicate Discovery hits for one paper must carry the same project Tag review."
                )
            local_tag_reviews[paper_id] = state
        for row in group.get("web_results") or []:
            if not isinstance(row, dict) or not _candidate_id(row, external=True):
                raise WorkflowValidationError("External candidates need a stable source identity.")
            row["selected_for_matrix"] = bool(row.get("selected_for_matrix"))
    for group in results:
        for row in group.get("local_results") or []:
            paper_id = _candidate_id(row, external=False)
            row["selected_for_matrix"] = local_selected[paper_id]
            confirmed_tags, review_status = local_tag_reviews[paper_id]
            row["confirmed_project_tags"] = deepcopy(confirmed_tags)
            row["tag_review_status"] = review_status
    review["selection_mode"] = "explicit"
    return review


def statistics(review: dict[str, Any]) -> dict[str, int]:
    groups = [group for group in review.get("results") or [] if group.get("keep") is not False]
    local_rows = [row for group in groups for row in group.get("local_results") or []]
    external_rows = [row for group in groups for row in group.get("web_results") or []]
    local_ids = {_candidate_id(row, external=False) for row in local_rows}
    selected_ids = {_candidate_id(row, external=False) for row in local_rows if _selected(row)}
    external_ids = {_candidate_id(row, external=True) for row in external_rows}
    reviewed_ids = {
        _candidate_id(row, external=False)
        for row in local_rows
        if str(row.get("tag_review_status") or "pending") == "confirmed"
    }
    selected_reviewed_ids = {
        _candidate_id(row, external=False)
        for row in local_rows
        if _selected(row) and str(row.get("tag_review_status") or "pending") == "confirmed"
    }
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
        "tag_reviewed_candidate_count": len(reviewed_ids),
        "tag_reviewed_selected_count": len(selected_reviewed_ids),
    }


class DiscoveryService:
    def __init__(self, repository: WorkflowRepository, artifacts: ArtifactService):
        self.repository = repository
        self.artifacts = artifacts
        self._write_lock = threading.RLock()

    def _owned_project(self, principal: Principal, project_id: str):
        principal.require(Permission.PROJECT_READ)
        project = self.repository.get_owned_project(principal.user_id, project_id)
        if project is None:
            raise WorkflowNotFound("Project not found.")
        return project

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
        (staging / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
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
                row["confirmed_project_tags"] = deepcopy(
                    source.get("confirmed_project_tags") or {}
                )
                row["tag_review_status"] = str(
                    source.get("tag_review_status") or "pending"
                )
            for row in group.get("web_results") or []:
                identity = _candidate_id(row, external=True)
                if identity not in proposed_external:
                    raise WorkflowValidationError("External candidates cannot be added or removed while saving review.")
                row["selected_for_matrix"] = bool(proposed_external[identity].get("selected_for_matrix"))
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
    ) -> dict[str, Any]:
        current, _artifact = self._read_current(principal, project_id, DISCOVERY_LOGICAL_NAME)
        merged = self._merge_mutable(current, results)
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
            and int((existing_matrix or {}).get("tag_policy_version") or 0)
            == MATRIX_TAG_POLICY_VERSION
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
            return _field_value(value) if value is not None else default

        def reusable_base_tags(row: LibraryPaper) -> dict[str, str]:
            metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            return verified_structured_tags(metadata)

        project_tag_reviews: dict[str, dict[str, Any]] = {}
        for group in current.get("results") or []:
            if group.get("keep") is False:
                continue
            for row in group.get("local_results") or []:
                paper_id = _candidate_id(row, external=False)
                if paper_id not in selected_ids:
                    continue
                status = str(row.get("tag_review_status") or "pending")
                assessment = row.get("project_tag_assessment") or {}
                suggested_tags = _normalize_project_tags(
                    assessment.get("suggested_tags") if isinstance(assessment, dict) else {},
                    field="project_tag_assessment.suggested_tags",
                )
                confirmed_tags = deepcopy(row.get("confirmed_project_tags") or {})
                if status == "confirmed":
                    applied_tags = confirmed_tags
                    applied_status = "confirmed"
                elif suggested_tags:
                    applied_tags = suggested_tags
                    applied_status = "automatic"
                else:
                    applied_tags = {}
                    applied_status = "verified_base_only" if reusable_base_tags(
                        catalog_by_id[paper_id]
                    ) else "untagged"
                project_tag_reviews[paper_id] = {
                    "status": applied_status,
                    "tags": applied_tags,
                    "topic_fingerprint": str(
                        (assessment.get("topic_fingerprint") or "")
                        if isinstance(assessment, dict)
                        else ""
                    ),
                }

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
                "journal": metadata_value(catalog_by_id[paper_id], "journal", "") or "",
                "doi": metadata_value(catalog_by_id[paper_id], "doi", "") or "",
                "base_tags": reusable_base_tags(catalog_by_id[paper_id]),
                "base_tags_verified": structured_tags_are_verified(
                    catalog_by_id[paper_id].metadata_json
                    if isinstance(catalog_by_id[paper_id].metadata_json, dict)
                    else {}
                ),
                "project_tags": deepcopy(
                    project_tag_reviews.get(paper_id, {}).get("tags") or {}
                ),
                "project_tag_review_status": str(
                    project_tag_reviews.get(paper_id, {}).get("status") or "pending"
                ),
                "project_tag_topic_fingerprint": str(
                    project_tag_reviews.get(paper_id, {}).get("topic_fingerprint") or ""
                ),
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
            rows.append(row)
        matrix = {
            "project_id": project_id,
            "review_topic": str(current.get("topic") or ""),
            "tag_policy_version": MATRIX_TAG_POLICY_VERSION,
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
