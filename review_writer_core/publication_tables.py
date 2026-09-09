"""Render existing, source-bound fact comparisons as reader-facing Markdown."""

from __future__ import annotations

import json
import re
from collections import OrderedDict, defaultdict
from math import ceil
from typing import Any

from .draft_bibliography import CALLOUT_RE, format_citation_group, _embedded_citation_map
from .scientific_facts import build_fact_comparison
from .stages.sections.source_writing import CONTRACT as SOURCE_CONTRACT, support_fingerprint

# Related fields share a column; values and experiment identities remain separate.
_COLUMNS = (
    ("Inputs", ("object_input",)),
    ("Conditions", ("method_conditions", "intervention_role")),
    ("Key result", ("quantitative_results", "specialized_metrics")),
    ("Evidence", ("mechanism", "validation_evidence")),
    ("Scope / limits", ("scope", "limitations", "scale_reproducibility", "safety_cost_sustainability")),
)

_MAX_TABLE_ROWS = 8
_MAX_ROWS_PER_PAPER = 1
_MAX_VALUES_PER_CELL = 2
_MAX_CELL_UNITS = 120
_MAX_LABEL_UNITS = 56

_GENERATED_TABLE_BLOCK = re.compile(
    r"^Table (?P<number>\d+)\.[^\n]*\n\n"
    r"(?:\|[^\n]*\|\n)+\n"
    r"<!-- comparison_table: (?P<metadata>\{[^\n]*\}) -->",
    re.MULTILINE,
)


def refresh_generated_comparison_tables(
    markdown: str, section_index: dict[str, Any], matrix_rows: list[dict[str, Any]],
) -> str:
    """Refresh marked system tables only; leave surrounding manuscript byte-for-byte.

    Missing section/citation context preserves the saved table. A valid section
    without comparable records removes its obsolete table rather than padding it.
    """
    citations = {paper: number for number, paper in _embedded_citation_map(markdown).items()}
    if not citations:
        return markdown
    sections = {str(section.get("section_id")): section
                for section in section_index.get("sections") or [] if isinstance(section, dict)}
    rows = {str(row.get("paper_id")): row for row in matrix_rows if isinstance(row, dict)}
    # Reserve numbers of tables we cannot safely replace (including manual tables).
    matches = list(_GENERATED_TABLE_BLOCK.finditer(markdown))
    replacements: dict[int, dict[str, Any]] = {}
    for match in matches:
        try:
            metadata = json.loads(match["metadata"])
        except ValueError:
            continue
        if not isinstance(metadata, dict) or not str(metadata.get("projection", "")).startswith("publication-comparison/"):
            continue
        section = sections.get(str(metadata.get("section_id")))
        if section is not None:
            replacements[match.start()] = section
    reserved = {int(match[1]) for match in re.finditer(r"^Table (\d+)\.", markdown, re.MULTILINE)
                if match.start() not in replacements}
    number = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal number
        section = replacements.get(match.start())
        if section is None:
            return match[0]
        candidate_number = number + 1
        while candidate_number in reserved:
            candidate_number += 1
        rendered = render_section_comparison(section, rows, citations, table_number=candidate_number)
        if rendered:
            number = candidate_number
        return rendered

    return _GENERATED_TABLE_BLOCK.sub(replace, markdown)


_REFERENCE_ONLY_QUALIFIER = re.compile(
    r"(?:^|_)(?:reference|table|figure|entry|page|source|chunk|id)(?:_|$)", re.I
)
_BOILERPLATE_PREFIXES = (
    re.compile(
        r"^(?:the\s+)?(?:paper|study|report|authors?)\s+"
        r"(?:reports?|reported|states?|stated|describes?|described|concludes?|concluded)\s+"
        r"(?:that\s+)?",
        re.I,
    ),
    re.compile(r"^authors?[’']?\s+interpretation:\s*", re.I),
    re.compile(r"^source-reported\s+(?:mechanistic\s+)?account:\s*", re.I),
    re.compile(
        r"^(?:the\s+)?(?:study|work)\s+"
        r"(?:demonstrates?|demonstrated|shows?|showed)\s+",
        re.I,
    ),
    re.compile(r"^(?:the\s+)?modified\s+(?:method|procedure)\s+(?:uses|used)\s+", re.I),
)

