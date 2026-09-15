"""Matrix-stage Planning actions mixed into PlanningService."""

from __future__ import annotations

import re
import hashlib
import json
from copy import deepcopy
from typing import Any

from review_writer_api.database import utc_now
from review_writer_api.model_catalog import resolve_model_tier
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError, WorkflowNotFound
from review_writer_core.stages.planning.fact_revision import pending_revisions, revise_fact, refresh_row_facts, review_fingerprint
from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION, fact_support_spans
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary
from review_writer_api.security import Permission, Principal
from review_writer_core.stages.planning.matrix import _json_bytes
from review_writer_core.workflow.artifacts import MATRIX as MATRIX_LOGICAL_NAME


class PlanningMatrixActionsMixin:
    @staticmethod
    def fact_source_lineages(row):
        """Read the source map, retaining the original single-article contract."""
        enrichment = row.get("fact_enrichment") or {}
        lineages = enrichment.get("source_lineages")
        if lineages:
            return dict(lineages)
        lineage = enrichment.get("source_lineage_hash")
        return {str(row["paper_id"]): str(lineage)} if lineage else {}

    def fact_source_candidates(self, principal, row, facts, lineages):
        """Resolve the original passages for cached facts and user revisions alike."""
        paper_id = str(row["paper_id"])
        candidates = {}
        for sid, lineage in lineages.items():
            refs = [ref for fact in facts for ref in fact.get("evidence_refs") or []
                    if str(ref.get("source_file_id") or ref.get("paper_id") or paper_id) == sid]
            if not refs:
                continue
            if self.library_index is not None and self.library_index.enabled:
                for hit in self.library_index.resolve_source_chunks(principal, sid,
                        [str(ref.get("chunk_id")) for ref in refs], expected_lineage=lineage):
                    for ref in refs:
                        if ref.get("chunk_id") == hit["chunk_id"] and ref.get("source_lineage_hash") == hit["source_lineage_hash"]:
                            key = str(ref["evidence_key"])
                            candidates[key] = {**hit, "evidence_key": key, "paper_id": paper_id,
                                "source_file_id": sid, "source_type": "article" if sid == paper_id else "supporting_information",
                                "question_ids": list(dict.fromkeys(f.get("field_id") for f in facts
                                    if any(r.get("evidence_key") == key for r in f.get("evidence_refs") or [])))}
        for fact in facts:
            for ref in fact.get("evidence_refs") or []:
                if ref.get("chunk_id") == "abstract" and str(ref.get("source_file_id") or paper_id) == paper_id:
                    quote = ref.get("support_excerpt") or fact.get("support_excerpt") or ""
                    if quote and quote in str(row.get("abstract") or ""):
                        candidates[str(ref["evidence_key"])] = {**ref, "paper_id": paper_id,
                            "content": row["abstract"], "content_type": "abstract", "question_ids": [fact.get("field_id")]}
        return candidates

    def _publish_matrix_update(self, principal, project_id, document, *, source_artifact_id, revision, input_snapshot):
        """One atomic publication policy for edits, their review, and explicit limited mode."""
        with self._write_lock:
            published, run = self._publish_files(principal, project_id, stage_id="matrix",
                files={MATRIX_LOGICAL_NAME: (_json_bytes(document), "json")}, input_snapshot=input_snapshot)
            state = self.repository.promote_stage_artifacts_atomically(
                principal.user_id, project_id, "matrix",
                artifact_ids={MATRIX_LOGICAL_NAME: published[MATRIX_LOGICAL_NAME].id}, run_id=run.id,
                expected_revision=revision, expected_current_artifacts={MATRIX_LOGICAL_NAME: source_artifact_id},
                status="review", invalidate_stages=("blueprint", "sections", "figure-review", "figures", "draft", "final"))
        return published, state

    def validate_matrix_enrichment_inputs(self, principal, project_id, payload):
        principal.require(Permission.PROJECT_WRITE)
        matrix, artifact = self._matrix(principal, project_id)
        if str(artifact.id) != str(payload.get("source_matrix_artifact_id")):
            raise WorkflowConflict("Matrix changed before scientific fact processing. Prepare the current Matrix again.")
        if payload.get("papers") and self.library_index is not None and self.library_index.enabled:
            self._validate_fact_sources(principal, payload["papers"])
        return matrix, artifact

    def fact_revision_payload(self, principal, project_id, *, source_matrix_artifact_id=None):
        project = self._owned_project(principal, project_id)
        matrix, artifact = self._matrix(principal, project_id)
        self.validate_matrix_enrichment_inputs(principal, project_id, {
            "source_matrix_artifact_id": source_matrix_artifact_id or artifact.id})
        targets = [row for row in matrix.get("rows") or [] if pending_revisions(row)]
        if not targets:
            return {"operation": "fact_revision", "papers": [], "pending_paper_count": 0,
                    "source_matrix_artifact_id": artifact.id}
        if self.library_index is None or not self.library_index.enabled:
            raise WorkflowConflict("Fact revisions were saved as pending. Restore the local source index, then continue fact verification.")
        ids = [str(row["paper_id"]) for row in targets]
        parents = self._supporting_source_parents(self._catalog(principal, ids), ids)
        summaries = self.library_index.summaries(principal, [*ids, *parents])
        papers = []
        for row in targets:
            paper_id = str(row["paper_id"])
            facts = deepcopy(pending_revisions(row))
            source_ids = [paper_id, *[key for key, parent in parents.items() if parent == paper_id]]
            lineages = {sid: str((summaries.get(sid) or {}).get("source_lineage_hash") or "") for sid in source_ids}
            candidates = self.fact_source_candidates(principal, row, facts, lineages)
            fingerprint = hashlib.sha256(json.dumps({"lineages": lineages,
                "source_context": [candidates[key] for key in sorted(candidates)],
                "targets": {fact["fact_id"]: review_fingerprint(fact) for fact in facts}}, sort_keys=True).encode()).hexdigest()
            papers.append({"paper_id": paper_id, "title": row.get("title"), "revision_facts": facts,
                "source_lineages": lineages, "index_summary": summaries.get(paper_id) or {},
                "evidence_candidates": list(candidates.values()), "source_fingerprint": fingerprint,
                "required_fact_roles": (row.get("fact_enrichment") or {}).get("required_fact_roles") or []})
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        return {"schema_version": 2, "operation": "fact_revision", "project_id": project_id,
            "actual_model_id": resolve_model_tier(project.model_tier, self.repository.session_factory).model,
            "source_matrix_artifact_id": artifact.id, "expected_matrix_revision": state.revision,
            "papers": papers, "pending_paper_count": len(papers), "fulltext_candidate_paper_count": len(papers)}

    def publish_fact_revisions(self, principal, project_id, payload, built):
        matrix, artifact = self.validate_matrix_enrichment_inputs(principal, project_id, payload)
        outputs = {str(row.get("paper_id")): row for row in built.get("papers") or []}
        inputs = {str(row["paper_id"]): row for row in payload.get("papers") or []}
        if set(outputs) != set(inputs) or len(outputs) != len(built.get("papers") or []):
            raise WorkflowValidationError("Fact verification changed the target paper set.")
        updated = deepcopy(matrix)
        for row in updated.get("rows") or []:
            paper = inputs.get(str(row["paper_id"]))
            if paper is None:
                continue
            targets = {fact["fact_id"]: fact for fact in paper["revision_facts"]}
            results = outputs[str(row["paper_id"])].get("facts") or []
            returned = {fact["fact_id"]: fact for fact in results}
            if set(targets) != set(returned) or len(returned) != len(results):
                raise WorkflowValidationError("Fact verification changed the target fact set.")
            registry = {item["evidence_key"]: item for item in paper["evidence_candidates"]}
            merged = []
            for current in row.get("scientific_facts") or []:
                target = targets.get(current["fact_id"])
                if target is None:
                    merged.append(current)
                    continue
                fact = returned[current["fact_id"]]
                fingerprint = review_fingerprint(target)
                if review_fingerprint(current) != fingerprint or review_fingerprint(fact) != fingerprint:
                    raise WorkflowConflict("The fact changed during verification; the old verdict cannot overwrite it.")
                verdict = fact.get("verification") or {}
                if verdict.get("status") == "supported":
                    if (verdict.get("contract") != FACT_VALIDATION_VERSION
                            or verdict.get("input_fingerprint") != fingerprint
                            or not fact_support_spans(fact, registry)):
                        raise WorkflowValidationError("A revised fact has no current source-bound verdict.")
                # Only review outputs can change. Do not accept a rewritten proposition here.
                merged.append({**current, **{key: deepcopy(fact[key]) for key in
                    ("verification", "support_level", "assertion_ceiling") if key in fact},
                    "review_status": "auto_resolved" if verdict.get("status") == "supported" else "needs_review"})
            row["scientific_facts"] = merged
            refresh_row_facts(row)
            row["fact_enrichment"]["error"] = outputs[str(row["paper_id"])].get("error") or ""
        refresh_matrix_fact_summary(updated)
        published, state = self._publish_matrix_update(principal, project_id, updated,
            source_artifact_id=artifact.id, revision=payload["expected_matrix_revision"],
            input_snapshot={"operation": "fact_revision", "source_matrix_artifact_id": artifact.id})
        return {"project_id": project_id, "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
                "matrix_revision": state.revision, "fact_enrichment_summary": updated["fact_enrichment_summary"],
                "blueprint_invalidated": True,
                "matrix_enrichment_checkpoint": built.get("matrix_enrichment_checkpoint") or {}}

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
        matrix, matrix_artifact = self._matrix(principal, project_id)
        updated = deepcopy(matrix)
        facts_changed = False
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
            if len(scientific_facts) != len(existing_facts):
                raise WorkflowValidationError("Matrix fact edits must not duplicate fact IDs.")
            try:
                row["scientific_facts"] = [
                    revise_fact(existing_facts[str(item["fact_id"])], item) for item in scientific_facts
                ]
            except ValueError as exc:
                raise WorkflowValidationError(str(exc)) from exc
            if row["scientific_facts"] != list(existing_facts.values()):
                facts_changed = True
                refresh_row_facts(row)
                refresh_matrix_fact_summary(updated)
                for name in ("limited_mode_confirmed", "limited_mode_confirmed_at", "limited_mode_reason"):
                    updated["fact_enrichment_summary"].pop(name, None)
        if mark_complete and len(re.sub(r"\s+", "", str(row.get("main_content") or ""))) < 300:
            raise WorkflowConflict(
                "Add at least 300 characters of full-paper reading notes before marking this paper complete."
            )
        row["matrix_status"] = (
            "full_reading_complete" if mark_complete else "needs_full_reading"
        )
        updated.pop("outline_compatible_matrix_artifact_ids", None)
        updated["updated_at"] = utc_now().isoformat()
        published, state = self._publish_matrix_update(principal, project_id, updated,
            source_artifact_id=matrix_artifact.id, revision=revision,
            input_snapshot={"operation": "matrix_edit", "paper_id": paper_id})
        return {
            "project_id": project_id,
            "paper_id": paper_id,
            "row": row,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "pending_fact_revisions": [fact["fact_id"] for fact in pending_revisions(row)] if facts_changed else [],
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
        published, state = self._publish_matrix_update(principal, project_id, updated,
            source_artifact_id=matrix_artifact.id, revision=revision,
            input_snapshot={"operation": "confirm-limited-mode", "source_matrix_artifact_id": matrix_artifact.id})
        return {
            "project_id": project_id,
            "matrix_artifact_id": published[MATRIX_LOGICAL_NAME].id,
            "matrix_revision": state.revision,
            "limited_mode_confirmed": True,
        }
