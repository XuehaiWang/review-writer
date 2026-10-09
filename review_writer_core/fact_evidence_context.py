"""Budgeted source selection; no scientific inference and no provider calls."""
from collections import deque
from copy import deepcopy
import json
import re

CONTEXT_VERSION = "role-balanced-windows/2"


def _candidate_order(row):
    # Contribution samples have an explicit reading purpose. Within the same
    # purpose, a source key breaks ties without relying on retrieval/set order.
    return (
        not row.get('opening_candidate', False),
        "primary_contribution_sample" not in (row.get("retrieval_passes") or []),
        str(row["evidence_key"]),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def select_fact_evidence_context(candidates, *, roles=(), max_chars=48000, max_items=24,
                                 per_source_chars=2400, support_spans=()):
    """Round-robin question coverage, with abstract context considered last.

    Source windows remain contiguous slices of normalized source text. Never
    append ellipses to evidence or concatenate different registered chunks.
    """
    # A sequence expresses Topic priority; a set only expresses membership.
    roles = tuple(dict.fromkeys(sorted(roles) if isinstance(roles, (set, frozenset)) else roles))
    role_order = {role: index for index, role in enumerate(roles)}

    def ordered_questions(values):
        return sorted(set(values), key=lambda role: (role_order.get(role, len(roles)), role))

    required = {}
    for span in support_spans:
        quote = " ".join(str(span.get("support_excerpt") or span.get("quote") or "").split())
        if quote:
            required.setdefault(span.get("evidence_key"), []).append(quote)
    normalized = []
    for row in candidates:
        key = row.get("evidence_key")
        if not key or not row.get("content"):
            continue
        row = deepcopy(row)
        if "question_ids" in row:
            row["question_ids"] = ordered_questions(row.get("question_ids") or [])
        if "retrieval_passes" in row:
            row["retrieval_passes"] = sorted(set(row.get("retrieval_passes") or []))
        normalized.append(row)
    unique = {}
    for row in sorted(normalized, key=_candidate_order):
        key = row["evidence_key"]
        if key not in unique:
            unique[key] = row
        else:
            unique[key]["question_ids"] = ordered_questions([
                *unique[key].get("question_ids", []), *row.get("question_ids", [])])
            if "retrieval_passes" in row:
                unique[key]["retrieval_passes"] = sorted(set([
                    *unique[key].get("retrieval_passes", []), *row["retrieval_passes"]]))
    queues = {}
    for row in unique.values():
        groups = [r for r in row.get("question_ids", []) if r in roles]
        if row.get("content_type") == "abstract":
            groups = ["__abstract"]
        for group in groups or ["__context"]:
            queues.setdefault(group, deque()).append(row)
    # Topic-priority roles precede incidental candidate ordering. Other roles
    # still get a turn within the same fixed source-window budget.
    order = ["__context"] if "__context" in queues else []
    order += [role for role in roles if role in queues and role not in order]
    order += [key for key in queues if key not in order and key != "__abstract"]
    if "__abstract" in queues:
        order.append("__abstract")
    selected, used, seen = [], 0, set()
    while order and len(selected) < max_items:
        progress = False
        for group in order:
            q = queues[group]
            while q and q[0]["evidence_key"] in seen:
                q.popleft()
            if not q:
                continue
            row = q.popleft()
            key = row["evidence_key"]
            seen.add(key)
            text = " ".join(str(row["content"]).split())
            limit = min(per_source_chars, max_chars - used)
            if limit <= 0:
                continue
            start = 0
            ranges = [(text.find(quote), len(quote)) for quote in required.get(key, []) if quote in text]
            if ranges:
                first, last = min(p for p, _ in ranges), max(p + n for p, n in ranges)
                if last - first <= limit:
                    start = max(0, last - limit)
                    # Keep enough leading context without cutting through a word.
                    if start and " " in text[start:first]:
                        start = text.index(" ", start, first) + 1
            window = text[start:start + limit]
            if len(text) > start + limit and not ranges:
                boundaries = list(re.finditer(r"[.!?。！？](?:\s|$)", window))
                if boundaries and boundaries[-1].end() >= limit // 2:
                    window = window[:boundaries[-1].end()].rstrip()
                else:
                    # Do not emit a tiny, detached tail of the total budget.
                    if limit < min(per_source_chars, len(text)):
                        continue
                    window = window.rsplit(" ", 1)[0] if " " in window else window
            row["content"] = window
            row["source_window"] = {"start": start, "end": start + len(window),
                                    "truncated": len(window) < len(text)}
            selected.append(row)
            used += len(window)
            progress = True
            if len(selected) >= max_items:
                break
        if not progress:
            break
    return selected
