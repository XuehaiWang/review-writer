#!/usr/bin/env python3
"""Generate a content summary chart for a generated REVIEW article.

Runs at the end of the review-writing workflow. Reads the final (or first)
draft of the review and produces a visual content-summary chart at two
granularities:

- **full** (全文大纲): a review-level Mermaid flowchart showing the section
  hierarchy, the number of cited papers per section, and the logical flow
  between sections.
- **section** (小节大纲): per-section detail cards listing each subsection,
  its key summary sentence, and the papers cited inside it (mapped from [n]
  callouts via citations.json, enriched by section_blueprint.json claims).

Inputs:
    review-projects/<project_id>/07_final_audit/final_draft.md
      (fallback: 05_first_draft/first_draft.md)
    review-projects/<project_id>/05_first_draft/draft_bundle.json's citation_map (optional)
    review-projects/<project_id>/02_section_blueprint/section_blueprint.json (optional)
    review-projects/<project_id>/00_discovery/topic_input.md     (optional)

Outputs (under 08_summary_chart/):
    review_summary_chart.html
    review_summary_chart.json

Usage:
    python generate_review_summary_chart.py \
        --review-root <review-root> \
        --project-id <project_id> \
        --scope both
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ReviewSection:
    """A node in the review's section hierarchy."""
    heading: str
    level: int
    line_number: int
    section_type: str = "body"
    children: list["ReviewSection"] = field(default_factory=list)
    summary: str = ""
    word_count: int = 0
    citation_callouts: list[str] = field(default_factory=list)  # raw [n] tokens
    cited_paper_ids: list[str] = field(default_factory=list)    # mapped paper IDs
    paragraph_ids: list[str] = field(default_factory=list)


SECTION_SIGNALS: dict[str, list[str]] = {
    "abstract": ["abstract", "summary"],
    "introduction": ["introduction", "background", "前言", "引言", "背景"],
    "results": ["result", "results and discussion", "findings", "结果"],
    "discussion": ["discussion", "讨论"],
    "conclusion": ["conclusion", "conclusions", "summary and outlook",
                   "summary and conclusions", "结论", "总结", "展望"],
    "methods": ["experimental", "methods", "materials and methods",
                "general procedure", "实验", "方法"],
    "references": ["references", "bibliography", "cited literature", "参考文献"],
    "supporting": ["supporting information", "supplementary", "补充"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ── Parsing ──────────────────────────────────────────────────────────────────

def classify_section(heading: str) -> str:
    low = heading.lower().strip("#").strip()
    for sec_type, signals in SECTION_SIGNALS.items():
        for signal in signals:
            if signal in low:
                return sec_type
    return "body"


def parse_review_outline(md_text: str) -> list[ReviewSection]:
    """Parse the review markdown headings into a hierarchy.

    Handles # through ####, numbered headings (1, 1.1, 1.2.3), and skips
    image references inside headings.
    """
    lines = md_text.splitlines()
    root: list[ReviewSection] = []
    stack: list[ReviewSection] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    current: ReviewSection | None = None

    for idx, line in enumerate(lines, start=1):
        m = heading_re.match(line.strip())
        if m:
            level = len(m.group(1))
            heading = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", m.group(2).strip()).strip()
            if not heading:
                continue
            node = ReviewSection(
                heading=heading, level=level, line_number=idx,
                section_type=classify_section(heading),
            )
            while stack and stack[-1].level >= level:
                stack.pop()
            (stack[-1].children.append(node) if stack else root.append(node))
            stack.append(node)
            current = node
        elif current is not None and line.strip():
            current.word_count += len(re.findall(r"\b\w+\b", line))
            # collect [n] citation callouts
            for tok in re.findall(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]", line):
                for part in re.split(r"[-,]", tok):
                    part = part.strip()
                    if part.isdigit() and part not in current.citation_callouts:
                        current.citation_callouts.append(part)
            # collect paragraph_id markers
            pm = re.search(r"<!--\s*paragraph_id:\s*([^\s-]+(?:-p\d+)?)\s*-->", line)
            if pm and pm.group(1) not in current.paragraph_ids:
                current.paragraph_ids.append(pm.group(1))
    return root


def extract_section_summary(md_text: str, heading: str, max_sentences: int = 2) -> str:
    """Extract key sentences from the body under a heading."""
    heading_escaped = re.escape(heading.strip())
    pattern = re.compile(
        rf"^#{{1,6}}\s+{heading_escaped}\s*$(.+?)(?=^#{{1,6}}\s+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(md_text)
    if not m:
        return ""
    body = m.group(1).strip()
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"```[\s\S]*?```", "", body)
    body = re.sub(r"<!--.*?-->", "", body)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。])\s+", body)
                 if len(s.strip()) > 30]
    if not sentences:
        return ""
    selected = [sentences[0]]
    if len(sentences) > 1 and sentences[-1] not in selected:
        selected.append(sentences[-1])
    return " ".join(selected[:max_sentences])


