#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sciatlas_client import SciAtlasClient, load_config, papers_from_response


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:96] or "review-discovery"


def resolve_project_path(review_root: Path, project_id: str) -> Path:
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,95})", project_id
    ):
        raise QueryPlanError(
            "project-id must be one safe slug component containing only letters, "
            "numbers, underscores, or hyphens"
        )
    projects_root = (review_root / "review-projects").resolve()
    project = (projects_root / project_id).resolve()
    try:
        relative = project.relative_to(projects_root)
    except ValueError as exc:
        raise QueryPlanError(
            "project-id resolves outside review-root/review-projects"
        ) from exc
    if relative == Path(".") or len(relative.parts) != 1:
        raise QueryPlanError("project-id must resolve to one project component")
    return project


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def split_keywords(raw: str) -> list[str]:
    return dedupe([x.strip() for x in re.split(r"[,;；\n]+", raw or "") if x.strip()])


def dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value).strip())
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def field_value(field: Any, default: Any = None) -> Any:
    if isinstance(field, dict) and "value" in field:
        return field.get("value", default)
    return field if field is not None else default


def load_metadata(review_root: Path) -> dict[str, dict[str, Any]]:
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    papers: dict[str, dict[str, Any]] = {}
    for path in sorted(meta_dir.glob("*.metadata.json")):
        try:
            meta = read_json(path)
        except Exception:
            continue
        pid = meta.get("paper_id")
        if pid:
            papers[pid] = meta
    return papers


STRUCTURED_TAG_KEYS = [
    "product",
    "substrate",
    "catalyst_or_method",
    "organometallic_partner",
    "ligand_or_chiral_source",
    "leaving_group",
    "reaction_type",
    "document_scope",
]

GENERIC_INSTRUCTION_KEYWORDS = {
    "a",
    "an",
    "and",
    "around",
    "by",
    "for",
    "from",
    "in",
    "into",
    "last",
    "literature",
    "new",
    "newly",
    "of",
    "on",
    "or",
    "paper",
    "papers",
    "past",
    "review",
    "generate",
    "organized",
    "developed",
    "reaction",
    "reactions",
    "catalyst",
    "catalysts",
    "the",
    "to",
    "topic",
    "type",
    "types",
    "with",
    "write",
    "writing",
    "year",
    "years",
}


class QueryPlanError(ValueError):
    pass


def _normalize_plan_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise QueryPlanError(f"{field} must be a string")
    normalized = re.sub(r"\s+", " ", value.strip())
    if not normalized:
        raise QueryPlanError(f"{field} must not be empty")
    return normalized


