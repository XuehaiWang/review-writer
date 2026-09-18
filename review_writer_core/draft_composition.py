"""Section-level composition shared by Draft creation, candidates and Final assembly."""
import hashlib
import re

ROLES = {"abstract": ("Abstract", "摘要"), "conclusion": ("Conclusion", "Conclusions", "Conclusions and Outlook", "Conclusion and Outlook", "Conclusion / Challenges / Insights", "Conclusions / Challenges / Insights", "结论", "结论与展望", "总结与展望", "总结")}


def manuscript_fields(text):
    title = re.search(r"(?m)^#[ \t]+([^\r\n]+)", text)
    keywords = re.search(r"(?im)^\*\*(?:Keywords|关键词):\*\*([^\r\n]*)", text)
    return {"title": title.group(1).strip() if title else "",
            "keywords": [v.strip() for v in re.split(r"[,;，；]", keywords.group(1)) if v.strip()] if keywords else []}


def replace_manuscript_fields(text, title, keywords):
    title = " ".join(title.split())
    if not title:
        raise ValueError("Manuscript title is required")
    text, count = re.subn(r"(?m)^#[ \t]+[^\r\n]+", lambda _: "# "+title, text, count=1)
    if not count:
        text = "# "+title+"\n\n"+text
    line = "**Keywords:** "+", ".join(" ".join(v.split()) for v in keywords if v.strip())
    text, count = re.subn(r"(?im)^\*\*(?:Keywords|关键词):\*\*[^\r\n]*", lambda _: line, text, count=1)
    if not count:
        span = section_span(text, "abstract")
        at = span[1] if span else text.index("\n")+1
        text = text[:at].rstrip()+"\n\n"+line+"\n\n"+text[at:]
    return text


def signature(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_span(text, role):
    """Stable role marker first; heading aliases only for pre-migration manuscripts."""
    headings = list(re.finditer(r"(?m)^(#{2,6})[ \t]+([^\r\n]+?)[ \t]*\r?$", text))
    marked = [i for i, h in enumerate(headings) if re.search(rf"<!-- draft_role: {role} -->\s*$", text[max(0, h.start()-100):h.start()])]
    for index, heading in enumerate(headings):
        prefix = text[max(0, heading.start()-100):heading.start()]
        title = re.sub(r"^\d+(?:\.\d+)*[.)]?[ \t]*", "", heading.group(2)).strip().casefold()
        named = title in {v.casefold() for v in ROLES[role]}
        if (marked and index != marked[0]) or (not marked and not named):
            continue
        end = next((h.start() for h in headings[index+1:] if len(h.group(1)) <= len(heading.group(1))), len(text))
        # A following role marker belongs to the next section.
        tail = re.search(r"<!-- draft_role: \w+ -->\s*$", text[heading.end():end])
        if tail:
            end = heading.end()+tail.start()
        if role == "abstract":
            keywords = re.search(r"(?im)^\*\*(?:Keywords|关键词):\*\*", text[heading.end():end])
            if keywords:
                end = heading.end()+keywords.start()
        marker = re.search(rf"<!-- draft_role: {role} -->\s*$", prefix)
        start = heading.start() - (len(prefix)-marker.start()) if marker else heading.start()
        return start, end
    return None


def section_text(text, role):
    span = section_span(text, role)
    return text[span[0]:span[1]].strip() if span else ""


def body_source(text, *, include_conclusion=False):
    for role in (["abstract"] if include_conclusion else ["abstract", "conclusion"]):
        span = section_span(text, role)
        if span:
            text = text[:span[0]] + text[span[1]:]
    text = re.sub(r"(?im)^\*\*(?:Keywords|关键词):\*\*[^\n]*\n?", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()


def source_signature(text, role):
    return signature(body_source(text, include_conclusion=role == "abstract"))


def add_keywords(text, keywords):
    if not keywords or re.search(r"(?im)^(?:\*\*)?(?:Keywords|关键词)[:：]", text):
        return text
    values = list(dict.fromkeys(str(k).replace("\n", " ").strip() for k in keywords if str(k).strip()))[:8]
    span = section_span(text, "abstract")
    at = span[1] if span else 0
    return text[:at].rstrip()+"\n\n**Keywords:** "+", ".join(values)+"\n\n"+text[at:]


def replace_section(text, role, content, *, identity=""):
    """Replace an entire typed section; no positional paragraph matching."""
    if role not in ROLES or not content.strip():
        raise ValueError("A supported section and non-empty content are required")
    clean = re.sub(r"<!--.*?-->", "", content, flags=re.S).strip()
    clean = re.sub(r"(?m)^#{1,6}\s+[^\n]+\n?", "", clean).strip()
    blocks = [p.strip() for p in re.split(r"\n\s*\n", clean) if p.strip()]
    if not blocks:
        raise ValueError("Section response contains no prose")
    prefix = "SABS" if role == "abstract" else "SCON"
    # Replacement paragraphs have new identities, so old paragraph candidates cannot attach.
    prefix += "_" + (identity or signature(clean))[:12]
    replacement = f"<!-- draft_role: {role} -->\n## {ROLES[role][0]}\n\n" + "\n\n".join(
        f"{p}\n\n<!-- paragraph_id: {prefix}-p{i+1} -->" for i, p in enumerate(blocks)) + "\n\n"
    span = section_span(text, role)
    if span:
        return (text[:span[0]] + replacement + text[span[1]:]).rstrip()+"\n"
    if role == "abstract":
        title = re.search(r"(?m)^#\s+[^\n]+\n", text)
        at = title.end() if title else 0
    else:
        references = re.search(r"(?im)^#{1,6}\s+(?:references|bibliography|参考文献)\s*$", text)
        at = references.start() if references else len(text)
    return (text[:at].rstrip()+"\n\n"+replacement+text[at:]).lstrip().rstrip()+"\n"
