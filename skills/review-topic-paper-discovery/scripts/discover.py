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

from sciatlas_client import SciAtlasClient, load_config as load_sciatlas_config, papers_from_response


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:96] or "review-discovery"


def resolve_project_path(review_root: Path, project_id: str) -> Path:
    """Resolve project_id to a path-traversal-safe directory under review-projects/."""
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,95})", project_id
    ):
        raise SystemExit(
            "--project-id must be one safe slug component containing only letters, "
            "numbers, underscores, or hyphens"
        )
    projects_root = (review_root / "review-projects").resolve()
    project = (projects_root / project_id).resolve()
    try:
        relative = project.relative_to(projects_root)
    except ValueError:
        raise SystemExit("--project-id resolves outside review-root/review-projects")
    if relative == Path(".") or len(relative.parts) != 1:
        raise SystemExit("--project-id must resolve to one project component")
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
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
]


def load_classification_rules(path: Path | None) -> dict[str, dict[str, list[str]]]:
    """Load an optional label-alias table used to widen keyword matching against
    structured_tags values (see SKILL.md). Purely an enrichment: if no file is
    given, or it doesn't exist, matching falls back to exact tag-value text only —
    scoring still works, just with slightly lower recall for phrasing variants.
    """
    labels = {key: {} for key in STRUCTURED_TAG_KEYS}
    if not path or not path.exists():
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
    raw = source_paths.get("markdown") or ""
    if not raw:
        return ""
    path = Path(str(raw))
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


def parse_year_filters(topic: str, current_year: int | None = None) -> dict[str, int]:
    """Parse an explicit year range out of the topic text (e.g. "past 5 years"
    or "近5年"). Returns {} when no such phrase is present. Domain-agnostic --
    unlike keyword expansion, this doesn't depend on the review's subject matter.
    """
    current_year = current_year or datetime.now().year
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
        return {"year_from": current_year - count + 1, "year_to": current_year}
    chinese = re.search(r"(?:近|过去)\s*(\d+)\s*年", topic)
    if chinese:
        count = int(chinese.group(1))
        return {"year_from": current_year - count + 1, "year_to": current_year}
    return {}


def group_selected_papers(
    selected: dict[str, Any],
    papers: dict[str, dict[str, Any]],
    group_by: list[str],
) -> dict[str, Any]:
    """Bucket selected local papers by one or more structured-tag fields."""
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


def load_agent_keywords(path: Path | None) -> list[dict[str, Any]]:
    """Load LLM-authored keyword expansion. See SKILL.md for the required schema.

    Expected JSON: a list of {"keyword": str, "category": str, "reason": str}.
    This file must be written by the LLM before running this script — no
    keyword expansion rules are hardcoded here, since expansion depends on
    the review topic's subject matter, which is not known ahead of time.
    """
    if not path:
        return []
    if not path.exists():
        raise SystemExit(
            f"--agent-keywords file not found: {path}\n"
            "Expand the topic into search keywords first (see SKILL.md) and write them to this path."
        )
    data = read_json(path)
    if not isinstance(data, list):
        raise SystemExit(f"--agent-keywords file must contain a JSON list: {path}")
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("keyword"):
            out.append(
                {
                    "keyword": str(item["keyword"]),
                    "category": str(item.get("category") or "").strip(),
                    "reason": str(item.get("reason") or "llm keyword expansion"),
                }
            )
    return unique_keyword_dicts(out)


