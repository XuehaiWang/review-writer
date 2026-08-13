"""Native Discovery review, explicit selection, and Matrix handoff."""

from __future__ import annotations

import hashlib
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


DISCOVERY_LOGICAL_NAME = "discovery/review.json"
MATRIX_LOGICAL_NAME = "matrix/literature_matrix.json"
MUTABLE_ROLES = {
    "core_candidate",
    "supporting_candidate",
    "background",
    "uncertain",
    "excluded",
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


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


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
        for row in group.get("web_results") or []:
            if not isinstance(row, dict) or not _candidate_id(row, external=True):
                raise WorkflowValidationError("External candidates need a stable source identity.")
            row["selected_for_matrix"] = bool(row.get("selected_for_matrix"))
    for group in results:
        for row in group.get("local_results") or []:
            row["selected_for_matrix"] = local_selected[_candidate_id(row, external=False)]
    review["selection_mode"] = "explicit"
    return review


def statistics(review: dict[str, Any]) -> dict[str, int]:
    groups = [group for group in review.get("results") or [] if group.get("keep") is not False]
    local_rows = [row for group in groups for row in group.get("local_results") or []]
    external_rows = [row for group in groups for row in group.get("web_results") or []]
    local_ids = {_candidate_id(row, external=False) for row in local_rows}
    selected_ids = {_candidate_id(row, external=False) for row in local_rows if _selected(row)}
    external_ids = {_candidate_id(row, external=True) for row in external_rows}
    return {
        "candidate_count": len(local_ids),
        "keyword_hit_count": len(local_rows),
        "selected_count": len(selected_ids),
        "keyword_group_count": len(groups),
        "external_candidate_count": len(external_ids),
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

    def get(self, principal: Principal, project_id: str) -> dict[str, Any]:
        payload, artifact = self._read_current(principal, project_id, DISCOVERY_LOGICAL_NAME)
        state = self.repository.get_stage_state(principal.user_id, project_id, "discovery")
        review = normalize_review(payload)
        return {
            **review,
            "artifact_id": artifact.id,
            "revision": state.revision if state else 0,
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
            )
            next_state = self.repository.compare_and_set_stage(
                principal.user_id,
                project_id,
                "discovery",
                expected_revision,
                status=status_value,
                current_run_id=run.id,
            )
        return {
            **review,
            "artifact_id": artifact.id,
            "revision": next_state.revision,
            "statistics": statistics(review),
            "selected_paper_ids": self.selected_paper_ids(review),
        }

    def replace_from_job(self, principal: Principal, project_id: str, payload: dict[str, Any], built: dict[str, Any]) -> dict[str, Any]:
        self._owned_project(principal, project_id)
        review = normalize_review({**built, "project_id": project_id, "topic": payload["topic"]})
        state = self.repository.get_stage_state(principal.user_id, project_id, "discovery")
        result = self._publish_review(
            principal,
            project_id,
            review,
            expected_revision=state.revision if state else 0,
        )
        self.repository.invalidate_downstream_after_discovery(principal.user_id, project_id)
        with database_session(self.repository.session_factory) as session:
            project = session.scalar(
                select(Project).where(
                    Project.id == uuid.UUID(project_id),
                    Project.user_id == uuid.UUID(principal.user_id),
                )
            )
            if project is not None:
                project.topic = str(payload["topic"])
                project.updated_at = utc_now()
        return {
            "artifact_id": result["artifact_id"],
            "revision": result["revision"],
            "statistics": result["statistics"],
        }

    def save(self, principal: Principal, project_id: str, revision: int, results: list[Any]) -> dict[str, Any]:
        current, _artifact = self._read_current(principal, project_id, DISCOVERY_LOGICAL_NAME)
        merged = self._merge_mutable(current, results)
        return self._publish_review(
            principal, project_id, merged, expected_revision=int(revision)
        )

    def select_one(self, principal: Principal, project_id: str, paper_id: str, selected: bool) -> dict[str, Any]:
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

    def select_top(self, principal: Principal, project_id: str, count: int) -> dict[str, Any]:
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
            for identity, _rank in sorted(ranked.items(), key=lambda item: (-item[1][0], item[1][1]))[: int(count)]
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
            for row in [*(group.get("local_results") or []), *(group.get("web_results") or [])]:
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
        missing = [paper_id for paper_id in selected_ids if paper_id not in catalog_by_id]
        if missing:
            raise DiscoverySelectionNotInLibrary(
                "Selected Discovery papers must belong to your active Library catalog.",
                details={"paper_ids": missing},
            )

        def metadata_value(row: LibraryPaper, key: str, default: Any) -> Any:
            return _field_value(row.metadata_json.get(key)) if row.metadata_json.get(key) is not None else default

        rows = [
            {
                "paper_id": paper_id,
                "title": metadata_value(catalog_by_id[paper_id], "title", catalog_by_id[paper_id].title) or paper_id,
                "authors": metadata_value(catalog_by_id[paper_id], "authors", catalog_by_id[paper_id].authors_json) or [],
                "keywords": metadata_value(catalog_by_id[paper_id], "keywords", catalog_by_id[paper_id].keywords_json) or [],
                "abstract": metadata_value(catalog_by_id[paper_id], "abstract", "abstract unavailable or unreliable") or "abstract unavailable or unreliable",
                "main_content": "",
                "year": metadata_value(catalog_by_id[paper_id], "year", None),
                "journal": metadata_value(catalog_by_id[paper_id], "journal", "") or "",
                "doi": metadata_value(catalog_by_id[paper_id], "doi", "") or "",
                "matrix_status": "needs_full_reading",
            }
            for paper_id in selected_ids
        ]
        fingerprint = hashlib.sha256("\n".join(sorted(selected_ids)).encode("utf-8")).hexdigest()
        matrix = {
            "project_id": project_id,
            "rows": rows,
            "sync": {
                "selection_fingerprint": fingerprint,
                "selected_paper_count": len(selected_ids),
                "synchronized_paper_count": len(rows),
                "synced_at": utc_now().isoformat(),
            },
        }
        with self._write_lock:
            matrix_state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
            matrix_artifact, matrix_run = self._write_json_artifact(
                principal,
                project_id,
                stage_id="matrix",
                logical_name=MATRIX_LOGICAL_NAME,
                payload=matrix,
            )
            next_matrix = self.repository.compare_and_set_stage(
                principal.user_id,
                project_id,
                "matrix",
                matrix_state.revision if matrix_state else 0,
                status="review",
                current_run_id=matrix_run.id,
            )
            discovery_state = self.repository.compare_and_set_stage(
                principal.user_id,
                project_id,
                "discovery",
                current["revision"],
                status="approved",
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
        }
