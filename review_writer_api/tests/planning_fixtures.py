"""Offline model doubles for exercising the real planning and artifact contracts."""
from copy import deepcopy
import hashlib

from review_writer_core.scientific_facts import FACT_VALIDATION_VERSION, review_fingerprint
from review_writer_core.stages.planning.academic_planning import enhance_blueprint
from review_writer_core.stages.planning.matrix import refresh_matrix_fact_summary


def seed_verified_matrix(service, principal, project_id):
    matrix, artifact = service._matrix(principal, project_id)
    for row in matrix["rows"]:
        pid = row["paper_id"]
        quote = str(row.get("abstract") or "")
        fact = {"fact_id": "F-" + pid, "paper_id": pid, "field_id": "object_input", "value": quote,
                "source_channel": "abstract", "support_level": "abstract_limited", "support_excerpt": quote,
                "epistemic_status": "direct_source_report", "assertion_ceiling": "abstract_report_only",
                "validation_contract": FACT_VALIDATION_VERSION,
                "evidence_refs": [{"paper_id": pid, "source_file_id": pid, "chunk_id": "abstract",
                    "source_lineage_hash": "abstract-fixture-" + pid,
                    "evidence_key": "sha256:" + hashlib.sha256((pid + quote).encode()).hexdigest(), "support_excerpt": quote}]}
        fact["verification"] = {"status": "supported", "contract": FACT_VALIDATION_VERSION,
                                "input_fingerprint": review_fingerprint(fact), "reason": "Offline source fixture."}
        row["scientific_facts"] = [fact]
        row["fact_enrichment"] = {"status": "complete"}
    refresh_matrix_fact_summary(matrix)
    state = service.repository.get_stage_state(principal.user_id, project_id, "matrix")
    service._publish_matrix_update(principal, project_id, matrix, source_artifact_id=artifact.id,
        revision=state.revision, input_snapshot={"operation": "offline_test_facts"})


def offline_argument_planner(context, payload):
    checkpoint = deepcopy(payload.get("blueprint_checkpoint") or {})
    def model(prompt, *, label, **kwargs):
        if label == "blueprint-structure":
            sections = [{**section, "review_problem": section.get("review_problem") or "What does the selected evidence establish?"}
                        for section in payload["section_blueprint"]["sections"]]
            assigned = {pid for s in sections for pid in [*s.get("primary_papers", []), *s.get("supporting_papers", [])]}
            return {"sections": sections, "unused_papers": [{"paper_id": row["paper_id"],
                "reason_code": "out_of_scope", "reason": "Outside the explicitly saved test outline."}
                for row in payload["matrix_snapshot"]["rows"] if row["paper_id"] not in assigned]}
        sid = label.removeprefix("blueprint-plan-")
        section = next(s for s in payload["section_blueprint"]["sections"] if s["section_id"] == sid)
        return {"question": "What does the selected evidence establish?", "purpose": "Summarize reported contributions.",
            "questions_to_answer": ["What results are reported?"], "retrieval_directions": ["Reported results and experimental conditions"],
            "comparison_axes": [], "boundaries": [], "open_questions": []}
    return enhance_blueprint(payload, model_call=model, checkpoint=checkpoint, report=lambda *args: None)
