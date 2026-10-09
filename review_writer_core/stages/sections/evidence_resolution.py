"""Explicit incomplete evidence outcomes, shared by generation and publication."""
import hashlib
import json
from copy import deepcopy

CONTRACT = "section-evidence-resolution/1"
BASE_HIT_KEYS = "supplement_base_hit_keys"


def merge_supplement_evidence(package, evidence):
    """Append host-validated sources without changing the frozen base identity.

    This is not source validation: API callers must resolve supplemental rows
    against their owned, frozen source manifest before calling this helper.
    Keep base keys so old paid outputs can be checked against the exact base
    projection, not a blindly trusted saved hash.
    """
    result = deepcopy(package)
    hits = result.setdefault("hits", [])
    existing = {row["evidence_key"] for row in hits}
    added = []
    for row in evidence:
        key = row["evidence_key"]
        if key not in existing:
            added.append(deepcopy(row))
            existing.add(key)
    if added:
        result.setdefault(BASE_HIT_KEYS, [row["evidence_key"] for row in hits])
        result["hits"] = hits + added
    return result


def _matches_evidence_fingerprint(recorded, package):
    if recorded == evidence_fingerprint(package):
        return True
    keys = package.get(BASE_HIT_KEYS)
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
        return False
    hits = package.get("hits") or []
    by_key = {row.get("evidence_key"): row for row in hits}
    if len(by_key) != len(hits) or any(key not in by_key for key in keys):
        return False
    base = {key: value for key, value in package.items() if key != BASE_HIT_KEYS}
    base["hits"] = [by_key[key] for key in keys]
    return recorded == evidence_fingerprint(base)


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
            and _matches_evidence_fingerprint(record.get("evidence_fingerprint"), package)
            and output.get("generation_mode") == record["status"]
            and bool(record.get("reason")))


def valid_pending_output(output, package):
    return (has_evidence_resolution(output, package)
            and output["generation_mode"] == "pending_evidence"
            and output.get("paragraphs") == [] and not output.get("overview")
            and str(output.get("draft_md") or "").strip() == pending_markdown(
                output.get("section_id"), output.get("heading")).strip())
