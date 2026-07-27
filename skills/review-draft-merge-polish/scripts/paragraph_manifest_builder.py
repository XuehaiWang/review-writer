from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


PARAGRAPH_ID_RE = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)
REFERENCES_RE = re.compile(
    r"^#{1,6}\s+(?:references|bibliography)\s*$", re.IGNORECASE | re.MULTILINE
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def split_body_and_references(markdown: str) -> tuple[str, str]:
    match = REFERENCES_RE.search(markdown)
    if not match:
        return markdown, ""
    return markdown[: match.start()], markdown[match.start() :]


def _section_id(heading: str) -> str:
    match = re.match(r"\s*(\d+)", heading)
    return f"sec{match.group(1)}" if match else "sec0"


def _numbered_sections(body: str) -> list[tuple[int, int, str, str]]:
    headings = list(HEADING_RE.finditer(body))
    sections: list[tuple[int, int, str, str]] = []
    for index, heading in enumerate(headings):
        section_id = _section_id(heading.group(2))
        if section_id == "sec0":
            continue
        next_start = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections.append((heading.end(), next_start, section_id, heading.group(2)))
    return sections


def _inject_section_markers(text: str, section_id: str) -> tuple[str, list[str], bool]:
    existing_ids = PARAGRAPH_ID_RE.findall(text)
    if existing_ids:
        return text, existing_ids, False
    pieces = re.split(r"(\n\s*\n)", text)
    result: list[str] = []
    paragraph_ids: list[str] = []
    counter = 0
    changed = False
    for piece in pieces:
        stripped = piece.strip()
        result.append(piece)
        if not stripped or piece.lstrip().startswith("<!--"):
            continue
        if PARAGRAPH_ID_RE.search(piece):
            paragraph_ids.extend(PARAGRAPH_ID_RE.findall(piece))
            continue
        if stripped.startswith("#") or stripped.startswith("!") or stripped.startswith("|"):
            continue
        counter += 1
        paragraph_id = f"{section_id}-p{counter}"
        paragraph_ids.append(paragraph_id)
        result.append(f"\n\n<!-- paragraph_id: {paragraph_id} -->")
        changed = True
    return "".join(result), paragraph_ids, changed


def build_manifest(review_root: Path, project_id: str) -> tuple[dict[str, Any], bool]:
    stage_dir = Path(review_root) / "review-projects" / project_id / "04_first_draft"
    draft_path = stage_dir / "first_draft.md"
    raw = draft_path.read_text(encoding="utf-8")
    body, references = split_body_and_references(raw)
    sections = _numbered_sections(body)
    rebuilt: list[str] = []
    position = 0
    manifest_sections: list[dict[str, Any]] = []
    changed = False

    for start, end, section_id, heading in sections:
        rebuilt.append(body[position:start])
        section_text, paragraph_ids, section_changed = _inject_section_markers(
            body[start:end], section_id
        )
        rebuilt.append(section_text)
        changed = changed or section_changed
        manifest_sections.append(
            {
                "section_id": section_id,
                "heading": heading,
                "paragraph_count": len(paragraph_ids),
                "paragraphs": [
                    {"paragraph_id": paragraph_id} for paragraph_id in paragraph_ids
                ],
            }
        )
        position = end
    rebuilt.append(body[position:])
    if changed:
        draft_path.write_text("".join(rebuilt) + references, encoding="utf-8")

    manifest = {
        "project_id": project_id,
        "paragraph_count": sum(section["paragraph_count"] for section in manifest_sections),
        "sections": manifest_sections,
    }
    _write_json(stage_dir / "paragraph_manifest.json", manifest)
    return manifest, changed
