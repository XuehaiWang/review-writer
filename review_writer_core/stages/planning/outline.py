"""Pure parsing and rendering helpers for planning outlines."""

from __future__ import annotations

import re
from typing import Any

from review_writer_core.review_structure import (
    infer_section_role,
    sanitize_internal_section_title,
)


OUTLINE_STYLES: dict[str, dict[str, str]] = {
    "substrate": {
        "en": "Substrate-classified",
        "zh": "按底物分类",
        "axis": "substrate classes and scope",
        "tag_key": "substrate",
        "introduction": "define the review scope and explain why substrate class is the primary comparison axis",
    },
    "catalyst": {
        "en": "Catalyst and method-classified",
        "zh": "按催化剂与方法分类",
        "axis": "catalysts, methods, and operating principles",
        "tag_key": "catalyst_or_method",
        "introduction": "compare how catalysts or methods shape outcomes, evidence quality, and applicability",
    },
    "reaction": {
        "en": "Reaction-type-classified",
        "zh": "按反应类型分类",
        "axis": "transformation and mechanistic strategy",
        "tag_key": "reaction_type",
        "introduction": "organize the literature by transformation logic and mechanistic strategy",
    },
    "topic-guided": {
        "en": "Topic-guided hybrid",
        "zh": "按 Topic 要求组织",
        "axis": "the explicit organization instructions in the user topic",
        "tag_key": "reaction_type",
        "introduction": "define the review scope and explain the organization requested in the topic",
    },
}


def capitalize_outline_heading(value: Any) -> str:
    """Capitalize a heading without title-casing chemical names."""

    heading = str(value or "").strip()
    match = re.search(r"[A-Za-z]", heading)
    if not match:
        return heading
    index = match.start()
    return heading[:index] + heading[index].upper() + heading[index + 1 :]


def sanitize_outline_markdown_headings(markdown: Any) -> str:
    """Sanitize generated heading text while preserving outline metadata."""

    heading = re.compile(
        r"(?m)^(\s*#{1,6}\s+)(\d+(?:\.\d+)*[.)]?\s+)?(.+?)\s*$"
    )

    def replace(match: re.Match[str]) -> str:
        return (
            f"{match.group(1)}{match.group(2) or ''}"
            f"{sanitize_internal_section_title(match.group(3))}"
        )

    return heading.sub(replace, str(markdown or ""))


def normalize_outline_section_ids(markdown: str) -> tuple[str, list[dict[str, Any]]]:
    """Repair duplicate explicit IDs without changing the outline's other content.

    The first section keeps its ID. New IDs avoid *all* explicit IDs, including
    those belonging to later sections, so a repair cannot displace another one.
    """

    lines = str(markdown or "").splitlines(keepends=True)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_index, raw_line in enumerate(lines):
        line = raw_line.strip()
        heading = re.match(r"^(#{2,6})\s+(?:\d+(?:\.\d+)*[.)]?\s+)?(.+?)\s*$", line)
        if heading:
            current = {"index": len(sections) + 1, "title": heading.group(2).strip()}
            sections.append(current)
            continue
        if current is None:
            continue
        identity = re.fullmatch(r"<!-- section_id: (S[A-Za-z0-9_-]{1,64}) -->", line)
        if identity:
            # Like outline_sections, the last marker in a section is effective.
            current["section_id"] = identity.group(1)
            current["line_index"] = line_index

    reserved = {section["section_id"] for section in sections if section.get("section_id")}
    seen: set[str] = set()
    repairs: list[dict[str, Any]] = []
    serial = 1
    for section in sections:
        old_id = section.get("section_id")
        if not old_id:
            continue
        if old_id not in seen:
            seen.add(old_id)
            continue
        while f"S{serial:02d}" in reserved:
            serial += 1
        new_id = f"S{serial:02d}"
        reserved.add(new_id)
        seen.add(new_id)
        line_index = section["line_index"]
        lines[line_index] = re.sub(
            r"<!-- section_id: S[A-Za-z0-9_-]{1,64} -->",
            f"<!-- section_id: {new_id} -->",
            lines[line_index],
            count=1,
        )
        repairs.append(
            {"section_index": section["index"], "title": section["title"],
             "old_id": old_id, "new_id": new_id}
        )
    return "".join(lines), repairs


