"""Reader-facing section reaction quick-reference table.

Consumes the shared experiment note (``result_context``) that source writing
already records, so prose and this table never re-read the papers separately.

The pipeline is:

    project  -> one row per checked experiment note (de-duplicated)
    group    -> one row per independently adoptable method
    validate -> reader acceptance gate (no table rather than a bad table)
    render   -> condensed columns + optional selectivity column
"""

from __future__ import annotations

import json
import re
from typing import Any

from .draft_bibliography import format_citation_group

# Core reader columns (condensed). "conditions" and "environment" merge into one
# column; the substrate class already lives inside the transformation.
CORE_COLUMNS = ("transformation", "catalyst", "conditions", "outcome")
SELECTIVITY_COLUMN = "selectivity"
GROUP_COLUMN = "group"
HEADERS = {
    "transformation": "Reaction system / transformation",
    "catalyst": "Key catalyst system",
    "conditions": "Conditions / environment",
    "outcome": "Outcome",
    "selectivity": "Selectivity",
}
_KNOWN_COLUMNS = set(CORE_COLUMNS) | {SELECTIVITY_COLUMN}
_METHOD_CELLS = ("catalyst", "conditions", "environment")

# Match a selectivity value or range (e.g. "97% ee", "93-98% ee", "90–96% de").
_SELECTIVITY_VALUE = re.compile(
    r"\d{1,3}(?:\s*[–\-]\s*\d{1,3})?\s*%\s*(?:ee|er|dr|de|rr)\b", re.I
)
# A screened condition that gave no product or was simply ineffective is not an
# adoptable method. Weak-but-positive results (low yield, trace product) stay.
# Positive "no X" phrases such as "no racemization" are deliberately excluded.
_NEGATIVE_OUTCOME = re.compile(
    r"\bno\s+(?:desired\s+)?(?:product|reaction|conversion|formation|adduct|allene|yield|rearrangement)\b"
    r"|\bnot\s+(?:observed|detected|formed|effective|successful|reactive)\b"
    r"|\bdid\s+not\s+(?:react|form|proceed|occur|convert)\b"
    r"|\bunsuccessful\b|\bineffective\b|\bnot\s+effective\b",
    re.I,
)
_EMPTY = "—"
_MERGE_LIMIT = 3
# Keep the reader table scannable: a long phrase is trimmed at a word boundary.
_CELL_LIMITS = {
    "transformation": 48,
    "catalyst": 44,
    "conditions": 44,
    "outcome": 40,
    "selectivity": 26,
}


def _short(value: Any, limit: int) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    head = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ;,.") or text[:limit]
    return head + "…"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _dedupe_join(*values: Any) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = _clean(value).strip(" ;,.")
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            ordered.append(text)
    return "; ".join(ordered)


def _split_outcome(result: Any, units: Any) -> tuple[str, str]:
    """Separate selectivity metrics from the remaining outcome."""
    result_text, units_text = _clean(result), _clean(units)
    text = " ".join(part for part in (result_text, units_text) if part)
    selectivity = list(dict.fromkeys(match.group(0) for match in _SELECTIVITY_VALUE.finditer(text)))
    remainder = _SELECTIVITY_VALUE.sub("", result_text).strip(" ;,.")
    # "up to 97% ee" leaves a bare intensifier once the metric moves to its own
    # column; drop it rather than show a meaningless Outcome cell.
    if re.fullmatch(
        r"(?:up to|as high as|maximum|max\.?|about|ca\.?|nearly|reaching|achieving|of)[\s;,\-]*",
        remainder,
        re.I,
    ):
        remainder = ""
    return _clean(remainder), "; ".join(selectivity)


def project_reaction_rows(section: dict[str, Any], citation_numbers: dict[str, int]) -> list[dict[str, Any]]:
    """Project checked experiment notes into de-duplicated reaction rows.

    Unsupported or unbound records are skipped rather than guessed. Legacy
    five-cell records (no catalyst/environment) stay readable.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for paragraph in section.get("paragraphs") or []:
        if not isinstance(paragraph, dict):
            continue
        for claim in paragraph.get("claim_realizations") or []:
            if not isinstance(claim, dict):
                continue
            key_to_paper = {
                str(ref.get("evidence_key")): str(ref.get("paper_id") or "")
                for ref in claim.get("evidence_refs") or []
                if isinstance(ref, dict)
            }
            fallback = str(claim.get("citation_group", [""])[0]) if claim.get("citation_group") else ""
            for record in claim.get("result_context") or []:
                if not isinstance(record, dict):
                    continue
                paper = key_to_paper.get(str(record.get("evidence_key"))) or fallback
                if not paper or paper not in citation_numbers:
                    continue
                transformation = _clean(record.get("object"))
                outcome_main, selectivity = _split_outcome(record.get("result"), record.get("units"))
                if not transformation or not outcome_main and not selectivity:
                    continue
                # Drop screened conditions that produced nothing or were
                # ineffective; keep weak positives and "no racemization" style
                # positive outcomes.
                if not selectivity and _NEGATIVE_OUTCOME.search(outcome_main):
                    continue
                row = {
                    "paper": paper,
                    "transformation": transformation,
                    "catalyst": _clean(record.get("catalyst")),
                    "conditions": _dedupe_join(record.get("conditions"), record.get("environment")),
                    "environment": _clean(record.get("environment")),
                    "outcome": outcome_main,
                    "selectivity": selectivity,
                    "group": _clean(record.get("group")),
                    "ref": format_citation_group([citation_numbers[paper]]),
                }
                identity = (
                    paper,
                    row["transformation"].casefold(),
                    row["catalyst"].casefold(),
                    row["conditions"].casefold(),
                    row["outcome"].casefold(),
                    row["selectivity"].casefold(),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(row)
    return rows


def _merge_cell(members: list[dict[str, Any]], column: str) -> str:
    values = [value for value in dict.fromkeys(_clean(m.get(column)) for m in members) if value]
    if not values:
        return ""
    if len(values) <= _MERGE_LIMIT:
        return "; ".join(values)
    # Substrate instances of one method: keep the representative classes and
    # mark that more are reported rather than enumerate every example.
    return "; ".join(values[:_MERGE_LIMIT]) + " (and related)"


def group_reaction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse one paper's variants of the same transformation into one row.

    A scope table lists one row per substrate or screen condition, so the same
    transformation can repeat many times. Those condition and substrate
    variants merge into a single adoptable method; the per-variant values are
    joined in the cell. A genuinely different transformation keeps its own row.
    """
    from collections import OrderedDict

    groups: "OrderedDict[tuple[str, str], list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        key = (row["paper"], _clean(row["transformation"]).casefold())
        groups.setdefault(key, []).append(row)
    merged: list[dict[str, Any]] = []
    for members in groups.values():
        merged.append({
            "paper": members[0]["paper"],
            "transformation": _merge_cell(members, "transformation"),
            "catalyst": _merge_cell(members, "catalyst"),
            "conditions": _merge_cell(members, "conditions"),
            "outcome": _merge_cell(members, "outcome"),
            "selectivity": _merge_cell(members, "selectivity"),
            "group": members[0]["group"],
            "ref": members[0]["ref"],
            "method_records": len(members),
        })
    return merged


