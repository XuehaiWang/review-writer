"""Blueprint version actions mixed into PlanningService."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from review_writer_core.claim_contracts import ARGUMENT_CONTRACT
from review_writer_core.academic_contracts import blueprint_taxonomy_diagnostics
from review_writer_core.scientific_facts import fact_is_usable, fact_support_spans, fact_needs_verification
from review_writer_core.classification_axes import classification_contract_from_document
from review_writer_api.database import utc_now
from review_writer_api.errors import (
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowValidationError,
)
from review_writer_api.security import Permission, Principal
from review_writer_core.stages.planning.blueprint import _blueprint_restructure_record
from review_writer_core.stages.planning.matrix import _json_bytes
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary
from review_writer_core.stages.planning.fact_revision import refresh_row_facts
from review_writer_core.workflow.artifacts import (
    BLUEPRINT as BLUEPRINT_LOGICAL_NAME,
    MATRIX as MATRIX_LOGICAL_NAME,
    PLANNING_OUTLINE as OUTLINE_LOGICAL_NAME,
)


class PlanningBlueprintActionsMixin:
    def _validate_candidate_matrix(self, principal, original, candidate):
        original_rows = {row["paper_id"]: row for row in original.get("rows") or []}
        rows = candidate.get("rows") or []
        if len(rows) != len(original_rows) or {row["paper_id"] for row in rows} != set(original_rows):
            raise WorkflowValidationError("Argument planning must retain the selected Matrix papers.")
        classification_changed = (
            classification_contract_from_document(original).get("fingerprint")
            != classification_contract_from_document(candidate).get("fingerprint")
        )
        issues = []
        for row in rows:
            before = {f["fact_id"]: f for f in original_rows[row["paper_id"]].get("scientific_facts") or []}
            facts = row.get("scientific_facts") or []
            # Extraction returns a fresh subset, not a deletion instruction.
            # Reconcile here so both planning and publication use the same rule.
            missing = set(before) - {f["fact_id"] for f in facts}
            candidate_enrichment = row.get("fact_enrichment") or {}
            previous_enrichment = original_rows[row["paper_id"]].get("fact_enrichment") or {}
            refreshed_this_candidate = bool(
                candidate_enrichment.get("classification_refreshed_from_facts")
                and candidate_enrichment.get("source_fingerprint")
                != previous_enrichment.get("source_fingerprint")
            )
            if classification_changed or refreshed_this_candidate:
                # A source fact omitted by an incremental supplement is kept,
                # but an old route fact cannot survive a new outline contract.
                missing = {fid for fid in missing
                           if str(before[fid].get("field_id") or "") != "topic_partition"}
            facts.extend(deepcopy(fact) for fid, fact in before.items() if fid in missing)
            row["scientific_facts"] = facts
            for fid in before:
                if fid in missing:
                    issues.append({"paper_id": row["paper_id"], "fact_id": fid,
                                   "reasons": ["omitted_by_supplement"],
                                   "action": "retained_previous",
                                   "value": str(before[fid].get("value") or "")})
            changed = [fact for fact in facts if fact_is_usable(fact)
                       and (fact["fact_id"] in missing or before.get(fact["fact_id"]) != fact)]
            if not changed:
                if missing:
                    refresh_row_facts(row)
                continue
            lineages = self.fact_source_lineages(row)
            registry = self.fact_source_candidates(principal, row, [*changed, *before.values()], lineages)
            for fact in changed:
                reasons = []
                if fact_needs_verification(fact):
                    reasons.append("verification_outdated")
                fact_support_spans(fact, registry, issues=reasons)
                if not reasons:
                    continue
                old = before.get(fact["fact_id"])
                restored = bool(old and fact_is_usable(old) and not fact_needs_verification(old)
                                and fact_support_spans(old, registry))
                issue = {"paper_id": row["paper_id"], "fact_id": fact["fact_id"],
                         "reasons": reasons, "action": "retained_previous" if restored else "withheld",
                         "value": str(fact.get("value") or "")}
                if restored:
                    fact.clear()
                    fact.update(deepcopy(old))
                else:
                    # Retain the record for inspection, but never count it as
                    # evidence or send it downstream as a supported proposition.
                    fact["verification"] = {**(fact.get("verification") or {}),
                                            "status": "unavailable", "source_validation_issues": reasons}
                    fact["review_status"] = "pending_verification"
                issues.append(issue)
            if any(issue["paper_id"] == row["paper_id"] for issue in issues):
                refresh_row_facts(row)
        if issues:
            refresh_matrix_fact_summary(candidate)
        return issues

    def reconcile_blueprint_facts(self, principal, project_id, prepared):
        """Check optional fact supplements before planning, without extra model calls."""
        original, _ = self._matrix(principal, project_id)
        candidate = prepared.get("matrix_snapshot")
        if candidate is None:
            return
        issues = self._validate_candidate_matrix(principal, original, candidate)
        if not issues:
            return
        prepared["section_blueprint"]["fact_source_issues"] = issues
        # Preserve retrieval-only context while sharing the corrected fact state.
        by_id = {row["paper_id"]: row for row in candidate.get("rows") or []}
        for row in (prepared.get("planning_matrix_snapshot") or {}).get("rows") or []:
            source = by_id.get(row["paper_id"])
            if source:
                for key in ("scientific_facts", "fact_enrichment", "comparison_evidence"):
                    if key in source:
                        row[key] = deepcopy(source[key])

    def blueprint_job_payload(self, principal, project_id, *, revision):
        matrix_job = self.repository.get_current_job(principal.user_id, scope="project",
            project_id=project_id, job_type="matrix.enrich", operation_key="matrix-enrichment")
        prepared = self.prepare_blueprint(principal, project_id, revision=revision)
        prepared["integrated_fact_enrichment"] = {
            "enabled": True,
            "mode": "reuse_current_facts_and_fill_gaps",
            "failure_policy": "continue_with_registered_source_passages",
            "source_matrix_artifact_id": prepared["section_blueprint"][
                "source_matrix_artifact_id"
            ],
        }
        previous = self.repository.list_project_jobs(principal.user_id, project_id, job_type="planning.blueprint", limit=1)
        if previous:
            # The planner checks content fingerprints before reusing each group.
            prepared["blueprint_checkpoint"] = (previous[0].result or {}).get("blueprint_checkpoint") or {}
        if matrix_job and matrix_job.payload.get("operation") != "fact_revision":
            checkpoint = (matrix_job.result or {}).get("matrix_enrichment_checkpoint") or (matrix_job.result or {}).get("section_checkpoint")
            if isinstance(checkpoint, dict):
                # The fact worker validates every per-paper fingerprint before
                # reuse, including partial results of a failed extraction.
                prepared["matrix_enrichment_checkpoint"] = deepcopy(checkpoint)
        if matrix_job and matrix_job.status in {"queued", "running", "cancel_requested"}:
            if matrix_job.payload.get("source_matrix_artifact_id") != prepared["section_blueprint"]["source_matrix_artifact_id"]:
                completed = self.repository.get_job(principal.user_id, matrix_job.id)
                if (completed and completed.status == "succeeded" and
                        completed.result.get("matrix_artifact_id") == prepared["section_blueprint"]["source_matrix_artifact_id"]):
                    return prepared
                raise WorkflowConflict("Earlier Matrix analysis uses a different selection. Wait for it to finish.")
            prepared["await_matrix_job_id"] = matrix_job.id
            state = self.repository.get_stage_state(principal.user_id, project_id, "blueprint")
            prepared["blueprint_state_exists"] = state is not None
        return prepared

    def resume_blueprint_after_matrix(self, principal, project_id, prepared):
        """Accept only the expected extraction publication, never unrelated edits."""
        dependency = prepared.get("await_matrix_job_id")
        if not dependency:
            return prepared
        job = self.repository.get_job(principal.user_id, dependency)
        if job and job.status in {"failed", "cancelled", "interrupted"}:
            # A retry of this planner retains its original input contract. Follow
            # only the extraction's explicit retry lineage, never another run.
            latest = self.repository.get_current_job(principal.user_id, scope="project",
                project_id=project_id, job_type="matrix.enrich", operation_key="matrix-enrichment")
            cursor, seen = latest, set()
            while cursor and cursor.id not in seen:
                seen.add(cursor.id)
                if cursor.id == dependency:
                    job = latest
                    break
                if cursor.project_id != project_id or cursor.job_type != "matrix.enrich" or not cursor.retry_of_job_id:
                    break
                cursor = self.repository.get_job(principal.user_id, cursor.retry_of_job_id)
        if (job is None or job.project_id != project_id or job.job_type != "matrix.enrich"
                or job.status != "succeeded"):
            raise WorkflowConflict("Scientific fact analysis did not complete. Retry it before generating the chapter plan.")
        result = job.result or {}
        expected = result.get("matrix_artifact_id")
        original = prepared["section_blueprint"]["source_matrix_artifact_id"]
        if not expected or job.payload.get("source_matrix_artifact_id") != original:
            raise WorkflowConflict("Scientific fact analysis has no compatible published Matrix.")
        rebased = deepcopy(prepared)
        rebased.pop("await_matrix_job_id", None)
        if expected != original:
            rebased["section_blueprint"]["source_matrix_artifact_id"] = expected
            rebased["matrix_revision"] = result["matrix_revision"]
            invalidated = bool(result.get("blueprint_invalidated",
                bool(result.get("changed_paper_ids") or result.get("classification_contract_changed"))))
            if invalidated:
                rebased["base_blueprint_artifact_id"] = None
                rebased["blueprint_revision"] += int(prepared.get("blueprint_state_exists", False))
        self.validate_prepared_blueprint(principal, project_id, rebased)
        refreshed = self.blueprint_job_payload(principal, project_id, revision=rebased["blueprint_revision"])
        # Pinning and request metadata remain the original job's responsibility.
        refreshed.pop("await_matrix_job_id", None)
        return refreshed

    def validate_prepared_blueprint(self, principal, project_id, prepared):
        for name, expected in (prepared.get("draft_repair_input_artifacts") or {}).items():
            actual = self.repository.get_current_artifact(principal.user_id, project_id, name)
            if actual is None or actual.id != expected:
                raise WorkflowConflict("Draft repair inputs changed while generating the Blueprint candidate.")
        _matrix, matrix_artifact = self._matrix(principal, project_id)
        _outline, outline_artifact = self._read_json(principal, project_id, OUTLINE_LOGICAL_NAME)
        current = self.repository.get_current_artifact(principal.user_id, project_id, BLUEPRINT_LOGICAL_NAME)
        state = self.repository.get_stage_state(principal.user_id, project_id, "blueprint")
        revision = state.revision if state else 0
        blueprint = prepared["section_blueprint"]
        matrix_state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if (revision != prepared["blueprint_revision"]
            or matrix_state is None or matrix_state.revision != prepared["matrix_revision"]
            or (current.id if current else None) != prepared.get("base_blueprint_artifact_id")
            or blueprint.get("source_matrix_artifact_id") != matrix_artifact.id
            or blueprint.get("source_outline_artifact_id") != outline_artifact.id):
            raise WorkflowConflict("Planning inputs changed. Regenerate this Blueprint candidate.")
        return matrix_artifact, outline_artifact, current, revision

    def _owned_blueprint_input(self, principal, project_id, artifact_id, logical_name):
        resolved = self.artifacts.resolve_owned_artifact(principal.user_id, artifact_id)
        if resolved.artifact.project_id != project_id or resolved.artifact.logical_name != logical_name:
            raise WorkflowNotFound("Blueprint input artifact not found.")
        return json.loads(resolved.path.read_text(encoding="utf-8")), resolved.artifact

    def _candidate_conflicts(self, principal, project_id, blueprint, current, revision):
        expected = {BLUEPRINT_LOGICAL_NAME: blueprint.get("candidate_base_artifact_id"),
                    MATRIX_LOGICAL_NAME: blueprint.get("candidate_base_matrix_artifact_id", blueprint.get("source_matrix_artifact_id")),
                    OUTLINE_LOGICAL_NAME: blueprint.get("candidate_base_outline_artifact_id", blueprint.get("source_outline_artifact_id"))}
        expected.update(blueprint.get("draft_repair_input_artifacts") or {})
        conflicts = ["blueprint_revision"] if blueprint.get("candidate_base_revision") != revision else []
        for name, expected_id in expected.items():
            actual = current if name == BLUEPRINT_LOGICAL_NAME else self.repository.get_current_artifact(principal.user_id, project_id, name)
            if (actual.id if actual else None) != expected_id:
                conflicts.append(name)
        matrix_state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if blueprint.get("candidate_base_matrix_revision") is not None and (matrix_state is None or matrix_state.revision != blueprint["candidate_base_matrix_revision"]):
            conflicts.append("matrix_revision")
        return conflicts

    def publish_blueprint_candidate(self, principal, project_id, prepared):
        """Store one immutable input chain; current pointers move only at confirmation."""
        principal.require(Permission.PROJECT_WRITE)
        blueprint = deepcopy(prepared["section_blueprint"])
        if prepared.get("draft_repair_context"):
            blueprint["draft_repair_context"] = deepcopy(prepared["draft_repair_context"])
            blueprint["draft_repair_input_artifacts"] = dict(prepared["draft_repair_input_artifacts"])
        with self._write_lock:
            matrix_artifact, outline_artifact, current, revision = self.validate_prepared_blueprint(principal, project_id, prepared)
            original_matrix, _ = self._owned_blueprint_input(principal, project_id, matrix_artifact.id, MATRIX_LOGICAL_NAME)
            original_outline, _ = self._owned_blueprint_input(principal, project_id, outline_artifact.id, OUTLINE_LOGICAL_NAME)
            matrix = deepcopy(prepared.get("matrix_snapshot") or original_matrix)
            outline = deepcopy(prepared.get("outline_snapshot") or original_outline)
            issues = self._validate_candidate_matrix(principal, original_matrix, matrix)
            if issues:
                previous = blueprint.get("fact_source_issues") or []
                blueprint["fact_source_issues"] = list({(i["paper_id"], i["fact_id"]): i
                                                       for i in [*previous, *issues]}.values())
                # The plan is provisional; it must not advertise withheld facts
                # as verified context if source state changed during generation.
                usable = {r["paper_id"]: {f["fact_id"] for f in r.get("scientific_facts") or [] if fact_is_usable(f)}
                          for r in matrix.get("rows") or []}
                for section in blueprint.get("sections") or []:
                    context = section.get("planning_fact_context")
                    if isinstance(context, dict):
                        ids = set(context.get("fact_ids") or [])
                        papers = [pid for pid in context.get("paper_ids") or [] if ids & usable.get(pid, set())]
                        kept = [fid for fid in context.get("fact_ids") or [] if any(fid in usable[pid] for pid in papers)]
                        context.update(fact_ids=kept, paper_ids=papers, fact_count=len(kept), paper_count=len(papers),
                                       mode="verified_fact_guided" if kept else "source_passage_only")
                        if not kept:
                            section["evidence_readiness"] = {"status": "not_reviewed",
                                "reason": "Original passages will be checked during drafting."}
            blueprint.update(candidate_base_revision=revision, candidate_base_artifact_id=current.id if current else None,
                candidate_base_matrix_artifact_id=matrix_artifact.id, candidate_base_outline_artifact_id=outline_artifact.id,
                candidate_base_matrix_revision=prepared["matrix_revision"])
            state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
            run = self.repository.create_stage_run(principal.user_id, project_id, "blueprint", status="succeeded",
                input_snapshot={"operation": "blueprint_candidate", "matrix_artifact_id": matrix_artifact.id,
                    "outline_artifact_id": outline_artifact.id, "matrix_revision": prepared["matrix_revision"],
                    "matrix_status": state.status, "blueprint_revision": revision})
            if matrix != original_matrix:
                published, _ = self._publish_files(principal, project_id, stage_id="blueprint", run=run,
                    files={MATRIX_LOGICAL_NAME: (_json_bytes(matrix), "json")})
                matrix_artifact = published[MATRIX_LOGICAL_NAME]
            outline["source_matrix_artifact_id"] = matrix_artifact.id
            if blueprint.get("argument_contract"):
                outline["argument_planning_source_md"] = original_outline.get("argument_planning_source_md") or original_outline["outline_md"]
                outline["outline_md"] = blueprint["resolved_outline_md"]
                outline["outline_complete"] = True
            if outline != original_outline:
                published, _ = self._publish_files(principal, project_id, stage_id="blueprint", run=run,
                    files={OUTLINE_LOGICAL_NAME: (_json_bytes(outline), "json")})
                outline_artifact = published[OUTLINE_LOGICAL_NAME]
            blueprint.update(source_matrix_artifact_id=matrix_artifact.id, source_outline_artifact_id=outline_artifact.id)
            published, _ = self._publish_files(principal, project_id, stage_id="blueprint", run=run,
                files={BLUEPRINT_LOGICAL_NAME: (_json_bytes(blueprint), "json")},
                metadata={"planning_candidate_inputs": {
                    MATRIX_LOGICAL_NAME: matrix_artifact.id, OUTLINE_LOGICAL_NAME: outline_artifact.id}})
            artifact = published[BLUEPRINT_LOGICAL_NAME]
        return {"project_id": project_id, "section_blueprint": blueprint, "blueprint_artifact_id": artifact.id,
                "blueprint_revision": revision, "matrix_revision": prepared["matrix_revision"],
                "auto_applied": False, "candidate_pending": True, "restructure_record": blueprint.get("restructure_record") or {}}

    def latest_blueprint_candidate(self, principal, project_id, current_artifact, revision):
        versions = self.repository.list_artifacts(principal.user_id, project_id, BLUEPRINT_LOGICAL_NAME)
        if not versions or (current_artifact and versions[0].id == current_artifact.id):
            return None, None
        candidate, artifact = self._owned_blueprint_input(principal, project_id, versions[0].id, BLUEPRINT_LOGICAL_NAME)
        if self._candidate_conflicts(principal, project_id, candidate, current_artifact, revision):
            return None, None
        return candidate, artifact

    def confirm_blueprint(self, principal: Principal, project_id: str, *, revision: int, artifact_id: str | None = None) -> dict[str, Any]:
        principal.require(Permission.PROJECT_WRITE)
        current = self.repository.get_current_artifact(principal.user_id, project_id, BLUEPRINT_LOGICAL_NAME)
        state = self.repository.get_stage_state(principal.user_id, project_id, "blueprint")
        current_revision = state.revision if state else 0
        if not artifact_id:
            versions = self.repository.list_artifacts(principal.user_id, project_id, BLUEPRINT_LOGICAL_NAME)
            eligible = [item for item in versions if (not current or item.id != current.id)
                and not self._candidate_conflicts(principal, project_id,
                    self._owned_blueprint_input(principal, project_id, item.id, BLUEPRINT_LOGICAL_NAME)[0], current, current_revision)]
            if len(eligible) > 1:
                raise WorkflowConflict("Choose the Blueprint candidate shown in your review before confirming.")
            artifact_id = eligible[0].id if eligible else current.id if current else None
        if not artifact_id:
            raise WorkflowNotFound("Generate a Blueprint candidate before confirmation.")
        blueprint, artifact = self._owned_blueprint_input(principal, project_id, artifact_id, BLUEPRINT_LOGICAL_NAME)
        matrix, matrix_artifact = self._owned_blueprint_input(principal, project_id, blueprint["source_matrix_artifact_id"], MATRIX_LOGICAL_NAME)
        outline, outline_artifact = self._owned_blueprint_input(principal, project_id, blueprint["source_outline_artifact_id"], OUTLINE_LOGICAL_NAME)
        if current and current.id == artifact.id and state and state.status == "approved":
            for name, identity in ((MATRIX_LOGICAL_NAME, matrix_artifact.id), (OUTLINE_LOGICAL_NAME, outline_artifact.id)):
                active = self.repository.get_current_artifact(principal.user_id, project_id, name)
                if not active or active.id != identity:
                    raise WorkflowConflict("The approved Blueprint inputs have changed.")
            return {"project_id": project_id, "revision": state.revision, "status": "approved", "next_stage": "sections",
                    "next_path": f"/sections?project={project_id}"}
        conflicts = self._candidate_conflicts(principal, project_id, blueprint, current, revision)
        if revision != current_revision or conflicts:
            raise WorkflowConflict("Planning inputs changed; review a candidate built from the current inputs.", details={"changed_inputs": conflicts})
        if outline.get("source_matrix_artifact_id") != matrix_artifact.id:
            raise WorkflowConflict("The candidate outline and facts belong to different input versions.")
        sources = [{"paper_id": row["paper_id"], "source_lineages": lineages}
                   for row in matrix.get("rows") or [] if (lineages := self.fact_source_lineages(row))]
        if sources and self.library_index is not None and self.library_index.enabled:
            self._validate_fact_sources(principal, sources)
        if blueprint.get("argument_contract") != ARGUMENT_CONTRACT or (blueprint.get("academic_planning") or {}).get("status") != "completed":
            raise WorkflowConflict("Chapter planning is unfinished. Continue generation to reuse the completed work.")
        unused = blueprint.get("unused_papers") or []
        assigned = {pid for s in blueprint.get("sections") or [] for pid in [*(s.get("primary_papers") or []), *(s.get("supporting_papers") or []), *(s.get("context_papers") or [])]}
        selected = {row["paper_id"] for row in matrix.get("rows") or []}
        if (len({item.get("paper_id") for item in unused}) != len(unused)
            or {item.get("paper_id") for item in unused} != selected - assigned or assigned - selected
            or any(item.get("reason_code") not in {"out_of_scope", "insufficient_evidence"} or not str(item.get("reason") or "").strip() for item in unused)):
            raise WorkflowValidationError("Every selected paper needs a reviewed assignment or unused-paper reason.")
        blueprint["taxonomy_diagnostics"] = blueprint_taxonomy_diagnostics(blueprint, selected)
        promotion = {BLUEPRINT_LOGICAL_NAME: artifact.id}
        # Publication may reuse identical immutable inputs from an earlier run.
        # Bind their exact IDs in server-owned metadata instead of equating
        # first creation with participation in this candidate's validated chain.
        registered_inputs = (artifact.metadata or {}).get("planning_candidate_inputs")
        for name, member in ((MATRIX_LOGICAL_NAME, matrix_artifact), (OUTLINE_LOGICAL_NAME, outline_artifact)):
            if registered_inputs is not None and (not isinstance(registered_inputs, dict)
                                                  or registered_inputs.get(name) != member.id):
                raise WorkflowValidationError("Candidate input does not match its registered planning artifact.")
            if member.id != blueprint.get("candidate_base_" + ("matrix" if name == MATRIX_LOGICAL_NAME else "outline") + "_artifact_id"):
                if registered_inputs is None and member.producer_run_id != artifact.producer_run_id:
                    raise WorkflowValidationError("This older candidate has no registered input chain. Republish the existing planning result before confirming.")
                promotion[name] = member.id
        run = self.repository.get_stage_run(principal.user_id, project_id, artifact.producer_run_id)
        matrix_state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if run is None or matrix_state is None:
            raise WorkflowConflict("The candidate planning input state is unavailable.")
        approvals = {"matrix": blueprint["candidate_base_matrix_revision"]} if len(promotion) > 1 or matrix_state.status != "approved" else {}
        state = self.repository.promote_stage_artifacts_atomically(principal.user_id, project_id, "blueprint",
            expected_revision=revision, artifact_ids=promotion, run_id=artifact.producer_run_id, status="approved",
            expected_current_artifacts={BLUEPRINT_LOGICAL_NAME: current.id if current else None,
                MATRIX_LOGICAL_NAME: blueprint["candidate_base_matrix_artifact_id"], OUTLINE_LOGICAL_NAME: blueprint["candidate_base_outline_artifact_id"]},
            expected_stage_states={"matrix": {"revision": blueprint["candidate_base_matrix_revision"], "status": run.input_snapshot["matrix_status"]}},
            approve_stages=approvals, invalidate_stages=("sections", "figure-review", "figures", "draft", "final"))
        return {"project_id": project_id, "revision": state.revision, "status": state.status,
                "next_stage": "sections", "next_path": f"/sections?project={project_id}"}

    def restore_blueprint(self, principal: Principal, project_id: str, *, revision: int, artifact_id: str) -> dict[str, Any]:
        """Restore an audited structure as a new candidate using still-current facts."""
        from review_writer_core.scientific_facts import review_fingerprint
        principal.require(Permission.PROJECT_WRITE)
        current, current_artifact = self._read_json(principal, project_id, BLUEPRINT_LOGICAL_NAME, required=False)
        if current_artifact and current_artifact.id == artifact_id:
            raise WorkflowValidationError("The selected Blueprint version is already current.")
        restored, source_artifact = self._owned_blueprint_input(principal, project_id, artifact_id, BLUEPRINT_LOGICAL_NAME)
        old_matrix, _ = self._owned_blueprint_input(principal, project_id, restored["source_matrix_artifact_id"], MATRIX_LOGICAL_NAME)
        old_outline, _ = self._owned_blueprint_input(principal, project_id, restored["source_outline_artifact_id"], OUTLINE_LOGICAL_NAME)
        matrix, matrix_artifact = self._matrix(principal, project_id)
        outline, outline_artifact = self._read_json(principal, project_id, OUTLINE_LOGICAL_NAME)
        if restored.get("argument_contract") != ARGUMENT_CONTRACT:
            raise WorkflowConflict("This historical structure needs current argument planning before it can be restored.")
        if ({r["paper_id"] for r in old_matrix["rows"]} != {r["paper_id"] for r in matrix["rows"]}
            or restored.get("scope_contract") != outline.get("scope_contract")):
            raise WorkflowConflict("The selected version uses a different paper selection or Scope. Generate a current candidate instead.")
        before = {f["fact_id"]: f for r in old_matrix["rows"] for f in r.get("scientific_facts") or []}
        now = {f["fact_id"]: f for r in matrix["rows"] for f in r.get("scientific_facts") or []}
        used = {fid for section in restored.get("sections") or [] for claim in section.get("scientific_claims") or [] for fid in claim.get("fact_ids") or []}
        if any(fid not in before or fid not in now or not fact_is_usable(now[fid])
               or review_fingerprint(before[fid]) != review_fingerprint(now[fid])
               or before[fid].get("assertion_ceiling") != now[fid].get("assertion_ceiling") for fid in used):
            raise WorkflowConflict("The selected version's argument premises changed. Replan the affected arguments before restoring.")
        restored["source_matrix_artifact_id"], restored["source_outline_artifact_id"] = matrix_artifact.id, outline_artifact.id
        restored["restructure_record"] = {
            **_blueprint_restructure_record(current or {}, restored.get("sections") or [],
                previous_artifact_id=current_artifact.id if current_artifact else None,
                trigger_reasons=["user_restored_previous_blueprint"]),
            "application_mode": "restored_candidate_requires_confirmation", "restored_from_artifact_id": source_artifact.id,
            "rollback_supported": True, "created_at": utc_now().isoformat()}
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        result = self.publish_blueprint_candidate(principal, project_id, {
            "section_blueprint": restored, "matrix_snapshot": matrix, "outline_snapshot": old_outline,
            "blueprint_revision": revision, "base_blueprint_artifact_id": current_artifact.id if current_artifact else None,
            "matrix_revision": state.revision})
        return {**result, "status": "review", "restored_from_artifact_id": source_artifact.id}