_CONDITION_PREFIXES = (
    re.compile(r"^(?:the\s+)?reported\s+[^,;]{1,64}\s+route\s+is\s+", re.I),
    re.compile(
        r"^for\s+(?:the\s+)?(?:specific\s+)?[^,]{1,64},\s*"
        r"(?:the\s+)?(?:reaction|experiment|procedure|method)\s+"
        r"(?:used|uses|employed)\s+",
        re.I,
    ),
    re.compile(
        r"^(?:the\s+)?(?:reported\s+|optimized\s+)?"
        r"(?:reaction|experiment|procedure|method|route)\s+"
        r"(?:used|uses|employed|is|was\s+(?:run|performed|carried\s+out)\s+(?:with|using))\s+",
        re.I,
    ),
    re.compile(r"^one-pot\s+(?:formation|synthesis|preparation)\s+of\s+[^,;]{1,48}\s+used\s+", re.I),
    re.compile(
        r"^(?:table|entry|scheme)\s+\S+(?:\s+\S+){0,4}\s+"
        r"(?:reaction|reactions|experiment|experiments)\s+"
        r"(?:were|was)\s+carried\s+out\s+(?:on|at|with|using)\s+",
        re.I,
    ),
)

_RESULT_VERB = re.compile(
    r"\b(?:gave|giving|afforded|affording|yielded|yielding|produced|producing|"
    r"furnish|furnished|furnishing|achieved|reached|increased|decreased)\b",
    re.I,
)
_RESULT_METRIC = re.compile(
    r"%|\b(?:yield|yields|conversion|selectivity|ee|de|dr|er|accuracy|precision|recall|auc)\b",
    re.I,
)

_FIELD_PREFERENCES = {
    "object_input": ("substrate", "input", "aldehyde", "ketone", "alkyne", "sample", "dataset"),
    "method_conditions": ("catalyst", "condition", "temperature", "solvent", "time", "equiv", "mol%"),
    "intervention_role": ("role", "catal", "promot", "mediate", "ligand"),
    "quantitative_results": ("yield", "conversion", "selectivity", " ee", " de", " dr", "accuracy", "%"),
    "specialized_metrics": ("yield", "conversion", "selectivity", " ee", " de", " dr", "metric", "%"),
    "mechanism": ("mechan", "intermediate", "pathway", "transition", "transfer", "proposed"),
    "validation_evidence": ("control", "kie", "dft", "nmr", "evidence", "experiment"),
    "scope": ("scope", "toler", "substrate", "applicable", "functional group"),
    "limitations": ("limit", "failed", "trace", "low", "not", "require"),
    "scale_reproducibility": ("scale", "gram", "reproduc", "repeat"),
    "safety_cost_sustainability": ("safety", "toxic", "cost", "waste", "sustain"),
}

_QUALIFIER_FIELD_TERMS = {
    "object_input": ("substrate", "input", "reagent", "reactant", "product", "object"),
    "method_conditions": ("catal", "ligand", "solvent", "temperature", "time", "loading", "equiv", "additive", "pressure"),
    "intervention_role": ("role", "catal", "ligand", "promot", "mediate"),
    "quantitative_results": ("yield", "conversion", "recovery", "selectivity", "ee", "de", "dr", "er", "accuracy", "precision", "recall", "auc"),
    "specialized_metrics": ("yield", "conversion", "recovery", "selectivity", "ee", "de", "dr", "er", "accuracy", "precision", "recall", "auc", "metric"),
    "mechanism": ("mechan", "intermediate", "pathway", "transition", "role", "transfer", "elimination"),
    "validation_evidence": ("control", "kie", "dft", "nmr", "evidence", "experiment", "observation"),
    "scope": ("scope", "substrate", "product", "toler", "functional", "class", "range"),
    "limitations": ("limit", "failed", "trace", "restriction", "incompat", "boundary"),
    "scale_reproducibility": ("scale", "gram", "reproduc", "repeat", "batch"),
    "safety_cost_sustainability": ("safety", "toxic", "cost", "waste", "sustain", "hazard"),
}


