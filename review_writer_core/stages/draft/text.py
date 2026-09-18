"""Pure paragraph-marker operations shared by draft workflows."""

from __future__ import annotations

import hashlib
import re
from typing import Any


PARAGRAPH_MARKER = re.compile(
    r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->"
)


def paragraph_spans(markdown: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for marker in PARAGRAPH_MARKER.finditer(markdown or ""):
        prefix = markdown[: marker.start()].rstrip()
        start = prefix.rfind("\n\n") + 2
        text = prefix[start:].strip()
        if not text or text.startswith(("#", "![", "<!--")):
            continue
        rows.append(
            {
                "paragraph_id": marker.group(1),
                "text": text,
                "start": start,
                "end": len(prefix),
                "marker_end": marker.end(),
            }
        )
    return rows


def normalize_draft_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_draft_text(text).encode("utf-8")).hexdigest()


def apply_rewrite_overlays(
    markdown: str, overlays: dict[str, Any]
) -> tuple[str, dict[str, list[str]]]:
    entries = overlays.get("entries") if isinstance(overlays, dict) else {}
    if not isinstance(entries, dict) or not entries:
        return markdown, {"applied": [], "conflicts": []}
    updated = markdown
    applied: list[str] = []
    conflicts: list[str] = []
    for raw_id, raw_entry in entries.items():
        paragraph_id = str(raw_id)
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        paragraph = next(
            (
                row
                for row in paragraph_spans(updated)
                if row["paragraph_id"] == paragraph_id
            ),
            None,
        )
        rewritten = str(entry.get("rewritten_text") or "").strip()
        if (
            paragraph is None
            or not rewritten
            or text_sha256(str(paragraph["text"]))
            != str(entry.get("source_text_sha256") or "")
        ):
            conflicts.append(paragraph_id)
            continue
        updated = updated[: paragraph["start"]] + rewritten + updated[paragraph["end"] :]
        applied.append(paragraph_id)
    return updated, {"applied": applied, "conflicts": conflicts}
