#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
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


# Kept only for the stub-metadata schema this skill's own registration path
# writes (see write_stub_metadata) -- structured tagging itself is LabKAG's
# job now, not this skill's.
STRUCTURED_TAG_KEYS = [
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
]


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


def load_agent_keywords(path: Path | None) -> list[dict[str, Any]]:
    """Load LLM-authored keyword expansion. See SKILL.md for the required schema.

    Expected JSON: a list of {"keyword": str, "reason": str}. This file must be
    written by the LLM before running this script -- no keyword expansion rules
    are hardcoded here, since expansion depends on the review topic's subject
    matter, which is not known ahead of time.
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
        merged[kw.lower()] = {"keyword": kw, "source": ["user"], "keep": True}
    for item in agent_keywords:
        key = item["keyword"].lower()
        if key in merged:
            if "agent" not in merged[key]["source"]:
                merged[key]["source"].append("agent")
        else:
            merged[key] = {"keyword": item["keyword"], "source": ["agent"], "keep": True, "reason": item.get("reason", "")}
    return {
        "user_topic": topic,
        "user_keywords": user_keywords,
        "agent_keywords": agent_keywords,
        "merged_keywords": list(merged.values()),
        "created_at": utc_now(),
    }


def contains_word(needle: str, haystack: str) -> bool:
    """Word-boundary substring check.

    Plain `needle in haystack` false-positives badly on short terms: "rag" is
    a literal substring of "encouRAGed", "storage", "paragraph", etc.
    """
    if not needle:
        return False
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def web_search(
    keyword: str,
    topic: str,
    limit: int = 8,
    mailto: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[dict[str, Any]]:
    query = f"{keyword} {topic} review paper DOI"
    params: dict[str, str] = {"query.bibliographic": query, "rows": str(limit)}
    filters = []
    if year_from is not None:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
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
        # Crossref sometimes lists direct full-text links (mostly open-access
        # publishers) in `link`; kept separate from `url` (the landing page)
        # so a download step can tell "this is specifically a PDF" apart from
        # "this is *a* link".
        pdf_url = None
        for link_entry in item.get("link") or []:
            content_type = str(link_entry.get("content-type") or "").lower()
            if "pdf" in content_type and link_entry.get("URL"):
                pdf_url = link_entry["URL"]
                break
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
                "pdf_url": pdf_url,
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
    # Keep a direct PDF link separate from the landing-page URL -- collapsing
    # them loses the "this is specifically downloadable" signal a download
    # step needs.
    pdf_url = first("pdf_url") or None
    landing_url = first("paper_url", "url", "html_url")
    url = pdf_url or landing_url or (f"https://doi.org/{doi}" if doi else "")
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
        "pdf_url": pdf_url,
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
        if not existing.get("pdf_url") and row.get("pdf_url"):
            existing["pdf_url"] = row.get("pdf_url")
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


# Score bands over Crossref/SciAtlas's own `score` field, replacing the old
# local-library SELECT_*/NEAR_MISS_* thresholds (there is no local pool left
# to threshold against). First-pass judgment call, no empirical tuning data
# yet -- revisit after seeing a real run's score distribution.
ONLINE_CANDIDATE_SCORE = 0.35
ONLINE_BORDERLINE_SCORE = 0.15


def aggregate_candidates(web_grouped: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten every keyword group's external-search results into one
    cross-keyword-deduped candidate list, banded by score into
    candidates/borderline_candidates. Replaces the old local-library
    combine_results()/selected_from_combined() pipeline."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in web_grouped:
        keyword = group["keyword"]
        for result in group.get("web_results", []):
            if not result.get("keep", True):
                continue
            key = _result_dedupe_key(result)
            if not key:
                continue
            if key not in merged:
                merged[key] = {**result, "matched_keywords": [keyword]}
                order.append(key)
                continue
            existing = merged[key]
            if keyword not in existing["matched_keywords"]:
                existing["matched_keywords"].append(keyword)
            if (result.get("score") or 0) > (existing.get("score") or 0):
                matched_keywords = existing["matched_keywords"]
                merged[key] = {**result, "matched_keywords": matched_keywords}
    rows = [merged[key] for key in order]
    candidates = [r for r in rows if (r.get("score") or 0) >= ONLINE_CANDIDATE_SCORE]
    borderline = [
        r for r in rows
        if ONLINE_BORDERLINE_SCORE <= (r.get("score") or 0) < ONLINE_CANDIDATE_SCORE
    ]
    dropped = sum(1 for r in rows if (r.get("score") or 0) < ONLINE_BORDERLINE_SCORE)
    candidates.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    borderline.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    return {
        "keywords": [{"keyword": g["keyword"]} for g in web_grouped],
        "candidates": candidates,
        "borderline_candidates": borderline,
        "dropped_low_score_count": dropped,
    }


def write_report(
    out_dir: Path,
    topic: str,
    keyword_set: dict[str, Any],
    web_grouped: list[dict[str, Any]],
    borderline_candidates: list[dict[str, Any]] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> None:
    lines = ["# Online Paper Discovery Report", "", f"Topic: {topic}", ""]
    if year_from is not None or year_to is not None:
        year_range = f"{year_from if year_from is not None else 'unbounded'}-{year_to if year_to is not None else 'unbounded'}"
        lines += [f"Effective year range applied to Crossref/SciAtlas queries: {year_range}", ""]
    lines += ["## Keywords", ""]
    for kw in keyword_set["merged_keywords"]:
        lines.append(f"- {kw['keyword']} (source={'+'.join(kw.get('source', []))})")
    if borderline_candidates:
        lines += ["", "## Borderline Candidates — review required", ""]
        lines.append(
            "These candidates scored below the confident-candidate cut but above the "
            "borderline floor. Do NOT treat them as rejected: read each title (and "
            "abstract if available) and promote the on-topic ones manually."
        )
        lines.append("")
        for entry in borderline_candidates:
            kws = ", ".join(entry.get("matched_keywords", [])[:4])
            lines.append(
                f"- score={entry.get('score', 0):.3f} ({kws}) {entry.get('title')} {entry.get('url') or ''}"
            )
    lines += ["", "## External Search Results by Keyword", ""]
    for group in web_grouped:
        lines.append(f"### {group['keyword']}")
        lines.append("")
        for result in group.get("web_results", [])[:10]:
            pdf_note = " [pdf]" if result.get("pdf_url") else ""
            lines.append(f"- score={result['score']:.3f}{pdf_note} {result['title']} {result.get('url') or ''}")
        lines.append("")
    (out_dir / "online_search_report.md").write_text("\n".join(lines), encoding="utf-8")


# This slug is now only used for this skill's own review-library
# registration bookkeeping (see write_stub_metadata/register_pdf). It no
# longer needs to match any sibling parser/tagger's slug scheme --
# mineru-precise-parse-review-writer and review-metadata-prep, which this
# used to have to agree with, have been removed from the pipeline.
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
    path writes), not from slug matching -- a real filesystem path is
    unambiguous regardless of slug scheme.
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


def next_paper_id(meta_dir: Path) -> str:
    existing_ids = {
        int(re.search(r"\d+", p.stem.split(".")[0]).group())
        for p in meta_dir.glob("P*.metadata.json")
        if re.search(r"\d+", p.stem.split(".")[0])
    }
    next_id = max(existing_ids, default=0) + 1
    return f"P{next_id:03d}"


def write_stub_metadata(
    review_root: Path,
    paper_id: str,
    slug: str,
    pdf_path: Path,
    relative_pdf_path: str,
    mineru_output: Path,
    seed_fields: dict[str, Any],
    notes: list[str],
) -> None:
    """Write a stub review-library metadata.json + registry entry for a single
    downloaded PDF, seeded with whatever bibliographic fields the online-search
    candidate already carried (title/authors/year/journal/doi/abstract).
    Structured tags are always left "not specified" -- tagging is LabKAG's job
    now, not this skill's."""
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    registry_path = review_root / "review-library" / "registry" / "papers.jsonl"
    meta_dir.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    def field(key: str, default: Any) -> dict[str, Any]:
        value = seed_fields.get(key)
        if value in (None, "", []):
            return {"value": default, "source": "pending", "confidence": 0.0, "human_checked": False}
        return {"value": value, "source": "online_discovery_search", "confidence": 0.5, "human_checked": False}

    title_field = field("title", slug)
    authors_field = field("authors", [])
    year_field = field("year", None)
    journal_field = field("journal", None)
    doi_field = field("doi", None)
    abstract_field = field("abstract", "")
    missing_fields = [
        name
        for name, f in (
            ("title", title_field),
            ("authors", authors_field),
            ("year", year_field),
            ("abstract", abstract_field),
        )
        if f["source"] == "pending"
    ] + ["structured_tags"]

    md_path = mineru_output / "markdown" / f"{slug}.md"
    content_list_dir = mineru_output / "extracted" / slug
    content_list_candidates = list(content_list_dir.glob("*_content_list.json")) if content_list_dir.exists() else []
    meta: dict[str, Any] = {
        "paper_id": paper_id,
        "slug": slug,
        "title": title_field,
        "authors": authors_field,
        "year": year_field,
        "journal": journal_field,
        "doi": doi_field,
        "abstract": abstract_field,
        "structured_tags": {
            "value": {k: "not specified" for k in STRUCTURED_TAG_KEYS},
            "source": "pending",
            "confidence": 0.0,
            "human_checked": False,
        },
        "source_paths": {
            "pdf": str(pdf_path),
            "markdown": str(md_path) if md_path.exists() else "",
            "content_list": str(content_list_candidates[0]) if content_list_candidates else "",
            "extracted_dir": str(content_list_dir) if content_list_dir.exists() else "",
        },
        "source_file": {"pdf_name": pdf_path.name, "relative_pdf_path": relative_pdf_path},
        "extraction": {"mode": "stub", "model": None, "created_at": utc_now(), "notes": notes},
        "human_review": {"status": "not_reviewed", "reviewed_at": None, "reviewer": None, "notes": []},
        "quality": {
            "missing_fields": missing_fields,
            "warnings": [],
            "overall_confidence": 0.5 if title_field["source"] != "pending" else 0.0,
            "needs_human_check": True,
        },
    }
    write_json(meta_dir / f"{paper_id}.metadata.json", meta)
    with registry_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "paper_id": paper_id, "slug": slug, "title": title_field["value"],
                    "pdf": str(pdf_path), "source_pdf": str(pdf_path.resolve()),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def register_pdf(
    review_root: Path,
    pdf_path: Path,
    paper_pdf_dir: Path,
    mineru_output_dir: Path,
    candidate: dict[str, Any],
    notes: list[str],
) -> str:
    """Register one PDF already sitting in paper_pdf_dir (agent-chosen, not
    assumed to be review-library/paper_pdf/) against candidate metadata, and
    seed source_paths.markdown/content_list from mineru_output_dir (also
    agent-chosen, not assumed to be <review-root>/mineru-outputs) if that
    directory already has this PDF's slug parsed."""
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    relative_stem = str(pdf_path.relative_to(paper_pdf_dir).with_suffix(""))
    slug = slugify_for_registration(relative_stem, mineru_output_dir)
    paper_id = next_paper_id(meta_dir)
    seed_fields = {
        "title": candidate.get("title"),
        "authors": candidate.get("authors"),
        "year": candidate.get("year"),
        "journal": candidate.get("journal"),
        "doi": candidate.get("doi"),
        "abstract": candidate.get("abstract"),
    }
    write_stub_metadata(
        review_root, paper_id, slug, pdf_path, str(pdf_path.relative_to(paper_pdf_dir)),
        mineru_output_dir, seed_fields, notes,
    )
    return paper_id