def outline_sections(markdown: str) -> list[dict[str, Any]]:
    markdown, _repairs = normalize_outline_section_ids(markdown)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        heading = re.match(r"^(#{2,6})\s+(?:\d+(?:\.\d+)*[.)]?\s+)?(.+?)\s*$", line)
        if heading:
            title = heading.group(2).strip()
            current = {
                "title": title,
                "heading_level": len(heading.group(1)),
                "paper_ids": [],
                "context_paper_ids": [],
                "excluded_papers": [],
                "section_role": infer_section_role(title),
                "purpose": "",
                "notes": "",
                "topic_partition": "",
                "boundary_rationale": "",
            }
            sections.append(current)
            continue
        if current is None:
            continue
        identity = re.fullmatch(r"<!-- section_id: (S[A-Za-z0-9_-]{1,64}) -->", line)
        if identity:
            current["section_id"] = identity.group(1)
            continue
        lowered = line.casefold()
        if lowered.startswith("section role:"):
            role = line.split(":", 1)[1].strip().casefold()
            if role in {"introduction", "body", "conclusion", "references"}:
                current["section_role"] = role
        elif lowered.startswith("assigned papers:"):
            current["paper_ids"] = _paper_id_list(line.split(":", 1)[1])
        elif re.match(r"^(?:context|contextual) papers:", line, re.I):
            current["context_paper_ids"] = _paper_id_list(line.split(":", 1)[1])
        else:
            exclusion = re.match(
                r"^(?:excluded papers?|排除论文)\s*[:：]\s*(.+?)\s*$", line, re.I
            )
            if exclusion:
                _append_exclusions(current, exclusion.group(1))
            elif lowered.startswith("purpose:"):
                current["purpose"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("notes:"):
                current["notes"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("topic partition:"):
                current["topic_partition"] = line.split(":", 1)[1].strip().rstrip(".。")
            elif lowered.startswith("boundary rationale:"):
                current["boundary_rationale"] = line.split(":", 1)[1].strip()
            elif line:
                current["notes"] = "\n".join(filter(None, [current["notes"], line]))
    used = {s["section_id"] for s in sections if s.get("section_id")}
    serial = 1
    parents = []
    for section in sections:
        if not section.get("section_id"):
            while f"S{serial:02d}" in used:
                serial += 1
            section["section_id"] = f"S{serial:02d}"
            used.add(section["section_id"])
        while parents and parents[-1]["heading_level"] >= section["heading_level"]:
            parents.pop()
        if parents:
            section["parent_section_id"] = parents[-1]["section_id"]
            section["parent_headings"] = [{"section_id": p["section_id"], "title": p["title"]} for p in parents]
            parents[-1]["organizing_only"] = True
        parents.append(section)
    return sections


def _paper_id_list(value: str) -> list[str]:
    return list(
        dict.fromkeys(
            paper_id.strip()
            for paper_id in re.split(r"[,，;；]", value.strip().rstrip(".。"))
            if paper_id.strip()
        )
    )


def _append_exclusions(section: dict[str, Any], value: str) -> None:
    parts = re.split(r"\s+(?:—|–)\s+", value.strip().rstrip(".。"), maxsplit=1)
    reason = parts[1].strip() if len(parts) == 2 else ""
    for paper_id in re.split(r"[,，;；]", parts[0].strip()):
        normalized_id = paper_id.strip()
        if normalized_id:
            section["excluded_papers"].append(
                {"paper_id": normalized_id, "reason": reason}
            )


def outline_markdown_from_sections(
    sections: list[dict[str, Any]],
    *,
    outline_style: str,
    automatically_adjusted: bool = False,
) -> str:
    """Render parsed sections back to beginner-readable outline Markdown."""

    definition = OUTLINE_STYLES.get(str(outline_style or "").casefold())
    lines = ["# Selected Outline", ""]
    if definition:
        lines.extend([f"Primary structure: {definition['en']}.", ""])
    if automatically_adjusted:
        lines.extend(
            [
                "The system automatically routed previously unclassified papers using the current taxonomy and paper evidence.",
                "",
            ]
        )
    body_number = 0
    for section in sections:
        role = str(section.get("section_role") or "body").casefold()
        title = str(section.get("title") or "").strip()
        if not title:
            continue
        if role == "body":
            body_number += 1
            heading = f"## {body_number}. {title}"
        else:
            heading = f"## {title}"
        heading = "#" * int(section.get("heading_level") or 2) + heading[2:]
        lines.extend([heading, f"Section role: {role}"])
        if section.get("section_id"):
            lines.append(f"<!-- section_id: {section['section_id']} -->")
        _append_rendered_section(lines, section)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _append_rendered_section(lines: list[str], section: dict[str, Any]) -> None:
    paper_ids = list(dict.fromkeys(section.get("paper_ids") or []))
    if paper_ids:
        lines.append(f"Assigned papers: {', '.join(paper_ids)}.")
    context_ids = list(dict.fromkeys(section.get("context_paper_ids") or []))
    if context_ids:
        lines.append(f"Context papers: {', '.join(context_ids)}.")
    for exclusion in section.get("excluded_papers") or []:
        if not isinstance(exclusion, dict):
            continue
        paper_id = str(exclusion.get("paper_id") or "").strip()
        reason = str(exclusion.get("reason") or "").strip()
        if paper_id:
            lines.append(
                f"Excluded paper: {paper_id}"
                + (f" — {reason}" if reason else "")
                + "."
            )
    for key, label, suffix in (
        ("purpose", "Purpose", ""),
        ("topic_partition", "Topic partition", "."),
        ("boundary_rationale", "Boundary rationale", ""),
        ("notes", "Notes", ""),
    ):
        value = str(section.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}{suffix}")