def validate_query_plan(plan: dict[str, Any], topic: str) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise QueryPlanError("query plan must be a JSON object")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise QueryPlanError("schema_version must be the integer 1")

    plan_topic = _normalize_plan_text(plan.get("topic"), "topic")
    requested_topic = _normalize_plan_text(topic, "topic")
    if plan_topic.casefold() != requested_topic.casefold():
        raise QueryPlanError(
            f"query plan topic {plan_topic!r} does not match requested topic {requested_topic!r}"
        )

    resolved = plan.get("resolved_concepts")
    if not isinstance(resolved, list):
        raise QueryPlanError("resolved_concepts must be a list")
    normalized_resolved: list[dict[str, Any]] = []
    for index, concept in enumerate(resolved):
        if not isinstance(concept, dict):
            raise QueryPlanError(f"resolved_concepts[{index}] must be an object")
        normalized_concept = dict(concept)
        for field in ("surface", "expanded_name", "reason"):
            normalized_concept[field] = _normalize_plan_text(
                concept.get(field), f"resolved_concepts[{index}].{field}"
            )
        confidence = concept.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise QueryPlanError(
                f"resolved_concepts[{index}].confidence must be a number"
            )
        if not 0 <= confidence <= 1:
            raise QueryPlanError(
                f"resolved_concepts[{index}].confidence must be between 0 and 1"
            )
        normalized_concept["confidence"] = confidence
        normalized_resolved.append(normalized_concept)

    unresolved = plan.get("unresolved_concepts")
    if not isinstance(unresolved, list):
        raise QueryPlanError("unresolved_concepts must be a list")
    normalized_unresolved: list[dict[str, Any]] = []
    for index, concept in enumerate(unresolved):
        if not isinstance(concept, dict):
            raise QueryPlanError(f"unresolved_concepts[{index}] must be an object")
        normalized_concept = dict(concept)
        for field in ("surface", "reason"):
            normalized_concept[field] = _normalize_plan_text(
                concept.get(field), f"unresolved_concepts[{index}].{field}"
            )
        normalized_unresolved.append(normalized_concept)

    keywords = plan.get("keywords")
    if not isinstance(keywords, list):
        raise QueryPlanError("keywords must be a list")
    normalized_keywords: list[dict[str, Any]] = []
    for index, item in enumerate(keywords):
        if not isinstance(item, dict):
            raise QueryPlanError(f"keywords[{index}] must be an object")
        normalized_item = dict(item)
        for field in ("keyword", "source", "reason"):
            normalized_item[field] = _normalize_plan_text(
                item.get(field), f"keywords[{index}].{field}"
            )
        source = normalized_item["source"]
        if source not in {"user", "agent"}:
            raise QueryPlanError(
                f"keywords[{index}].source {source!r} must be 'user' or 'agent'"
            )
        keyword = normalized_item["keyword"]
        if keyword.casefold() in GENERIC_INSTRUCTION_KEYWORDS:
            raise QueryPlanError(
                f"keywords[{index}].keyword {keyword!r} is a generic instruction token"
            )
        category = _normalize_plan_text(
            item.get("category"), f"keywords[{index}].category"
        )
        if category not in STRUCTURED_TAG_KEYS:
            raise QueryPlanError(
                f"keywords[{index}].category {category!r} is not supported"
            )
        normalized_item["category"] = category
        normalized_keywords.append(normalized_item)

    resolved_surfaces = {
        concept["surface"].casefold(): concept["surface"]
        for concept in normalized_resolved
    }
    unresolved_surfaces = {
        concept["surface"].casefold(): concept["surface"]
        for concept in normalized_unresolved
    }
    overlapping_surfaces = resolved_surfaces.keys() & unresolved_surfaces.keys()
    if overlapping_surfaces:
        surfaces = ", ".join(
            unresolved_surfaces[key] for key in sorted(overlapping_surfaces)
        )
        raise QueryPlanError(
            f"concept surfaces cannot be both resolved and unresolved: {surfaces}"
        )
    for concept in normalized_unresolved:
        surface = concept["surface"]
        for index, item in enumerate(normalized_keywords):
            if contains_phrase(surface, item["keyword"]):
                raise QueryPlanError(
                    f"keywords[{index}].keyword {item['keyword']!r} contains "
                    f"unresolved concept surface {surface!r}"
                )

    filters = plan.get("filters")
    if not isinstance(filters, dict):
        raise QueryPlanError("filters must be an object")
    normalized_filters = dict(filters)
    for field in ("year_from", "year_to"):
        if field in normalized_filters and type(normalized_filters[field]) is not int:
            raise QueryPlanError(f"filters.{field} must be an integer")
    year_from = normalized_filters.get("year_from")
    year_to = normalized_filters.get("year_to")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise QueryPlanError("filters.year_from must not be greater than year_to")
    topic_filters = parse_topic_intent(requested_topic)["filters"]
    for field in ("year_from", "year_to"):
        if field in topic_filters and normalized_filters.get(field) != topic_filters[field]:
            raise QueryPlanError(
                f"filters.{field} must match the relative-year topic "
                f"(expected {topic_filters[field]})"
            )

    group_by = plan.get("group_by")
    if not isinstance(group_by, list):
        raise QueryPlanError("group_by must be a list")
    normalized_groups: list[str] = []
    for index, group in enumerate(group_by):
        group = _normalize_plan_text(group, f"group_by[{index}]")
        if group not in STRUCTURED_TAG_KEYS:
            raise QueryPlanError(f"group_by[{index}] {group!r} is not supported")
        if group not in normalized_groups:
            normalized_groups.append(group)

    if not normalized_keywords:
        if normalized_unresolved:
            surfaces = ", ".join(item["surface"] for item in normalized_unresolved)
            raise QueryPlanError(
                "no meaningful keyword remains; resolve the unresolved concepts "
                f"or provide a validated chemistry keyword: {surfaces}"
            )
        raise QueryPlanError(
            "no meaningful keyword remains; clarify the topic or provide a "
            "validated chemistry keyword"
        )

    normalized = dict(plan)
    normalized.update(
        {
            "schema_version": 1,
            "topic": plan_topic,
            "resolved_concepts": normalized_resolved,
            "unresolved_concepts": normalized_unresolved,
            "keywords": normalized_keywords,
            "filters": normalized_filters,
            "group_by": normalized_groups,
        }
    )
    return normalized


def load_query_plan(path: Path, topic: str) -> dict[str, Any]:
    try:
        plan = read_json(path)
    except Exception as exc:
        raise QueryPlanError(f"could not read query plan {path}: {exc}") from exc
    return validate_query_plan(plan, topic)


def load_classification_rules(review_root: Path) -> dict[str, dict[str, list[str]]]:
    labels = {key: {} for key in STRUCTURED_TAG_KEYS}
    path = review_root / "allene_classification_rules.py"
    if not path.exists():
        return labels
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rules_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "rules":
                    rules_node = node.value
                    break
        if rules_node is not None:
            break
    if rules_node is None:
        return labels
    for item in ast.literal_eval(rules_node):
        if not isinstance(item, tuple) or len(item) < 3:
            continue
        label, category, aliases = str(item[0]).strip(), str(item[1]).strip(), item[2]
        if category in labels and label:
            labels[category][label] = [str(alias).strip() for alias in aliases if str(alias).strip()]
    return labels


def markdown_signal(meta: dict[str, Any], max_chars: int = 12000) -> str:
    source_paths = meta.get("source_paths") or {}
    raw_path = str(source_paths.get("markdown") or "").strip()
    if not raw_path:
        return ""
    path = Path(raw_path)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]


