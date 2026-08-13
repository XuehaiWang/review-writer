from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "review_writer_core").is_dir()),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.paragraph_markers import (  # noqa: E402
    build_paragraph_manifest,
    ensure_prose_paragraph_markers,
)

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
    rebuilt, marker_report = ensure_prose_paragraph_markers(raw)
    changed = bool(marker_report.get("changed"))
    if changed:
        temporary = draft_path.with_suffix(".md.markers.tmp")
        temporary.write_text(rebuilt, encoding="utf-8")
        temporary.replace(draft_path)

    manifest = build_paragraph_manifest(rebuilt, project_id)
    manifest["marker_report"] = marker_report
    _write_json(stage_dir / "paragraph_manifest.json", manifest)
    return manifest, changed
