#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:96] or "review-discovery"


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
    path = Path(str(source_paths.get("markdown") or ""))
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]


def tokenize(text: str) -> list[str]:
    return dedupe([w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'′\\-]*", text or "") if len(w) >= 3])


def infer_keywords(topic: str, user_keywords: list[str]) -> list[dict[str, Any]]:
    text = " ".join([topic] + user_keywords).lower()
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
        if any(n in text for n in needles) or any(n in kw.lower() for n in tokenize(topic)):
            candidates.append({"keyword": kw, "category": category, "reason": "rule expansion from topic/user keywords"})
    for token in tokenize(topic):
        if token in {"propargylic", "allene", "allenes", "synthesis", "derivatives"}:
            continue
        if len(token) > 6:
            candidates.append({"keyword": token, "category": "reaction_type", "reason": "topic token"})
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


def build_keyword_set(topic: str, user_keywords: list[str]) -> dict[str, Any]:
    agent = infer_keywords(topic, user_keywords)
    merged: dict[str, dict[str, Any]] = {}
    for kw in user_keywords:
        merged[kw.lower()] = {"keyword": kw, "category": classify_keyword(kw), "source": ["user"], "keep": True}
    for item in agent:
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
        "agent_keywords": agent,
        "merged_keywords": list(merged.values()),
        "created_at": utc_now(),
    }


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
    low = text.lower()
    t = term.lower()
    if t in low:
        return 1.0
    tokens = tokenize(t)
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in low)
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
    topic_terms: list[str],
    classification_rules: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    matched_fields: list[str] = []
    matched_terms: list[str] = []
    reasons: list[str] = []
    raw = 0.0
    direct_raw = 0.0
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
            markdown_signal(meta),
        ]
    )
    source_signal = match_score(keyword, source_text)
    if source_signal > 0 and direct_raw > 0:
        raw += min(source_signal * 0.8, 0.8)
        reasons.append("source text confirms keyword")
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


def local_search_by_keyword(
    papers: dict[str, dict[str, Any]],
    keywords: list[dict[str, Any]],
    topic: str,
    classification_rules: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    topic_terms = tokenize(topic)
    grouped: list[dict[str, Any]] = []
    for kw in keywords:
        if not kw.get("keep", True):
            continue
        keyword = kw["keyword"]
        results = [score_local_paper(meta, keyword, topic_terms, classification_rules) for meta in papers.values()]
        results = [r for r in results if r["direct_raw_score"] >= 1.4 and r["score"] >= 0.12]
        results.sort(key=lambda r: (r["score"], r["raw_score"], r.get("year") or 0), reverse=True)
        grouped.append({"keyword": keyword, "category": kw.get("category"), "keep": True, "local_results": results})
    return grouped


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


def role_rank(role: str | None) -> int:
    order = {"core_candidate": 0, "supporting_candidate": 1, "background": 2, "uncertain": 3, "excluded": 4}
    return order.get(role or "uncertain", 3)


def write_report(out_dir: Path, topic: str, keyword_set: dict[str, Any], combined: list[dict[str, Any]]) -> None:
    lines = ["# Topic Paper Discovery Report", "", f"Topic: {topic}", "", "## Keywords", ""]
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


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    user_keywords = split_keywords(args.keywords)
    project_id = args.project_id or slugify(args.topic)
    project = review_root / "review-projects" / project_id
    out_dir = project / "00_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "topic_input.md").write_text(
        f"# {args.topic}\n\nUser keywords:\n\n" + "\n".join(f"- {kw}" for kw in user_keywords) + "\n",
        encoding="utf-8",
    )
    keyword_set = build_keyword_set(args.topic, user_keywords)
    write_json(out_dir / "keyword_set.draft.json", keyword_set)
    papers = load_metadata(review_root)
    classification_rules = load_classification_rules(review_root)
    local_grouped = local_search_by_keyword(papers, keyword_set["merged_keywords"], args.topic, classification_rules)
    write_json(out_dir / "local_results_by_keyword.json", {"project_id": project_id, "results": local_grouped})
    web_grouped = []
    if args.web_search:
        for group in local_grouped:
            web_grouped.append({"keyword": group["keyword"], "web_results": web_search(group["keyword"], args.topic, args.web_limit)})
            if args.web_delay:
                time.sleep(args.web_delay)
    write_json(out_dir / "web_results_by_keyword.json", {"project_id": project_id, "enabled": bool(args.web_search), "results": web_grouped})
    combined = combine_results(local_grouped, web_grouped)
    write_json(out_dir / "combined_results_by_keyword.json", {"project_id": project_id, "topic": args.topic, "results": combined})
    selected = selected_from_combined(combined)
    selected["project_id"] = project_id
    selected["human_confirmed"] = False
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
    write_report(out_dir, args.topic, keyword_set, combined)
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
    parser.add_argument("--web-search", action="store_true")
    parser.add_argument("--web-limit", type=int, default=8)
    parser.add_argument("--web-delay", type=float, default=0.2)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