def normalize_title(title: str) -> str:
    text = re.sub(r"\\[a-zA-Z]+", " ", title or "")
    text = re.sub(r"[{}$^_]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str) -> set[str]:
    return {t for t in normalize_title(title).split(" ") if len(t) >= 3}


def extract_markdown_title(md_path: Path) -> str:
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    for line in text.splitlines()[:40]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def match_title_to_candidates(
    title: str, candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Exact-normalized match first, then token-overlap >= 0.6 -- same bar
    used elsewhere in this pipeline (export_discovery_format.py's
    resolve_paper_id). Never guesses below that bar."""
    if not title:
        return None
    key = normalize_title(title)
    for candidate in candidates:
        if normalize_title(candidate.get("title", "")) == key:
            return candidate
    query_tokens = title_tokens(title)
    if not query_tokens:
        return None
    best, best_overlap = None, 0.0
    for candidate in candidates:
        tokens = title_tokens(candidate.get("title", ""))
        if not tokens:
            continue
        overlap = len(query_tokens & tokens) / len(query_tokens | tokens)
        if overlap > best_overlap:
            best_overlap, best = overlap, candidate
    return best if best_overlap >= 0.6 else None


def existing_library_keys(papers: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Normalized (doi, title) sets already present in the library, so download
    mode can skip candidates already ingested from another source instead of
    downloading a duplicate PDF."""
    dois: set[str] = set()
    titles: set[str] = set()
    for meta in papers.values():
        doi = str(field_value(meta.get("doi")) or "").strip().lower()
        if doi:
            dois.add(doi)
        title = re.sub(r"\s+", " ", str(field_value(meta.get("title")) or "").strip().lower())
        if title:
            titles.add(title)
    return dois, titles


def unpaywall_lookup(
    doi: str, email: str, base_url: str = "https://api.unpaywall.org/v2", timeout: int = 20
) -> dict[str, Any]:
    """Look up an open-access PDF location for a DOI via the free Unpaywall API.
    No OA copy found is a normal, expected outcome (status="ok", is_oa=False),
    not an error -- only network/HTTP failures set status="error"."""
    contact = email or "anonymous@example.com"
    url = f"{base_url.rstrip('/')}/{urllib.parse.quote(doi, safe='')}?" + urllib.parse.urlencode({"email": contact})
    req = urllib.request.Request(url, headers={"User-Agent": f"review-writer-discovery/0.1 (mailto:{contact})"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "ok", "is_oa": False, "pdf_url": None, "landing_url": None, "error": None}
        return {"status": "error", "is_oa": False, "pdf_url": None, "landing_url": None, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"status": "error", "is_oa": False, "pdf_url": None, "landing_url": None, "error": f"{type(exc).__name__}: {exc}"}
    best = data.get("best_oa_location") or {}
    if not best and data.get("oa_locations"):
        best = data["oa_locations"][0]
    return {
        "status": "ok",
        "is_oa": bool(data.get("is_oa")),
        "pdf_url": best.get("url_for_pdf") or None,
        "landing_url": best.get("url") or None,
        "host_type": best.get("host_type"),
        "license": best.get("license"),
        "error": None,
    }


def resolve_pdf_source(
    candidate: dict[str, Any], email: str, unpaywall_base_url: str, unpaywall_timeout: int
) -> dict[str, Any]:
    """Resolution order: the candidate's own direct pdf_url (no network call)
    -> Unpaywall via DOI -> none."""
    direct = candidate.get("pdf_url")
    if direct:
        return {"resolved": True, "pdf_url": direct, "source": "direct_link", "reason": "search result carried a direct PDF link"}
    doi = candidate.get("doi")
    if doi:
        result = unpaywall_lookup(doi, email, unpaywall_base_url, unpaywall_timeout)
        if result["status"] == "ok" and result["pdf_url"]:
            return {
                "resolved": True,
                "pdf_url": result["pdf_url"],
                "source": "unpaywall",
                "reason": f"Unpaywall OA location ({result.get('host_type') or 'unknown host'})",
            }
        if result["status"] == "error":
            return {"resolved": False, "pdf_url": None, "source": "none", "reason": f"Unpaywall lookup failed: {result.get('error')}"}
        return {"resolved": False, "pdf_url": None, "source": "none", "reason": "no open-access copy found via Unpaywall"}
    return {"resolved": False, "pdf_url": None, "source": "none", "reason": "no DOI and no direct PDF link available"}


def write_download_list_report(out_dir: Path, results: list[dict[str, Any]]) -> None:
    resolved = [r for r in results if r["status"] == "resolved"]
    skipped_existing = [r for r in results if r["status"] == "skipped_existing_in_library"]
    no_pdf = [r for r in results if r["status"] == "no_pdf_source_found"]
    lines = [
        "# Manual Download List",
        "",
        "Nothing here was downloaded automatically. For each resolved paper below,",
        "open the URL, save the PDF yourself, then place it in whatever folder you",
        "give `register-pdfs --paper-pdf-dir` -- the suggested filename is only a",
        "convenience so filename-matching at registration time works without",
        "further input; rename freely if you prefer, `register-pdfs` falls back to",
        "matching by title once the file has a MinerU markdown counterpart.",
        "",
        f"Resolved (has a URL to try): {len(resolved)}",
        f"Already in library (skipped): {len(skipped_existing)}",
        f"No PDF source found: {len(no_pdf)}",
        "",
    ]

    def section(title: str, rows: list[dict[str, Any]], show_url: bool) -> list[str]:
        out = [f"## {title}", ""]
        if not rows:
            out.append("None.")
        for r in rows:
            extra = f" -- suggested filename: `{r['suggested_filename']}`" if show_url and r.get("suggested_filename") else ""
            url = f" -- {r.get('pdf_url', '')}" if show_url else ""
            out.append(f"- {r.get('title', '(untitled)')} -- {r.get('reason', '')}{url}{extra}")
        out.append("")
        return out

    lines += section("Resolved", resolved, show_url=True)
    lines += section("Already in Library (Skipped)", skipped_existing, show_url=False)
    lines += section("No PDF Source Found", no_pdf, show_url=False)
    (out_dir / "online_search_manual_download_list.md").write_text("\n".join(lines), encoding="utf-8")


def run_search(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    _load_dotenv_if_present(review_root)
    if not args.web_search and not args.sciatlas_search:
        raise SystemExit(
            "At least one of --web-search or --sciatlas-search is required -- "
            "with neither, there are no candidates to find."
        )

    user_keywords = split_keywords(args.keywords)
    project_id = args.project_id or slugify(args.topic)
    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
    else:
        out_dir = resolve_project_path(review_root, project_id) / "00_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "online_search_topic.md").write_text(
        f"# {args.topic}\n\nUser keywords:\n\n" + "\n".join(f"- {kw}" for kw in user_keywords) + "\n",
        encoding="utf-8",
    )

    default_filters = parse_year_filters(args.topic)
    year_from = args.year_from if args.year_from else default_filters.get("year_from")
    year_to = args.year_to if args.year_to else default_filters.get("year_to")

    agent_keywords_path = Path(args.agent_keywords).resolve() if args.agent_keywords else None
    agent_keywords = load_agent_keywords(agent_keywords_path)
    keyword_set = build_keyword_set(args.topic, user_keywords, agent_keywords)
    keyword_set["filters"] = {k: v for k, v in {"year_from": year_from, "year_to": year_to}.items() if v is not None}
    write_json(out_dir / "online_search_keywords.json", keyword_set)

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

    web_grouped: list[dict[str, Any]] = []
    sources_used: list[str] = []
    for kw in keyword_set["merged_keywords"]:
        if not kw.get("keep", True):
            continue
        keyword = kw["keyword"]
        rows: list[dict[str, Any]] = []
        if sciatlas_client is not None:
            sciatlas_rows = sciatlas_search(
                sciatlas_client, keyword, args.topic, args.sciatlas_limit,
                args.sciatlas_time_range or None, args.sciatlas_domain or None,
            )
            rows.extend(sciatlas_rows)
            if sciatlas_rows and "sciatlas" not in sources_used:
                sources_used.append("sciatlas")
            if args.web_delay:
                time.sleep(args.web_delay)
        if crossref_requested:
            crossref_rows = web_search(
                keyword, args.topic, args.web_limit, args.mailto,
                year_from=year_from, year_to=year_to,
            )
            rows.extend(crossref_rows)
            if crossref_rows and "crossref" not in sources_used:
                sources_used.append("crossref")
            if args.web_delay:
                time.sleep(args.web_delay)
        web_grouped.append({"keyword": keyword, "web_results": merge_external_results(rows)})

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
        out_dir / "online_search_results_by_keyword.json",
        {
            "project_id": project_id,
            "topic": args.topic,
            "enabled": bool(web_grouped),
            "source": "+".join(sources_used) if sources_used else "none",
            "status": external_status,
            "sources": sources_used,
            "results": web_grouped,
        },
    )

    aggregated = aggregate_candidates(web_grouped)
    aggregated["project_id"] = project_id
    aggregated["topic"] = args.topic
    aggregated["human_confirmed"] = False
    aggregated["filters"] = keyword_set["filters"]
    write_json(out_dir / "online_search_candidates.json", aggregated)

    write_json(
        out_dir / "online_search_human_check_state.json",
        {
            "project_id": project_id,
            "status": "pending",
            "confirmed_at": None,
            "instructions": "Use the dashboard to delete irrelevant keywords/candidates, then mark "
            "discovery confirmed. Confirming triggers the automatic PDF-download step.",
        },
    )

    write_report(
        out_dir, args.topic, keyword_set, web_grouped, aggregated.get("borderline_candidates"),
        year_from=year_from, year_to=year_to,
    )
    if aggregated.get("borderline_candidates"):
        print(
            f"[borderline] {len(aggregated['borderline_candidates'])} paper(s) scored in the near-miss band -- "
            "see 'Borderline Candidates' in online_search_report.md and review them before confirming the candidate set"
        )
    print(f"Output directory: {out_dir}")
    print(f"Candidates: {len(aggregated.get('candidates', []))}")
    print(f"Keyword set: {out_dir / 'online_search_keywords.json'}")
    return 0


def _load_candidates_for_download(args: argparse.Namespace, review_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    """Loads the full confirmed candidate set (minus excluded ones), gated on
    human confirmation. Never crops by --limit -- callers that only want to
    *act* on a subset (list-for-download) apply the cap themselves, since
    register-pdfs needs the complete set available for title-matching."""
    project = resolve_project_path(review_root, args.project_id)
    out_dir = project / "00_discovery"

    candidates_path = (
        Path(args.candidates_file).resolve() if args.candidates_file else out_dir / "online_search_candidates.json"
    )
    if not candidates_path.exists():
        raise SystemExit(f"Candidates file not found: {candidates_path} -- run 'discover.py search' first.")
    data = read_json(candidates_path)

    if not args.allow_unconfirmed:
        check_state_path = out_dir / "online_search_human_check_state.json"
        state = read_json(check_state_path) if check_state_path.exists() else {}
        if state.get("status") != "confirmed":
            raise SystemExit(
                f"{check_state_path} is not confirmed (status={state.get('status')!r}). "
                "Confirm the candidate set via the dashboard first, or pass --allow-unconfirmed to override."
            )

    candidates = [c for c in (data.get("candidates") or []) if isinstance(c, dict)]
    # excluded_candidates isn't written by 'search' today, but is agent-editable
    # in online_search_candidates.json per the Agent relevance check step (see
    # SKILL.md) -- respect it if present so an excluded candidate never gets
    # listed just because it wasn't formally removed from `candidates`.
    excluded = data.get("excluded_candidates")
    if isinstance(excluded, list):
        excluded_keys = {_result_dedupe_key(c) for c in excluded if isinstance(c, dict)}
        candidates = [c for c in candidates if _result_dedupe_key(c) not in excluded_keys]
    return out_dir, candidates


def run_list_for_download(args: argparse.Namespace) -> int:
    """Resolve a PDF source for every confirmed candidate and write a report
    for a human to act on -- no fetch, no file writes into any paper
    directory. Auto-fetch was removed: in practice it almost never succeeds
    against mainstream paywalled journals or Cloudflare-protected preprint
    servers, so the only thing it reliably did was add failure modes. The
    resolution logic (Unpaywall lookup, direct-link check) is still useful --
    it tells a human where to look -- so it's kept."""
    review_root = Path(args.review_root).resolve()
    _load_dotenv_if_present(review_root)
    out_dir, candidates = _load_candidates_for_download(args, review_root)
    if args.limit:
        candidates = candidates[: args.limit]

    papers = load_metadata(review_root)
    known_dois, known_titles = existing_library_keys(papers)

    results: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _result_dedupe_key(candidate)
        title = candidate.get("title", "(untitled)")
        doi = str(candidate.get("doi") or "").strip().lower()
        norm_title = re.sub(r"\s+", " ", str(title).strip().lower())
        if (doi and doi in known_dois) or (norm_title and norm_title in known_titles):
            results.append(
                {
                    "key": key, "title": title, "doi": candidate.get("doi"),
                    "status": "skipped_existing_in_library",
                    "reason": "already present in review-library by DOI or title match",
                }
            )
            continue

        resolution = resolve_pdf_source(candidate, args.mailto, args.unpaywall_base_url, args.unpaywall_timeout)
        if not resolution["resolved"]:
            print(f"[no-pdf-source] {str(title)[:80]} -- {resolution['reason']}")
            results.append(
                {
                    "key": key, "title": title, "doi": candidate.get("doi"),
                    "status": "no_pdf_source_found", "reason": resolution["reason"],
                }
            )
            if args.delay:
                time.sleep(args.delay)
            continue

        slug_source = title if title and title != "(untitled)" else (candidate.get("doi") or key)
        slug = cap_slug_length(_slugify_no_dir(str(slug_source)[:120]), 180)
        print(f"[resolved] {str(title)[:80]} -- {resolution['source']}: {resolution['pdf_url']}")
        results.append(
            {
                "key": key, "title": title, "doi": candidate.get("doi"),
                "status": "resolved", "reason": f"resolved via {resolution['source']}",
                "pdf_url": resolution["pdf_url"], "pdf_source": resolution["source"],
                "suggested_filename": f"{slug}.pdf",
            }
        )
        if args.delay:
            time.sleep(args.delay)

    write_json(
        out_dir / "online_search_manual_download_list.json",
        {
            "project_id": args.project_id,
            "updated_at": utc_now(),
            "instructions": "Nothing was downloaded automatically. Open each resolved paper's URL, "
            "save the PDF yourself, place it in whatever folder you'll later pass to "
            "'register-pdfs --paper-pdf-dir', then run 'register-pdfs' to link it into review-library.",
            "results": results,
        },
    )
    write_download_list_report(out_dir, results)

    resolved_count = sum(1 for r in results if r["status"] == "resolved")
    print(f"Resolved: {resolved_count} / {len(candidates)} candidates")
    print(f"List: {out_dir / 'online_search_manual_download_list.json'}")
    return 0


def _slugify_no_dir(value: str) -> str:
    """Same cleanup as slugify_for_registration, without a mineru_output
    dependency -- the list step doesn't know where PDFs or parsed output will
    eventually live, so it can't compute a filesystem-path-length budget yet.
    register-pdfs re-slugs from the PDF's own filename anyway; this is only a
    convenience suggestion."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._/-]+", "-", ascii_text).strip("-._/")
    cleaned = cleaned.replace("/", "__")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.lower() or "document"


def run_register_pdfs(args: argparse.Namespace) -> int:
    """Scan --paper-pdf-dir (agent-chosen -- can be anywhere on disk, e.g. a
    Downloads folder or another drive, never assumed to be under review-root)
    for PDFs not yet in the registry, match each to a confirmed candidate (by
    suggested filename, falling back to a title match against
    --mineru-output-dir's parsed markdown), and register it. Never guesses:
    an unmatched file is reported, not silently skipped or force-registered
    under a wrong title.

    --mineru-output-dir defaults to <review-root>/review-library/mineru-outputs
    -- unlike --paper-pdf-dir, this one nests inside review-library by
    convention, since it holds derived/processed data (parsed markdown,
    extracted content) that belongs with the rest of the review-library
    workspace rather than wherever the raw PDFs happen to live."""
    review_root = Path(args.review_root).resolve()
    _load_dotenv_if_present(review_root)
    out_dir, candidates = _load_candidates_for_download(args, review_root)

    paper_pdf_dir = Path(args.paper_pdf_dir).resolve()
    if not paper_pdf_dir.is_dir():
        raise SystemExit(f"--paper-pdf-dir does not exist or is not a directory: {paper_pdf_dir}")
    mineru_output_dir = (
        Path(args.mineru_output_dir).resolve()
        if args.mineru_output_dir
        else review_root / "review-library" / "mineru-outputs"
    )

    download_list_path = (
        Path(args.download_list_file).resolve()
        if args.download_list_file
        else out_dir / "online_search_manual_download_list.json"
    )
    suggested_by_filename: dict[str, dict[str, Any]] = {}
    if download_list_path.exists():
        listed = read_json(download_list_path).get("results") or []
        for row in listed:
            name = row.get("suggested_filename")
            if name:
                # candidate metadata isn't on the list row itself -- recover it
                # by re-matching this row's own title against `candidates`.
                match = match_title_to_candidates(row.get("title", ""), candidates)
                if match is not None:
                    suggested_by_filename[Path(name).stem] = match

    md_dir = mineru_output_dir / "markdown"
    already_registered = registered_pdf_paths(review_root)

    results: list[dict[str, Any]] = []
    for pdf_path in sorted(paper_pdf_dir.rglob("*.pdf")):
        if str(pdf_path.resolve()) in already_registered:
            continue

        candidate = suggested_by_filename.get(pdf_path.stem)
        method = "filename"
        if candidate is None:
            slug = slugify_for_registration(pdf_path.stem, mineru_output_dir)
            md_path = md_dir / f"{slug}.md"
            if md_path.exists():
                candidate = match_title_to_candidates(extract_markdown_title(md_path), candidates)
                method = "title_via_markdown"
        if candidate is None:
            results.append(
                {"pdf": str(pdf_path), "status": "unmatched",
                 "reason": "no filename match on the download list, and no MinerU markdown title match"}
            )
            print(f"[unmatched] {pdf_path.name}")
            continue

        notes = [
            f"manually downloaded and placed by the user, registered from {paper_pdf_dir}",
            "run labkag-review-skill's ingest workflow for full extraction and taxonomy tagging",
        ]
        paper_id = register_pdf(review_root, pdf_path, paper_pdf_dir, mineru_output_dir, candidate, notes)
        results.append({"pdf": str(pdf_path), "status": "registered", "paper_id": paper_id, "method": method})
        print(f"[registered] {paper_id} <- {pdf_path.name} ({method})")

    registered = sum(1 for r in results if r["status"] == "registered")
    unmatched = [r for r in results if r["status"] == "unmatched"]
    write_json(
        out_dir / "online_search_register_report.json",
        {"project_id": args.project_id, "updated_at": utc_now(), "results": results},
    )
    print(f"Registered: {registered}  Unmatched: {len(unmatched)}")
    if unmatched:
        print("Unmatched files (not registered -- rename to a suggested filename or check the title matches):")
        for r in unmatched:
            print(f"  {r['pdf']}")
    return 0


def run_probe(args: argparse.Namespace) -> int:
    """Lightweight, project-agnostic search -- no files written, no project
    required. Meant for disambiguating an ambiguous topic term before
    committing to full keyword expansion (see references/
    topic_decomposition_prompt.md): run a quick search on a candidate
    meaning and see what the actual literature returns, instead of resolving
    it from general/training-data knowledge alone."""
    _load_dotenv_if_present(Path(args.review_root).resolve())
    if not args.web_search and not args.sciatlas_search:
        raise SystemExit("At least one of --web-search or --sciatlas-search is required for a probe.")

    rows: list[dict[str, Any]] = []
    if args.web_search:
        rows.extend(web_search(args.query, "", args.limit, args.mailto))
    if args.sciatlas_search:
        sciatlas_config = load_sciatlas_config(
            base_url=args.sciatlas_base_url or None,
            api_key=args.sciatlas_api_key or None,
            timeout=args.sciatlas_timeout or None,
        )
        if not sciatlas_config.configured:
            print("[probe] SciAtlas not configured (missing API key) -- skipping.")
        else:
            client = SciAtlasClient(config=sciatlas_config)
            try:
                client.health()
                rows.extend(sciatlas_search(client, args.query, "", args.limit, None, None))
            except Exception as exc:
                print(f"[probe] SciAtlas unavailable: {exc}")

    rows = merge_external_results(rows)[: args.limit]
    if not rows:
        print(f"[probe] No results for {args.query!r}. Inconclusive -- do not guess; ask the user.")
        return 0

    print(f"[probe] Top {len(rows)} result(s) for {args.query!r}:")
    for row in rows:
        journal = row.get("journal") or "(no journal)"
        print(f"  - {row.get('year')} | {journal} | {row.get('title')}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Crossref/SciAtlas for papers by topic, and download confirmed "
        "candidates' PDFs into the shared library."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    search = subparsers.add_parser(
        "search", help="Search Crossref/SciAtlas for candidate papers and write a report for human review."
    )
    search.add_argument("--review-root", default=str(Path.cwd()))
    search.add_argument(
        "--output-dir", default="",
        help="Override output folder. Defaults to <review-root>/review-projects/<project-id>/00_discovery/",
    )
    search.add_argument("--project-id", default="")
    search.add_argument("--topic", required=True)
    search.add_argument("--keywords", default="")
    search.add_argument(
        "--web-search", action="store_true",
        help="Query Crossref per keyword. Independent of --sciatlas-search; at least one is required.",
    )
    search.add_argument("--web-limit", type=int, default=8)
    search.add_argument("--web-delay", type=float, default=0.2)
    search.add_argument("--mailto", default="", help="Contact email for Crossref polite pool.")
    search.add_argument(
        "--sciatlas-search", action="store_true",
        help="Query the hosted SciAtlas KG /v1/search per keyword. Requires SCIATLAS_API_KEY "
        "(env, .env, or --sciatlas-api-key).",
    )
    search.add_argument("--sciatlas-limit", type=int, default=8)
    search.add_argument("--sciatlas-api-key", default="", help="Overrides SCIATLAS_API_KEY env var.")
    search.add_argument("--sciatlas-base-url", default="", help="Overrides SCIATLAS_API_BASE_URL env var.")
    search.add_argument("--sciatlas-timeout", type=int, default=0, help="HTTP timeout in seconds. 0 = use env/default.")
    search.add_argument("--sciatlas-time-range", default="", help="Optional year range like 2018-2025.")
    search.add_argument("--sciatlas-domain", default="", help="Optional domain hint, e.g. 'organic chemistry' or 'urban climate'.")
    search.add_argument(
        "--agent-keywords", default="",
        help="Path to a JSON file of LLM-expanded keywords (see SKILL.md). Required for keyword expansion beyond --keywords.",
    )
    search.add_argument(
        "--year-from", type=int, default=0,
        help="Explicit lower year bound (overrides any year range parsed from --topic). 0 = unset.",
    )
    search.add_argument(
        "--year-to", type=int, default=0,
        help="Explicit upper year bound (overrides any year range parsed from --topic). 0 = unset.",
    )

    list_for_download = subparsers.add_parser(
        "list-for-download",
        help="Resolve a PDF source (Unpaywall/direct link) for every confirmed candidate and write "
        "a report for manual download -- no fetch, no file writes into any paper directory.",
    )
    list_for_download.add_argument("--review-root", default=str(Path.cwd()))
    list_for_download.add_argument("--project-id", required=True)
    list_for_download.add_argument(
        "--candidates-file", default="",
        help="Defaults to <review-root>/review-projects/<project-id>/00_discovery/online_search_candidates.json",
    )
    list_for_download.add_argument("--mailto", default="", help="Contact email for Unpaywall lookups.")
    list_for_download.add_argument("--unpaywall-base-url", default="https://api.unpaywall.org/v2")
    list_for_download.add_argument("--unpaywall-timeout", type=int, default=20)
    list_for_download.add_argument("--delay", type=float, default=0.5, help="Politeness delay between lookups.")
    list_for_download.add_argument("--limit", type=int, default=0, help="Max candidates to attempt this run. 0 = all.")
    list_for_download.add_argument(
        "--allow-unconfirmed", action="store_true",
        help="Bypass the online_search_human_check_state.json confirmation gate.",
    )

    register_pdfs = subparsers.add_parser(
        "register-pdfs",
        help="Scan a folder of manually-downloaded PDFs and register each against its confirmed "
        "candidate metadata into review-library. --paper-pdf-dir is never assumed -- it can be "
        "anywhere on disk (a Downloads folder, another drive, wherever the PDFs actually are).",
    )
    register_pdfs.add_argument("--review-root", default=str(Path.cwd()))
    register_pdfs.add_argument("--project-id", required=True)
    register_pdfs.add_argument(
        "--paper-pdf-dir", required=True,
        help="Folder to scan for PDFs to register. Can be anywhere on disk -- the agent/user "
        "decides this, no default and no assumption it's under review-root.",
    )
    register_pdfs.add_argument(
        "--mineru-output-dir", default="",
        help="Folder with this PDF batch's MinerU output (markdown/, extracted/), used to seed "
        "source_paths and to resolve a title-match fallback for files not matched by filename. "
        "Defaults to <review-root>/review-library/mineru-outputs -- unlike --paper-pdf-dir, this "
        "one belongs inside the review-library workspace since it's derived/processed data.",
    )
    register_pdfs.add_argument(
        "--candidates-file", default="",
        help="Defaults to <review-root>/review-projects/<project-id>/00_discovery/online_search_candidates.json",
    )
    register_pdfs.add_argument(
        "--download-list-file", default="",
        help="Defaults to <review-root>/review-projects/<project-id>/00_discovery/"
        "online_search_manual_download_list.json (for filename-based matching).",
    )
    register_pdfs.add_argument(
        "--allow-unconfirmed", action="store_true",
        help="Bypass the online_search_human_check_state.json confirmation gate.",
    )

    probe = subparsers.add_parser(
        "probe", help="Quick, project-agnostic search for a term -- no files written. "
        "Use to gather evidence when disambiguating an ambiguous topic term before "
        "committing to full keyword expansion (see references/topic_decomposition_prompt.md).",
    )
    probe.add_argument("--review-root", default=str(Path.cwd()))
    probe.add_argument("--query", required=True, help="The ambiguous term or a candidate expansion of it.")
    probe.add_argument("--web-search", action="store_true", help="Query Crossref. At least one source is required.")
    probe.add_argument("--sciatlas-search", action="store_true", help="Query SciAtlas.")
    probe.add_argument("--limit", type=int, default=8)
    probe.add_argument("--mailto", default="", help="Contact email for Crossref polite pool.")
    probe.add_argument("--sciatlas-api-key", default="", help="Overrides SCIATLAS_API_KEY env var.")
    probe.add_argument("--sciatlas-base-url", default="", help="Overrides SCIATLAS_API_BASE_URL env var.")
    probe.add_argument("--sciatlas-timeout", type=int, default=0, help="HTTP timeout in seconds. 0 = use env/default.")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "search":
        return run_search(args)
    if args.mode == "probe":
        return run_probe(args)
    if args.mode == "register-pdfs":
        return run_register_pdfs(args)
    return run_list_for_download(args)


if __name__ == "__main__":
    import traceback
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
