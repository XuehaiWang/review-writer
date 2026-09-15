"""Bounded, extractive conversation context. No model, vector store, or new truth source."""
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _excerpt(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit] + " … [excerpt]"


def conversation_memory(records, previous=None, *, recent_budget=12000, summary_budget=6000):
    """Keep recent turns verbatim within budget; cache older excerpts by content AND decisions.

    The returned snapshot lives inside the existing job payload. An acceptance or
    rejection changes its digest, so old summaries never silently retain old decisions.
    """
    records = list(records)
    split, size = len(records), 0
    while split and len(records) - split < 6:
        length = len(json.dumps(records[split - 1], ensure_ascii=False))
        if size + length > recent_budget:
            break
        split -= 1
        size += length
    older, recent = records[:split], records[split:]
    fingerprint = _digest(older)
    cached = (previous or {}).get("summary", {})
    if cached.get("source_sha256") == fingerprint and cached.get("budget") == summary_budget:
        summary = cached
    else:
        excerpts, used = [], 0
        for record in reversed(older):
            item = {"id": record["id"], "user_excerpt": _excerpt(record.get("user"), 500),
                "responses": [{"paragraph_id": r.get("paragraph_id", ""),
                    "assistant_excerpt": _excerpt(r.get("assistant"), 400),
                    "candidate_excerpt": _excerpt(r.get("candidate_text"), 500),
                    "decision": r.get("decision", ""), "matches_saved_text": r.get("matches_saved_text", False)}
                    for r in record.get("responses", [])]}
            # Large chapter turns cannot displace every other historical request.
            available = summary_budget - used
            while item["responses"] and len(json.dumps(item, ensure_ascii=False)) > min(2500, available):
                item["responses"].pop()
                item["responses_omitted"] = len(record.get("responses", [])) - len(item["responses"])
            length = len(json.dumps(item, ensure_ascii=False))
            if length > available:
                break
            excerpts.append(item)
            used += length
        summary = {"kind": "extractive", "source_sha256": fingerprint, "budget": summary_budget,
            "through_id": older[-1]["id"] if older else "", "source_count": len(older),
            "omitted_turn_count": len(older) - len(excerpts), "turns": list(reversed(excerpts))}
    return {"summary": summary, "recent_turns": recent,
        "record_count": len(records), "policy": (
            "Conversation is context, NOT scientific evidence. Current saved text and the explicitly selected "
            "pending candidate are the only editing bases. Latest explicit user instructions take precedence; "
            "old temporary requests are not permanent preferences. Accepted means adopted at that time, not "
            "necessarily still current. Rejected means not adopted; do not invent a reason or repeat it "
            "without a new request or evidence. Excerpts may be incomplete; never infer omitted details.")}


def candidate_response(candidate, current_hash):
    decision = candidate.get("status", "")
    if decision == "pending" and candidate.get("base_text_sha256") != current_hash:
        decision = "stale"
    return {"paragraph_id": candidate.get("paragraph_id", ""), "assistant": candidate.get("reply", ""),
        "candidate_text": candidate.get("candidate_text") or "",
        "candidate_id": candidate.get("candidate_id", ""), "decision": decision,
        "matches_saved_text": bool(candidate.get("candidate_text")) and
            hashlib.sha256(candidate["candidate_text"].encode()).hexdigest() == current_hash}