def tokenize(text: str) -> list[str]:
    return dedupe([w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'′\\-]*", text or "") if len(w) >= 3])


ENGLISH_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def contains_phrase(needle: str, haystack: str) -> bool:
    chunks = re.split(r"\s+", (needle or "").strip())
    if not chunks or not chunks[0]:
        return False
    pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(
        re.escape(chunk) for chunk in chunks
    ) + r"(?![A-Za-z0-9])"
    return re.search(pattern, haystack or "", re.I) is not None


def parse_topic_intent(topic: str, current_year: int | None = None) -> dict[str, Any]:
    current_year = current_year or datetime.now().year
    filters: dict[str, int] = {}
    match = re.search(
        r"(?<![A-Za-z0-9])(?:past|last)\s+"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"years?(?![A-Za-z0-9])",
        topic,
        re.I,
    )
    if match:
        count = (
            int(match.group(1))
            if match.group(1).isdigit()
            else ENGLISH_NUMBER_WORDS[match.group(1).lower()]
        )
        filters = {"year_from": current_year - count + 1, "year_to": current_year}
    chinese = re.search(r"(?:近|过去)\s*(\d+)\s*年", topic)
    if chinese:
        count = int(chinese.group(1))
        filters = {"year_from": current_year - count + 1, "year_to": current_year}
    catalyst_grouping = bool(
        re.search(
            r"(?<![A-Za-z0-9])(?:organized|grouped)\s+by\s+"
            r"(?:types?\s+of\s+)?catalysts?(?![A-Za-z0-9])",
            topic,
            re.I,
        )
        or re.search(r"(?:按照|按)\s*催化剂(?:种类|类型)", topic)
    )
    acronyms = dedupe(
        re.findall(r"(?<![A-Za-z0-9])([A-Z]{2,8})(?![A-Za-z0-9])", topic)
    )
    return {
        "filters": filters,
        "group_by": ["catalyst_or_method"] if catalyst_grouping else [],
        "unresolved_concepts": acronyms,
    }


def infer_keywords(
    topic: str,
    user_keywords: list[str],
    unresolved_surfaces: list[str] | None = None,
) -> list[dict[str, Any]]:
    text = " ".join([topic] + user_keywords)
    for surface in unresolved_surfaces or []:
        if not surface.isupper():
            continue
        chunks = re.split(r"\s+", surface.strip())
        if not chunks or not chunks[0]:
            continue
        pattern = r"(?<![A-Za-z0-9])" + r"\s+".join(
            re.escape(chunk) for chunk in chunks
        ) + r"(?![A-Za-z0-9])"
        text = re.sub(pattern, " ", text)
    rules = [
        ("polysubstituted allenes", "product", ["polysubstituted allene", "substituted allene"]),
        ("allenes", "product", ["allene", "allenes"]),
        ("allene synthesis", "reaction_type", ["allene synthesis", "synthesis of allene"]),
        ("propargylic alcohols", "substrate", ["propargylic alcohol"]),
        ("propargylic halides", "substrate", ["propargylic derivative", "propargyl halide", "propargyl bromide", "propargylic bromide"]),
        ("propargylic acetates", "substrate", ["acetate"]),
        ("propargylic carbonates", "substrate", ["carbonate"]),
        ("propargylic phosphates", "substrate", ["phosphate"]),
        ("propargylic halides", "substrate", ["bromide"]),
        ("propargylic sulfinates and sulfonates", "substrate", ["sulfide", "sulfinate", "sulfonate", "tosylate"]),
        ("propargylic dichlorides", "substrate", ["dichloride", "gem-dichloride"]),
        ("propargylic substitution and cross-coupling", "reaction_type", ["sn2", "substitution"]),
        ("propargylic substitution and cross-coupling", "reaction_type", ["allenylation"]),
        ("copper catalysis", "catalyst_or_method", ["copper", "cu", "cu(i)", "cu(iii)", "cubr", "cui", "cuoac", "cucl2", "icycucl", "organocopper", "cuprate"]),
        ("palladium catalysis", "catalyst_or_method", ["palladium", "pd", "pd(0)", "pd(ii)", "palladium species", "propargylpalladium", "allenylpalladium"]),
        ("zinc-mediated methods", "catalyst_or_method", ["zinc", "zn", "zn(ii)", "zni2", "znbr2", "zncl2", "organozinc"]),
        ("cadmium-mediated methods", "catalyst_or_method", ["cadmium", "cd", "cd(ii)", "cdi2"]),
        ("gold catalysis", "catalyst_or_method", ["gold", "au", "au(i)", "au(iii)", "kaucl4", "gold salen complex"]),
        ("silver-mediated methods", "catalyst_or_method", ["silver", "ag", "ag(i)", "agno3"]),
        ("rhodium catalysis", "catalyst_or_method", ["rhodium", "rh", "rh(i)", "rhodium complex", "rh/chiral diene complex"]),
        ("iron catalysis", "catalyst_or_method", ["iron", "fe", "iron-porphyrin", "iron porphyrin", "fe-porphyrin"]),
        ("copper-zinc bimetallic catalysis", "catalyst_or_method", ["copper-zinc", "copper/zinc", "cu/zn", "cu+/zn2+", "cubr/znbr2", "bimetallic approach", "bimetallic catalysis"]),
        ("photoredox catalysis", "catalyst_or_method", ["photoredox", "visible-light"]),
        ("asymmetric synthesis", "reaction_type", ["asymmetric", "enantioselective", "enantiospecific"]),
        ("radical and single-electron allene synthesis", "reaction_type", ["radical"]),
        ("Meyer-Schuster rearrangement", "reaction_type", ["meyer-schuster"]),
    ]
    candidates: list[dict[str, Any]] = []
    for kw, category, needles in rules:
        if any(contains_phrase(needle, text) for needle in needles):
            candidates.append({"keyword": kw, "category": category, "reason": "rule expansion from topic/user keywords"})
    return unique_keyword_dicts(candidates)


def unique_keyword_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = item["keyword"].lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build_keyword_set(
    topic: str,
    user_keywords: list[str],
    agent_keywords: list[dict[str, Any]] | None = None,
    query_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_keywords = dedupe(user_keywords)
    ignored_user_keywords = [
        keyword
        for keyword in user_keywords
        if keyword.casefold() in GENERIC_INSTRUCTION_KEYWORDS
    ]
    user_keywords = [
        keyword
        for keyword in user_keywords
        if keyword.casefold() not in GENERIC_INSTRUCTION_KEYWORDS
    ]
    unresolved_surfaces = []
    if query_context is not None:
        unresolved_surfaces = [
            str(item.get("surface") or "").strip()
            if isinstance(item, dict)
            else str(item).strip()
            for item in query_context.get("unresolved_concepts", [])
        ]
    agent = (
        infer_keywords(topic, user_keywords, unresolved_surfaces)
        if agent_keywords is None
        else agent_keywords
    )
    merged: dict[str, dict[str, Any]] = {}
    for kw in user_keywords:
        merged[kw.casefold()] = {"keyword": kw, "category": classify_keyword(kw), "source": ["user"], "keep": True}
    for item in agent:
        normalized_keyword = re.sub(r"\s+", " ", str(item["keyword"]).strip())
        key = normalized_keyword.casefold()
        declared_source = str(item.get("source") or "agent")
        if key in merged:
            if declared_source not in merged[key]["source"]:
                merged[key]["source"].append(declared_source)
            merged[key].update(
                {
                    "keyword": normalized_keyword,
                    "category": item["category"],
                    "reason": item.get("reason", ""),
                }
            )
        else:
            merged[key] = {"keyword": normalized_keyword, "category": item["category"], "source": [declared_source], "keep": True, "reason": item.get("reason", "")}
    for surface in unresolved_surfaces:
        for item in merged.values():
            if contains_phrase(surface, item["keyword"]):
                raise QueryPlanError(
                    f"merged keyword {item['keyword']!r} contains unresolved "
                    f"concept surface {surface!r}; resolve it or remove that keyword"
                )
    if not merged:
        if unresolved_surfaces:
            surfaces = ", ".join(unresolved_surfaces)
            raise QueryPlanError(
                "no meaningful keyword remains; resolve the unresolved concepts "
                f"or provide a validated chemistry keyword: {surfaces}"
            )
        raise QueryPlanError(
            "no meaningful keyword remains; clarify the topic or provide a "
            "validated chemistry keyword"
        )
    result = {
        "user_topic": topic,
        "user_keywords": user_keywords,
        "ignored_user_keywords": ignored_user_keywords,
        "agent_keywords": agent,
        "merged_keywords": list(merged.values()),
        "created_at": utc_now(),
    }
    if query_context is not None:
        for field in (
            "resolved_concepts",
            "unresolved_concepts",
            "filters",
            "group_by",
            "query_plan_source",
            "query_plan_path",
        ):
            if field in query_context:
                result[field] = query_context[field]
    return result


def classify_keyword(keyword: str) -> str:
    low = keyword.lower()
    if any(x in low for x in ["alcohol", "acetate", "carbonate", "phosphate", "sulfide", "bromide", "derivative", "dichloride"]):
        return "substrate"
    if "allene" in low:
        return "product"
    if any(x in low for x in ["catalysis", "copper", "nickel", "palladium", "photoredox"]):
        return "catalyst_or_method"
    if any(x in low for x in ["sn2", "rearrangement", "allenylation", "synthesis"]):
        return "reaction_type"
    return "reaction_type"


STRUCTURED_TAG_WEIGHTS = {
    "product": 5.0,
    "substrate": 5.0,
    "catalyst_or_method": 4.4,
    "organometallic_partner": 4.0,
    "ligand_or_chiral_source": 3.8,
    "leaving_group": 3.8,
    "reaction_type": 4.8,
    "document_scope": 1.5,
}


def structured_tag_text(meta: dict[str, Any], tag_key: str, classification_rules: dict[str, dict[str, list[str]]]) -> str:
    structured = field_value(meta.get("structured_tags"), {})
    if not isinstance(structured, dict):
        return ""
    value = str(structured.get(tag_key) or "")
    if value.strip().lower() == "not specified":
        return ""
    aliases = classification_rules.get(tag_key, {}).get(value, [])
    return " ".join([value] + aliases)


def match_score(term: str, text: str) -> float:
    if not term or not text:
        return 0.0
    t = term.lower()
    if contains_phrase(t, text):
        return 1.0
    tokens = tokenize(t)
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if contains_phrase(token, text))
    ratio = hits / len(tokens)
    if len(tokens) == 1:
        return 0.65 if hits else 0.0
    if ratio == 1.0:
        return 0.72
    if ratio >= 0.67 and len(tokens) >= 3:
        return 0.38
    return 0.0


def score_local_paper(
    meta: dict[str, Any],
    keyword: str,
    keyword_category: str,
    topic_terms: list[str],
    classification_rules: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    if keyword_category not in STRUCTURED_TAG_KEYS:
        raise ValueError(f"unsupported keyword category: {keyword_category!r}")
    matched_fields: list[str] = []
    matched_terms: list[str] = []
    reasons: list[str] = []
    raw = 0.0
    direct_raw = 0.0
    text = structured_tag_text(meta, keyword_category, classification_rules)
    s = match_score(keyword, text)
    if s > 0:
        contribution = s * STRUCTURED_TAG_WEIGHTS[keyword_category]
        raw += contribution
        direct_raw += contribution
        matched_fields.append(keyword_category)
        matched_terms.append(keyword)
        reasons.append(f"structured_tags.{keyword_category} matched keyword")
    topic_hits = sum(1 for term in topic_terms if match_score(term, text) > 0)
    if topic_hits and s > 0:
        raw += min(topic_hits * 0.15, 0.9)
    source_text = " ".join(
        [
            str(field_value(meta.get("title"), "")),
            markdown_signal(meta),
        ]
    )
    source_signal = match_score(keyword, source_text)
    if source_signal > 0 and direct_raw > 0:
        raw += min(source_signal * 0.8, 0.8)
        reasons.append("source text confirms keyword")
    raw_year = field_value(meta.get("year"))
    year = raw_year if type(raw_year) is int else None
    source_paths = meta.get("source_paths") or {}
    normalized = min(round(raw / 8.0, 4), 1.0)
    if normalized >= 0.65:
        role = "core_candidate"
    elif normalized >= 0.35:
        role = "supporting_candidate"
    elif normalized >= 0.15:
        role = "background"
    else:
        role = "uncertain"
    return {
        "paper_id": meta.get("paper_id"),
        "title": field_value(meta.get("title"), ""),
        "authors": field_value(meta.get("authors"), []),
        "year": year,
        "journal": field_value(meta.get("journal")),
        "doi": field_value(meta.get("doi")),
        "score": normalized,
        "raw_score": round(raw, 3),
        "direct_raw_score": round(direct_raw, 3),
        "matched_fields": dedupe(matched_fields),
        "matched_terms": dedupe(matched_terms),
        "reason": "; ".join(reasons) if reasons else "weak or no direct local metadata match",
        "role": role,
        "keep": normalized > 0,
        "source_paths": source_paths,
    }


def local_search_by_keyword(
    papers: dict[str, dict[str, Any]],
    keywords: list[dict[str, Any]],
    topic: str,
    classification_rules: dict[str, dict[str, list[str]]],
    year_from: int | None = None,
    year_to: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    filter_stats = {
        "before_filter": len(papers),
        "after_filter": 0,
        "missing_year_excluded": 0,
        "out_of_range_excluded": 0,
    }
    filtered_papers: list[dict[str, Any]] = []
    year_filter_active = year_from is not None or year_to is not None
    for meta in papers.values():
        year = field_value(meta.get("year"))
        valid_year = year if type(year) is int else None
        if year_filter_active and valid_year is None:
            filter_stats["missing_year_excluded"] += 1
            continue
        if (
            (year_from is not None and valid_year < year_from)
            or (year_to is not None and valid_year > year_to)
        ):
            filter_stats["out_of_range_excluded"] += 1
            continue
        filtered_papers.append(meta)
    filter_stats["after_filter"] = len(filtered_papers)

    topic_terms = tokenize(topic)
    grouped: list[dict[str, Any]] = []
    for kw in keywords:
        if not kw.get("keep", True):
            continue
        keyword = kw["keyword"]
        keyword_category = kw.get("category")
        results = [
            score_local_paper(
                meta,
                keyword,
                keyword_category,
                topic_terms,
                classification_rules,
            )
            for meta in filtered_papers
        ]
        results = [r for r in results if r["direct_raw_score"] >= 1.4 and r["score"] >= 0.12]
        results.sort(key=lambda r: (r["score"], r["raw_score"], r.get("year") or 0), reverse=True)
        grouped.append({"keyword": keyword, "category": keyword_category, "keep": True, "local_results": results})
    return grouped, filter_stats


def web_search(keyword: str, topic: str, limit: int = 8) -> list[dict[str, Any]]:
    query = f"{keyword} {topic} review paper DOI"
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query, "rows": str(limit)})
    req = urllib.request.Request(url, headers={"User-Agent": "review-writer-discovery/0.1 (mailto:example@example.com)"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return [{"title": f"WEB_SEARCH_FAILED: {type(exc).__name__}", "url": "", "score": 0, "reason": str(exc), "keep": False}]
    results = []
    topic_terms = tokenize(topic)
    for item in data.get("message", {}).get("items", []):
        title = " ".join(item.get("title") or []) or "(untitled)"
        container = " ".join(item.get("container-title") or [])
        abstract = re.sub("<[^>]+>", " ", item.get("abstract") or "")
        hay = " ".join([title, container, abstract]).lower()
        score = 0.0
        if keyword.lower() in hay:
            score += 0.55
        score += min(sum(1 for term in topic_terms if term in hay) * 0.04, 0.32)
        if item.get("DOI"):
            score += 0.08
        year = None
        issued = item.get("issued", {}).get("date-parts") or []
        if issued and issued[0]:
            year = issued[0][0]
            if isinstance(year, int) and year >= 2020:
                score += 0.05
        doi = item.get("DOI")
        link = f"https://doi.org/{doi}" if doi else item.get("URL", "")
        results.append(
            {
                "title": title,
                "authors": format_crossref_authors(item.get("author", [])),
                "year": year,
                "journal": container,
                "doi": doi,
                "url": link,
                "score": round(min(score, 1.0), 4),
                "reason": "Crossref title/snippet/topic/DOI overlap score",
                "keep": score > 0.15,
                "source": "crossref",
            }
        )
    results.sort(key=lambda r: (r["score"], r.get("year") or 0), reverse=True)
    return results


def format_crossref_authors(authors: list[dict[str, Any]]) -> list[str]:
    out = []
    for author in authors[:8]:
        name = " ".join(x for x in [author.get("given"), author.get("family")] if x)
        if name:
            out.append(name)
    return out




def normalize_sciatlas_paper(item: dict[str, Any]) -> dict[str, Any]:
    # SciAtlas /v1/search nests the canonical record in `paper`; fall back to top-level keys.
    nested = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    def first(*keys):
        for src in (item, nested):
            for k in keys:
                v = src.get(k)
                if v not in (None, "", []):
                    return v
        return None
    title = first("title", "paper_title") or "(untitled)"
    if isinstance(title, str):
        title = title.replace("\n", " ").strip()
    authors = first("authors", "author_names") or []
    if isinstance(authors, list):
        normalized_authors: list[str] = []
        for entry in authors:
            if isinstance(entry, str):
                normalized_authors.append(entry)
            elif isinstance(entry, dict):
                name = entry.get("name") or entry.get("display_name")
                if not name:
                    parts = [entry.get("given"), entry.get("family")]
                    name = " ".join(x for x in parts if x).strip()
                if name:
                    normalized_authors.append(name)
        authors = normalized_authors
    else:
        authors = []
    year = first("year", "publication_year")
    journal = first("journal", "venue", "container_title", "venue_source_display_name") or ""
    doi = first("doi", "DOI") or ""
    if isinstance(doi, str) and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    paper_url = first("paper_url", "pdf_url", "url", "html_url")
    url = paper_url or (f"https://doi.org/{doi}" if doi else "")
    abstract = first("abstract") or ""
    raw_score = item.get("score") or item.get("relevance_score") or item.get("graph_score") or 0.0
    try:
        raw_score = float(raw_score)
    except (TypeError, ValueError):
        raw_score = 0.0
    # SciAtlas scores can exceed 1; clamp + soft normalize for UI consistency.
    norm = min(round(raw_score / 10.0, 4) if raw_score > 1 else round(raw_score, 4), 1.0)
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": doi,
        "url": url,
        "abstract": abstract[:600],
        "score": norm,
        "raw_score": raw_score,
        "reason": "SciAtlas KG retrieval (hybrid)",
        "keep": norm > 0,
        "source": "sciatlas",
    }


def sciatlas_search(
    client: SciAtlasClient,
    keyword: str,
    topic: str,
    limit: int,
    time_range: str | None,
    domain: str | None,
) -> list[dict[str, Any]]:
    try:
        response = client.search_papers(
            query=topic or keyword,
            keyword=keyword,
            top_k=max(limit, 1),
            retrieval_mode="hybrid",
            time_range=time_range,
            domain=domain,
        )
    except Exception as exc:
        return [{"title": f"SCIATLAS_SEARCH_FAILED: {type(exc).__name__}", "url": "", "score": 0, "reason": str(exc), "keep": False, "source": "sciatlas"}]
    results = [normalize_sciatlas_paper(item) for item in papers_from_response(response)]
    results.sort(key=lambda r: (r.get("score", 0), r.get("year") or 0), reverse=True)
    return results



def _result_dedupe_key(row: dict[str, Any]) -> str:
    doi = (row.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    url = (row.get("url") or "").strip().lower()
    if url:
        return "url:" + url
    title = re.sub(r"\s+", " ", str(row.get("title") or "").strip().lower())
    return "title:" + title


def merge_external_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _result_dedupe_key(row)
        if not key:
            continue
        if key not in merged:
            merged[key] = {**row, "sources": [row.get("source", "external")]}
            order.append(key)
            continue
        existing = merged[key]
        src = row.get("source", "external")
        if src not in existing.get("sources", []):
            existing.setdefault("sources", []).append(src)
        if (row.get("score") or 0) > (existing.get("score") or 0):
            # Promote the higher-scoring record while keeping merged source list.
            sources = existing.get("sources", [])
            merged[key] = {**row, "sources": sources}
        if not existing.get("doi") and row.get("doi"):
            existing["doi"] = row.get("doi")
        if not existing.get("url") and row.get("url"):
            existing["url"] = row.get("url")
        if not existing.get("abstract") and row.get("abstract"):
            existing["abstract"] = row.get("abstract")
    out: list[dict[str, Any]] = []
    for key in order:
        row = merged[key]
        sources = row.get("sources") or [row.get("source", "external")]
        # Keep `source` as the primary (highest-scoring) one for backward compat.
        row["source"] = sources[0] if len(sources) == 1 else "+".join(sources)
        row["sources"] = sources
        out.append(row)
    out.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    return out

def combine_results(local_grouped: list[dict[str, Any]], web_grouped: list[dict[str, Any]]) -> list[dict[str, Any]]:
    web_map = {g["keyword"]: g for g in web_grouped}
    combined = []
    for group in local_grouped:
        keyword = group["keyword"]
        combined.append(
            {
                "keyword": keyword,
                "category": group.get("category"),
                "keep": group.get("keep", True),
                "local_results": group.get("local_results", []),
                "web_results": web_map.get(keyword, {}).get("web_results", []),
            }
        )
    return combined


def selected_from_combined(combined: list[dict[str, Any]]) -> dict[str, Any]:
    selected = {"keywords": [], "local_papers": {}, "web_papers": []}
    for group in combined:
        if not group.get("keep", True):
            continue
        selected["keywords"].append({"keyword": group["keyword"], "category": group.get("category")})
        for result in group.get("local_results", []):
            if not result.get("keep", True):
                continue
            pid = result.get("paper_id")
            if not pid:
                continue
            entry = selected["local_papers"].setdefault(
                pid,
                {
                    "paper_id": pid,
                    "title": result.get("title"),
                    "year": result.get("year"),
                    "journal": result.get("journal"),
                    "role": result.get("role", "uncertain"),
                    "matched_keywords": [],
                    "best_score": 0,
                    "keep": True,
                },
            )
            entry["matched_keywords"].append(group["keyword"])
            entry["best_score"] = max(entry["best_score"], result.get("score", 0))
            if role_rank(result.get("role")) < role_rank(entry["role"]):
                entry["role"] = result.get("role")
        for result in group.get("web_results", []):
            if result.get("keep", True):
                selected["web_papers"].append({**result, "matched_keyword": group["keyword"]})
    selected["local_papers"] = list(selected["local_papers"].values())
    selected["local_papers"].sort(key=lambda r: (r["best_score"], r.get("year") or 0), reverse=True)
    selected["local_papers"] = selected["local_papers"][:30]
    return selected


def group_selected_papers(
    selected: dict[str, Any],
    papers: dict[str, dict[str, Any]],
    group_by: list[str],
) -> dict[str, Any]:
    grouped: dict[str, Any] = {}
    selected_ids = {
        row.get("paper_id")
        for row in selected.get("local_papers", [])
        if row.get("paper_id")
    }
    for field in group_by:
        buckets: dict[str, set[str]] = {}
        for paper_id in selected_ids:
            meta = papers.get(paper_id, {})
            structured_tags = field_value(meta.get("structured_tags"), {})
            raw_value = (
                structured_tags.get(field)
                if isinstance(structured_tags, dict)
                else None
            )
            value = str(raw_value).strip() if raw_value is not None else ""
            value = value or "not specified"
            buckets.setdefault(value, set()).add(paper_id)
        grouped[field] = {
            value: {
                "count": len(paper_ids),
                "paper_ids": sorted(paper_ids),
            }
            for value, paper_ids in sorted(buckets.items())
        }
    return grouped


def role_rank(role: str | None) -> int:
    order = {"core_candidate": 0, "supporting_candidate": 1, "background": 2, "uncertain": 3, "excluded": 4}
    return order.get(role or "uncertain", 3)


def write_report(
    out_dir: Path,
    topic: str,
    keyword_set: dict[str, Any],
    combined: list[dict[str, Any]],
    selected_count: int,
) -> None:
    filters = keyword_set.get("filters") or {}
    filter_stats = keyword_set.get("filter_stats") or {}
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if year_from is None and year_to is None:
        effective_year_range = "none"
    else:
        effective_year_range = (
            f"{year_from if year_from is not None else 'unbounded'}-"
            f"{year_to if year_to is not None else 'unbounded'}"
        )
    unresolved = keyword_set.get("unresolved_concepts") or []
    unresolved_surfaces = [
        str(item.get("surface") or "").strip()
        if isinstance(item, dict)
        else str(item).strip()
        for item in unresolved
    ]
    unresolved_text = ", ".join(value for value in unresolved_surfaces if value) or "none"
    grouping_text = ", ".join(keyword_set.get("group_by") or []) or "none"
    zero_match_groups = sum(
        1 for group in combined if not group.get("local_results")
    )
    matched_groups = len(combined) - zero_match_groups
    lines = [
        "# Topic Paper Discovery Report",
        "",
        f"Topic: {topic}",
        f"Query-plan source: {keyword_set.get('query_plan_source') or 'topic_intent'}",
        f"Query-plan path: {keyword_set.get('query_plan_path') or 'none'}",
        f"Effective year range: {effective_year_range}",
        f"Papers before year filtering: {filter_stats.get('before_filter', 0)}",
        f"Papers after year filtering: {filter_stats.get('after_filter', 0)}",
        f"Papers excluded for missing year: {filter_stats.get('missing_year_excluded', 0)}",
        f"Papers excluded outside year range: {filter_stats.get('out_of_range_excluded', 0)}",
        f"Unresolved concepts: {unresolved_text}",
        f"Requested grouping fields: {grouping_text}",
        f"Selected local papers: {selected_count}",
        f"Keyword groups with local matches: {matched_groups}",
        f"Keyword groups with zero local matches: {zero_match_groups}",
        "",
        "## Keywords",
        "",
    ]
    if selected_count == 0:
        lines.extend(
            [
                "No local papers matched the validated keywords and filters.",
                "Fewer than 20 local papers were selected because only 0 unique "
                "local papers matched the validated keywords and filters.",
                "",
            ]
        )
    elif selected_count < 20:
        lines.extend(
            [
                f"Fewer than 20 local papers were selected because only {selected_count} "
                "unique local papers matched the validated keywords and filters.",
                "",
            ]
        )
    for kw in keyword_set["merged_keywords"]:
        lines.append(f"- {kw['keyword']} ({kw.get('category')}, source={'+'.join(kw.get('source', []))})")
    lines += ["", "## Results by Keyword", ""]
    for group in combined:
        lines.append(f"### {group['keyword']}")
        lines.append("")
        lines.append("Local:")
        for result in group.get("local_results", [])[:10]:
            lines.append(f"- `{result['paper_id']}` score={result['score']:.3f} role={result['role']} {result['title']}")
        if group.get("web_results"):
            lines.append("")
            lines.append("Web:")
            for result in group.get("web_results", [])[:8]:
                lines.append(f"- score={result['score']:.3f} {result['title']} {result.get('url') or ''}")
        lines.append("")
    (out_dir / "discovery_report.md").write_text("\n".join(lines), encoding="utf-8")


def _load_dotenv_if_present(review_root: Path) -> None:
    env_path = review_root / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except Exception:
        pass


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    project_id = args.project_id or slugify(args.topic)
    project = resolve_project_path(review_root, project_id)
    _load_dotenv_if_present(review_root)
    user_keywords = split_keywords(args.keywords)
    query_plan_path = getattr(args, "query_plan", "")
    if query_plan_path:
        query_plan = load_query_plan(Path(query_plan_path), args.topic)
        query_plan_source = "llm_plan"
        effective_query_plan_path = str(query_plan_path)
        agent_keywords = query_plan["keywords"]
        resolved_concepts = query_plan["resolved_concepts"]
        unresolved_concepts = query_plan["unresolved_concepts"]
        filters = query_plan["filters"]
        group_by = query_plan["group_by"]
    else:
        topic_intent = parse_topic_intent(args.topic)
        query_plan_source = "topic_intent"
        effective_query_plan_path = None
        agent_keywords = None
        resolved_concepts = []
        unresolved_concepts = topic_intent["unresolved_concepts"]
        filters = topic_intent["filters"]
        group_by = topic_intent["group_by"]
    query_context = {
        "query_plan_source": query_plan_source,
        "resolved_concepts": resolved_concepts,
        "unresolved_concepts": unresolved_concepts,
        "filters": filters,
        "group_by": group_by,
    }
    if effective_query_plan_path is not None:
        query_context["query_plan_path"] = effective_query_plan_path

    keyword_set = build_keyword_set(
        args.topic,
        user_keywords,
        agent_keywords=agent_keywords,
        query_context=query_context,
    )
    out_dir = project / "00_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "topic_input.md").write_text(
        f"# {args.topic}\n\nUser keywords:\n\n" + "\n".join(f"- {kw}" for kw in user_keywords) + "\n",
        encoding="utf-8",
    )
    papers = load_metadata(review_root)
    classification_rules = load_classification_rules(review_root)
    local_grouped, filter_stats = local_search_by_keyword(
        papers,
        keyword_set["merged_keywords"],
        args.topic,
        classification_rules,
        year_from=filters.get("year_from"),
        year_to=filters.get("year_to"),
    )
    sciatlas_requested = bool(args.sciatlas_search)
    crossref_requested = bool(args.web_search)
    sciatlas_client: SciAtlasClient | None = None
    sciatlas_status = "disabled"
    if sciatlas_requested:
        config = load_config(
            base_url=args.sciatlas_base_url or None,
            api_key=args.sciatlas_api_key or None,
            timeout=args.sciatlas_timeout or None,
        )
        if not config.configured:
            sciatlas_status = "missing_api_key"
        else:
            sciatlas_client = SciAtlasClient(config=config)
            try:
                sciatlas_client.health()
                sciatlas_status = "ok"
            except Exception as exc:
                sciatlas_status = f"health_failed: {exc}"
                sciatlas_client = None

    external_grouped: list[dict[str, Any]] = []
    sources_used: list[str] = []
    for group in local_grouped:
        rows: list[dict[str, Any]] = []
        if sciatlas_client is not None:
            sciatlas_rows = sciatlas_search(
                sciatlas_client,
                group["keyword"],
                args.topic,
                args.sciatlas_limit,
                args.sciatlas_time_range or None,
                args.sciatlas_domain or None,
            )
            rows.extend(sciatlas_rows)
            if sciatlas_rows and "sciatlas" not in sources_used:
                sources_used.append("sciatlas")
            if args.web_delay:
                time.sleep(args.web_delay)
        if crossref_requested:
            crossref_rows = web_search(group["keyword"], args.topic, args.web_limit)
            rows.extend(crossref_rows)
            if crossref_rows and "crossref" not in sources_used:
                sources_used.append("crossref")
            if args.web_delay:
                time.sleep(args.web_delay)
        merged = merge_external_results(rows)
        if merged:
            external_grouped.append({"keyword": group["keyword"], "web_results": merged})

    if sciatlas_requested and sciatlas_client is None and not crossref_requested:
        external_status = sciatlas_status
    elif sciatlas_requested and crossref_requested and sciatlas_client is None:
        external_status = f"sciatlas_unavailable({sciatlas_status}); crossref_active"
    elif sciatlas_client is not None and crossref_requested:
        external_status = "sciatlas+crossref"
    elif sciatlas_client is not None:
        external_status = "sciatlas"
    elif crossref_requested:
        external_status = "crossref"
    else:
        external_status = "disabled"

    if not sources_used:
        external_source = "none"
    elif len(sources_used) == 1:
        external_source = sources_used[0]
    else:
        external_source = "+".join(sources_used)

    write_json(out_dir / "web_results_by_keyword.json", {
        "project_id": project_id,
        "enabled": bool(external_grouped),
        "source": external_source,
        "status": external_status,
        "sources": sources_used,
        "results": external_grouped,
    })
    web_grouped = external_grouped
    combined = combine_results(local_grouped, web_grouped)
    selected = selected_from_combined(combined)
    groups = group_selected_papers(selected, papers, group_by)
    output_context = {
        **query_context,
        "filter_stats": filter_stats,
        "groups": groups,
    }
    keyword_set.update(output_context)
    write_json(out_dir / "keyword_set.draft.json", keyword_set)
    write_json(
        out_dir / "local_results_by_keyword.json",
        {"project_id": project_id, **output_context, "results": local_grouped},
    )
    write_json(
        out_dir / "combined_results_by_keyword.json",
        {
            "project_id": project_id,
            "topic": args.topic,
            **output_context,
            "results": combined,
        },
    )
    selected["project_id"] = project_id
    selected["human_confirmed"] = False
    selected.update(output_context)
    write_json(out_dir / "selected_discovery_results.json", selected)
    write_json(
        out_dir / "human_check_state.json",
        {
            "project_id": project_id,
            "status": "pending",
            "confirmed_at": None,
            "instructions": "Use the dashboard to delete irrelevant keywords/results, then mark discovery confirmed.",
        },
    )
    write_report(
        out_dir,
        args.topic,
        keyword_set,
        combined,
        selected_count=len(selected["local_papers"]),
    )
    print(f"Discovery project: {project}")
    print(f"Keyword set: {out_dir / 'keyword_set.draft.json'}")
    print(f"Human dashboard data: {out_dir / 'combined_results_by_keyword.json'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover local and web papers by expanded topic keywords.")
    parser.add_argument("--review-root", default="/home/ps/review-writer")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--query-plan", default="", help="Path to a validated query-plan JSON file.")
    parser.add_argument("--web-search", action="store_true", help="Fallback: query Crossref when SciAtlas is unavailable.")
    parser.add_argument("--web-limit", type=int, default=8)
    parser.add_argument("--web-delay", type=float, default=0.2)
    parser.add_argument("--sciatlas-search", action="store_true", help="Query the hosted SciAtlas KG /v1/search per keyword.")
    parser.add_argument("--sciatlas-limit", type=int, default=8)
    parser.add_argument("--sciatlas-api-key", default="", help="Overrides SCIATLAS_API_KEY env var.")
    parser.add_argument("--sciatlas-base-url", default="", help="Overrides SCIATLAS_API_BASE_URL env var.")
    parser.add_argument("--sciatlas-timeout", type=int, default=0, help="HTTP timeout in seconds. 0 = use env/default.")
    parser.add_argument("--sciatlas-time-range", default="", help="Optional year range like 2018-2025.")
    parser.add_argument("--sciatlas-domain", default="", help="Optional domain hint, e.g. 'organic chemistry'.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