def split_table_row(line: str) -> list[str]:
    """Parse the escaped pipe convention used by both publication exporters."""
    return [cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _cell_text(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_cell_text(item)}" for key, item in value.items() if item is not None and item != "")
    if isinstance(value, list):
        return "; ".join(_cell_text(item) for item in value if item is not None and item != "")
    text = " ".join(str(value if value is not None else "").split())
    # Preserve source-local compound labels without treating them as bibliography IDs.
    text = CALLOUT_RE.sub(lambda match: "［" + match[1] + "］", text)
    # Fact values are data, not executable Markdown or workflow comments.
    return text.replace("<", "‹").replace(">", "›").replace("|", r"\|").replace("`", "′").replace("*", "∗")


def _display_units(value: Any) -> int:
    return sum(2 if ord(character) > 0xFF else 1 for character in str(value or ""))


def _strip_boilerplate(value: Any) -> str:
    text = _cell_text(value).strip(" .;,：:")
    changed = True
    while changed and text:
        changed = False
        for pattern in _BOILERPLATE_PREFIXES:
            candidate = pattern.sub("", text).strip(" .;,：:")
            if candidate != text:
                text = candidate
                changed = True
    return text


def _bounded_words(value: str, limit: int) -> str:
    if _display_units(value) <= limit:
        return value.strip(" .;,：:")
    tokens = re.findall(r"\$[^$\n]*\$|\\\(.+?\\\)|\S+", value)
    kept: list[str] = []
    used = 0
    for token in tokens:
        extra = _display_units(token) + (1 if kept else 0)
        if kept and used + extra > limit:
            break
        kept.append(token)
        used += extra
    result = " ".join(kept).strip(" .;,：:")
    if len(kept) < len(tokens):
        while kept and _display_units(result) + 1 > limit:
            kept.pop()
            result = " ".join(kept).strip(" .;,：:")
        result += "…"
    return result


def _bounded_tail(value: str, limit: int) -> str:
    """Keep a complete metric-bearing tail instead of a dangling sentence prefix."""

    tokens = re.findall(r"\$[^$\n]*\$|\\\(.+?\\\)|\S+", value)
    kept: list[str] = []
    used = 0
    for token in reversed(tokens):
        extra = _display_units(token) + (1 if kept else 0)
        if kept and used + extra > limit:
            break
        kept.append(token)
        used += extra
    return " ".join(reversed(kept)).strip(" .;,：:")


def _compact_phrase(value: Any, *, field_id: str = "", limit: int = _MAX_CELL_UNITS) -> str:
    """Project source text into a short exact phrase without inventing content."""

    text = _strip_boilerplate(value)
    if field_id in {"method_conditions", "intervention_role"}:
        for pattern in _CONDITION_PREFIXES:
            focused = pattern.sub("", text).strip(" .;,：:")
            if focused != text:
                text = focused
                break
    elif field_id in {"quantitative_results", "specialized_metrics"}:
        result_clauses = [
            clause.strip(" .;,：:")
            for clause in re.split(
                r"\s*[;；]\s*|,\s+(?=(?:and\s+)?[^,]{1,45}\b"
                r"(?:gave|afforded|yielded|produced|furnished)\b)",
                text,
                flags=re.I,
            )
            if clause.strip(" .;,：:")
        ]
        metric_clauses = [clause for clause in result_clauses if _RESULT_METRIC.search(clause)]
        if len(metric_clauses) >= 2:
            text = "; ".join(metric_clauses[:2])
        result_match = _RESULT_VERB.search(text)
        # Long fact-card prose often states the procedure before its result.
        # Keep the exact result clause, while the method remains available in
        # its own comparison column and in the full evidence package.
        if result_match and result_match.start() > 28:
            text = text[result_match.start():].strip(" .;,：:")
        if _display_units(text) > limit and _RESULT_METRIC.search(text):
            text = _bounded_tail(text, limit)
    if not text or _display_units(text) <= limit:
        return text
    fragments = [
        fragment.strip(" .;,：:")
        for fragment in re.split(r"(?<=[.!?。！？])\s+|\s*[;；]\s*", text)
        if fragment.strip(" .;,：:")
    ]
    if len(fragments) == 1:
        fragments = [
            fragment.strip(" ,，")
            for fragment in re.split(r"\s*[,，]\s*", text)
            if fragment.strip(" ,，")
        ]
    preferences = _FIELD_PREFERENCES.get(field_id, ())
    ranked = sorted(
        enumerate(fragments),
        key=lambda pair: (
            -sum(term in (" " + pair[1].casefold()) for term in preferences),
            pair[0],
        ),
    )
    selected: list[tuple[int, str]] = []
    used = 0
    for index, fragment in ranked:
        bounded = _bounded_words(fragment, limit)
        extra = _display_units(bounded) + (2 if selected else 0)
        if not bounded or (selected and used + extra > limit):
            continue
        selected.append((index, bounded))
        used += extra
        if len(selected) == 2:
            break
    if not selected:
        return _bounded_words(text, limit)
    return "; ".join(value for _index, value in sorted(selected)).strip(" .;,：:")


def _qualifier_items(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for key, item in value.items():
            if item is None or item == "":
                continue
            path = f"{prefix}_{key}" if prefix else str(key)
            result.extend(_qualifier_items(item, path))
        return result
    if isinstance(value, list):
        rendered = ", ".join(_cell_text(item) for item in value if item is not None and item != "")
        return [(prefix, rendered)] if rendered else []
    return [(prefix, _cell_text(value))] if value is not None and value != "" else []


def _render_qualifier(key: str, value: str) -> str:
    raw_key = str(key or "").strip()
    normalized = raw_key.casefold()
    label = re.sub(r"_loading$", "", raw_key, flags=re.I).replace("_", " ").strip()
    leaf = normalized.rsplit("_", 1)[-1]
    if leaf == "temperature":
        return f"T {value}"
    if leaf == "time":
        return f"time {value}"
    if leaf == "solvent":
        return value
    if leaf == "yield":
        return f"yield {value}"
    if leaf in {"ee", "de", "dr", "er"}:
        return f"{leaf} {value}"
    return f"{label}: {value}" if label else value


def _qualifier_matches_field(field_id: str, key: str) -> bool:
    terms = _QUALIFIER_FIELD_TERMS.get(field_id, ())
    if not terms:
        return True
    normalized = str(key or "").casefold()
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", normalized)))
    return any(
        term in tokens if len(term) <= 3 else term in normalized
        for term in terms
    )


def _compact_qualifiers(
    value: Any, *, field_id: str, limit: int = _MAX_CELL_UNITS
) -> tuple[str, list[str]]:
    items = [
        (key, item)
        for key, item in _qualifier_items(value)
        if not _REFERENCE_ONLY_QUALIFIER.search(key)
        and _qualifier_matches_field(field_id, key)
    ]
    rendered: list[str] = []
    keys: list[str] = []
    used = 0
    for key, item in items:
        phrase = _bounded_words(_render_qualifier(key, item), limit)
        extra = _display_units(phrase) + (2 if rendered else 0)
        if rendered and used + extra > limit:
            continue
        rendered.append(phrase)
        keys.append(key)
        used += extra
    return "; ".join(rendered), keys


def _compact_cell(cell: dict[str, Any]) -> str:
    field_id = str(cell.get("field_id") or "")
    value = _compact_phrase(cell.get("value"), field_id=field_id)
    qualifiers, qualifier_keys = _compact_qualifiers(
        cell.get("qualifiers"), field_id=field_id
    )
    if qualifiers and (
        len(qualifier_keys) >= 2 or _display_units(value) > _MAX_CELL_UNITS // 2
    ):
        value = qualifiers
    elif qualifiers and qualifiers.casefold() not in value.casefold():
        combined = f"{value}; {qualifiers}" if value else qualifiers
        value = _compact_phrase(combined, field_id=field_id)
    status = str(cell.get("epistemic_status") or "").casefold()
    ceiling = str(cell.get("assertion_ceiling") or "").casefold()
    if ceiling == "attributed_author_interpretation" or any(
        token in status for token in ("propos", "infer", "interpret")
    ):
        value = f"Author interpretation: {value}"
    elif field_id == "mechanism":
        value = f"Reported mechanism: {value}"
    elif field_id in {"quantitative_results", "specialized_metrics"} and re.match(
        r"^(?:afford|furnish|give|produce|yield)\b", value, re.I
    ):
        value = f"Reported to {value}"
    return _bounded_words(value, _MAX_CELL_UNITS)


def _selected_fact_ids(section: dict[str, Any]) -> set[str]:
    return {
        str(fact_id)
        for paragraph in section.get("paragraphs") or []
        if isinstance(paragraph, dict)
        for claim in paragraph.get("claim_realizations") or []
        if isinstance(claim, dict)
        for fact_id in claim.get("fact_ids") or []
        if str(fact_id)
    }


def _claim_fact_groups(section: dict[str, Any]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(section.get("paragraphs") or []):
        if not isinstance(paragraph, dict):
            continue
        for claim_index, claim in enumerate(paragraph.get("claim_realizations") or []):
            if not isinstance(claim, dict):
                continue
            fact_ids = list(dict.fromkeys(
                str(value) for value in claim.get("fact_ids") or [] if str(value)
            ))
            if not fact_ids:
                continue
            groups.append(
                {
                    "claim_id": str(claim.get("claim_id") or f"p{paragraph_index}-c{claim_index}"),
                    "paper_ids": list(dict.fromkeys(
                        str(value) for value in claim.get("citation_group") or [] if str(value)
                    )),
                    "fact_ids": fact_ids,
                }
            )
    return groups


def _column_coverage(
    cells: list[dict[str, Any]], fields: tuple[str, ...]
) -> set[str]:
    return {
        str(cell.get("paper_id") or "")
        for cell in cells
        if cell.get("field_id") in fields and str(cell.get("paper_id") or "")
    }


def _select_groups(
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]],
    columns: list[tuple[str, tuple[str, ...]]],
    paper_order: list[str],
    *, minimum_fields: int = 2,
) -> list[tuple[tuple[str, str], list[dict[str, Any]]]]:
    selected_fields = {field for _label, fields in columns for field in fields}
    by_paper: dict[str, list[tuple[int, tuple[str, str], list[dict[str, Any]]]]] = defaultdict(list)
    for order, (identity, cells) in enumerate(groups.items()):
        populated = len({cell.get("field_id") for cell in cells if cell.get("field_id") in selected_fields})
        if populated < minimum_fields:
            continue
        by_paper[identity[0]].append((order, identity, cells))
    for values in by_paper.values():
        values.sort(
            key=lambda row: (
                -len({cell.get("field_id") for cell in row[2] if cell.get("field_id") in selected_fields}),
                -len(row[2]),
                row[0],
            )
        )
    selected: list[tuple[tuple[str, str], list[dict[str, Any]]]] = []
    for rank in range(_MAX_ROWS_PER_PAPER):
        for paper in paper_order:
            candidates = by_paper.get(paper) or []
            if rank < len(candidates):
                if rank > 0:
                    fields = {
                        cell.get("field_id") for cell in candidates[rank][2]
                        if cell.get("field_id") in selected_fields
                    }
                    if len(fields) < 2:
                        continue
                selected.append((candidates[rank][1], candidates[rank][2]))
                if len(selected) == _MAX_TABLE_ROWS:
                    return selected
    return selected


def _result_with_units(result, units):
    result, units = str(result or "").strip(), str(units or "").strip()
    if not result or not units:
        return result
    # Legacy writers sometimes put the entire metric in units. Keep it when
    # it adds information, but do not repeat an outcome already in result.
    if re.search(r"\d", units):
        return result if units.casefold() in result.casefold() else f"{result}; {units}"
    tokens = re.findall(r"%|[A-Za-z°µμ]+", units)
    present = set(re.findall(r"%|[a-z°µμ]+", result.casefold()))
    missing = [token for token in tokens if token.casefold() not in present]
    return " ".join([result, *missing])


def _standalone_conditions(value):
    text = str(value or "").strip()
    # Source-local pointers are provenance, not readable experimental conditions.
    text = re.sub(r"\b(?:entry|table|scheme|figure)\s+[A-Za-z]?\d+[A-Za-z]?\b", "", text, flags=re.I)
    text = re.sub(r"\bconditions?\s+[A-Z]\b", "", text)
    text = re.sub(r"\b(?:(?:under|using)\s+)?(?:the\s+)?same conditions?\b", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:reaction in|in|under)\s*", "", text, flags=re.I)
    text = text.strip(" ;,.")
    if not text or re.fullmatch(r"(?:conditions?|and|or|under|the|same|\s|[,;])+", text, re.I):
        return ""
    return text


def _source_comparison_cells(section, papers):
    """Project audited Claim records, without reconstructing facts or experiments.

    The section publisher owns source-version validation. Here the same audit
    fingerprint prevents changed text/records from reusing an old audit.
    """
    structured = []
    for pi, paragraph in enumerate(section.get("paragraphs") or []):
        for ci, claim in enumerate(paragraph.get("claim_realizations") or []):
            audit = claim.get("source_verification") or {}
            refs = claim.get("evidence_refs") or []
            text = str(claim.get("text") or "").strip()
            records = claim.get("result_context") or []
            if (audit.get("contract") != SOURCE_CONTRACT or audit.get("status") != "supported"
                    or not text or not refs
                    or any(not isinstance(ref, dict) or not ref.get("quote") or not ref.get("evidence_key") for ref in refs)
                    or audit.get("input_fingerprint") != support_fingerprint(
                        text, refs, claim.get("claim_kind"), records, claim.get("fact_ids") or [])):
                continue
            by_key = {ref["evidence_key"]: ref for ref in refs}
            bound_papers = {str(ref.get("paper_id") or "") for ref in refs}
            if bound_papers != set(claim.get("citation_group") or []):
                continue
            identity = f"p{pi}-c{ci}"
            def cell(paper, field, value, context, evidence, subject=""):
                return {"paper_id": paper, "field_id": field, "value": value,
                        "experiment_id": context, "subject": subject, "fact_ids": [],
                        "evidence_refs": evidence, "claim_id": claim.get("claim_id")}
            for ri, record in enumerate(records):
                ref = by_key.get(record.get("evidence_key"))
                if not ref or ref.get("paper_id") not in papers:
                    continue
                if not str(record.get("object") or "").strip() or not str(record.get("result") or "").strip():
                    continue
                if re.match(r"^(?:it|its|this|that|the same)\b", str(record.get("object") or ""), re.I):
                    continue
                context = f"{identity}-r{ri}"
                result = _result_with_units(record.get("result"), record.get("units"))
                for field, value in (("object_input", record.get("object")),
                                     ("method_conditions", _standalone_conditions(record.get("conditions"))),
                                     ("quantitative_results", result)):
                    if value:
                        structured.append(cell(ref["paper_id"], field, value, context, [ref], str(record.get("object") or "")))
    return structured



def render_section_comparison(
    section: dict[str, Any], rows: dict[str, dict[str, Any]],
    citation_numbers: dict[str, int], *, table_number: int,
) -> str:
    """Select already-cited facts; never infer missing values or add model calls."""
    if str(section.get("section_role") or "body").casefold() != "body":
        return ""
    papers = list(dict.fromkeys(
        str(paper) for paragraph in section.get("paragraphs") or []
        for paper in (paragraph.get("cited_paper_ids") or
                      [paper for claim in paragraph.get("claim_realizations") or []
                       for paper in claim.get("citation_group") or []])
        if str(paper) in citation_numbers
    ))
    source_mode = section.get("evidence_mode") == SOURCE_CONTRACT or any(
        claim.get("source_verification") for paragraph in section.get("paragraphs") or []
        for claim in paragraph.get("claim_realizations") or [])
    # Supporting/background citations do not define the chapter comparison.
    if source_mode and "primary_papers" in section:
        primary = set(section.get("primary_papers") or [])
        papers = [paper for paper in papers if paper in primary]
    source_cells = _source_comparison_cells(section, papers) if source_mode else []
    comparison = {"cells": source_cells} if source_mode else build_fact_comparison(str(section.get("section_id") or ""), papers, rows)
    selected_fact_ids = _selected_fact_ids(section)
    cells = [
        cell for cell in comparison["cells"]
        if source_mode or not selected_fact_ids or selected_fact_ids.intersection(cell.get("fact_ids") or [])
    ]
    selected_papers = {
        str(cell.get("paper_id") or "") for cell in cells if cell.get("paper_id")
    }
    if len(selected_papers) < 2:
        return ""
    minimum_fields = 2
    available_columns = _COLUMNS[:3] if source_mode else _COLUMNS
    columns = [
        (label, fields) for label, fields in available_columns
        if len(_column_coverage(cells, fields)) >= 2
    ]
    if len(columns) < minimum_fields:
        return ""
    selected_fields = {field for _, fields in columns for field in fields}
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    claim_groups = _claim_fact_groups(section)
    if selected_fact_ids and not source_mode:
        # The writer has already validated which facts jointly support each
        # Claim. Reuse that safe grouping instead of treating fact-card IDs as
        # experimental identities or merging unrelated observations by paper.
        for claim in claim_groups:
            claim_fact_ids = set(claim["fact_ids"])
            for paper in claim["paper_ids"]:
                matched = [
                    cell for cell in cells
                    if cell.get("paper_id") == paper
                    and cell.get("field_id") in selected_fields
                    and claim_fact_ids.intersection(cell.get("fact_ids") or [])
                ]
                if not matched:
                    continue
                identity = (paper, f"claim:{claim['claim_id']}")
                existing = groups.setdefault(identity, [])
                existing_ids = {
                    (cell.get("field_id"), tuple(cell.get("fact_ids") or []))
                    for cell in existing
                }
                existing.extend(
                    cell for cell in matched
                    if (cell.get("field_id"), tuple(cell.get("fact_ids") or []))
                    not in existing_ids
                )
    else:
        for cell in cells:
            if cell["field_id"] not in selected_fields:
                continue
            # Legacy facts without Claim bindings remain separate unless they
            # carry the same explicit experiment identity.
            context = str(cell.get("experiment_id") or "") or "/".join(cell["fact_ids"])
            groups.setdefault((cell["paper_id"], context), []).append(cell)
    selected_groups = _select_groups(
        groups,
        columns,
        papers,
        minimum_fields=minimum_fields,
    )
    if len({identity[0] for identity, _cells in selected_groups}) < 2:
        return ""
    # Drop columns dominated by blank cells after representative rows are chosen.
    minimum_rows = max(2, ceil(len(selected_groups) * 0.4))
    columns = [
        (label, fields)
        for label, fields in columns
        if sum(
            any(cell.get("field_id") in fields for cell in group_cells)
            for _identity, group_cells in selected_groups
        ) >= minimum_rows
    ]
    if len(columns) < minimum_fields:
        return ""
    selected_groups = [
        (identity, group_cells)
        for identity, group_cells in selected_groups
        if sum(
            any(cell.get("field_id") in fields for cell in group_cells)
            for _label, fields in columns
        ) >= minimum_fields
    ]
    if len({identity[0] for identity, _cells in selected_groups}) < 2:
        return ""
    header = [*("System / substrate" if label == "Inputs" else label for label, _ in columns), "Ref."]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    provenance = []
    displayed_fact_ids: set[str] = set()
    for (paper, context), group_cells in selected_groups:
        citation = format_citation_group([citation_numbers[paper]])
        rendered = []
        for _, fields in columns:
            values: list[str] = []
            for cell in group_cells:
                if cell["field_id"] not in fields:
                    continue
                value = _compact_cell(cell)
                if not value or value.casefold() in {item.casefold() for item in values}:
                    continue
                combined = "; ".join([*values, value])
                if values and (
                    len(values) >= _MAX_VALUES_PER_CELL
                    or _display_units(combined) > _MAX_CELL_UNITS
                ):
                    continue
                values.append(value)
                displayed_fact_ids.update(str(item) for item in cell.get("fact_ids") or [])
                provenance.append({
                    **{key: cell[key] for key in ("paper_id", "field_id", "fact_ids", "experiment_id")},
                    "evidence_keys": [str(ref.get("evidence_key") or ref.get("chunk_id") or "")
                                      for ref in cell.get("evidence_refs") or [] if isinstance(ref, dict)],
                    "claim_id": cell.get("claim_id"),
                })
            rendered.append("; ".join(dict.fromkeys(values)) or "—")
        rendered.append(citation)
        lines.append("| " + " | ".join(rendered) + " |")
    heading = _cell_text(section.get("heading") or "this section").strip(" .")
    dimensions = [label.lower() for label, _ in columns]
    question = "How do the selected studies compare in " + ", ".join(dimensions) + "?"
    caption = f"Table {table_number}. {heading}."
    if any(label == "Key result" for label, _ in columns):
        caption += " Results are source-specific, not a performance ranking."
    if any("—" in line for line in lines[2:]):
        caption += " — = not specified in the selected evidence."
    all_fact_ids = {
        str(item) for cell in cells for item in cell.get("fact_ids") or [] if str(item)
    }
    metadata = json.dumps(
        {
            "section_id": section.get("section_id"),
            "cells": provenance,
            "projection": "publication-comparison/4",
            "comparison_question": question,
            "comparison_dimensions": dimensions,
            "evidence_mode": "source_claims" if source_mode else "legacy_facts",
            "selected_fact_ids": sorted(selected_fact_ids),
            "omitted_fact_ids": sorted(all_fact_ids - displayed_fact_ids),
            "source_row_count": len(groups),
            "displayed_row_count": len(selected_groups),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return caption + "\n\n" + "\n".join(lines) + "\n\n<!-- comparison_table: " + metadata + " -->"