def unique_keyword_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = item["keyword"].lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build_keyword_set(topic: str, user_keywords: list[str], agent_keywords: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for kw in user_keywords:
        merged[kw.lower()] = {"keyword": kw, "category": "", "source": ["user"], "keep": True}
    for item in agent_keywords:
        key = item["keyword"].lower()
        if key in merged:
            if "agent" not in merged[key]["source"]:
                merged[key]["source"].append("agent")
            if not merged[key].get("category"):
                merged[key]["category"] = item["category"]
        else:
            merged[key] = {"keyword": item["keyword"], "category": item["category"], "source": ["agent"], "keep": True, "reason": item.get("reason", "")}
    return {
        "user_topic": topic,
        "user_keywords": user_keywords,
        "agent_keywords": agent_keywords,
        "merged_keywords": list(merged.values()),
        "created_at": utc_now(),
    }


STRUCTURED_TAG_WEIGHTS = {
    "output": 5.0,
    "input": 5.0,
    "method": 4.4,
    "co_input": 4.0,
    "modifier": 3.8,
    "process_type": 4.8,
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


def contains_word(needle: str, haystack: str) -> bool:
    """Word-boundary substring check.

    Plain `needle in haystack` false-positives badly on short terms: "rag" is
    a literal substring of "encouRAGed", "storage", "paragraph", etc. Every
    match in this module must go through this (or tokenize(), which already
    splits on non-word characters) instead of the bare `in` operator.
    """
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def tokens_cooccur(tokens: list[str], text: str, window: int = 300) -> bool:
    """True if every token's first occurrence falls within `window` chars of the others.

    Guards the ratio==1.0 case below: a multi-word term where every individual
    token happens to appear *somewhere* in a long blob (markdown_signal is up
    to 12000 chars) is not evidence the phrase's meaning is present — e.g.
    "knowledge base" scored a full match on a chemistry paper because
    "knowledge" and "base" (a common reagent term) each occurred, unrelated,
    thousands of characters apart. Requiring rough proximity keeps genuine
    phrase-like co-occurrences while rejecting coincidental long-range hits.
    """
    positions = []
    for tok in tokens:
        m = re.search(r"\b" + re.escape(tok) + r"\b", text)
        if not m:
            return False
        positions.append(m.start())
    return (max(positions) - min(positions)) <= window


def match_score(term: str, text: str) -> float:
    if not term or not text:
        return 0.0
    low = text.lower()
    t = term.lower()
    if contains_word(t, low):
        return 1.0
    tokens = tokenize(t)
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if contains_word(token, low))
    ratio = hits / len(tokens)
    if len(tokens) == 1:
        return 0.65 if hits else 0.0
    if ratio == 1.0:
        return 0.72 if tokens_cooccur(tokens, low) else 0.0
    # 0.66 not 0.67: a 2-of-3 token match is ratio 0.6667, and the intent is to
    # accept it — with 0.67 it silently scores zero (float comparison off-by-epsilon).
    if ratio >= 0.66 and len(tokens) >= 3:
        return 0.38
    return 0.0


def has_structured_tags(meta: dict[str, Any]) -> bool:
    structured = field_value(meta.get("structured_tags"), {})
    if not isinstance(structured, dict):
        return False
    return any(str(v).strip().lower() not in ("", "not specified") for v in structured.values())


# Weight for title/abstract/markdown matching when a paper has no structured
# tags yet (rule-only or stub metadata). Comparable to the mid-range tag
# weights so untagged papers can still be shortlisted — this enables the
# two-pass "tag after discovery" flow for large libraries (see SKILL.md).
UNTAGGED_SOURCE_WEIGHT = 4.5


def score_local_paper(
    meta: dict[str, Any],
    keyword: str,
    topic_terms: list[str],
    classification_rules: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    matched_fields: list[str] = []
    matched_terms: list[str] = []
    reasons: list[str] = []
    raw = 0.0
    direct_raw = 0.0
    tags_available = has_structured_tags(meta)
    for field, weight in STRUCTURED_TAG_WEIGHTS.items():
        text = structured_tag_text(meta, field, classification_rules)
        s = match_score(keyword, text)
        if s > 0:
            contribution = s * weight
            raw += contribution
            direct_raw += contribution
            matched_fields.append(field)
            matched_terms.append(keyword)
            reasons.append(f"structured_tags.{field} matched keyword")
        topic_hits = sum(1 for term in topic_terms if match_score(term, text) > 0)
        if topic_hits and s > 0:
            raw += min(topic_hits * 0.15, 0.9)
    source_text = " ".join(
        [
            str(field_value(meta.get("title"), "")),
            str(field_value(meta.get("abstract"), "") or ""),
            markdown_signal(meta),
        ]
    )
    source_signal = match_score(keyword, source_text)
    if source_signal > 0 and direct_raw > 0:
        raw += min(source_signal * 0.8, 0.8)
        reasons.append("source text confirms keyword")
    elif source_signal > 0 and not tags_available:
        contribution = source_signal * UNTAGGED_SOURCE_WEIGHT
        raw += contribution
        direct_raw += contribution
        matched_fields.append("source_text")
        matched_terms.append(keyword)
        reasons.append("no structured tags yet; scored on title/abstract/markdown")
    year = field_value(meta.get("year"))
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


# Selection thresholds for local scoring, and the near-miss band beneath them.
# A paper falling below the selection cut but at or above the near-miss floor is
# not silently dropped: it is surfaced as a borderline paper for agent/human
# review, since phrasing drift between topic keywords and paper metadata is the
# most common cause of on-topic papers scoring low.
SELECT_DIRECT_RAW = 1.4
SELECT_SCORE = 0.12
NEAR_MISS_DIRECT_RAW = 0.6


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
    year_filter_active = year_from is not None or year_to is not None
    filtered_papers: dict[str, dict[str, Any]] = {}
    for paper_id, meta in papers.items():
        year = field_value(meta.get("year"))
        valid_year = year if type(year) is int else None
        if year_filter_active and valid_year is None:
            filter_stats["missing_year_excluded"] += 1
            continue
        if (
            (year_from is not None and valid_year is not None and valid_year < year_from)
            or (year_to is not None and valid_year is not None and valid_year > year_to)
        ):
            filter_stats["out_of_range_excluded"] += 1
            continue
        filtered_papers[paper_id] = meta
    filter_stats["after_filter"] = len(filtered_papers)

    topic_terms = tokenize(topic)
    grouped: list[dict[str, Any]] = []
    for kw in keywords:
        if not kw.get("keep", True):
            continue
        keyword = kw["keyword"]
        scored = [score_local_paper(meta, keyword, topic_terms, classification_rules) for meta in filtered_papers.values()]
        results = [r for r in scored if r["direct_raw_score"] >= SELECT_DIRECT_RAW and r["score"] >= SELECT_SCORE]
        selected_ids = {r["paper_id"] for r in results}
        near_miss = [
            r
            for r in scored
            if r["paper_id"] not in selected_ids and r["direct_raw_score"] >= NEAR_MISS_DIRECT_RAW
        ]
        results.sort(key=lambda r: (r["score"], r["raw_score"], r.get("year") or 0), reverse=True)
        near_miss.sort(key=lambda r: (r["direct_raw_score"], r["score"]), reverse=True)
        grouped.append(
            {
                "keyword": keyword,
                "category": kw.get("category"),
                "keep": True,
                "local_results": results,
                "near_miss_results": near_miss,
            }
        )
    return grouped, filter_stats


def web_search(keyword: str, topic: str, limit: int = 8, mailto: str = "") -> list[dict[str, Any]]:
    query = f"{keyword} {topic} review paper DOI"
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": query, "rows": str(limit)})
    contact = mailto or "anonymous@example.com"
    req = urllib.request.Request(url, headers={"User-Agent": f"review-writer-discovery/0.1 (mailto:{contact})"})
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
        if contains_word(keyword.lower(), hay):
            score += 0.55
        score += min(sum(1 for term in topic_terms if contains_word(term, hay)) * 0.04, 0.32)
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
                "volume": item.get("volume") or "",
                "pages": item.get("page") or "",
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

    def first(*keys: str) -> Any:
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
    volume = first("volume") or ""
    pages = first("pages", "page") or ""
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
        "volume": volume,
        "pages": pages,
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
    """Dedupe and merge result rows from multiple external sources (SciAtlas, Crossref).

    A paper found by more than one source is kept once, with `sources` recording
    every source that returned it and `source` collapsing to a single display
    value (kept for backward compatibility with code that reads `source`).
    """
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
            # Promote the higher-scoring record while keeping the merged source list.
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
        row["source"] = sources[0] if len(sources) == 1 else "+".join(sources)
        row["sources"] = sources
        out.append(row)
    out.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    return out


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
                "near_miss_results": group.get("near_miss_results", []),
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
    selected_ids = {p["paper_id"] for p in selected["local_papers"]}
    borderline: dict[str, dict[str, Any]] = {}
    for group in combined:
        if not group.get("keep", True):
            continue
        for result in group.get("near_miss_results", []):
            pid = result.get("paper_id")
            if not pid or pid in selected_ids:
                continue
            entry = borderline.setdefault(
                pid,
                {
                    "paper_id": pid,
                    "title": result.get("title"),
                    "year": result.get("year"),
                    "journal": result.get("journal"),
                    "matched_keywords": [],
                    "best_direct_raw_score": 0.0,
                    "best_score": 0.0,
                    "note": "near-miss: scored below the selection cut; review title/abstract and promote manually if on-topic",
                },
            )
            entry["matched_keywords"].append(group["keyword"])
            entry["best_direct_raw_score"] = max(entry["best_direct_raw_score"], result.get("direct_raw_score", 0.0))
            entry["best_score"] = max(entry["best_score"], result.get("score", 0.0))
    borderline_list = list(borderline.values())
    for entry in borderline_list:
        entry["matched_keywords"] = dedupe(entry["matched_keywords"])
    borderline_list.sort(key=lambda r: (r["best_direct_raw_score"], r["best_score"]), reverse=True)
    selected["borderline_papers"] = borderline_list
    return selected


def role_rank(role: str | None) -> int:
    order = {"core_candidate": 0, "supporting_candidate": 1, "background": 2, "uncertain": 3, "excluded": 4}
    return order.get(role or "uncertain", 3)


def write_report(
    out_dir: Path,
    topic: str,
    keyword_set: dict[str, Any],
    combined: list[dict[str, Any]],
    borderline_papers: list[dict[str, Any]] | None = None,
    filter_stats: dict[str, int] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    group_by: list[str] | None = None,
) -> None:
    lines = ["# Topic Paper Discovery Report", "", f"Topic: {topic}", ""]
    if filter_stats and (year_from is not None or year_to is not None):
        year_range = f"{year_from if year_from is not None else 'unbounded'}-{year_to if year_to is not None else 'unbounded'}"
        lines += [
            f"Effective year range: {year_range}",
            f"Papers before year filtering: {filter_stats.get('before_filter', 0)}",
            f"Papers after year filtering: {filter_stats.get('after_filter', 0)}",
            f"Papers excluded for missing year: {filter_stats.get('missing_year_excluded', 0)}",
            f"Papers excluded outside year range: {filter_stats.get('out_of_range_excluded', 0)}",
            "",
        ]
    if group_by:
        lines += [f"Requested grouping fields: {', '.join(group_by)}", ""]
    lines += ["## Keywords", ""]
    for kw in keyword_set["merged_keywords"]:
        lines.append(f"- {kw['keyword']} ({kw.get('category')}, source={'+'.join(kw.get('source', []))})")
    if borderline_papers:
        lines += ["", "## Borderline Papers — review required", ""]
        lines.append(
            "These papers scored below the selection cut but above the near-miss floor. "
            "Do NOT treat them as rejected: read each title (and abstract if needed) and "
            "promote the on-topic ones into the candidate set manually."
        )
        lines.append("")
        for entry in borderline_papers:
            kws = ", ".join(entry.get("matched_keywords", [])[:4])
            lines.append(
                f"- `{entry['paper_id']}` direct_raw={entry['best_direct_raw_score']:.2f} "
                f"score={entry['best_score']:.3f} ({kws}) {entry.get('title')}"
            )
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


# Must produce the IDENTICAL slug for a given filename+mineru_output as
# mineru-precise-parse-review-writer/scripts/parse_review_writer_pdfs.py's
# slugify_text/slug_budget and review-metadata-prep/scripts/prepare_metadata.py's
# slugify_mineru/slug_budget. All three scripts independently derive a slug
# from the same PDF filename and must agree, or downstream matching between
# MinerU markdown, metadata, and registry entries silently breaks.
WINDOWS_MAX_PATH = 260
_IMAGE_SUFFIX_RESERVE = len("\\images\\") + 64 + 5 + 8


def slug_budget(mineru_output: Path) -> int:
    extracted_root = str((mineru_output / "extracted").resolve())
    reserved = len(extracted_root) + 1 + _IMAGE_SUFFIX_RESERVE
    return max(24, WINDOWS_MAX_PATH - reserved)


def cap_slug_length(slug: str, max_len: int) -> str:
    if len(slug) <= max_len:
        return slug
    import hashlib

    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    return f"{slug[: max_len - len(digest) - 1]}-{digest}"


def slugify_for_registration(value: str, mineru_output: Path) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", ascii_text).strip("-._/")
    cleaned = cleaned.replace("/", "__")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = cleaned.lower() or "document"
    return cap_slug_length(cleaned, slug_budget(mineru_output))


def registered_pdf_paths(review_root: Path) -> set[str]:
    """Resolved absolute PDF paths already present in the library.

    Sourced from metadata files' source_paths.pdf (the field every registration
    path -- prepare_metadata.py and this script -- writes), not from slug
    matching. Different scripts have historically computed different slugs for
    the same filename (see slugify_for_registration's docstring), which made
    slug-based dedup silently re-register already-known PDFs as duplicates.
    A real filesystem path is unambiguous regardless of slug scheme.
    """
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    paths: set[str] = set()
    if not meta_dir.is_dir():
        return paths
    for meta_path in meta_dir.glob("*.metadata.json"):
        try:
            meta = read_json(meta_path)
        except Exception:
            continue
        pdf_path = (meta.get("source_paths") or {}).get("pdf")
        if pdf_path:
            try:
                paths.add(str(Path(pdf_path).resolve()))
            except Exception:
                paths.add(str(pdf_path))
    return paths


def auto_register_papers(review_root: Path, paper_dir: Path) -> list[str]:
    """Scan paper_dir for PDF files not yet in the registry and register stubs."""
    if not paper_dir.exists():
        print(f"[auto-register] paper_dir not found: {paper_dir}", file=sys.stderr)
        return []
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    registry_path = review_root / "review-library" / "registry" / "papers.jsonl"
    meta_dir.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    mineru_output = review_root / "mineru-outputs"
    known_paths = registered_pdf_paths(review_root)
    existing_ids = {
        int(re.search(r"\d+", p.stem.split(".")[0]).group())
        for p in meta_dir.glob("P*.metadata.json")
        if re.search(r"\d+", p.stem.split(".")[0])
    }
    next_id = max(existing_ids, default=0) + 1
    registered: list[str] = []
    for pdf in sorted(paper_dir.rglob("*.pdf")):
        if str(pdf.resolve()) in known_paths:
            continue
        relative_stem = str(pdf.relative_to(paper_dir).with_suffix(""))
        slug = slugify_for_registration(relative_stem, mineru_output)
        paper_id = f"P{next_id:03d}"
        next_id += 1
        md_path = review_root / "mineru-outputs" / "markdown" / f"{slug}.md"
        content_list_dir = review_root / "mineru-outputs" / "extracted" / slug
        content_list_candidates = list(content_list_dir.glob("*_content_list.json")) if content_list_dir.exists() else []
        meta: dict[str, Any] = {
            "paper_id": paper_id,
            "slug": slug,
            "title": {"value": slug, "source": "filename", "confidence": 0.1, "human_checked": False},
            "authors": {"value": [], "source": "pending", "confidence": 0.0, "human_checked": False},
            "year": {"value": None, "source": "pending", "confidence": 0.0, "human_checked": False},
            "journal": {"value": None, "source": "pending", "confidence": 0.0, "human_checked": False},
            "doi": {"value": None, "source": "pending", "confidence": 0.0, "human_checked": False},
            "abstract": {"value": "", "source": "pending", "confidence": 0.0, "human_checked": False},
            "structured_tags": {
                "value": {k: "not specified" for k in STRUCTURED_TAG_KEYS},
                "source": "pending",
                "confidence": 0.0,
                "human_checked": False,
            },
            "source_paths": {
                "pdf": str(pdf),
                "markdown": str(md_path) if md_path.exists() else "",
                "content_list": str(content_list_candidates[0]) if content_list_candidates else "",
                "extracted_dir": str(content_list_dir) if content_list_dir.exists() else "",
            },
            "source_file": {"pdf_name": pdf.name, "relative_pdf_path": str(pdf.relative_to(paper_dir))},
            "extraction": {"mode": "stub", "model": None, "created_at": utc_now(), "notes": ["auto-registered by discover.py; run review-metadata-prep for full extraction"]},
            "human_review": {"status": "not_reviewed", "reviewed_at": None, "reviewer": None, "notes": []},
            "quality": {"missing_fields": ["title", "authors", "year", "abstract", "structured_tags"], "warnings": [], "overall_confidence": 0.0, "needs_human_check": True},
        }
        write_json(meta_dir / f"{paper_id}.metadata.json", meta)
        with registry_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"paper_id": paper_id, "slug": slug, "pdf": str(pdf), "source_pdf": str(pdf.resolve())},
                    ensure_ascii=False,
                )
                + "\n"
            )
        registered.append(paper_id)
        print(f"[auto-register] {paper_id} ← {pdf.name}")
    return registered


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    _load_dotenv_if_present(review_root)
    if args.paper_dir:
        paper_dir = Path(args.paper_dir).resolve()
        new_ids = auto_register_papers(review_root, paper_dir)
        if new_ids:
            print(f"[auto-register] registered {len(new_ids)} new paper(s): {', '.join(new_ids)}")
            print("[auto-register] run review-metadata-prep to extract full metadata (title, authors, tags)")
    user_keywords = split_keywords(args.keywords)
    project_id = args.project_id or slugify(args.topic)
    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
    else:
        out_dir = resolve_project_path(review_root, project_id) / "00_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "topic_input.md").write_text(
        f"# {args.topic}\n\nUser keywords:\n\n" + "\n".join(f"- {kw}" for kw in user_keywords) + "\n",
        encoding="utf-8",
    )
    default_filters = parse_year_filters(args.topic)
    year_from = args.year_from if args.year_from else default_filters.get("year_from")
    year_to = args.year_to if args.year_to else default_filters.get("year_to")
    group_by = [field.strip() for field in args.group_by.split(",") if field.strip()] if args.group_by else []
    for field in group_by:
        if field not in STRUCTURED_TAG_KEYS:
            raise SystemExit(f"--group-by field '{field}' is not one of {STRUCTURED_TAG_KEYS}")

    agent_keywords_path = Path(args.agent_keywords).resolve() if args.agent_keywords else None
    agent_keywords = load_agent_keywords(agent_keywords_path)
    keyword_set = build_keyword_set(args.topic, user_keywords, agent_keywords)
    keyword_set["filters"] = {k: v for k, v in {"year_from": year_from, "year_to": year_to}.items() if v is not None}
    keyword_set["group_by"] = group_by
    write_json(out_dir / "keyword_set.draft.json", keyword_set)
    papers = load_metadata(review_root)
    classification_rules_path = Path(args.classification_rules).resolve() if args.classification_rules else None
    classification_rules = load_classification_rules(classification_rules_path)

    local_grouped, filter_stats = local_search_by_keyword(
        papers, keyword_set["merged_keywords"], args.topic, classification_rules,
        year_from=year_from, year_to=year_to,
    )
    write_json(out_dir / "local_results_by_keyword.json", {"project_id": project_id, "results": local_grouped})

    sciatlas_requested = bool(args.sciatlas_search)
    crossref_requested = bool(args.web_search)
    sciatlas_client: SciAtlasClient | None = None
    sciatlas_status = "disabled"
    if sciatlas_requested:
        sciatlas_config = load_sciatlas_config(
            base_url=args.sciatlas_base_url or None,
            api_key=args.sciatlas_api_key or None,
            timeout=args.sciatlas_timeout or None,
        )
        if not sciatlas_config.configured:
            sciatlas_status = "missing_api_key"
        else:
            sciatlas_client = SciAtlasClient(config=sciatlas_config)
            try:
                sciatlas_client.health()
                sciatlas_status = "ok"
            except Exception as exc:
                sciatlas_status = f"health_failed: {exc}"
                sciatlas_client = None

    web_grouped = []
    sources_used: list[str] = []
    if sciatlas_requested or crossref_requested:
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
                crossref_rows = web_search(group["keyword"], args.topic, args.web_limit, args.mailto)
                rows.extend(crossref_rows)
                if crossref_rows and "crossref" not in sources_used:
                    sources_used.append("crossref")
                if args.web_delay:
                    time.sleep(args.web_delay)
            web_grouped.append({"keyword": group["keyword"], "web_results": merge_external_results(rows)})

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

    write_json(
        out_dir / "web_results_by_keyword.json",
        {
            "project_id": project_id,
            "enabled": bool(web_grouped),
            "source": "+".join(sources_used) if sources_used else "none",
            "status": external_status,
            "sources": sources_used,
            "results": web_grouped,
        },
    )
    combined = combine_results(local_grouped, web_grouped)
    write_json(out_dir / "combined_results_by_keyword.json", {"project_id": project_id, "topic": args.topic, "results": combined})
    selected = selected_from_combined(combined)
    selected["project_id"] = project_id
    selected["human_confirmed"] = False
    selected["filters"] = keyword_set["filters"]
    if group_by:
        selected["groups"] = group_selected_papers(selected, papers, group_by)
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
        out_dir, args.topic, keyword_set, combined, selected.get("borderline_papers"),
        filter_stats=filter_stats, year_from=year_from, year_to=year_to, group_by=group_by,
    )
    if selected.get("borderline_papers"):
        print(
            f"[borderline] {len(selected['borderline_papers'])} paper(s) scored in the near-miss band -- "
            "see 'Borderline Papers' in discovery_report.md and review them before confirming the candidate set"
        )
    print(f"Output directory: {out_dir}")
    print(f"Keyword set: {out_dir / 'keyword_set.draft.json'}")
    print(f"Human dashboard data: {out_dir / 'combined_results_by_keyword.json'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover local and web papers by expanded topic keywords.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--output-dir", default="", help="Override output folder. Defaults to <review-root>/review-projects/<project-id>/00_discovery/")
    parser.add_argument("--project-id", default="")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--web-search", action="store_true", help="Query Crossref per keyword. Independent of --sciatlas-search; use both, either, or neither.")
    parser.add_argument("--web-limit", type=int, default=8)
    parser.add_argument("--web-delay", type=float, default=0.2)
    parser.add_argument("--mailto", default="", help="Contact email for Crossref polite pool.")
    parser.add_argument("--sciatlas-search", action="store_true", help="Query the hosted SciAtlas KG /v1/search per keyword. Requires SCIATLAS_API_KEY (env, .env, or --sciatlas-api-key).")
    parser.add_argument("--sciatlas-limit", type=int, default=8)
    parser.add_argument("--sciatlas-api-key", default="", help="Overrides SCIATLAS_API_KEY env var.")
    parser.add_argument("--sciatlas-base-url", default="", help="Overrides SCIATLAS_API_BASE_URL env var.")
    parser.add_argument("--sciatlas-timeout", type=int, default=0, help="HTTP timeout in seconds. 0 = use env/default.")
    parser.add_argument("--sciatlas-time-range", default="", help="Optional year range like 2018-2025.")
    parser.add_argument("--sciatlas-domain", default="", help="Optional domain hint, e.g. 'organic chemistry' or 'urban climate'.")
    parser.add_argument("--paper-dir", default="", help="Path to local paper storage directory. When provided, unregistered PDFs are auto-registered before discovery.")
    parser.add_argument("--agent-keywords", default="", help="Path to a JSON file of LLM-expanded keywords (see SKILL.md). Required for keyword expansion beyond --keywords.")
    parser.add_argument("--classification-rules", default="", help="Optional path to a Python file defining a 'rules' list of (label, category, aliases) tuples, used to widen structured-tag matching. Safe to omit.")
    parser.add_argument("--year-from", type=int, default=0, help="Explicit lower year bound (overrides any year range parsed from --topic). 0 = unset.")
    parser.add_argument("--year-to", type=int, default=0, help="Explicit upper year bound (overrides any year range parsed from --topic). 0 = unset.")
    parser.add_argument("--group-by", default="", help="Comma-separated structured-tag field(s) to bucket selected papers by, e.g. 'method' or 'method,input'. Must be one of: " + ", ".join(STRUCTURED_TAG_KEYS))
    return parser.parse_args()


if __name__ == "__main__":
    import traceback
    try:
        raise SystemExit(run(parse_args()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
