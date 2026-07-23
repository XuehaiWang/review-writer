#!/usr/bin/env python3
"""Translate a LabKAG match-topic response into review-writer's discovery
output format (review-projects/<project_id>/00_discovery/*), so a downstream
review-writer stage (e.g. review-literature-matrix-outline, which reads only
selected_discovery_results.json + topic_input.md) can run unmodified. This
script is the sole producer of that file set -- review-online-paper-discovery
(the review-writer skill that used to own it) now only searches Crossref/
SciAtlas and downloads PDFs into review-library/paper_pdf/; it no longer
scores or shortlists papers already in the local library.

Standalone script -- no LabKAG app import needed, pure JSON transformation
over already-computed match-topic output.

## Known mapping limitations (read before trusting the output blindly)

- **Paper-ID join is by normalized title, not a stable key.** LabKAG's
  MatchedPaper carries no DOI, and the paper's title was independently
  extracted (from the same PDF) by whatever process built the review-library
  registry, so exact string equality is unreliable (LaTeX artifacts,
  whitespace, OCR differences). This script normalizes titles (strip LaTeX
  commands/braces, lowercase, collapse whitespace) and falls back to a
  token-overlap match if the exact normalized string doesn't hit. Any LabKAG
  paper that still can't be resolved to a review-library paper_id is
  EXCLUDED from selected_discovery_results.json and listed in the printed
  unmatched report -- never silently guessed.
- **best_score is a rescaled approximation, not a literal score.**
  LabKAG's match_score is a corroboration-strength count (2 per essential
  category matched, 1 per supporting, +1 for co-occurrence). This script
  rescales match_score / 4.0 clamped to 1.0 -- reasonable for sorting/display,
  not directly comparable to any other tool's scoring scale.
- **role mapping is tier-based, not score-threshold-based**:
  confirmed -> core_candidate, borderline -> supporting_candidate. LabKAG's
  tier is already a corroboration-bar decision (see topic_matcher.py), so
  this is more principled than re-deriving a role from the rescaled score.
- **filter_stats is partial.** LabKAG's match_topic summary reports one
  combined excluded_count (year AND tag mismatches together), not separate
  year-exclusion counts. missing_year_excluded/out_of_range_excluded are
  left at 0 with a note -- never fabricated from LabKAG data that doesn't
  actually distinguish them.
- **resolved_concepts is usually empty.** LabKAG's TopicConcept has a free-text
  `reason` field but no structured abbreviation-expansion record. Pass
  --resolved-concepts with your own JSON if you want this populated (e.g.
  from the disambiguation step already done in topic_decomposition_prompt.md).
- **Taxonomy category -> structured-tag category mapping is a small,
  editable dict** (DEFAULT_CATEGORY_MAP below), targeting review-writer's
  current domain-agnostic 7-category schema (output/input/method/co_input/
  modifier/process_type/document_scope -- defined in
  review-online-paper-discovery/scripts/discover.py's STRUCTURED_TAG_KEYS).
  Categories with no natural match fall back to "document_scope" as a
  neutral, low-signal category.

Usage:
    python scripts/export_discovery_format.py \\
      --match-result match_result.json \\
      --review-root D:/Git_projects/Self_test/comparison/metadata_output \\
      --discovery-project-id apa-allenylation-from-labkag \\
      --group-by method
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STRUCTURED_TAG_KEYS = [
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
]

# Maps LabKAG taxonomy categories (chemistry-flavored examples seen in practice)
# onto review-writer's current domain-agnostic 7-category schema (defined in
# review-online-paper-discovery/scripts/discover.py's STRUCTURED_TAG_KEYS).
# Categories with no natural match fall back to "document_scope" as a
# neutral, low-signal category.
DEFAULT_CATEGORY_MAP = {
    "catalyst_class": "method",
    "catalyst_metal": "method",
    "catalyst_type": "method",
    "substrate_class": "input",
    "reaction_type": "process_type",
    "product_class": "output",
    "stereochemistry": "modifier",
}

ROLE_BY_TIER = {"confirmed": "core_candidate", "borderline": "supporting_candidate"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_title(title: str) -> str:
    text = re.sub(r"\\[a-zA-Z]+", " ", title or "")
    text = re.sub(r"[{}$^_]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(title: str) -> set[str]:
    return {t for t in normalize_title(title).split(" ") if len(t) >= 3}


def load_registry(review_root: Path) -> list[dict[str, Any]]:
    path = review_root / "review-library" / "registry" / "papers.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def build_title_index(rows: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, set[str]]]:
    exact: dict[str, str] = {}
    tokens_by_id: dict[str, set[str]] = {}
    for row in rows:
        pid = row.get("paper_id")
        title = row.get("title") or ""
        if not pid or not title:
            continue
        exact[normalize_title(title)] = pid
        tokens_by_id[pid] = title_tokens(title)
    return exact, tokens_by_id


def resolve_paper_id(
    title: str, exact_index: dict[str, str], tokens_by_id: dict[str, set[str]]
) -> str | None:
    key = normalize_title(title)
    if key in exact_index:
        return exact_index[key]
    query_tokens = title_tokens(title)
    if not query_tokens:
        return None
    best_id, best_overlap = None, 0.0
    for pid, tokens in tokens_by_id.items():
        if not tokens:
            continue
        overlap = len(query_tokens & tokens) / len(query_tokens | tokens)
        if overlap > best_overlap:
            best_overlap, best_id = overlap, pid
    return best_id if best_overlap >= 0.6 else None


def load_metadata_field(review_root: Path, paper_id: str, field: str, default: Any = None) -> Any:
    path = review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    if not path.exists():
        return default
    meta = read_json(path)
    value = meta.get(field)
    if isinstance(value, dict) and "value" in value:
        return value.get("value", default)
    return value if value is not None else default


def map_category(labkag_category: str, category_map: dict[str, str]) -> str:
    return category_map.get(labkag_category, "document_scope")


def build_matched_keywords(matched_concepts: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for concept in matched_concepts:
        label = f"{concept.get('category')}={concept.get('value')}"
        if label not in seen:
            seen.append(label)
    return seen


def rescale_score(match_score: float) -> float:
    return round(min(match_score / 4.0, 1.0), 4)


def resolve_all(
    matched_papers: list[dict[str, Any]],
    exact_index: dict[str, str],
    tokens_by_id: dict[str, set[str]],
    review_root: Path,
    category_map: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for paper in matched_papers:
        pid = resolve_paper_id(paper.get("title", ""), exact_index, tokens_by_id)
        if pid is None:
            unresolved.append(
                {
                    "labkag_paper_id": paper.get("paper_id"),
                    "title": paper.get("title"),
                    "reason": "no review-library paper_id matched this title "
                    "(exact-normalized and token-overlap>=0.6 both failed)",
                }
            )
            continue
        matched_concepts = paper.get("matched_concepts", [])
        resolved.append(
            {
                "paper_id": pid,
                "labkag_paper_id": paper.get("paper_id"),
                "title": load_metadata_field(review_root, pid, "title", paper.get("title", "")),
                "year": load_metadata_field(review_root, pid, "year", paper.get("year")),
                "journal": load_metadata_field(review_root, pid, "journal"),
                "doi": load_metadata_field(review_root, pid, "doi"),
                "authors": load_metadata_field(review_root, pid, "authors", []),
                "role": ROLE_BY_TIER.get(paper.get("tier"), "uncertain"),
                "tier": paper.get("tier"),
                "matched_keywords": build_matched_keywords(matched_concepts),
                "matched_concepts": matched_concepts,
                "score": rescale_score(paper.get("match_score", 0.0)),
                "raw_score": paper.get("match_score", 0.0),
                "reason": "; ".join(paper.get("reasons", [])),
                "keep": True,
            }
        )
    return resolved, unresolved


def build_keyword_groups(
    resolved_papers: list[dict[str, Any]], category_map: dict[str, str]
) -> list[dict[str, Any]]:
    # matched_concepts has one entry per matched *entity*, so the same
    # (category, value) can repeat many times for one paper (e.g. three
    # different Method/Condition entities all tagged substrate_class=X).
    # Dedupe to one row per (paper_id, category, value) per keyword group.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for paper in resolved_papers:
        for concept in paper["matched_concepts"]:
            category = map_category(concept.get("category", ""), category_map)
            value = concept.get("value", "")
            dedupe_key = (paper["paper_id"], category, value)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            key = (value, category)
            groups.setdefault(key, []).append(
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "authors": paper["authors"],
                    "year": paper["year"],
                    "journal": paper["journal"],
                    "doi": paper["doi"],
                    "score": paper["score"],
                    "raw_score": paper["raw_score"],
                    "direct_raw_score": paper["raw_score"],
                    "matched_fields": [category],
                    "matched_terms": [value],
                    "reason": paper["reason"],
                    "role": paper["role"],
                    "keep": True,
                    "source_paths": {},
                }
            )
    out = []
    for (value, category), results in groups.items():
        results.sort(key=lambda r: (r["score"], r.get("year") or 0), reverse=True)
        out.append({"keyword": value, "category": category, "keep": True, "local_results": results})
    return out


def build_groups(
    resolved_papers: list[dict[str, Any]], review_root: Path, group_by: list[str]
) -> dict[str, Any]:
    grouped: dict[str, Any] = {}
    for field in group_by:
        buckets: dict[str, set[str]] = {}
        for paper in resolved_papers:
            tags = load_metadata_field(review_root, paper["paper_id"], "structured_tags", {}) or {}
            value = str(tags.get(field) or "not specified").strip() or "not specified"
            buckets.setdefault(value, set()).add(paper["paper_id"])
        grouped[field] = {
            value: {"count": len(ids), "paper_ids": sorted(ids)}
            for value, ids in sorted(buckets.items())
        }
    return grouped


def write_discovery_report(
    out_dir: Path,
    topic: str,
    summary: dict[str, Any],
    unresolved_concepts: list[dict[str, Any]],
    group_by: list[str],
    keyword_groups: list[dict[str, Any]],
    selected_count: int,
    unmatched: list[dict[str, Any]],
) -> None:
    year_from = summary.get("year_from")
    year_to = summary.get("year_to")
    year_range = "none" if year_from is None and year_to is None else f"{year_from}-{year_to}"
    unresolved_text = ", ".join(c.get("surface", "") for c in unresolved_concepts) or "none"
    lines = [
        "# Topic Paper Discovery Report (exported from LabKAG match-topic)",
        "",
        f"Topic: {topic}",
        "Query-plan source: labkag_match_topic",
        f"Effective year range: {year_range}",
        f"Candidates scanned (LabKAG): {summary.get('candidates_scanned', 0)}",
        f"Confirmed (LabKAG tier): {summary.get('confirmed_count', 0)}",
        f"Borderline (LabKAG tier): {summary.get('borderline_count', 0)}",
        "Excluded (LabKAG tier, tag+year combined -- not separable): "
        f"{summary.get('excluded_count', 0)}",
        f"Unresolved concepts: {unresolved_text}",
        f"Requested grouping fields: {', '.join(group_by) or 'none'}",
        f"Selected local papers: {selected_count}",
        f"Papers unresolved to a review-metadata paper_id (excluded from output): {len(unmatched)}",
        "",
        "## Results by Keyword (derived from LabKAG matched_concepts)",
        "",
    ]
    for group in keyword_groups:
        lines.append(f"### {group['keyword']} ({group['category']})")
        lines.append("")
        for result in group["local_results"][:10]:
            lines.append(
                f"- `{result['paper_id']}` score={result['score']:.3f} "
                f"role={result['role']} {result['title']}"
            )
        lines.append("")
    if unmatched:
        lines += ["## Unresolved LabKAG Papers (no review-metadata match)", ""]
        for row in unmatched:
            lines.append(f"- {row['labkag_paper_id']}: {row['title']}")
    (out_dir / "discovery_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    match_result = read_json(Path(args.match_result))
    data = match_result.get("data", match_result)
    topic_plan = data.get("topic_plan", {})
    summary = dict(data.get("summary", {}))
    summary["year_from"] = topic_plan.get("year_from")
    summary["year_to"] = topic_plan.get("year_to")

    review_root = Path(args.review_root).resolve()
    registry_rows = load_registry(review_root)
    if not registry_rows:
        print(f"ERROR: no registry found under {review_root}", file=sys.stderr)
        return 2
    exact_index, tokens_by_id = build_title_index(registry_rows)

    category_map = dict(DEFAULT_CATEGORY_MAP)
    if args.category_map:
        category_map.update(read_json(Path(args.category_map)))

    all_matched = data.get("confirmed", []) + data.get("borderline", [])
    resolved, unmatched = resolve_all(
        all_matched, exact_index, tokens_by_id, review_root, category_map
    )
    resolved.sort(key=lambda p: (p["score"], p.get("year") or 0), reverse=True)

    group_by = args.group_by or []
    resolved_concepts = []
    if args.resolved_concepts:
        resolved_concepts = read_json(Path(args.resolved_concepts))
    unresolved_concepts = [
        {"surface": str(item.get("surface") or item), "reason": str(item.get("reason") or "")}
        if isinstance(item, dict)
        else {"surface": str(item), "reason": ""}
        for item in topic_plan.get("unresolved", [])
    ]

    project_root = review_root / "review-projects" / args.discovery_project_id
    out_dir = project_root / "00_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)

    topic = topic_plan.get("topic", args.topic or "")
    (out_dir / "topic_input.md").write_text(
        f"# {topic}\n\nUser keywords:\n\n"
        "(none -- this project's candidates came from LabKAG match-topic, not keyword search)\n",
        encoding="utf-8",
    )

    query_plan = {
        "schema_version": 1,
        "topic": topic,
        "resolved_concepts": resolved_concepts,
        "unresolved_concepts": unresolved_concepts,
        "keywords": [
            {
                "keyword": concept.get("value", ""),
                "category": map_category(concept.get("category", ""), category_map),
                "source": "agent",
                "reason": concept.get("reason", ""),
            }
            for concept in topic_plan.get("concepts", [])
        ],
        "filters": {
            k: v
            for k, v in {
                "year_from": topic_plan.get("year_from"),
                "year_to": topic_plan.get("year_to"),
            }.items()
            if v is not None
        },
        "group_by": group_by,
    }
    write_json(out_dir / "query_plan.draft.json", query_plan)

    filter_stats = {
        "before_filter": summary.get("candidates_scanned", 0),
        "after_filter": summary.get("candidates_scanned", 0) - summary.get("excluded_count", 0),
        "missing_year_excluded": 0,
        "out_of_range_excluded": 0,
    }
    groups = build_groups(resolved, review_root, group_by)
    keyword_groups = build_keyword_groups(resolved, category_map)

    keyword_set = {
        "user_topic": topic,
        "user_keywords": [],
        "ignored_user_keywords": [],
        "agent_keywords": query_plan["keywords"],
        "merged_keywords": [
            {**kw, "source": [kw["source"]], "keep": True} for kw in query_plan["keywords"]
        ],
        "created_at": utc_now(),
        "resolved_concepts": resolved_concepts,
        "unresolved_concepts": unresolved_concepts,
        "filters": query_plan["filters"],
        "group_by": group_by,
        "query_plan_source": "labkag_match_topic",
        "filter_stats": filter_stats,
        "groups": groups,
    }
    write_json(out_dir / "keyword_set.draft.json", keyword_set)

    write_json(
        out_dir / "local_results_by_keyword.json",
        {
            "project_id": args.discovery_project_id,
            "resolved_concepts": resolved_concepts,
            "unresolved_concepts": unresolved_concepts,
            "filters": query_plan["filters"],
            "group_by": group_by,
            "query_plan_source": "labkag_match_topic",
            "filter_stats": filter_stats,
            "groups": groups,
            "results": keyword_groups,
        },
    )
    write_json(
        out_dir / "web_results_by_keyword.json",
        {
            "project_id": args.discovery_project_id,
            "enabled": False,
            "source": "none",
            "status": "disabled",
            "sources": [],
            "results": [],
        },
    )
    combined = [
        {
            "keyword": group["keyword"],
            "category": group["category"],
            "keep": True,
            "local_results": group["local_results"],
            "web_results": [],
        }
        for group in keyword_groups
    ]
    write_json(
        out_dir / "combined_results_by_keyword.json",
        {
            "project_id": args.discovery_project_id,
            "topic": topic,
            "resolved_concepts": resolved_concepts,
            "unresolved_concepts": unresolved_concepts,
            "filters": query_plan["filters"],
            "group_by": group_by,
            "query_plan_source": "labkag_match_topic",
            "filter_stats": filter_stats,
            "groups": groups,
            "results": combined,
        },
    )

    selected = {
        "project_id": args.discovery_project_id,
        "human_confirmed": False,
        "keywords": [{"keyword": g["keyword"], "category": g["category"]} for g in keyword_groups],
        "local_papers": [
            {
                "paper_id": p["paper_id"],
                "title": p["title"],
                "year": p["year"],
                "journal": p["journal"],
                "role": p["role"],
                "matched_keywords": p["matched_keywords"],
                "best_score": p["score"],
                "keep": True,
            }
            for p in resolved
        ],
        "web_papers": [],
        "resolved_concepts": resolved_concepts,
        "unresolved_concepts": unresolved_concepts,
        "filters": query_plan["filters"],
        "group_by": group_by,
        "query_plan_source": "labkag_match_topic",
        "filter_stats": filter_stats,
        "groups": groups,
    }
    write_json(out_dir / "selected_discovery_results.json", selected)

    write_json(
        out_dir / "human_check_state.json",
        {
            "project_id": args.discovery_project_id,
            "status": "pending",
            "confirmed_at": None,
            "instructions": "Use the dashboard to delete irrelevant keywords/results, "
            "then mark discovery confirmed.",
        },
    )

    write_discovery_report(
        out_dir,
        topic,
        summary,
        unresolved_concepts,
        group_by,
        keyword_groups,
        len(resolved),
        unmatched,
    )

    print(f"Exported discovery-format output to: {out_dir}")
    print(f"Resolved: {len(resolved)}  Unresolved (no paper_id match): {len(unmatched)}")
    if unmatched:
        print("Unresolved papers (title didn't match any review-metadata paper_id):")
        for row in unmatched:
            print(f"  {row['labkag_paper_id']}: {row['title']}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a LabKAG match-topic response into "
        "review-writer's discovery output format."
    )
    parser.add_argument(
        "--match-result", required=True, help="Path to a saved match-topic JSON response."
    )
    parser.add_argument(
        "--review-root", required=True, help="review-writer-style root (has review-library/)."
    )
    parser.add_argument("--discovery-project-id", required=True)
    parser.add_argument(
        "--topic", default="", help="Override topic text (defaults to the plan's own topic)."
    )
    parser.add_argument(
        "--group-by",
        action="append",
        default=[],
        help="Structured-tag category to group selected papers by (repeatable). "
        "E.g. method.",
    )
    parser.add_argument(
        "--category-map",
        default="",
        help="Path to a JSON dict overriding taxonomy-category -> structured-tag-category mapping.",
    )
    parser.add_argument(
        "--resolved-concepts",
        default="",
        help="Path to a JSON list of {surface, expanded_name, confidence, reason} to populate "
        "resolved_concepts (LabKAG's plan has no structured equivalent of this field).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
