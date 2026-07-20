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
    review-projects/<project_id>/05_final_audit/final_draft.md
      (fallback: 04_first_draft/first_draft.md)
    review-projects/<project_id>/04_first_draft/citations.json   (optional)
    review-projects/<project_id>/01_matrix_outline/section_blueprint.json (optional)
    review-projects/<project_id>/00_discovery/topic_input.md     (optional)

Outputs (under 05_final_audit/):
    review_summary_chart.html
    review_summary_chart.json
    review_summary_chart.png
    review_section_chart_<nn>_<section>.png

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
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


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

NUMBERED_SECTION_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*[.)]?\s+\S")
EXCLUDED_CHART_HEADING_KEYS = {
    "abstract", "keywords", "key words", "references", "reference list",
    "bibliography", "cited literature", "supporting information",
    "supplementary information", "table of contents",
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

def normalize_chart_heading(heading: str) -> str:
    value = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", heading)
    value = re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def classify_section(heading: str) -> str:
    low = normalize_chart_heading(heading)
    if low in {"keywords", "key words"}:
        return "keywords"
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
    heading_re = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*$")
    current: ReviewSection | None = None
    fence_character: str | None = None
    fence_length = 0

    for idx, line in enumerate(lines, start=1):
        if fence_character is not None:
            closing_fence = re.compile(
                rf"^\s*{re.escape(fence_character)}{{{fence_length},}}\s*$"
            )
            if closing_fence.match(line):
                fence_character = None
                fence_length = 0
            continue
        fence_match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        m = heading_re.match(line)
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
        rf"^ {{0,3}}#{{1,6}}\s+{heading_escaped}\s*$(.+?)(?=^ {{0,3}}#{{1,6}}\s+|\Z)",
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

def load_citation_map(citations_path: Path) -> dict[str, str]:
    """Map [n] callout -> paper_id from citations.json.

    citations.json may be a list of {callout, paper_id} or a dict
    {callout: paper_id} or {reference_number: paper_id}.
    """
    data = read_json(citations_path)
    mapping: dict[str, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).strip()
            pid = ""
            if isinstance(v, dict):
                pid = str(v.get("paper_id") or v.get("id") or "")
            elif isinstance(v, list) and v:
                pid = str(v[0].get("paper_id") if isinstance(v[0], dict) else v[0])
            else:
                pid = str(v or "")
            if key and pid:
                mapping[key] = pid
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            n = str(item.get("callout") or item.get("reference_number")
                    or item.get("number") or item.get("n") or "").strip()
            pid = str(item.get("paper_id") or item.get("id") or "").strip()
            if n and pid:
                mapping[n] = pid
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


def chartable_sections(sections: list[ReviewSection]) -> list[ReviewSection]:
    """Return manuscript body sections that should receive DOCX charts."""
    return [
        section for section in sections
        if normalize_chart_heading(section.heading) not in EXCLUDED_CHART_HEADING_KEYS
    ]


def manuscript_sections(sections: list[ReviewSection]) -> list[ReviewSection]:
    """Select the same top-level manuscript sections used by export and gating."""
    all_sections = flatten(sections)
    level_two = [section for section in all_sections if section.level == 2]
    if level_two:
        return level_two
    return [
        section for section in all_sections
        if section.level == 1 and NUMBERED_SECTION_HEADING_RE.match(section.heading)
    ]


def section_detail_text(section: ReviewSection) -> str:
    """Return concise chart text without leaking Markdown table syntax."""
    if section.children:
        return ", ".join(child.heading for child in section.children[:5])
    summary = re.sub(r"\s+", " ", section.summary).strip()
    if summary and "|" not in summary:
        return summary
    return (
        f"{section.word_count} words; "
        f"{len(section.citation_callouts)} citation callouts; "
        f"{len(section.cited_paper_ids)} mapped papers"
    )


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


# ── Offline PNG output ───────────────────────────────────────────────────────

_FONT_CANDIDATES = {
    "regular": [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ],
    "bold": [
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ],
}


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES["bold" if bold else "regular"]:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _slug(text: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return value[:48] or fallback


def _wrapped_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
                   max_width: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    # Long CJK strings may contain no spaces. Use a conservative character wrap.
    if len(lines) == 1 and draw.textbbox((0, 0), lines[0], font=font)[2] > max_width:
        average = max(1, draw.textbbox((0, 0), "测", font=font)[2])
        width = max(1, max_width // average)
        lines = textwrap.wrap(text, width=width, break_long_words=True,
                              break_on_hyphens=False)
    return lines


def _draw_centered_lines(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                         lines: list[str], font: ImageFont.ImageFont,
                         fill: str = "#1f2937", spacing: int = 8) -> None:
    x1, y1, x2, y2 = box
    heights = [draw.textbbox((0, 0), line, font=font)[3] for line in lines]
    total = sum(heights) + spacing * max(0, len(lines) - 1)
    y = y1 + max(0, (y2 - y1 - total) // 2)
    for line, height in zip(lines, heights):
        width = draw.textbbox((0, 0), line, font=font)[2]
        draw.text((x1 + (x2 - x1 - width) / 2, y), line, font=font, fill=fill)
        y += height + spacing


def _save_png(image: Image.Image, path: Path) -> dict[str, str]:
    image.save(path, format="PNG", optimize=True)
    return {"path": path.name, "sha256": sha256_file(path)}


def render_full_chart_png(sections: list[ReviewSection], review_title: str,
                          path: Path) -> dict[str, str]:
    width = 1800
    margin = 90
    title_height = 150
    card_width = width - 2 * margin
    card_height = 150
    gap = 70
    height = margin + title_height + 80 + len(sections) * (card_height + gap) + margin
    image = Image.new("RGB", (width, max(height, 720)), "#f8fafc")
    draw = ImageDraw.Draw(image)
    title_font = _font(42, bold=True)
    heading_font = _font(30, bold=True)
    detail_font = _font(23)

    title_box = (margin, margin, width - margin, margin + title_height)
    draw.rounded_rectangle(title_box, radius=24, fill="#e8eaf6", outline="#3f51b5", width=4)
    title_lines = _wrapped_lines(draw, review_title or "Review Article", title_font,
                                 card_width - 80)[:2]
    _draw_centered_lines(draw, title_box, title_lines, title_font, fill="#1e3a8a")

    previous_bottom = title_box[3]
    for index, section in enumerate(sections, start=1):
        top = previous_bottom + gap
        box = (margin, top, width - margin, top + card_height)
        center_x = width // 2
        draw.line((center_x, previous_bottom, center_x, top - 14), fill="#64748b", width=5)
        draw.polygon([(center_x, top), (center_x - 12, top - 18),
                      (center_x + 12, top - 18)], fill="#64748b")
        fill = "#fdfdfd" if index % 2 else "#f0f9ff"
        draw.rounded_rectangle(box, radius=22, fill=fill, outline="#64748b", width=3)
        heading = f"{index}. {section.heading}"
        heading_lines = _wrapped_lines(draw, heading, heading_font, card_width - 100)[:2]
        details = section_detail_text(section)
        detail_lines = _wrapped_lines(draw, details, detail_font, card_width - 120)[:2]
        all_lines = heading_lines + detail_lines
        fonts = [heading_font] * len(heading_lines) + [detail_font] * len(detail_lines)
        heights = [draw.textbbox((0, 0), line, font=font)[3]
                   for line, font in zip(all_lines, fonts)]
        y = top + max(18, (card_height - sum(heights) - 8 * max(0, len(all_lines) - 1)) // 2)
        for line, font, line_height in zip(all_lines, fonts, heights):
            line_width = draw.textbbox((0, 0), line, font=font)[2]
            draw.text(((width - line_width) / 2, y), line, font=font,
                      fill="#0f172a" if font is heading_font else "#475569")
            y += line_height + 8
        previous_bottom = box[3]
    return _save_png(image, path)


def render_section_chart_png(section: ReviewSection, path: Path) -> dict[str, str]:
    width = 1800
    margin = 90
    rows = section.children or [section]
    row_height = 145
    gap = 55
    header_height = 145
    height = margin * 2 + header_height + 80 + len(rows) * (row_height + gap)
    image = Image.new("RGB", (width, max(height, 700)), "#ffffff")
    draw = ImageDraw.Draw(image)
    title_font = _font(38, bold=True)
    heading_font = _font(28, bold=True)
    detail_font = _font(22)

    header = (margin, margin, width - margin, margin + header_height)
    draw.rounded_rectangle(header, radius=24, fill="#e0f2fe", outline="#0284c7", width=4)
    _draw_centered_lines(
        draw,
        header,
        _wrapped_lines(draw, section.heading, title_font, width - 2 * margin - 80)[:2],
        title_font,
        fill="#0c4a6e",
    )
    previous_bottom = header[3]
    for index, node in enumerate(rows, start=1):
        top = previous_bottom + gap
        box = (margin + 100, top, width - margin - 100, top + row_height)
        center_x = width // 2
        draw.line((center_x, previous_bottom, center_x, top - 12), fill="#94a3b8", width=4)
        draw.polygon([(center_x, top), (center_x - 10, top - 16),
                      (center_x + 10, top - 16)], fill="#94a3b8")
        draw.rounded_rectangle(box, radius=20, fill="#f8fafc", outline="#94a3b8", width=3)
        label = node.heading if section.children else "Section overview"
        papers = ", ".join(node.cited_paper_ids[:8])
        details = papers or section_detail_text(node)
        heading_lines = _wrapped_lines(draw, f"{index}. {label}", heading_font,
                                       box[2] - box[0] - 100)[:2]
        detail_lines = _wrapped_lines(draw, details, detail_font,
                                      box[2] - box[0] - 100)[:2]
        all_lines = heading_lines + detail_lines
        fonts = [heading_font] * len(heading_lines) + [detail_font] * len(detail_lines)
        heights = [draw.textbbox((0, 0), line, font=font)[3]
                   for line, font in zip(all_lines, fonts)]
        y = top + max(16, (row_height - sum(heights) - 7 * max(0, len(all_lines) - 1)) // 2)
        for line, font, line_height in zip(all_lines, fonts, heights):
            line_width = draw.textbbox((0, 0), line, font=font)[2]
            draw.text(((width - line_width) / 2, y), line, font=font,
                      fill="#0f172a" if font is heading_font else "#475569")
            y += line_height + 7
        previous_bottom = box[3]
    entry = _save_png(image, path)
    entry["heading"] = section.heading
    return entry


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
    for rel in ("05_final_audit/final_draft.md", "04_first_draft/first_draft.md",
                "02_section_drafting/section_drafts.md"):
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
    citations_path = project / "04_first_draft" / "citations.json"
    citation_map = load_citation_map(citations_path)
    if citation_map:
        print(f"Loaded {len(citation_map)} citation->paper mappings.")
    for s in sections:
        map_callouts_to_papers(s, citation_map)

    # Blueprint enrichment (for stats / future claim display)
    blueprint_claims = load_blueprint_claims(
        project / "01_matrix_outline" / "section_blueprint.json")

    topic = infer_topic(project)
    body_sections = manuscript_sections(sections)
    first_is_title = bool(
        sections
        and sections[0].level == 1
        and not NUMBERED_SECTION_HEADING_RE.match(sections[0].heading)
        and any(section.level == 2 for section in flatten(sections))
    )
    review_title = (
        sections[0].heading if first_is_title else (topic or "Review Article")
    )

    full_mermaid = generate_full_mermaid(body_sections or sections, review_title)

    body_chart_sections = chartable_sections(body_sections or sections)

    # Per-section detail charts (小节大纲) for every manuscript body section.
    section_charts: list[dict[str, str]] = []
    if args.scope in ("section", "both"):
        for s in body_chart_sections:
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

    out_dir = draft_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    image_manifest: dict[str, Any] = {"full": None, "sections": []}
    if args.scope in ("full", "both"):
        image_manifest["full"] = render_full_chart_png(
            body_sections or sections,
            review_title,
            out_dir / "review_summary_chart.png",
        )
    if args.scope in ("section", "both"):
        for index, section in enumerate(body_chart_sections, start=1):
            filename = (
                f"review_section_chart_{index:02d}_"
                f"{_slug(section.heading, f'section-{index:02d}')}.png"
            )
            image_manifest["sections"].append(
                render_section_chart_png(section, out_dir / filename)
            )
    stats["image_manifest"] = image_manifest

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
