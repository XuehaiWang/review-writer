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