def validate_reaction_table(rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    """Reader acceptance gate: return the reasons a table must not be published."""
    issues: list[str] = []
    if set(columns) - _KNOWN_COLUMNS:
        issues.append("unknown_reader_column")
    if len(rows) < 2:
        issues.append("too_few_rows")
    if len({row["paper"] for row in rows}) < 2:
        issues.append("too_few_papers")
    for row in rows:
        if not _clean(row.get("transformation")):
            issues.append("row_without_transformation")
            break
    for row in rows:
        if not _clean(row.get("outcome")) and not _clean(row.get("selectivity")):
            issues.append("row_without_outcome")
            break
    for row in rows:
        if not _clean(row.get("ref")):
            issues.append("row_without_reference")
            break
    return list(dict.fromkeys(issues))


def build_table(
    section: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    table_number: int,
) -> dict[str, Any] | None:
    """Select columns, validate, and assemble the reader table."""
    rows = group_reaction_rows(rows)
    columns = list(CORE_COLUMNS)
    # Add the selectivity column only when it is genuinely reported for at least
    # two rows (asymmetric chapters); ordinary chapters stay condensed.
    if sum(1 for row in rows if row["selectivity"]) >= 2:
        columns.append(SELECTIVITY_COLUMN)
    # Drop a core column that is empty for every row.
    for column in ("catalyst", "conditions"):
        if all(not _clean(row.get(column)) for row in rows):
            columns.remove(column)
    if sum(1 for column in columns if any(_clean(row.get(column)) for row in rows)) < 2:
        return None
    issues = validate_reaction_table(rows, columns)
    if issues:
        return None
    heading = _clean(section.get("heading") or section.get("title")) or "this section"
    header = [HEADERS[column] for column in columns] + ["Ref."]
    table_lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    provenance: list[dict[str, Any]] = []
    empty_used = False
    for row in rows:
        cells: list[str] = []
        for column in columns:
            value = _short(row.get(column), _CELL_LIMITS.get(column, 48))
            if not value:
                value = _EMPTY
                empty_used = True
            cells.append(value)
        cells.append(row["ref"])
        table_lines.append("| " + " | ".join(cells) + " |")
        provenance.append({"paper_id": row["paper"], "ref": row["ref"],
                           "method_records": row.get("method_records", 1),
                           **{column: _clean(row.get(column)) for column in columns}})
    # The caption must sit immediately before the table so the manuscript
    # parser binds it to the table (and LaTeX numbers it as "Table N"). The
    # legend goes after the table and stays a normal paragraph.
    lines = [f"Table {table_number}. {heading}.", "", *table_lines]
    if empty_used:
        lines += ["", f"{_EMPTY} = not reported in the selected evidence."]
    metadata = json.dumps(
        {
            "section_id": section.get("section_id"),
            "projection": "section-reaction-table/2",
            "columns": columns,
            "rows": provenance,
            "row_count": len(rows),
            "validation": "passed",
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return {
        "markdown": "\n".join(lines) + "\n\n<!-- reaction_table: " + metadata + " -->",
        "columns": columns,
        "rows": provenance,
    }


def select_table_style(section: dict[str, Any]) -> str:
    """Choose the reader-table renderer for one section.

    Body chapters that compare at least two primary papers get the condensed
    quick-reference table; everything else keeps the legacy fact comparison.
    """
    if str(section.get("section_role") or "body").casefold() != "body":
        return "comparison"
    primary = {str(paper) for paper in section.get("primary_papers") or [] if str(paper)}
    if len(primary) < 2:
        return "comparison"
    return "reaction_quickref"


def render_reaction_table(
    section: dict[str, Any],
    citation_numbers: dict[str, int],
    *,
    table_number: int,
) -> str:
    """Render the condensed reader table, or "" when the section cannot support one."""
    if str(section.get("section_role") or "body").casefold() != "body":
        return ""
    result = build_table(section, project_reaction_rows(section, citation_numbers), table_number=table_number)
    return result["markdown"] if result else ""
