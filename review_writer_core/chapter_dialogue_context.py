"""Bounded manuscript locators and read-only cross-chapter context."""
from review_writer_core.paragraph_revision import exact_hash


def chapter_outline(sections, selected_id, budget=14000):
    """Sample each chapter fairly, prioritizing its opening and ending paragraphs."""
    outline, queues = [], []
    for section in sections:
        paragraphs = section["paragraphs"] if section["section_id"] != selected_id else []
        entry = {"section_id": section["section_id"], "title": section["title"],
                 "paragraph_count": len(section["paragraphs"]), "locators": []}
        outline.append(entry)
        order = list(dict.fromkeys([0, len(paragraphs)-1, *range(1, len(paragraphs)-1)])) if paragraphs else []
        queues.append([(entry, i, paragraphs[i]) for i in order])
    while any(queues):
        for queue in queues:
            if not queue:
                continue
            entry, position, paragraph = queue.pop(0)
            text = paragraph["text"].strip()
            excerpt = text if len(text) <= 240 else text[:160] + " … " + text[-80:]
            cost = len(excerpt) + len(paragraph["paragraph_id"]) + 80
            if cost > budget:
                return outline
            budget -= cost
            entry["locators"].append({"paragraph_id": paragraph["paragraph_id"],
                "position": position + 1, "excerpt": excerpt})
    return outline


def normalize_related(related, section_context):
    local = {p["paragraph_id"] for p in section_context.get("paragraphs", [])}
    foreign = {p["paragraph_id"] for s in section_context.get("outline", []) for p in s.get("locators", [])}
    result, seen, cross_count = [], set(), 0
    for item in related if isinstance(related, list) else []:
        if not isinstance(item, dict):
            continue
        pid, reason = item.get("paragraph_id"), str(item.get("reason") or "").strip()
        if not isinstance(pid, str) or pid in seen or pid not in local | foreign or not reason:
            continue
        if pid not in local:
            if cross_count >= 6:
                continue
            cross_count += 1
        seen.add(pid)
        result.append({"paragraph_id": pid, "reason": reason[:500]})
    return result


def read_cross_chapter(sections, selected_id, related, budget=18000):
    indexed = {p["paragraph_id"]: (s, p) for s in sections if s["section_id"] != selected_id
               for p in s["paragraphs"]}
    passages, hashes, omitted = [], {}, []
    for item in related:
        pid = item["paragraph_id"]
        if pid not in indexed or pid in hashes:
            continue
        section, paragraph = indexed[pid]
        text = paragraph["text"]
        if len(text) > budget or len(passages) >= 6:
            omitted.append(pid)
            continue
        budget -= len(text)
        passages.append({"section_id": section["section_id"], "title": section["title"],
                         "paragraph_id": pid, "text": text, "reason": item["reason"]})
        hashes[pid] = exact_hash(text)
    return {"read_only": True, "paragraphs": passages, "omitted_paragraph_ids": omitted}, hashes
