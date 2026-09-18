"""Shared Overview layout for Draft preview and Final publication."""
import re
import json


def overview_block(artifact_id, editable):
    if not artifact_id:
        return ""
    caption = ". ".join(str(editable.get(key) or "").strip() for key in ("title", "subtitle")
                        if str(editable.get(key) or "").strip())
    labels = [str(value).strip() for value in editable.get("labels") or [] if str(value).strip()]
    if labels:
        caption = (caption + " — " if caption else "") + ", ".join(labels)
    caption = caption.rstrip(" .") + "." if caption else "Review overview."
    caption = re.sub(r"^(?:Figure|Fig\.)\s*\d+[.:]?\s*", "", caption, flags=re.I)
    return f"![Overview figure](/api/v1/artifacts/{artifact_id}/content)\n*Figure 1. {caption}*"


def compose_overview(markdown, artifact_id, editable):
    """Number the composed document, leaving the saved drafting source untouched.

    Call only on the canonical draft, not a previously composed preview. This
    keeps regeneration from incrementing labels again and preserves source IDs.
    """
    if not artifact_id:
        return markdown
    reference = re.search(r"(?im)^#{1,6}\s+(?:\d+[.)]?\s*)?(?:references|bibliography|参考文献)\s*$", markdown)
    body, references = (markdown[:reference.start()], markdown[reference.start():]) if reference else (markdown, "")
    numbers = set(re.findall(r"(?im)^\s*\*{0,2}Figure\s+(\d+)\.", body))
    markers = re.compile(r"(<!--\s*inserted_figure:\s*)(\{.*?\})(\s*-->)", re.S)
    for match in markers.finditer(body):
        try:
            label = json.loads(match.group(2)).get("published_label", "")
        except (ValueError, AttributeError):
            continue
        number = re.fullmatch(r"Figure\s+(\d+)", str(label))
        if number:
            numbers.add(number.group(1))
    mapping = {number: str(int(number) + 1) for number in numbers}

    def visible(part):
        # One pass avoids cascading Figure 1 -> 2 -> 3. Include grouped callouts.
        return re.sub(r"\b(Figures?\s+|Figs?\.\s*)(\d+[a-z]?(?:\s*(?:,|and|[-–—])\s*\d+[a-z]?)*)(?!\w)",
                      lambda m: m.group(1) + re.sub(r"\d+", lambda n: mapping.get(n.group(), n.group()), m.group(2)), part)

    parts = re.split(r"(<!--.*?-->)", body, flags=re.S)
    for index, part in enumerate(parts):
        if not part.startswith("<!--"):
            parts[index] = visible(part)
            continue
        match = markers.fullmatch(part)
        if not match:
            continue
        try:
            metadata = json.loads(match.group(2))
            if isinstance(metadata, dict):
                metadata["published_label"] = visible(str(metadata.get("published_label") or ""))
                parts[index] = match.group(1) + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + match.group(3)
        except ValueError:
            pass
    return insert_before_introduction("".join(parts) + references, overview_block(artifact_id, editable))


def insert_before_introduction(markdown, block):
    body, block = str(markdown or "").rstrip(), str(block or "").strip()
    if not block:
        return body
    headings = list(re.finditer(r"(?m)^\s*(#{1,6})\s+(.+?)\s*$", body))
    insertion = None
    for match in headings:
        title = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", match.group(2)).strip().casefold()
        if any(title == name or any(title.startswith(name + sep) for sep in (" ", ":", "：", "与", "和"))
               for name in ("introduction", "background", "引言", "绪论", "研究背景")):
            insertion = match.start()
            break
    if insertion is None:
        insertion = headings[0].end() if headings and len(headings[0].group(1)) == 1 else 0
    return "\n\n".join(value for value in (body[:insertion].rstrip(), block, body[insertion:].lstrip()) if value)
