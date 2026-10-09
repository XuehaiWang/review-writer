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
from review_writer_core.abstract_extraction import recover_opening_abstract
from review_writer_core.evidence_integrity import source_contains_excerpt
from review_writer_core.evidence_queries import COMPARISON_FIELD_IDS, extraction_fact_field_ids
from review_writer_core.review_fact_readiness import (
    REVIEW_READING_STRATEGY,
    fact_processing_state,
    fact_readiness_report,
)
from review_writer_core.scientific_facts import (
    FACT_VALIDATION_VERSION,
    fact_is_usable,
    fact_needs_verification,
    fact_support_spans,
    fact_usage,
    merge_facts,
    normalize_assertion_ceiling,
)
from review_writer_core.stages.planning.fact_revision import pending_revisions, revise_fact, refresh_row_facts, review_fingerprint
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary
from review_writer_core.stages.planning.topic import _canonical_declared_partition
from review_writer_api.security import Permission, Principal
from review_writer_core.stages.planning.matrix import _json_bytes
from review_writer_core.workflow.artifacts import MATRIX as MATRIX_LOGICAL_NAME


class PlanningMatrixActionsMixin:
    def _paper_partition_classification(
        self,
        raw_classification: Any,
        *,
        declared_partitions: list[str],
        partition_candidates: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Classify this paper on the Topic-partition axis from bounded sources."""
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
                    source_contains_excerpt(
                        partition_candidates[
                            str(ref.get("evidence_key") or "")
                        ].get("content"),
                        support_excerpt,
                    )
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
        return partition_classification

    def _paper_evidence_backed_tags(
        self,
        result: dict[str, Any],
        *,
        classification_axes: list[dict[str, Any]],
        partition_candidates: dict[str, dict[str, Any]],
        facts_by_id: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        """Return the classification tags whose facts and sources both hold."""
        axis_contract = {
            str(axis.get("axis_id") or ""): axis
            for axis in classification_axes
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
                            or fact_usage(facts_by_id[fact_id]) != "classification"
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
                            "assertion_ceiling": normalize_assertion_ceiling(
                                raw_tag.get("assertion_ceiling")
                            ),
                        }
                    )
        return evidence_backed_tags, axis_contract

    def _paper_classification_outcomes(
        self,
        result: dict[str, Any],
        *,
        axis_contract: dict[str, Any],
        evidence_backed_tags: dict[str, list[dict[str, Any]]],
        partition_candidates: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return the axes this paper could not be classified on, with reasons."""
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
        return classification_outcomes

    def _paper_routing_recommendation(
        self,
        result: dict[str, Any],
        *,
        allowed_routing_labels: dict[str, str],
        routing_axis_id: str,
        fact_candidates: dict[str, dict[str, Any]],
        facts_by_id: dict[str, dict[str, Any]],
        evidence_backed_tags: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Return this paper's publication-category routing, evidence bound."""
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
                source_contains_excerpt(
                    fact_candidates[
                        str(ref.get("evidence_key") or "")
                    ].get("content"),
                    routing_excerpt,
                )
                for ref in routing_refs
            )
        )
        routing_classified = bool(
            routing_axis_id
            and str(raw_routing.get("axis_id") or "") == routing_axis_id
            and str(raw_routing.get("status") or "").casefold()
            == "classified"
            and routing_label
            and routing_excerpt_valid
            and fact_usage(facts_by_id.get(str(raw_routing.get("verification_fact_id") or ""), {})) == "classification"
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
            "verification_fact_id": raw_routing.get("verification_fact_id") if routing_classified else None,
            "verification": dict((facts_by_id.get(str(raw_routing.get("verification_fact_id") or ""), {}).get("verification") or {})),
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
        return routing_recommendation

    def _apply_paper_fact_state(
        self,
        row: dict[str, Any],
        result: dict[str, Any],
        *,
        source: dict[str, Any],
        facts: list[dict[str, Any]],
        status: str,
        partition_classification: dict[str, Any],
        evidence_backed_tags: dict[str, list[dict[str, Any]]],
        classification_outcomes: list[dict[str, Any]],
        routing_recommendation: dict[str, Any],
        topic_required_roles: list[str],
        review_topic: str,
        contract_version: int,
    ) -> list[dict[str, Any]]:
        """Write this paper's facts, classifications and states back onto its row."""
        row["scientific_facts"] = merge_facts(facts)
        row["topic_partition_classification"] = partition_classification
        row["evidence_backed_tags"] = evidence_backed_tags
        row["classification_outcomes"] = classification_outcomes
        row["routing_recommendation"] = routing_recommendation
        facts = row["scientific_facts"]
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
        readiness = fact_readiness_report(
            facts=facts,
            required_roles=topic_required_roles,
            extraction_status=status,
            failed_fields=result.get("failed_fields") or [],
            baseline=True, topic=review_topic,
        )
        row["fact_enrichment"] = {
            "schema_version": 2,
            "contract_version": contract_version,
            "status": status,
            "extraction_status": status,
            **readiness,
            "review_status": review_status,
            "source_fingerprint": str(source.get("source_fingerprint") or ""),
            "fact_fingerprint": str(source.get("fact_fingerprint") or ""),
            "classification_refreshed_from_facts": bool(source.get("reusable_fact_result")),
            "fact_cache_key": str(source.get("fact_cache_key") or ""),
            "source_lineage_hash": str(
                (source.get("index_summary") or {}).get("source_lineage_hash") or ""
            ),
            "source_lineages": deepcopy(source.get("source_lineages") or {}),
            "fact_count": len(facts),
            "failed_fields": list(result.get("failed_fields") or []),
            "failed_field_details": deepcopy(
                result.get("failed_field_details") or []
            ),
            "normalization_rejections": deepcopy(result.get("normalization_rejections") or []),
            "error": str(result.get("error") or "")[:1000],
            "automatic_resolution": deepcopy(
                result.get("automatic_resolution") or {}
            ),
            "fact_extraction_profile": deepcopy(
                result.get("fact_extraction_profile") or {}
            ),
            "updated_at": utc_now().isoformat(),
        }
        return row["scientific_facts"]

    def _apply_paper_analysis_and_reading(
        self,
        row: dict[str, Any],
        result: dict[str, Any],
        *,
        facts: list[dict[str, Any]],
        fact_candidates: dict[str, dict[str, Any]],
        source: dict[str, Any],
        review_reading: bool,
        review_topic: str,
    ) -> None:
        """Write the paper guide, source navigation and reading state back."""
        analysis = deepcopy(result.get("paper_analysis") or row.get("paper_analysis") or {})
        analysis["fact_ids"] = [f["fact_id"] for f in facts if fact_is_usable(f)
                                and f.get("fact_id") in (analysis.get("fact_ids") or [])]
        # Keep source-addressable navigation even before individual facts
        # pass verification; it never substitutes for a supported claim.
        allowed_keys = set(fact_candidates)
        analysis["evidence_keys"] = [k for k in analysis.get("evidence_keys") or [] if isinstance(k, str) and k in allowed_keys]
        analysis["evidence_refs"] = [
            {name: fact_candidates[key].get(name) for name in (
                "paper_id", "source_file_id", "source_type", "chunk_id",
                "source_lineage_hash", "page_start", "page_end")}
            | {"evidence_key": key}
            for key in analysis["evidence_keys"]
        ]
        if result.get("paper_analysis"):
            analysis["review_topic"] = review_topic
        analysis["usage"] = "navigation_only"
        row["paper_analysis"] = analysis if analysis["fact_ids"] or analysis["evidence_keys"] else {}
        recovered = recover_opening_abstract(result.get('abstract_recovery'),
            list(fact_candidates.values()), existing=row.get('abstract'))
        if recovered:
            row['abstract'] = recovered['value']
            row['abstract_recovery'] = recovered
        profile = row["fact_enrichment"]["fact_extraction_profile"]
        if (review_reading and profile.get("reading_strategy") == REVIEW_READING_STRATEGY
                and (profile.get("reading_source_key") != source.get("reading_source_key")
                     or not facts and not analysis["evidence_keys"])):
            profile["reading_status"] = "failed"
            row["fact_enrichment"]["status"] = "failed"
            row["fact_enrichment"]["extraction_status"] = "failed"
        row["fact_enrichment"].update(fact_processing_state(facts, row["fact_enrichment"]))

    def _validated_paper_facts(
        self,
        row: dict[str, Any],
        result: dict[str, Any],
        *,
        fact_candidates: dict[str, dict[str, Any]],
        paper_id: str,
        review_reading: bool,
        topic_required_roles: list[str],
    ) -> list[dict[str, Any]]:
        """Return the fact records this paper's own source spans can support."""
        previous_facts = {str(fact.get("fact_id") or ""): fact
                          for fact in row.get("scientific_facts") or [] if isinstance(fact, dict)}
        facts = []
        for fact in result.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            if review_reading and fact.get("validation_contract") != FACT_VALIDATION_VERSION:
                # A navigation candidate cannot gain legacy implicit trust
                # merely because the model omitted the validation marker.
                fact = {**fact, "validation_contract": FACT_VALIDATION_VERSION,
                        "verification": {"status": "pending"},
                        "review_status": "pending_verification", "human_checked": False}
            raw_refs = fact_support_spans(fact, fact_candidates)
            if not raw_refs:
                continue
            excerpt = " ".join(ref["support_excerpt"] for ref in raw_refs)
            if not excerpt or not str(fact.get("value") or "").strip():
                continue
            if not str(fact.get("evidence_ceiling") or "").strip():
                continue
            field_id = str(fact.get("field_id") or "").casefold()
            if field_id != "topic_partition" and field_id not in extraction_fact_field_ids(required_roles=topic_required_roles,
                    evidence_candidates=list(fact_candidates.values())):
                continue
            assertion_ceiling = normalize_assertion_ceiling(
                fact.get("assertion_ceiling")
            )
            facts.append(
                {
                    **fact,
                    "paper_id": paper_id,
                    "support_level": "context_only" if fact.get("validation_contract") == FACT_VALIDATION_VERSION
                        and (fact.get("verification") or {}).get("status") != "supported" else fact.get("support_level"),
                    "assertion_ceiling": assertion_ceiling,
                    "evidence_refs": raw_refs,
                    "support_spans": raw_refs,
                    "support_excerpt": excerpt,
                }
            )
            # Source registration may fill previously null metadata. Rebind
            # only when the shared checker proves the old audit still fits.
            if ((fact.get("verification") or {}).get("input_fingerprint")
                    and not fact_needs_verification(fact)
                    and not fact_needs_verification(facts[-1])):
                facts[-1]["verification"] = {**fact["verification"],
                    "input_fingerprint": review_fingerprint(facts[-1])}
        if review_reading:
            # An initial reading/supplement is never an instruction to
            # delete source-current facts or overwrite a user's revision.
            by_id = {str(fact.get("fact_id") or ""): fact for fact in facts}
            for fid, old in previous_facts.items():
                if not fid or not fact_support_spans(old, fact_candidates):
                    continue
                if (fid not in by_id or fact_is_usable(old)
                        or old.get("revision_of_fact_id") or old.get("human_checked")):
                    by_id[fid] = deepcopy(old)
            facts = list(by_id.values())
        return facts

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

    def fact_revision_payload(self, principal, project_id, *, source_matrix_artifact_id=None,
                              selected_paper_ids=None):
        project = self._owned_project(principal, project_id)
        matrix, artifact = self._matrix(principal, project_id)
        self.validate_matrix_enrichment_inputs(principal, project_id, {
            "source_matrix_artifact_id": source_matrix_artifact_id or artifact.id})
        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        if selected_paper_ids is not None:
            selected = {str(paper_id) for paper_id in selected_paper_ids}
            if not selected <= {str(row.get("paper_id")) for row in rows}:
                raise WorkflowValidationError("Fact repair requested papers outside the current Matrix.")
            rows = [row for row in rows if str(row.get("paper_id")) in selected]
        targets = [row for row in rows if pending_revisions(row)]
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

    def apply_fact_revision_result(self, row, paper, output):
        """Apply exact revision verdicts without rewriting the user's propositions.

        Both standalone verification and mixed reading batches use this merger;
        their caller validates the owned Matrix snapshot and source versions.
        """
        if str(row.get("paper_id")) != str(paper.get("paper_id")) or str(output.get("paper_id")) != str(paper.get("paper_id")):
            raise WorkflowValidationError("Fact verification changed the target paper set.")
        targets = {fact["fact_id"]: fact for fact in paper["revision_facts"]}
        results = output.get("facts") or []
        returned = {fact["fact_id"]: fact for fact in results}
        if set(targets) != set(returned) or len(returned) != len(results):
            raise WorkflowValidationError("Fact verification changed the target fact set.")
        if not set(targets) <= {fact["fact_id"] for fact in row.get("scientific_facts") or []}:
            raise WorkflowConflict("The fact changed during verification; the old verdict cannot overwrite it.")
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
        row["fact_enrichment"]["error"] = output.get("error") or ""

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
            self.apply_fact_revision_result(row, paper, outputs[str(row["paper_id"])])
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
