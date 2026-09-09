"""Pure Blueprint version-mapping helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _blueprint_restructure_record(
    previous: dict[str, Any] | None,
    current_sections: list[dict[str, Any]],
    *,
    previous_artifact_id: str = "",
    trigger_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Describe a structure change without erasing the prior Blueprint.

    Section IDs are positional and can change during regrouping, so the map is
    based on paper overlap first and normalized headings second.  The record is
    intentionally stored inside the new version for audit and rollback UX.
    """

    old_sections = [
        item
        for item in (previous or {}).get("sections") or []
        if isinstance(item, dict)
    ]

    def paper_ids(section: dict[str, Any]) -> set[str]:
        return {
            str(item)
            for item in (
                section.get("primary_papers")
                or section.get("major_papers")
                or section.get("paper_ids")
                or []
            )
            if str(item).strip()
        }

    def heading(section: dict[str, Any]) -> str:
        return re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+",
            " ",
            str(section.get("title") or "").casefold(),
        ).strip()

    mappings: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for old in old_sections:
        old_id = str(old.get("section_id") or "")
        old_papers = paper_ids(old)
        candidates: list[tuple[float, dict[str, Any]]] = []
        for new in current_sections:
            new_papers = paper_ids(new)
            union = old_papers | new_papers
            overlap = len(old_papers & new_papers) / len(union) if union else 0.0
            if heading(old) and heading(old) == heading(new):
                overlap = max(overlap, 1.0)
            candidates.append((overlap, new))
        score, target = max(candidates, key=lambda item: item[0], default=(0.0, {}))
        target_id = str(target.get("section_id") or "") if score > 0 else ""
        if target_id:
            used_targets.add(target_id)
        mappings.append(
            {
                "previous_section_id": old_id,
                "previous_title": str(old.get("title") or ""),
                "current_section_id": target_id or None,
                "current_title": str(target.get("title") or "") if target_id else None,
                "paper_overlap": round(score, 4),
                "migration_action": "reuse_and_revalidate" if target_id else "retire",
            }
        )
    for new in current_sections:
        new_id = str(new.get("section_id") or "")
        if new_id and new_id not in used_targets:
            mappings.append(
                {
                    "previous_section_id": None,
                    "previous_title": None,
                    "current_section_id": new_id,
                    "current_title": str(new.get("title") or ""),
                    "paper_overlap": 0.0,
                    "migration_action": "generate_new",
                }
            )

    old_signature = [
        (heading(item), sorted(paper_ids(item)), str(item.get("section_role") or "body"))
        for item in old_sections
    ]
    new_signature = [
        (heading(item), sorted(paper_ids(item)), str(item.get("section_role") or "body"))
        for item in current_sections
    ]
    return {
        "is_restructure": bool(old_sections) and old_signature != new_signature,
        "previous_blueprint_artifact_id": previous_artifact_id or None,
        "trigger_reasons": list(dict.fromkeys(trigger_reasons or [])),
        "section_mapping": mappings,
        "rollback_supported": bool(previous_artifact_id),
        "created_at": utc_now().isoformat(),
    }
