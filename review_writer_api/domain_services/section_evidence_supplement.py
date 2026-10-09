"""One local-source supplement per section, tied to the prepared source snapshot."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re
from uuid import uuid4

from review_writer_api.errors import WorkflowValidationError
from review_writer_core.academic_contracts import evidence_key
from review_writer_core.review_fact_readiness import REVIEW_READING_STRATEGY
from review_writer_core.atomic_io import atomic_write_json
from review_writer_core.evidence_queries import boolean_query, query_terms
from review_writer_core.stages.sections.source_supplement import MAX_HITS, MAX_QUESTIONS, supplement_request_id
from review_writer_core.stages.sections.evidence_resolution import merge_supplement_evidence


class SectionEvidenceSupplement:
    def __init__(self, service, principal, payload, directory):
        self.service, self.principal, self.payload = service, principal, payload
        self.directory = Path(directory)
        self.validation_nonce = uuid4().hex
        self.tasks = {str(row.get("section_id")): row for row in payload.get("tasks") or []}
        self.sections = {str(row.get("section_id")): row
                         for row in (payload.get("evidence_package") or {}).get("sections") or []}
        self.cache, self.responses = {}, {}
        self.used = {}
        self.trusted = {}
        self.enabled = bool(service and getattr(service, "library_index", None) and any(
            task.get("reading_strategy") == REVIEW_READING_STRATEGY
            and self.sections.get(sid, {}).get("source_manifest") for sid, task in self.tasks.items()))
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)
            ledger = self.directory / "rounds.json"
            if ledger.is_file():
                self.used = json.loads(ledger.read_text(encoding="utf-8"))
                if not isinstance(self.used, dict):
                    raise WorkflowValidationError("Invalid local supplement round ledger.")

    _request_id = staticmethod(supplement_request_id)

    def _mark_used(self, section_id, request_id):
        if section_id not in self.tasks or (section_id in self.used and self.used[section_id] != request_id):
            raise WorkflowValidationError("This section already used its supplement round or is outside scope.")
        self.used[section_id] = request_id
        atomic_write_json(self.directory / "rounds.json", self.used)

    def _scope(self, section_id):
        task, section = self.tasks.get(section_id, {}), self.sections.get(section_id, {})
        if task.get("reading_strategy") != REVIEW_READING_STRATEGY or not section.get("source_manifest"):
            raise WorkflowValidationError("This section has no frozen local-source supplement scope.")
        allowed = set(task.get("allowed_papers") or [])
        catalog = self.service._catalog(self.principal, sorted(allowed))
        parents = self.service._supporting_source_parents(catalog, allowed)
        manifest = section["source_manifest"]
        summaries = self.service.library_index.summaries(self.principal, list(manifest))
        for source_id, recorded in manifest.items():
            parent = str(recorded.get("paper_id") or "")
            lineage = str(recorded.get("source_lineage_hash") or "")
            if (source_id not in catalog or parent not in catalog or parent not in allowed
                    or parents.get(source_id, source_id) != parent or not lineage
                    or (summaries.get(source_id) or {}).get("source_lineage_hash") != lineage):
                raise WorkflowValidationError("A supplement source is unavailable or its version changed.")
        return allowed, manifest, catalog

    def _requests(self, section_id, requests):
        allowed, manifest, catalog = self._scope(section_id)
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_QUESTIONS:
            raise WorkflowValidationError("A section supplement requires one to three bounded questions.")
        for request in requests:
            if (not isinstance(request, dict) or not isinstance(request.get("claim_id"), str)
                    or not request["claim_id"] or len(request["claim_id"]) > 240
                    or not isinstance(request.get("query"), str) or not request["query"].strip()
                    or len(request["query"]) > 1200 or not isinstance(request.get("paper_ids"), list)
                    or not request["paper_ids"] or any(not isinstance(pid, str) or pid not in allowed
                                                      for pid in request["paper_ids"])):
                raise WorkflowValidationError("A supplement question exceeds its section paper scope.")
        return manifest, catalog

    def _checked_hits(self, section_id, rows, *, paper_ids=None):
        _, manifest, catalog = self._scope(section_id)
        if not isinstance(rows, list) or len(rows) > MAX_HITS:
            raise WorkflowValidationError("A section supplement exceeded its source budget.")
        checked = {}
        for row in rows:
            if not isinstance(row, dict):
                raise WorkflowValidationError("Invalid section supplement source.")
            sid = str(row.get("source_file_id") or row.get("paper_id") or "")
            source = manifest.get(sid) or {}
            lineage, chunk = source.get("source_lineage_hash"), str(row.get("chunk_id") or "")
            if (not lineage or row.get("paper_id") != source.get("paper_id") or not chunk
                    or (paper_ids is not None and row.get("paper_id") not in paper_ids)
                    or row.get("source_lineage_hash") != lineage
                    or row.get("evidence_key") != evidence_key(sid, chunk, lineage)):
                raise WorkflowValidationError("A supplement source identity does not match the frozen manifest.")
            resolved = self.service.library_index.resolve_source_chunks(
                self.principal, sid, [chunk], expected_lineage=lineage)
            exact = next((item for item in resolved if item.get("paper_id") == sid
                          and item.get("chunk_id") == chunk and item.get("source_lineage_hash") == lineage
                          and item.get("content") == row.get("content")), None)
            if exact is None:
                raise WorkflowValidationError("The exact supplement passage is no longer available.")
            parent = source["paper_id"]
            kind = str(exact.get("content_type") or "text")
            channel = "abstract" if kind == "abstract" else "table" if kind == "table" else "figure_caption" if kind in {"image", "caption", "figure"} else "body"
            neighbor = row.get("is_neighbor") is True
            eligible = (not neighbor and channel != "abstract"
                        and row.get("claim_eligible") is not False and row.get("counts_as_evidence") is not False)
            key = evidence_key(sid, chunk, lineage)
            checked[key] = {**exact, "paper_id": parent, "paper_title": catalog[parent].title,
                "source_file_id": sid, "source_type": "main_article" if sid == parent else "supporting_information",
                "evidence_key": key, "evidence_id": f"EV-{key.removeprefix('sha256:')[:12].upper()}",
                "source_channel": channel, "support_level": "abstract_limited" if channel == "abstract" else "direct" if eligible else "context_only",
                "is_neighbor": neighbor, "claim_eligible": eligible, "counts_as_evidence": eligible,
                "assertion_ceiling": "abstract_report_only" if channel == "abstract" else "context_only" if not eligible else "direct_report_with_local_context"
                    if channel in {"table", "figure_caption"} else "direct_source_report",
                "match_type": "neighbor_context" if neighbor else "table_or_figure" if channel != "body" else "direct_match",
                "match_reason": "section_local_supplement", "question_ids": [],
                "retrieval_passes": ["section_local_supplement"], "asset_refs": [], "score": 0,
                "index_id": "", "previous_chunk_id": "", "next_chunk_id": ""}
        return list(checked.values())

    def retrieve(self, section_id, requests):
        manifest, _ = self._requests(section_id, requests)
        found = {}
        existing = {row.get("evidence_key") for row in self.sections[section_id].get("hits") or []}
        for request in requests:
            sources = sorted(sid for sid, row in manifest.items() if row["paper_id"] in request["paper_ids"])
            terms = query_terms(request["query"], limit=32)
            query = boolean_query([terms]) if terms else request["query"]
            signature = json.dumps([[(sid, manifest[sid]["source_lineage_hash"]) for sid in sources],
                                    query], ensure_ascii=False, sort_keys=True)
            if signature not in self.cache:
                # A sentence is AND-ed by PostgreSQL websearch; it would often
                # require the reviewer's wording to occur verbatim in a paper.
                # Reuse the portable OR-group query contract within owned papers.
                self.cache[signature] = self.service.library_index.retrieve(
                    self.principal, query, allowed_papers=sources, top_k=3, term_groups=[terms] if terms else None,
                    per_paper_limit=3, include_neighbors=True, use_semantic=False) if sources else []
            for hit in self.cache[signature]:
                raw = asdict(hit)
                sid = raw["paper_id"]
                if sid not in sources or raw["source_lineage_hash"] != manifest[sid]["source_lineage_hash"]:
                    raise WorkflowValidationError("Retrieved supplement source escaped the prepared source scope.")
                key = evidence_key(sid, raw["chunk_id"], raw["source_lineage_hash"])
                if key in existing or key in found:
                    continue
                found[key] = {**raw, "source_file_id": sid, "paper_id": manifest[sid]["paper_id"], "evidence_key": key}
                if len(found) == MAX_HITS:
                    break
            if len(found) == MAX_HITS:
                break
        evidence = self._checked_hits(section_id, list(found.values()),
                                      paper_ids={pid for request in requests for pid in request["paper_ids"]})
        self.trusted[section_id] = evidence
        return {"status": "ok", "evidence": evidence, "source_manifest": deepcopy(manifest)}

    def process_mailbox(self):
        if not self.enabled:
            return
        for path in sorted(self.directory.glob("*.request.json")):
            rid = path.name.removesuffix(".request.json")
            if not re.fullmatch(r"[a-f0-9]{64}", rid):
                continue
            output = self.directory / f"{rid}.response.json"
            if rid in self.responses:
                continue
            try:
                if path.stat().st_size > 128_000:
                    raise WorkflowValidationError("Oversized supplement request.")
                request = json.loads(path.read_text(encoding="utf-8"))
                sid = str(request.get("section_id") or "")
                if request.get("request_id") != rid or self._request_id(sid, request.get("requests")) != rid:
                    raise WorkflowValidationError("The supplement request identity changed.")
                self._mark_used(sid, rid)
                # Recheck persisted results; a cached file never bypasses source validation.
                if output.is_file():
                    response = json.loads(output.read_text(encoding="utf-8"))
                    self._requests(sid, request.get("requests"))
                    if response.get("request_id") != rid:
                        raise WorkflowValidationError("Supplement response identity mismatch.")
                    if response.get("status") == "ok":
                        response["evidence"] = self._checked_hits(sid, response.get("evidence"),
                            paper_ids={pid for row in request["requests"] for pid in row["paper_ids"]})
                        self.trusted[sid] = response["evidence"]
                else:
                    response = {"request_id": rid, **self.retrieve(sid, request.get("requests"))}
            except Exception as exc:
                response = {"request_id": rid, "status": "unavailable", "evidence": [],
                            "error": f"Local evidence supplement unavailable: {type(exc).__name__}"}
            # The CLI must not consume a persisted response before this worker
            # has rechecked its exact source passages against the frozen scope.
            response["validation_nonce"] = self.validation_nonce
            atomic_write_json(output, response)
            self.responses[rid] = response

    def publication_payload(self, checkpoint):
        """A validated copy for resume/publication; original model inputs stay frozen."""
        result = deepcopy(self.payload)
        if not self.enabled:
            return result
        for sid, entry in (checkpoint.get("authoring_states") or {}).items():
            supplement = (entry.get("state") or {}).get("evidence_supplement") or {}
            self._restore(sid, supplement)
        for sid, entry in (checkpoint.get("entries") or {}).items():
            self._restore(sid, ((entry.get("synthesis") or {}).get("source_review") or {}).get("evidence_supplement") or {})
        package = result.setdefault("evidence_package", {})
        registry = {row["evidence_key"]: row for row in package.get("evidence_registry") or []}
        for index, section in enumerate(package.get("sections") or []):
            evidence = self.trusted.get(str(section.get("section_id")), [])
            for hit in evidence:
                registry.setdefault(hit["evidence_key"], hit)
            package["sections"][index] = merge_supplement_evidence(section, evidence)
        package["evidence_registry"] = list(registry.values())
        return result

    def _restore(self, section_id, supplement):
        if not supplement or supplement.get("status") not in {"ok", "unavailable"}:
            return
        manifest, _ = self._requests(section_id, supplement.get("requests"))
        self._mark_used(section_id, self._request_id(section_id, supplement["requests"]))
        if supplement.get("status") == "unavailable":
            return
        if supplement.get("source_manifest") != manifest:
            raise WorkflowValidationError("Supplement checkpoint does not match its original source manifest.")
        self.trusted[section_id] = self._checked_hits(section_id, supplement.get("evidence"),
            paper_ids={pid for row in supplement["requests"] for pid in row["paper_ids"]})
