"""Topic-agnostic structural rules for evidence-based review writing.

The helpers in this module deliberately know nothing about a scientific
domain.  They distinguish section roles and make each paper's detailed
discussion belong to one primary body section.  Introduction, conclusion,
and later cross-cutting sections may still cite the paper as supporting
evidence, but they must not independently expand it as a second paper
summary.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


SECTION_ROLES = {"introduction", "body", "conclusion", "references"}


def _normalized_heading(title: Any) -> str:
    text = str(title or "").casefold().strip()
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)、：:\-]?\s*", "", text)
    return re.sub(r"\s+", " ", text)


def infer_section_role(title: Any, explicit_role: Any = "") -> str:
    """Return a generic manuscript role from an explicit role or heading.

    Explicit valid roles always win.  Heading inference intentionally uses
    broad manuscript vocabulary rather than topic-specific terms, so the
    same policy works for chemistry, medicine, materials, computing, and
    other review domains.
    """

    explicit = str(explicit_role or "").casefold().strip()
    if explicit in SECTION_ROLES:
        return explicit

    heading = _normalized_heading(title)
    if not heading:
        return "body"
    if re.search(
        r"(?:^|\b)(references?|bibliography|cited literature)(?:\b|$)", heading
    ) or any(term in heading for term in ("参考文献", "文献目录")):
        return "references"
    if re.search(
        r"(?:^|\b)(conclusions?|summary and outlook|outlook|future directions?|"
        r"perspectives?|closing remarks)(?:\b|$)",
        heading,
    ) or any(
        term in heading
        for term in ("结论", "总结与展望", "结语", "未来展望", "前景与挑战")
    ):
        return "conclusion"
    if re.search(
        r"(?:^|\b)(introduction|general introduction|background and scope|"
        r"scope and organization)(?:\b|$)",
        heading,
    ) or any(term in heading for term in ("引言", "绪论", "研究背景与范围", "范围与组织")):
        return "introduction"
    return "body"


def _unique(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(
            text
            for value in values
            if (text := str(value or "").strip())
        )
    )


def assign_primary_paper_sections(
    sections: list[dict[str, Any]],
    matrix_order: Iterable[Any],
    *,
    max_synthesis_papers: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Normalize outline assignments into primary and supporting evidence.

    A paper receives at most one primary body-section owner.  Repeated
    assignments become supporting references.  Introduction and conclusion
    never own papers; they receive a bounded representative set for concise
    framing and cross-section synthesis.
    """

    normalized: list[dict[str, Any]] = []
    for source in sections:
        item = dict(source)
        item["section_role"] = infer_section_role(
            item.get("title"), item.get("section_role")
        )
        item["paper_ids"] = _unique(item.get("paper_ids") or [])
        item["primary_papers"] = []
        item["supporting_papers"] = []
        normalized.append(item)

    primary_owner: dict[str, str] = {}
    for index, item in enumerate(normalized, start=1):
        if item["section_role"] != "body":
            continue
        section_id = str(item.get("section_id") or f"S{index:02d}")
        for paper_id in item["paper_ids"]:
            if paper_id not in primary_owner:
                primary_owner[paper_id] = section_id
                item["primary_papers"].append(paper_id)
            else:
                item["supporting_papers"].append(paper_id)

    representatives: list[str] = []
    for item in normalized:
        if item["section_role"] != "body" or not item["primary_papers"]:
            continue
        representatives.append(item["primary_papers"][0])
    for paper_id in _unique(matrix_order):
        if paper_id in primary_owner and paper_id not in representatives:
            representatives.append(paper_id)
    representatives = representatives[: max(1, int(max_synthesis_papers))]

    for item in normalized:
        if item["section_role"] not in {"introduction", "conclusion"}:
            continue
        requested = [
            paper_id
            for paper_id in item["paper_ids"]
            if paper_id in primary_owner
        ]
        item["supporting_papers"] = (
            requested or representatives
        )[: max(1, int(max_synthesis_papers))]

    return normalized, primary_owner
