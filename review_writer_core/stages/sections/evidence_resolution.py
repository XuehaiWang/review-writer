"""Explicit incomplete evidence outcomes, shared by generation and publication."""
import hashlib
import json

CONTRACT = "section-evidence-resolution/1"


def evidence_fingerprint(package):
    return hashlib.sha256(json.dumps(package, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pending_markdown(section_id, heading):
    return (f"## {heading}\n\n"
            "> 待补充 / Evidence pending: 本章尚无可发布的证据支持正文。可继续编辑其他章节；"
            "此提示不是综述结论，不代表文献中不存在相关研究。\n\n"
            f"<!-- review-writer:pending-evidence:{section_id} -->\n")


def resolution_record(package, *, pending, reason):
    return {"contract": CONTRACT, "status": "pending_evidence" if pending else "limited_evidence",
            "evidence_fingerprint": evidence_fingerprint(package), "reason": str(reason)[:1200],
            "requires_user_action_to_continue": False}


def has_evidence_resolution(output, package):
    record = output.get("evidence_resolution") or {}
    return (record.get("contract") == CONTRACT
            and record.get("status") in {"pending_evidence", "limited_evidence"}
            and record.get("evidence_fingerprint") == evidence_fingerprint(package)
            and output.get("generation_mode") == record["status"]
            and bool(record.get("reason")))


def valid_pending_output(output, package):
    return (has_evidence_resolution(output, package)
            and output["generation_mode"] == "pending_evidence"
            and output.get("paragraphs") == [] and not output.get("overview")
            and str(output.get("draft_md") or "").strip() == pending_markdown(
                output.get("section_id"), output.get("heading")).strip())