# ── Citation mapping ─────────────────────────────────────────────────────────

def load_citation_map(draft_bundle_path: Path) -> dict[str, str]:
    """Map [n] callout -> paper_id from draft_bundle.json's citation_map
    ({paper_id: bracket_number}), inverted to {str(bracket_number): paper_id}."""
    data = read_json(draft_bundle_path)
    citation_map = data.get("citation_map") if isinstance(data, dict) else None
    mapping: dict[str, str] = {}
    if not isinstance(citation_map, dict):
        return mapping
    for paper_id, callout in citation_map.items():
        try:
            mapping[str(int(callout))] = str(paper_id).strip()
        except (TypeError, ValueError):
            continue
    return mapping


def map_callouts_to_papers(section: ReviewSection, citation_map: dict[str, str]) -> None:
    """Resolve [n] callouts to paper IDs, recursively."""
    section.cited_paper_ids = []
    seen: set[str] = set()
    for n in section.citation_callouts:
        pid = citation_map.get(n)
        if pid and pid not in seen:
            seen.add(pid)
            section.cited_paper_ids.append(pid)
    for child in section.children:
        map_callouts_to_papers(child, citation_map)


# ── Section blueprint enrichment ─────────────────────────────────────────────

def load_blueprint_claims(blueprint_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Map section_id -> list of {claim, claim_type, paper_ids} from blueprint."""
    data = read_json(blueprint_path)
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(data, dict):
        return out
    sections = data.get("sections") or []
    if not isinstance(sections, list):
        return out
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        sid = str(sec.get("section_id") or sec.get("id") or "")
        claims: list[dict[str, Any]] = []
        for c in sec.get("review_claims") or []:
            if not isinstance(c, dict):
                continue
            pids = [str(p.get("paper_id")) for p in (c.get("supporting_papers") or [])
                    if isinstance(p, dict) and p.get("paper_id")]
            claims.append({
                "claim": c.get("claim", ""),
                "claim_type": c.get("claim_type", ""),
                "paper_ids": pids,
            })
        if sid:
            out[sid] = claims
    return out


# ── Flatten ──────────────────────────────────────────────────────────────────

def flatten(sections: list[ReviewSection]) -> list[ReviewSection]:
    out: list[ReviewSection] = []
    for s in sections:
        out.append(s)
        out.extend(flatten(s.children))
    return out


def infer_section_id(section: ReviewSection) -> str:
    """Infer a section id like 'sec1' from a numbered heading '1 ...' or '1.1 ...'."""
    m = re.match(r"^(\d+)", section.heading.strip())
    if m:
        return f"sec{m.group(1)}"
    return ""


# ── Mermaid: full-review chart (全文大纲) ────────────────────────────────────

def sanitize_mermaid(text: str, max_len: int = 50) -> str:
    text = text.replace('"', "'").replace("[", "(").replace("]", ")")
    text = re.sub(r"[\r\n]+", " ", text)
    if len(text) > max_len:
        text = text[:max_len - 3] + "..."
    return text


def style_for(sec_type: str) -> str:
    colors = {
        "abstract": "fill:#e8f5e9,stroke:#4caf50",
        "introduction": "fill:#e3f2fd,stroke:#2196f3",
        "results": "fill:#fff3e0,stroke:#ff9800",
        "discussion": "fill:#f3e5f5,stroke:#9c27b0",
        "conclusion": "fill:#fce4ec,stroke:#e91e63",
        "methods": "fill:#e0f7fa,stroke:#00bcd4",
        "references": "fill:#f5f5f5,stroke:#9e9e9e",
        "supporting": "fill:#efebe9,stroke:#795548",
    }
    return colors.get(sec_type, "fill:#fafafa,stroke:#bdbdbd")


def icon_for(sec_type: str) -> str:
    return {
        "abstract": "📄", "introduction": "📖", "results": "🔬",
        "discussion": "💬", "conclusion": "🏁", "methods": "⚗️",
        "references": "📚", "supporting": "📎",
    }.get(sec_type, "📌")


def generate_full_mermaid(sections: list[ReviewSection], review_title: str) -> str:
    """全文大纲: review-level structure flowchart with paper-count badges."""
    lines = ["graph TD"]
    counter = [0]

    def nid() -> str:
        counter[0] += 1
        return f"N{counter[0]}"

    def render(node: ReviewSection, parent_id: str | None) -> str:
        i = nid()
        badge = f" ({len(node.cited_paper_ids)} papers)" if node.cited_paper_ids else ""
        label = f"{icon_for(node.section_type)} {sanitize_mermaid(node.heading)}{badge}"
        lines.append(f'    {i}["{label}"]')
        lines.append(f"    style {i} {style_for(node.section_type)}")
        if parent_id:
            lines.append(f"    {parent_id} --> {i}")
        for child in node.children:
            render(child, i)
        return i

    if review_title:
        root_id = nid()
        lines.append(f'    {root_id}["📝 {sanitize_mermaid(review_title, 40)}"]')
        lines.append(f"    style {root_id} fill:#e8eaf6,stroke:#3f51b5")
        for s in sections:
            render(s, root_id)
    else:
        for s in sections:
            render(s, None)
    return "\n".join(lines)


# ── Mermaid: per-section detail (小节大纲) ───────────────────────────────────

def generate_section_mermaid(section: ReviewSection) -> str:
    """小节大纲: one section's subsections + cited papers as leaf nodes."""
    lines = ["graph TD"]
    counter = [0]

    def nid() -> str:
        counter[0] += 1
        return f"S{counter[0]}"

    def render(node: ReviewSection, parent_id: str | None) -> str:
        i = nid()
        label = f"{icon_for(node.section_type)} {sanitize_mermaid(node.heading, 40)}"
        lines.append(f'    {i}["{label}"]')
        lines.append(f"    style {i} {style_for(node.section_type)}")
        if parent_id:
            lines.append(f"    {parent_id} --> {i}")
        # paper leaves
        for pid in node.cited_paper_ids[:8]:
            p = nid()
            lines.append(f'    {p}[["{pid}"]]')
            lines.append(f"    style {p} fill:#fffde7,stroke:#fbc02d")
            lines.append(f"    {i} -.-> {p}")
        for child in node.children:
            render(child, i)
        return i

    render(section, None)
    return "\n".join(lines)


# ── HTML ─────────────────────────────────────────────────────────────────────

def generate_html(
    sections: list[ReviewSection],
    review_title: str,
    topic: str,
    full_mermaid: str,
    section_charts: list[dict[str, str]],
    stats: dict[str, Any],
) -> str:
    flat = flatten(sections)
    total_papers = len({pid for s in flat for pid in s.cited_paper_ids})

    cards = ""
    for s in flat[:40]:
        papers = ", ".join(s.cited_paper_ids[:12]) or "—"
        cards += f"""
        <div class="summary-card type-{s.section_type}">
            <div class="card-header">
                <span class="card-type">{s.section_type.upper()}</span>
                <span class="card-heading">{s.heading}</span>
                <span class="card-words">{s.word_count}w · {len(s.cited_paper_ids)} papers</span>
            </div>
            <div class="card-body">
                <p>{s.summary or 'No summary extracted.'}</p>
                <p class="papers"><strong>Cited:</strong> {papers}</p>
            </div>
        </div>"""

    section_chart_blocks = ""
    for sc in section_charts:
        section_chart_blocks += f"""
        <div class="chart-section">
            <h2>🔬 {sc['heading']}</h2>
            <div class="mermaid">{sc['mermaid']}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Review Summary Chart: {review_title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
.review-header {{ background: white; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.review-header h1 {{ font-size: 1.5rem; margin-bottom: 8px; }}
.review-meta {{ color: #666; font-size: 0.9rem; }}
.stats-bar {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
.stat {{ background: white; border-radius: 8px; padding: 12px 20px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); text-align: center; }}
.stat-value {{ font-size: 1.5rem; font-weight: 700; color: #3f51b5; }}
.stat-label {{ font-size: 0.75rem; color: #999; text-transform: uppercase; }}
.chart-section {{ background: white; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow-x: auto; }}
.chart-section h2 {{ font-size: 1.2rem; margin-bottom: 16px; color: #555; }}
.summary-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }}
.summary-card {{ background: white; border-radius: 8px; padding: 16px; border-left: 4px solid #ccc; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
.summary-card.type-abstract {{ border-left-color: #4caf50; }}
.summary-card.type-introduction {{ border-left-color: #2196f3; }}
.summary-card.type-results {{ border-left-color: #ff9800; }}
.summary-card.type-discussion {{ border-left-color: #9c27b0; }}
.summary-card.type-conclusion {{ border-left-color: #e91e63; }}
.summary-card.type-methods {{ border-left-color: #00bcd4; }}
.card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }}
.card-type {{ font-size: 0.7rem; font-weight: 700; padding: 2px 8px; border-radius: 4px; background: #eee; text-transform: uppercase; }}
.card-heading {{ font-weight: 600; font-size: 0.95rem; flex: 1; }}
.card-words {{ font-size: 0.75rem; color: #999; }}
.card-body p {{ font-size: 0.85rem; color: #555; margin-bottom: 4px; }}
.card-body .papers {{ font-size: 0.75rem; color: #888; }}
h2.section-title {{ margin: 24px 0 16px; color: #555; }}
</style>
</head>
<body>
<div class="container">
    <div class="review-header">
        <h1>{review_title}</h1>
        <div class="review-meta"><p><strong>Topic:</strong> {topic or 'N/A'}</p>
        <p><strong>Generated:</strong> {utc_now()}</p></div>
    </div>
    <div class="stats-bar">
        <div class="stat"><div class="stat-value">{len(flat)}</div><div class="stat-label">Sections</div></div>
        <div class="stat"><div class="stat-value">{sum(s.word_count for s in flat):,}</div><div class="stat-label">Total Words</div></div>
        <div class="stat"><div class="stat-value">{total_papers}</div><div class="stat-label">Cited Papers</div></div>
        <div class="stat"><div class="stat-value">{sum(len(s.citation_callouts) for s in flat)}</div><div class="stat-label">Citations</div></div>
    </div>
    <div class="chart-section">
        <h2>📊 全文大纲 · Full-Review Structure Chart</h2>
        <div class="mermaid">{full_mermaid}</div>
    </div>
    <h2 class="section-title">📋 小节大纲 · Section Summaries</h2>
    <div class="summary-cards">{cards}</div>
    {section_chart_blocks}
</div>
<script>mermaid.initialize({{ startOnLoad: true, theme: 'default', flowchart: {{ useMaxWidth: true, htmlLabels: true }} }});</script>
</body>
</html>"""


# ── JSON output ──────────────────────────────────────────────────────────────

def build_json(sections: list[ReviewSection], review_title: str, topic: str,
               full_mermaid: str, stats: dict[str, Any]) -> dict[str, Any]:
    def node_to_dict(s: ReviewSection) -> dict[str, Any]:
        return {
            "heading": s.heading, "level": s.level,
            "section_type": s.section_type, "word_count": s.word_count,
            "summary": s.summary,
            "citation_callouts": s.citation_callouts,
            "cited_paper_ids": s.cited_paper_ids,
            "paragraph_ids": s.paragraph_ids,
            "children": [node_to_dict(c) for c in s.children],
        }
    return {
        "review_title": review_title, "topic": topic,
        "created_at": utc_now(),
        "mermaid_full_chart": full_mermaid,
        "stats": stats,
        "sections": [node_to_dict(s) for s in sections],
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def infer_topic(project: Path) -> str:
    ti = project / "00_discovery" / "topic_input.md"
    if ti.exists():
        for line in ti.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("# "):
                return line.strip()[2:]
    return ""


def resolve_draft(project: Path) -> tuple[Path, bytes]:
    """Find the review draft (final > first) and return one byte snapshot."""
    for rel in ("07_final_audit/final_draft.md", "05_first_draft/first_draft.md",
                "03_section_drafting/section_drafts.md"):
        p = project / rel
        if p.exists():
            return p, p.read_bytes()
    return Path(), b""


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    if not project.exists():
        print(f"ERROR: Project not found: {project}", file=__import__("sys").stderr)
        return 2

    draft_path, draft_payload = resolve_draft(project)
    if not draft_payload:
        print("ERROR: No review draft found (final_draft.md / first_draft.md / section_drafts.md).",
              file=__import__("sys").stderr)
        return 2
    draft_text = draft_payload.decode("utf-8", errors="ignore")
    print(f"Using draft: {draft_path}")

    sections = parse_review_outline(draft_text)
    if not sections:
        print("WARN: No headings found in draft; chart will be minimal.")

    # Summaries
    for s in flatten(sections):
        s.summary = extract_section_summary(draft_text, s.heading)

    # Citation mapping
    draft_bundle_path = project / "05_first_draft" / "draft_bundle.json"
    citation_map = load_citation_map(draft_bundle_path)
    if citation_map:
        print(f"Loaded {len(citation_map)} citation->paper mappings.")
    for s in sections:
        map_callouts_to_papers(s, citation_map)

    # Blueprint enrichment (for stats / future claim display)
    blueprint_claims = load_blueprint_claims(
        project / "02_section_blueprint" / "section_blueprint.json")

    topic = infer_topic(project)
    review_title = sections[0].heading if sections else (topic or "Review Article")
    # The review title is usually a level-1 heading whose level-2 children are
    # the real body sections (Abstract, Introduction, ...). Use those children
    # as the body tree so the title is not duplicated in the chart.
    if sections and sections[0].level == 1 and sections[0].children:
        body_sections = sections[0].children
    elif sections and sections[0].level == 1:
        body_sections = sections[1:]
    else:
        body_sections = sections

    full_mermaid = generate_full_mermaid(body_sections or sections, review_title)

    # Per-section detail charts (小节大纲) for top-level sections
    section_charts: list[dict[str, str]] = []
    if args.scope in ("section", "both"):
        for s in (body_sections or sections)[:8]:
            section_charts.append({
                "heading": s.heading,
                "mermaid": generate_section_mermaid(s),
            })

    flat = flatten(sections)
    stats = {
        "section_count": len(flat),
        "total_words": sum(s.word_count for s in flat),
        "citation_callout_count": sum(len(s.citation_callouts) for s in flat),
        "unique_cited_papers": len({pid for s in flat for pid in s.cited_paper_ids}),
        "blueprint_claim_sections": len(blueprint_claims),
        "draft_source": str(draft_path.resolve()),
        "draft_sha256": hashlib.sha256(draft_payload).hexdigest(),
        "generation_scope": args.scope,
    }

    out_dir = project / "08_summary_chart"
    out_dir.mkdir(parents=True, exist_ok=True)

    html = generate_html(body_sections or sections, review_title, topic,
                         full_mermaid, section_charts, stats)
    stats["html_sha256"] = hashlib.sha256(html.encode("utf-8")).hexdigest()

    if args.scope in ("json", "both") or args.scope == "full":
        json_data = build_json(body_sections or sections, review_title, topic,
                               full_mermaid, stats)
        write_json(out_dir / "review_summary_chart.json", json_data)
        print(f"Wrote JSON: {out_dir / 'review_summary_chart.json'}")

    if args.scope in ("html", "both") or args.scope == "full":
        write_text(out_dir / "review_summary_chart.html", html)
        print(f"Wrote HTML: {out_dir / 'review_summary_chart.html'}")

    print(f"\nReview summary chart: {len(flat)} sections, "
          f"{stats['citation_callout_count']} citations, "
          f"{stats['unique_cited_papers']} unique papers.")
    return 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Generate a content summary chart for a generated review article.")
    ap.add_argument("--review-root", default=".",
                    help="Review project root (contains review-projects/). Default: cwd.")
    ap.add_argument("--project-id", required=True,
                    help="Project ID (directory under review-projects/)")
    ap.add_argument("--scope", choices=["full", "section", "both", "html", "json"],
                    default="both",
                    help="full=全文大纲 only; section=小节大纲 only; both=all; "
                         "html/json limit output format")
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
