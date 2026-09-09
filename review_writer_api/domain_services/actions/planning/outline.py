"""Whole-outline paper recommendation for user-authored Planning outlines."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from typing import Any

from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.security import Permission, Principal
from review_writer_core.model_gateway_client import parse_json_object_text
from review_writer_core.review_structure import infer_section_role
from review_writer_core.stages.planning.outline import outline_sections
from review_writer_core.stages.planning.matrix import _paper_ids
from review_writer_core.stages.planning.routing import routing_facts, verified_route


_TOKEN = re.compile(r"[a-z][a-z0-9+./()\[\]-]*|[\u3400-\u9fff]{2,}", re.I)
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "review", "section",
    "study", "studies", "comparison", "overview", "introduction", "conclusion",
    "本节", "研究", "比较", "综述", "章节", "介绍", "结论",
}


def _terms(value: Any) -> list[str]:
    terms: list[str] = []
    for token in _TOKEN.findall(str(value or "").casefold()):
        if token in _STOP_WORDS:
            continue
        candidates = [token]
        if re.fullmatch(r"[\u3400-\u9fff]{5,}", token):
            candidates.extend(token[index : index + 2] for index in range(len(token) - 1))
        for candidate in candidates:
            if candidate not in terms:
                terms.append(candidate)
    return terms[:80]


def _matches(term: str, text: str) -> bool:
    if not term or not text:
        return False
    if re.fullmatch(r"[a-z0-9+./()\[\]-]+", term, re.I):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, re.I))
    return term in text


def _fact_text(row: dict[str, Any], tags: dict[str, Any]) -> str:
    parts: list[str] = []
    for fact in routing_facts(row):
        parts.extend(
            str(fact.get(key) or "").strip()
            for key in ("value", "normalized_value", "support_excerpt")
            if str(fact.get(key) or "").strip()
        )
    routing = verified_route(row)
    if isinstance(routing, dict) and routing.get("evidence_refs"):
        parts.extend(
            str(routing.get(key) or "").strip()
            for key in ("label", "rationale")
            if str(routing.get(key) or "").strip()
        )
    for key, values in tags.items():
        parts.append(str(key))
        if isinstance(values, list):
            parts.extend(str(value) for value in values)
        else:
            parts.append(str(values))
    return " ".join(parts).casefold()


def _metadata_text(row: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in (
            row.get("title"),
            " ".join(str(item) for item in row.get("keywords") or []),
            row.get("abstract"),
            row.get("main_content"),
        )
        if str(value or "").strip()
    ).casefold()


def _score_pair(
    section: dict[str, Any],
    row: dict[str, Any],
    *,
    tags: dict[str, Any],
    source_text: str,
    fulltext_rank: int | None,
    already_selected: bool,
) -> tuple[float, list[str]]:
    title_terms = _terms(section.get("title"))
    detail_terms = _terms(
        f"{section.get('purpose') or ''} {section.get('notes') or ''}"
    )
    facts = _fact_text(row, tags)
    metadata = _metadata_text(row)
    fact_hits = sum(1 for term in [*title_terms, *detail_terms] if _matches(term, facts))
    title_hits = sum(1 for term in title_terms if _matches(term, metadata))
    detail_hits = sum(1 for term in detail_terms if _matches(term, metadata))
    source_hits = sum(
        1 for term in [*title_terms, *detail_terms] if _matches(term, source_text)
    )
    score = fact_hits * 5.0 + title_hits * 2.5 + detail_hits * 1.25 + source_hits * 0.5
    sources: list[str] = []
    if fact_hits:
        sources.append("matrix_facts")
    if title_hits or detail_hits or source_hits:
        sources.append("metadata")
    if fulltext_rank is not None:
        score += 4.0 + 2.0 / max(1, fulltext_rank)
        sources.append("fulltext")
    if already_selected:
        score += 0.1
    return score, sources


def _compact_model_paper(row: dict[str, Any], score_by_section: dict[int, float]) -> dict[str, Any]:
    facts = []
    for fact in routing_facts(row):
        value = str(fact.get("normalized_value") or fact.get("value") or "").strip()
        if value:
            facts.append({"field": str(fact.get("field_id") or "fact"), "value": value[:700]})
    return {
        "paper_id": str(row.get("paper_id") or ""),
        "title": str(row.get("title") or "")[:500],
        "keywords": [str(value)[:100] for value in (row.get("keywords") or [])[:20]],
        "scientific_facts": facts[:16],
        "deterministic_scores": {str(key): round(value, 3) for key, value in score_by_section.items()},
    }


class PlanningOutlineActionsMixin:
    async def recommend_outline_papers(
        self,
        principal: Principal,
        project_id: str,
        *,
        revision: int,
        outline_md: str,
    ) -> dict[str, Any]:
        """Recommend Matrix papers across all body sections in one coherent pass."""

        principal.require(Permission.PROJECT_WRITE)
        matrix, _matrix_artifact = self._matrix(principal, project_id)
        matrix, _metadata_artifacts = self._with_current_bibliography(principal, matrix)
        state = self.repository.get_stage_state(principal.user_id, project_id, "matrix")
        if state is None or state.revision != int(revision):
            raise WorkflowConflict("The Matrix changed. Refresh Planning before recommending papers.")

        sections = outline_sections(outline_md)
        if not sections:
            raise WorkflowValidationError("Add at least one outline section before recommending papers.")
        if any(
            not str(section.get("title") or "").strip()
            or "outline-untitled" in str(section.get("title") or "")
            for section in sections
        ):
            raise WorkflowValidationError("Complete every section title before recommending papers.")

        body_indices = [
            index
            for index, section in enumerate(sections)
            if infer_section_role(section.get("title"), section.get("section_role")) == "body"
        ]
        if not body_indices:
            raise WorkflowValidationError("Add at least one body section before recommending papers.")

        rows = [row for row in matrix.get("rows") or [] if isinstance(row, dict)]
        rows_by_id = {str(row.get("paper_id") or ""): row for row in rows}
        paper_order = _paper_ids(rows)
        tags_by_paper, source_text_by_paper = self._outline_sources(principal, rows)

        fulltext_rank: dict[int, dict[str, int]] = defaultdict(dict)
        fulltext_used = False
        if self.library_index is not None and self.library_index.enabled:
            for section_index in body_indices:
                section = sections[section_index]
                query = " ".join(
                    str(section.get(key) or "").strip()
                    for key in ("title", "purpose", "notes")
                    if str(section.get(key) or "").strip()
                )
                try:
                    hits = self.library_index.retrieve(
                        principal,
                        query,
                        allowed_papers=paper_order,
                        top_k=min(50, max(12, len(paper_order) * 2)),
                        per_paper_limit=2,
                        include_neighbors=False,
                    )
                except Exception:
                    hits = []
                for rank, hit in enumerate(hits, start=1):
                    fulltext_rank[section_index].setdefault(hit.paper_id, rank)
                fulltext_used = fulltext_used or bool(hits)

        scores: dict[str, dict[int, float]] = {}
        score_sources: dict[tuple[str, int], list[str]] = {}
        for paper_id in paper_order:
            row = rows_by_id.get(paper_id) or {}
            scores[paper_id] = {}
            for section_index in body_indices:
                score, sources = _score_pair(
                    sections[section_index],
                    row,
                    tags=tags_by_paper.get(paper_id) or {},
                    source_text=source_text_by_paper.get(paper_id, ""),
                    fulltext_rank=fulltext_rank.get(section_index, {}).get(paper_id),
                    already_selected=paper_id in (sections[section_index].get("paper_ids") or []),
                )
                scores[paper_id][section_index] = score
                score_sources[(paper_id, section_index)] = sources

        ranked_sections = {
            paper_id: sorted(section_scores, key=lambda index: (-section_scores[index], index))
            for paper_id, section_scores in scores.items()
        }
        ambiguous: list[str] = []
        confident: list[tuple[float, float, str]] = []
        for paper_id, ranked in ranked_sections.items():
            best = scores[paper_id][ranked[0]]
            second = scores[paper_id][ranked[1]] if len(ranked) > 1 else 0.0
            margin = best - second
            if best <= 0 or (second > 0 and margin <= max(1.0, best * 0.2)):
                ambiguous.append(paper_id)
            else:
                confident.append((margin, best, paper_id))

        assignments: dict[int, list[dict[str, Any]]] = {index: [] for index in body_indices}
        assigned_papers: set[str] = set()

        def assign(paper_id: str, section_index: int, *, basis: str, reason: str, confidence: float) -> bool:
            if paper_id in assigned_papers or len(assignments[section_index]) >= 6:
                return False
            assignments[section_index].append(
                {
                    "paper_id": paper_id,
                    "basis": basis,
                    "reason": reason,
                    "confidence": round(max(0.0, min(confidence, 1.0)), 3),
                }
            )
            assigned_papers.add(paper_id)
            return True

        for _margin, best, paper_id in sorted(confident, reverse=True):
            for section_index in ranked_sections[paper_id]:
                score = scores[paper_id][section_index]
                if score <= 0:
                    break
                sources = score_sources[(paper_id, section_index)]
                if assign(
                    paper_id,
                    section_index,
                    basis="+".join(sources) or "outline_match",
                    reason="Matched the section against structured facts, metadata, or full text.",
                    confidence=min(0.95, 0.55 + score / 40.0),
                ):
                    break

        model_used = False
        model_resolved = 0
        model_failed = False
        if ambiguous and self.model_gateway is not None:
            digest = hashlib.sha256(
                (outline_md + "\n" + ",".join(paper_order)).encode("utf-8")
            ).hexdigest()
            gateway_job = self._begin_gateway_job(
                principal,
                project_id,
                job_type="planning.outline-recommend",
                idempotency_key=f"{digest[:40]}-{uuid.uuid4().hex[:8]}",
                payload={"outline_sha256": digest, "ambiguous_paper_count": len(ambiguous)},
            )
            _normal, secrets = self.model_gateway.environment_for_job(gateway_job)
            prompt = json.dumps(
                {
                    "task": (
                        "Assign each ambiguous paper to at most one best-fitting body section. "
                        "Use the paper's scientific facts before title words. Return null when "
                        "the evidence does not support any section; never force coverage."
                    ),
                    "output_schema": {
                        "assignments": [
                            {
                                "paper_id": "P001",
                                "section_index": 0,
                                "confidence": 0.0,
                                "reason": "short evidence-based reason",
                            }
                        ]
                    },
                    "sections": [
                        {
                            "section_index": index,
                            "title": sections[index].get("title"),
                            "purpose": sections[index].get("purpose"),
                            "notes": sections[index].get("notes"),
                        }
                        for index in body_indices
                    ],
                    "papers": [
                        _compact_model_paper(rows_by_id.get(paper_id) or {}, scores[paper_id])
                        for paper_id in ambiguous
                    ],
                },
                ensure_ascii=False,
            )
            try:
                response = await self.model_gateway.complete(
                    secrets["REVIEW_WRITER_TASK_TOKEN"],
                    request_key=f"outline-recommend-{digest[:32]}",
                    stage="planning.outline-recommend",
                    prompt=prompt,
                    response_format="json",
                )
                model_payload = parse_json_object_text(
                    str(response.get("output_text") or ""),
                    required_list="assignments",
                    context="Outline recommendation model",
                )
                allowed_sections = set(body_indices)
                ambiguous_set = set(ambiguous)
                for item in model_payload.get("assignments") or []:
                    if not isinstance(item, dict):
                        continue
                    paper_id = str(item.get("paper_id") or "")
                    section_index = item.get("section_index")
                    try:
                        normalized_index = int(section_index)
                    except (TypeError, ValueError):
                        continue
                    if paper_id not in ambiguous_set or normalized_index not in allowed_sections:
                        continue
                    try:
                        confidence = float(item.get("confidence") or 0.65)
                    except (TypeError, ValueError):
                        confidence = 0.65
                    if assign(
                        paper_id,
                        normalized_index,
                        basis="model_on_matrix_facts",
                        reason=str(item.get("reason") or "Model resolved an ambiguous cross-section match.")[:500],
                        confidence=confidence,
                    ):
                        model_resolved += 1
                model_used = True
                self._finish_gateway_job(
                    gateway_job.job_id,
                    succeeded=True,
                    error_code="OUTLINE_RECOMMENDATION_FAILED",
                    result={"resolved_paper_count": model_resolved},
                )
            except Exception as exc:
                model_failed = True
                self._finish_gateway_job(
                    gateway_job.job_id,
                    succeeded=False,
                    error_code="OUTLINE_RECOMMENDATION_FAILED",
                    error_message=str(exc),
                )

        # When the model is unavailable or declines a paper, keep only a positive
        # deterministic match. Never fall back to the first Matrix papers.
        for paper_id in ambiguous:
            if paper_id in assigned_papers:
                continue
            if model_used:
                continue
            for section_index in ranked_sections[paper_id]:
                score = scores[paper_id][section_index]
                if score <= 0:
                    break
                sources = score_sources[(paper_id, section_index)]
                if assign(
                    paper_id,
                    section_index,
                    basis="+".join(sources) or "outline_match",
                    reason="Best available evidence match; review because the cross-section margin was small.",
                    confidence=min(0.7, 0.4 + score / 50.0),
                ):
                    break

        response_sections = []
        for index in body_indices:
            recommendations = sorted(
                assignments[index],
                key=lambda item: (-float(item["confidence"]), paper_order.index(item["paper_id"])),
            )
            response_sections.append(
                {
                    "section_index": index,
                    "title": str(sections[index].get("title") or ""),
                    "paper_ids": [item["paper_id"] for item in recommendations],
                    "recommendations": recommendations,
                }
            )
        return {
            "project_id": project_id,
            "matrix_revision": state.revision,
            "sections": response_sections,
            "summary": {
                "body_section_count": len(body_indices),
                "recommended_paper_count": len(assigned_papers),
                "unassigned_paper_count": len(paper_order) - len(assigned_papers),
                "fulltext_used": fulltext_used,
                "model_used": model_used,
                "model_resolved_paper_count": model_resolved,
                "model_fallback_used": model_failed,
            },
        }
