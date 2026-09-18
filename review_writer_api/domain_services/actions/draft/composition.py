"""Draft-owned synthesis: immutable section candidates and first-run completion."""
import json
import uuid

from review_writer_api.database import utc_now
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.security import Permission
from review_writer_core.draft_composition import section_text, replace_section, signature, source_signature
from review_writer_core.workflow.artifacts import DRAFT_MANUSCRIPT, DRAFT_REWRITE_CANDIDATES, FINAL_FRONT_MATTER, FINAL_CONCLUSION
from review_writer_core.workflow.artifacts import BLUEPRINT, MATRIX, SECTION_DRAFTS, SECTION_WRITING_PLAN, DRAFT_REWRITE_OVERLAYS
from review_writer_core.section_narrative_contracts import build_argument_execution
from review_writer_core.stages.draft.revisions import effective_writing_plan
from review_writer_core.draft_composition import manuscript_fields, replace_manuscript_fields
from review_writer_core.draft_synthesis import publication_title
from review_writer_core.overview_composition import compose_overview
from review_writer_core.workflow.artifacts import FINAL_OVERVIEW_IMAGE, FINAL_OVERVIEW_TEXT


def encoded(value):
    return (json.dumps(value, ensure_ascii=False)+"\n").encode()


class DraftCompositionMixin:
    def manuscript_preview(self, principal, project_id, text):
        if not text:
            return text
        image = self._artifact(principal, project_id, FINAL_OVERVIEW_IMAGE)
        if not image:
            return text
        caption, _ = self._read_json(principal, project_id, FINAL_OVERVIEW_TEXT, required=False)
        # Current artifacts only: late/conflicting job candidates are not selected.
        return compose_overview(text, image.id, caption)

    def legacy_manuscript_fields(self, principal, project_id):
        front, artifact = self._read_json(principal, project_id, FINAL_FRONT_MATTER, required=False)
        if not artifact:
            return None
        return {"title": str(front.get("title") or ""),
                "keywords": [] if (front.get("field_states") or {}).get("keywords") == "user_omitted" else list(front.get("keywords") or [])}

    def legacy_synthesis_candidates(self, principal, project_id, text, entries):
        """Read-only migration projection. Adoption is explicit, never a GET side effect."""
        front, front_artifact = self._read_json(principal, project_id, FINAL_FRONT_MATTER, required=False)
        conclusion, conclusion_artifact = self._read_text(principal, project_id, FINAL_CONCLUSION, required=False)
        values = [("abstract", front.get("abstract") or "", front_artifact),
                  ("conclusion", conclusion, conclusion_artifact)]
        candidates = []
        for role, content, artifact in values:
            if not artifact or not content.strip() or (front.get("field_states") or {}).get(role) == "user_omitted":
                continue
            cid = str(uuid.uuid5(uuid.NAMESPACE_URL, "draft-migration:"+artifact.id+":"+role))
            if cid in entries:
                continue
            current = section_text(text, role)
            if current and content.strip() in current:
                continue
            candidates.append({"candidate_id": cid, "revision_mode": "section_synthesis", "section_role": role,
                "paragraph_id": role.title(), "paragraph_key": "synthesis:"+role,
                "base_text_sha256": signature(current), "original_text": current, "candidate_text": content,
                "source_signature": "legacy", "legacy_artifact_id": artifact.id,
                "status": "pending", "reply": "Existing Final content — review before importing into Draft",
                "created_at": artifact.created_at.isoformat(), "validation_errors": [], "source_refs": []})
        return candidates

    def synthesis_payload(self, principal, project_id, role, *, initial=False):
        principal.require(Permission.PROJECT_WRITE)
        project = self._owned_project(principal, project_id)
        if role not in {"abstract", "conclusion", "initial"}:
            raise WorkflowValidationError("Unknown synthesis section.")
        text, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
        if self._freshness(principal, project_id, artifact)["upstream_stale"]:
            raise WorkflowConflict("Draft sources changed. Reassemble before generating.")
        roles = ["conclusion", "abstract"] if role == "initial" else [role]
        legacy, _ = self._read_json(principal, project_id, FINAL_FRONT_MATTER, required=False)
        omitted = {k for k, v in (legacy.get("field_states") or {}).items() if v == "user_omitted"}
        if artifact.metadata.get("keywords_user_omitted"):
            omitted.add("keywords")
        roles = [r for r in roles if not initial or not section_text(text, r)]
        if initial:
            roles = [r for r in roles if r not in omitted]
        current_title = manuscript_fields(text)["title"]
        default_title = str(project.topic or project.slug or project_id).strip()
        generate_title = bool("abstract" in roles and "title" not in omitted
                              and not artifact.metadata.get("title_user_modified")
                              and (not current_title or current_title == default_title))
        conclusion_context = {}
        if "conclusion" in roles:
            # Match the former Final generator's realized-claim projection without
            # loading retrieval results, images or the full compatibility workspace.
            inputs = [self._read_json(principal, project_id, name, required=False)[0]
                      for name in (BLUEPRINT, SECTION_WRITING_PLAN, SECTION_DRAFTS, MATRIX, DRAFT_REWRITE_OVERLAYS)]
            blueprint, plan, sections, matrix, overlays = inputs
            execution = build_argument_execution(blueprint, effective_writing_plan(plan, overlays),
                                                 sections, matrix, draft_text=text)
            conclusion_context = {"sections": [
                {"title": section["title"], "claims": [
                    {key: claim.get(key) for key in ("claim", "claim_kind", "paper_ids")}
                    for claim in section["claims"][:3]]}
                for section in execution["sections"] if section["section_role"] == "body"]}
        return {"project_id": project_id, "revision_mode": "section_synthesis", "initial": initial,
                "generate_title": generate_title,
                "generate_keywords": bool("abstract" in roles and "keywords" not in omitted
                                          and not manuscript_fields(text)["keywords"]),
                "conclusion_context": conclusion_context,
                "omitted_fields": sorted(omitted),
                "roles": roles, "source_draft_artifact_id": artifact.id, "draft_text": text,
                "base_sections": {r: section_text(text, r) for r in roles},
                "source_artifact_ids": {k: v for k, v in artifact.metadata.items() if k in {
                    "source_sections_artifact_id", "source_matrix_artifact_id", "source_section_evidence_artifact_id"}},
                "source_signatures": {r: source_signature(text, r) for r in roles},
                "candidate_seed": str(uuid.uuid5(uuid.NAMESPACE_URL, project_id+":"+artifact.id+":"+role)) if initial else str(uuid.uuid4())}

    def publish_synthesis(self, principal, project_id, payload, built):
        principal.require(Permission.PROJECT_WRITE)
        for attempt in range(3):
            revision = self._revision(principal, project_id)
            text, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
            store, record = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES, required=False)
            entries = dict(store.get("entries") or {})
            updated = text
            ids = []
            metadata = dict(artifact.metadata)
            sources = dict(metadata.get("synthesis_sources") or {})
            for role in payload["roles"]:
                content = str((built.get("sections") or {}).get(role) or "").strip()
                if not content:
                    continue
                cid = str(uuid.uuid5(uuid.NAMESPACE_URL, payload["candidate_seed"]+":"+role))
                ids.append(cid)
                if cid in entries:
                    continue
                current = section_text(updated, role)
                auto = bool(payload.get("initial") and not current and
                            source_signature(text, role) == payload["source_signatures"][role]
                            and all(artifact.metadata.get(k) == v for k, v in payload.get("source_artifact_ids", {}).items())
                            and not self._freshness(principal, project_id, artifact)["upstream_stale"])
                # Initial Abstract was generated after the returned Conclusion.
                generated_source = payload["draft_text"]
                if role == "abstract" and payload.get("initial") and built.get("sections", {}).get("conclusion"):
                    generated_source = replace_section(generated_source, "conclusion", built["sections"]["conclusion"])
                generated_signature = source_signature(generated_source, role)
                if auto and role == "abstract":
                    auto = source_signature(updated, role) == generated_signature
                entries[cid] = {"candidate_id": cid, "revision_mode": "section_synthesis", "section_role": role,
                    "paragraph_key": "synthesis:"+role, "paragraph_id": role.title(),
                    "source_draft_artifact_id": payload["source_draft_artifact_id"],
                    "source_signature": generated_signature,
                    "base_text_sha256": signature(payload["base_sections"].get(role, "")),
                    "original_text": payload["base_sections"].get(role, ""), "candidate_text": content,
                    "status": "accepted" if auto else "pending", "reply": "Whole-section candidate",
                    "created_at": utc_now().isoformat(), "validation_errors": [], "source_refs": []}
                if auto:
                    updated = replace_section(updated, role, content, identity=cid.replace("-", ""))
                    sources[role] = generated_signature
            if (payload.get("generate_keywords", payload.get("initial"))
                    and "keywords" not in payload.get("omitted_fields", [])
                    and not artifact.metadata.get("keywords_user_omitted")
                    and artifact.id == payload["source_draft_artifact_id"]
                    and built.get("sections", {}).get("abstract") and built.get("keywords")
                    and not manuscript_fields(updated)["keywords"]):
                fields = manuscript_fields(updated)
                updated = replace_manuscript_fields(updated, fields["title"], built["keywords"])
            title = publication_title(built.get("title"))
            accepted = [entries[cid] for cid in ids if entries[cid].get("status") == "accepted"]
            if (payload.get("generate_title", payload.get("initial")) and title and "title" not in payload.get("omitted_fields", [])
                    and built.get("sections", {}).get("abstract")
                    and (not payload.get("initial") or any(c["section_role"] == "abstract" for c in accepted))
                    and artifact.id == payload["source_draft_artifact_id"]):
                # Only the untouched snapshot with a default title may receive a title.
                # Any concurrent save (even an edit-and-revert) preserves user control.
                updated = replace_manuscript_fields(updated, title, manuscript_fields(updated)["keywords"])
                for role, old_signature in list(sources.items()):
                    if old_signature == source_signature(text, role):
                        sources[role] = source_signature(updated, role)
                for cid in ids:
                    candidate = entries[cid]
                    if candidate["status"] == "pending":
                        titled_source = replace_manuscript_fields(payload["draft_text"], title, manuscript_fields(payload["draft_text"])["keywords"])
                        candidate["source_signature"] = source_signature(titled_source, candidate["section_role"])
                for candidate in accepted:
                    role = candidate["section_role"]
                    sources[role] = candidate["source_signature"] = source_signature(updated, role)
            metadata.update(synthesis_sources=sources, operation="draft-synthesis",
                            previous_draft_artifact_id=artifact.id)
            files = {DRAFT_REWRITE_CANDIDATES: (encoded({"entries": entries}), "json")}
            if updated != text:
                files[DRAFT_MANUSCRIPT] = (updated.encode(), "markdown")
            try:
                _, state = self._publish_files(principal, project_id, files, expected_revision=revision,
                    expected_current_artifacts={DRAFT_MANUSCRIPT: artifact.id,
                        DRAFT_REWRITE_CANDIDATES: record.id if record else ""}, metadata=metadata,
                    invalidate_final=updated != text)
                return {"candidate_ids": ids, "revision": state.revision, "warnings": built.get("warnings", [])}
            except WorkflowConflict:
                if attempt == 2:
                    raise

    def decide_synthesis(self, principal, project_id, candidate_id, *, decision, expected_base_text_sha256=None, edited_text=None):
        principal.require(Permission.PROJECT_WRITE)
        if decision not in {"accept", "reject"}:
            raise WorkflowValidationError("Unknown candidate decision.")
        with self._write_lock:
            revision = self._revision(principal, project_id)
            text, artifact = self._read_text(principal, project_id, DRAFT_MANUSCRIPT)
            store, record = self._read_json(principal, project_id, DRAFT_REWRITE_CANDIDATES, required=False)
            entries = dict(store.get("entries") or {})
            candidate = dict(entries.get(candidate_id) or {})
            if not candidate:
                candidate = next((c for c in self.legacy_synthesis_candidates(principal, project_id, text, entries)
                                  if c["candidate_id"] == candidate_id), {})
            if candidate.get("revision_mode") != "section_synthesis":
                raise WorkflowValidationError("Section candidate not found.")
            status = "accepted" if decision == "accept" else "rejected"
            if candidate.get("status") == status:
                return {"candidate_id": candidate_id, "revision": revision}
            if candidate.get("status") != "pending":
                raise WorkflowConflict("Candidate is no longer pending.")
            files = {}
            metadata = dict(artifact.metadata)
            if decision == "accept":
                role = candidate["section_role"]
                if edited_text is not None:
                    if expected_base_text_sha256 != candidate["base_text_sha256"] or not edited_text.strip():
                        raise WorkflowConflict("Refresh the section preview before saving.")
                    candidate["generated_text"] = candidate["candidate_text"]
                    candidate["candidate_text"] = edited_text.strip()
                if candidate.get("legacy_artifact_id") and expected_base_text_sha256 != candidate["base_text_sha256"]:
                    raise WorkflowConflict("The migration preview changed. Refresh and compare before importing.")
                if signature(section_text(text, role)) != candidate["base_text_sha256"]:
                    raise WorkflowConflict("This section changed. Candidate retained for comparison.")
                if self._freshness(principal, project_id, artifact)["upstream_stale"]:
                    raise WorkflowConflict("Draft sources changed before acceptance.")
                updated = replace_section(text, role, candidate["candidate_text"], identity=candidate_id.replace("-", ""))
                if edited_text is not None and edited_text.strip() != candidate.get("generated_text"):
                    changed_ids = {p["paragraph_id"] for p in self._paragraph_spans(updated)
                                   if p["text"] not in {old["text"] for old in self._paragraph_spans(text)}}
                    metadata["unverified_manual_paragraph_ids"] = sorted(
                        set(metadata.get("unverified_manual_paragraph_ids") or []) | changed_ids)
                files[DRAFT_MANUSCRIPT] = (updated.encode(), "markdown")
                sources = dict(metadata.get("synthesis_sources") or {})
                sources[role] = candidate["source_signature"]
                metadata.update(synthesis_sources=sources, previous_draft_artifact_id=artifact.id,
                                operation="section-synthesis-accept")
                for entry in entries.values():
                    if entry.get("section_role") == role and entry.get("status") == "pending":
                        entry["status"] = "superseded"
            candidate.update(status=status, decided_at=utc_now().isoformat())
            entries[candidate_id] = candidate
            files[DRAFT_REWRITE_CANDIDATES] = (encoded({"entries": entries}), "json")
            _, state = self._publish_files(principal, project_id, files, expected_revision=revision,
                expected_current_artifacts={DRAFT_MANUSCRIPT: artifact.id, DRAFT_REWRITE_CANDIDATES: record.id if record else ""},
                metadata=metadata, invalidate_final=decision == "accept")
            return {"candidate_id": candidate_id, "revision": state.revision}
