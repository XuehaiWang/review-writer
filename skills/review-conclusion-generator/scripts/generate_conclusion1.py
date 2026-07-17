#!/usr/bin/env python3
"""
Generate high-quality Conclusion / Challenges / Insights for a review project.

Aggregates information from the full paper draft, literature matrix, section
blueprint, section drafts, paper reading notes, and audit report to construct
a comprehensive LLM prompt. The LLM generates 2-3 paragraphs covering:

1. Overall conclusions (synthesis of what the field has achieved)
2. Current challenges / limitations (specific, named, paper-referenced)
3. Future directions or methodological insights

Quality constraints are enforced: reference to specific paper IDs, organic
connection to body text, prohibition of vague "more research is needed" language,
and review-level abstraction/comparison/judgment.

Usage:
    python generate_conclusion1.py --review-root <review-root> --project-id my-project
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_NUMERIC_CITATION_RE = re.compile(r"\[\d+(?:\s*[-,]\s*\d+)*\]")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _invalidate_required_outputs(out_dir: Path) -> None:
    for filename in ("conclusion_generated.md", "conclusion_quality_report.json"):
        (out_dir / filename).unlink(missing_ok=True)


def _normalize_callout(value: Any) -> str | None:
    match = re.fullmatch(r"(?:\[(\d+)\]|(\d+))", str(value).strip())
    if not match:
        return None
    return str(int(match.group(1) or match.group(2)))


def _paper_ids_from_slot(slot: Any) -> list[str]:
    if isinstance(slot, str):
        return [slot.strip()] if slot.strip() else []
    if not isinstance(slot, dict):
        return []

    paper_ids: list[str] = []
    paper_id = slot.get("paper_id")
    if isinstance(paper_id, str) and paper_id.strip():
        paper_ids.append(paper_id.strip())
    for key in ("paper_ids", "cited_paper_ids"):
        values = slot.get(key)
        if isinstance(values, list):
            paper_ids.extend(
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            )
    return paper_ids


def load_paper_to_callout(path: Path) -> dict[str, str]:
    """Load supported citation slots as a paper-ID-to-callout mapping."""
    data = read_json(path)
    mapping: dict[str, str] = {}

    if isinstance(data, list):
        slots = (
            (item.get("callout", item.get("index")), item)
            for item in data
            if isinstance(item, dict)
        )
    elif isinstance(data, dict):
        slots = data.items()
    else:
        slots = []

    for raw_callout, slot in slots:
        callout = _normalize_callout(raw_callout)
        if callout is None:
            continue
        for paper_id in _paper_ids_from_slot(slot):
            mapping.setdefault(paper_id, callout)
    return mapping


def render_conclusion_markdown(
    result: dict[str, Any],
    paper_to_callout: dict[str, str],
) -> str:
    """Render manuscript-only conclusion Markdown with numeric callouts."""
    lines = ["## Conclusion / Challenges / Insights", ""]
    paragraphs = result.get("paragraphs") or []
    if isinstance(paragraphs, list):
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                continue
            content = str(paragraph.get("content") or "").strip()
            if not content:
                continue
            content = _NUMERIC_CITATION_RE.sub("", content)
            references = paragraph.get("referenced_papers") or []
            callouts: set[str] = set()
            if isinstance(references, list):
                for reference in references:
                    paper_id = str(reference).strip()
                    if paper_id in paper_to_callout:
                        callouts.add(paper_to_callout[paper_id])
            if callouts:
                content += " " + "".join(f"[{slot}]" for slot in sorted(callouts, key=int))
            lines.extend([content, ""])
    return "\n".join(lines)


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


# ── Data collection ──────────────────────────────────────────────────────────

def collect_limitations(
    matrix: dict[str, Any],
    notes: dict[str, Any],
) -> list[dict[str, Any]]:
    """Aggregate limitations from literature matrix and paper reading notes."""
    limitations: list[dict[str, Any]] = []

    # From literature matrix
    papers = matrix.get("papers") if isinstance(matrix, dict) else []
    if isinstance(papers, list):
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            pid = paper.get("paper_id", "")
            limitation = paper.get("limitation") or paper.get("main_limitation") or ""
            if limitation and str(limitation).strip() and str(limitation).strip().lower() != "not specified":
                limitations.append({
                    "paper_id": str(pid),
                    "limitation": str(limitation).strip(),
                    "source": "literature_matrix",
                })

    # From paper reading notes
    if isinstance(notes, dict):
        for pid, note in notes.items():
            if isinstance(note, dict):
                lims = note.get("limitations") or ""
                if isinstance(lims, str) and lims.strip() and lims.strip().lower() != "not specified":
                    limitations.append({
                        "paper_id": str(pid),
                        "limitation": lims.strip(),
                        "source": "paper_reading_notes",
                    })
                elif isinstance(lims, list):
                    for lim in lims:
                        if str(lim).strip():
                            limitations.append({
                                "paper_id": str(pid),
                                "limitation": str(lim).strip(),
                                "source": "paper_reading_notes",
                            })

    return limitations


def identify_trends(matrix: dict[str, Any]) -> dict[str, Any]:
    """Identify methodology trends from the literature matrix."""
    papers = matrix.get("papers") if isinstance(matrix, dict) else []
    if not isinstance(papers, list):
        papers = []

    reaction_types: dict[str, int] = {}
    catalyst_methods: dict[str, int] = {}
    years: list[int] = []
    substrate_classes: dict[str, int] = {}

    for paper in papers:
        if not isinstance(paper, dict):
            continue
        # Reaction type
        rt = paper.get("reaction_type") or ""
        if rt and str(rt).strip():
            reaction_types[str(rt).strip()] = reaction_types.get(str(rt).strip(), 0) + 1
        # Catalyst
        cat = paper.get("catalyst_or_method") or ""
        if cat and str(cat).strip():
            catalyst_methods[str(cat).strip()] = catalyst_methods.get(str(cat).strip(), 0) + 1
        # Year
        y = paper.get("year")
        if isinstance(y, (int, float)):
            years.append(int(y))
        elif isinstance(y, dict) and y.get("value"):
            try:
                years.append(int(y["value"]))
            except (ValueError, TypeError):
                pass
        # Substrate
        sub = paper.get("substrate") or ""
        if sub and str(sub).strip():
            substrate_classes[str(sub).strip()] = substrate_classes.get(str(sub).strip(), 0) + 1

    top_reactions = sorted(reaction_types.items(), key=lambda x: -x[1])[:5]
    top_catalysts = sorted(catalyst_methods.items(), key=lambda x: -x[1])[:5]
    top_substrates = sorted(substrate_classes.items(), key=lambda x: -x[1])[:5]

    return {
        "dominant_reaction_types": [{"name": k, "count": v} for k, v in top_reactions],
        "dominant_catalyst_methods": [{"name": k, "count": v} for k, v in top_catalysts],
        "dominant_substrate_classes": [{"name": k, "count": v} for k, v in top_substrates],
        "year_range": f"{min(years)}-{max(years)}" if years else "N/A",
        "paper_count": len(papers),
    }


def collect_section_claims(blueprint: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect all section-level claims from the section blueprint."""
    claims: list[dict[str, Any]] = []
    sections = blueprint.get("sections") if isinstance(blueprint, dict) else []
    if not isinstance(sections, list):
        return claims
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_claims = section.get("review_claims") or []
        for claim in section_claims if isinstance(section_claims, list) else []:
            if isinstance(claim, dict):
                claims.append({
                    "section_id": section.get("section_id", ""),
                    "section_title": section.get("title", ""),
                    "claim": claim.get("claim", ""),
                    "claim_type": claim.get("claim_type", ""),
                    "paper_ids": [p.get("paper_id") for p in (claim.get("supporting_papers") or []) if isinstance(p, dict)],
                })
    return claims


# ── Full-text structure parsing ──────────────────────────────────────────────

_SECTION_SIGNALS: dict[str, list[str]] = {
    "abstract": ["abstract", "summary"],
    "introduction": ["introduction", "background", "前言", "引言", "背景"],
    "results": ["result", "results and discussion", "findings", "结果"],
    "discussion": ["discussion", "讨论"],
    "conclusion": ["conclusion", "conclusions", "summary and outlook",
                   "结论", "总结", "展望"],
    "methods": ["experimental", "methods", "materials and methods",
                "实验", "方法"],
    "references": ["references", "bibliography", "参考文献"],
    "supporting": ["supporting information", "supplementary", "补充"],
}


def _classify_section(heading: str) -> str:
    low = heading.lower().strip("#").strip()
    for sec_type, signals in _SECTION_SIGNALS.items():
        for signal in signals:
            if signal in low:
                return sec_type
    return "body"


def parse_review_structure(md_text: str) -> list[dict[str, Any]]:
    """Parse the review draft into per-section structure summaries.

    Returns a list of {heading, level, line_number, section_type, summary,
    cited_paper_ids}. The summary is the first substantive sentence(s) under
    each heading. cited_paper_ids are not resolved here (kept as []); the
    conclusion prompt uses the section list for structural grounding.
    """
    if not md_text:
        return []
    lines = md_text.splitlines()
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_buf: list[str] = []

    def flush(sec: dict[str, Any] | None) -> None:
        if sec is None:
            return
        body = " ".join(body_buf)
        body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
        body = re.sub(r"```[\s\S]*?```", "", body)
        body = re.sub(r"<!--.*?-->", "", body)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。])\s+", body)
                     if len(s.strip()) > 30]
        sec["summary"] = " ".join(sentences[:2]) if sentences else ""

    for idx, line in enumerate(lines, start=1):
        m = heading_re.match(line.strip())
        if m:
            flush(current)
            level = len(m.group(1))
            heading = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", m.group(2).strip()).strip()
            if not heading:
                current = None
                body_buf = []
                continue
            current = {
                "heading": heading,
                "level": level,
                "line_number": idx,
                "section_type": _classify_section(heading),
                "summary": "",
                "cited_paper_ids": [],
            }
            sections.append(current)
            body_buf = []
        elif current is not None and line.strip():
            body_buf.append(line)
    flush(current)
    return sections


def collect_available_paper_ids(matrix: dict[str, Any],
                                claims: list[dict[str, Any]]) -> list[str]:
    """Collect the set of paper IDs actually present in the review project."""
    pids: list[str] = []
    seen: set[str] = set()
    papers = matrix.get("papers") if isinstance(matrix, dict) else []
    if isinstance(papers, list):
        for p in papers:
            if isinstance(p, dict):
                pid = str(p.get("paper_id") or "").strip()
                if pid and pid not in seen:
                    seen.add(pid)
                    pids.append(pid)
    for c in claims:
        for pid in c.get("paper_ids", []) or []:
            pid = str(pid).strip()
            if pid and pid not in seen:
                seen.add(pid)
                pids.append(pid)
    return pids


# ── LLM prompt construction ──────────────────────────────────────────────────

def build_conclusion_prompt(context: dict[str, Any]) -> str:
    """Build a comprehensive LLM prompt for conclusion generation."""
    draft = context.get("draft_text", "")
    claims = context.get("claims", [])
    limitations = context.get("limitations", [])
    trends = context.get("trends", {})
    topic = context.get("topic", "this review")
    section_summaries = context.get("section_summaries", [])
    available_paper_ids = context.get("available_paper_ids", [])

    # Build per-section structure summary (preferred over raw draft truncation).
    # This gives the LLM the full review structure so the conclusion stays
    # connected to the body and is not a simple recap of one excerpt.
    if section_summaries:
        structure_text = ""
        for s in section_summaries:
            indent = "  " * (s.get("level", 2) - 2)
            papers = ", ".join(s.get("cited_paper_ids", [])) or "-"
            structure_text += (f"{indent}- [{s.get('section_type','body')}] "
                               f"{s.get('heading','')} (papers: {papers})\n")
            if s.get("summary"):
                structure_text += f"{indent}    summary: {s['summary']}\n"
        structure_block = f"### Review Section Structure (full text)\n{structure_text}"
    else:
        # Fallback: truncated raw draft (older projects without parsed structure)
        draft_excerpt = draft[:8000] if len(draft) > 8000 else draft
        structure_block = (f"### Draft Excerpt (first 8000 chars; full structure "
                           f"not parsed)\n{draft_excerpt}")

    # Build claims summary
    claims_text = ""
    for claim in claims[:15]:
        claims_text += f"- [{claim['section_title']}] {claim['claim']} (papers: {', '.join(claim.get('paper_ids', []))})\n"

    # Build limitations summary
    limitations_text = ""
    seen_lims: set[str] = set()
    for lim in limitations[:20]:
        key = lim["limitation"].lower()[:50]
        if key not in seen_lims:
            seen_lims.add(key)
            limitations_text += f"- [{lim['paper_id']}] {lim['limitation']}\n"

    # Build trends summary
    top_reactions = ", ".join(r["name"] for r in trends.get("dominant_reaction_types", [])[:3])
    top_catalysts = ", ".join(c["name"] for c in trends.get("dominant_catalyst_methods", [])[:3])
    top_substrates = ", ".join(s["name"] for s in trends.get("dominant_substrate_classes", [])[:3])
    paper_count = trends.get("paper_count", "N/A")

    # Build the list of available paper IDs so the LLM cites real papers only.
    paper_ids_hint = ", ".join(available_paper_ids[:60]) if available_paper_ids else "(paper IDs not enumerated)"

    prompt = f"""You are an expert scientific review writer in organic chemistry. Generate a high-quality Conclusion / Challenges / Insights section for a review paper on: {topic}.

## Instructions

Write 2-3 paragraphs covering:
1. **Overall Conclusions**: Synthesize what the field has collectively achieved across the reviewed papers. Connect dominant approaches to key products. Avoid simple enumeration.
2. **Current Challenges / Limitations**: Present 2-3 specific, named challenges drawn from the aggregated limitations. Each challenge must be tied to a specific paper or substrate class. Avoid vague "more research is needed" language.
3. **Future Directions / Methodological Insights**: Offer 1-2 review-level insights that transcend individual papers. Highlight cross-cutting patterns, tensions, or opportunities.

## Quality Requirements
- **CRITICAL: Do NOT simply recapitulate or restate the body text.** The conclusion must ABSTRACT, COMPARE, and JUDGE at the review level — synthesize across papers, contrast competing approaches, and render a judgment. A paragraph that mostly repeats what the body already said is a failure.
- **CRITICAL: Reference specific paper IDs from the available list below.** Only cite papers that actually appear in the review; do NOT invent paper IDs.
- **CRITICAL: Use a diversity of papers.** Spread references across paragraphs; do not lean on a single paper.
- Use review-level judgment language: "while...", "in contrast...", "collectively...", "however..."
- Each paragraph should be 150-300 words.
- Connect organically to the body text — do not introduce new topics not discussed in the review structure.
- Avoid vague future outlooks ("further studies are required", "more research is needed").
- Base conclusions on the provided evidence, not on general knowledge.

## Context Data

### Review Topic
{topic}

### Available Papers ({paper_count} papers reviewed)
Reference ONLY paper IDs from this list: {paper_ids_hint}

### Key Section Claims
{claims_text if claims_text else '(No structured claims available)'}

### Aggregated Limitations
{limitations_text if limitations_text else '(No limitations identified)'}

### Methodology Trends
- Dominant reaction types: {top_reactions or 'N/A'}
- Dominant catalyst/methods: {top_catalysts or 'N/A'}
- Dominant substrate classes: {top_substrates or 'N/A'}
- Year range: {trends.get('year_range', 'N/A')}
- Papers reviewed: {paper_count}

{structure_block}

## Output Format
Return the conclusion as a JSON object with this structure:
{{
    "paragraphs": [
        {{
            "index": 1,
            "type": "conclusion|challenges|insights",
            "content": "The paragraph text...",
            "referenced_papers": ["P001", "P003"]
        }}
    ],
    "total_words": 0,
    "quality_notes": ["Any self-assessment notes about the generated text"]
}}"""
    return prompt


# ── LLM API call ─────────────────────────────────────────────────────────────

def call_llm(prompt: str, api_key: str, base_url: str, model: str) -> str:
    """Call the LLM API and return the generated text."""
    payload = {
        "model": model,
        "input": [
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "conclusion_generation",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["paragraphs", "total_words", "quality_notes"],
                    "properties": {
                        "paragraphs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["index", "type", "content", "referenced_papers"],
                                "properties": {
                                    "index": {"type": "integer"},
                                    "type": {"type": "string", "enum": ["conclusion", "challenges", "insights"]},
                                    "content": {"type": "string"},
                                    "referenced_papers": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "total_words": {"type": "integer"},
                        "quality_notes": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "strict": True,
            }
        },
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Browser-like UA: some relays (e.g. naiccc.com) sit behind
            # Cloudflare and block non-browser User-Agents with 403 (error 1010).
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text = data.get("output_text")
    if not text:
        parts: list[str] = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    parts.append(content["text"])
        text = "\n".join(parts)
    if not text:
        raise RuntimeError("LLM response did not contain output_text")
    return text


# ── Quality validation ───────────────────────────────────────────────────────

def _word_shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    """Return the set of k-word shingles (n-grams) from text."""
    words = re.findall(r"\b\w+\b", text.lower())
    if len(words) < k:
        return set()
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def validate_conclusion(
    result: dict[str, Any],
    draft_text: str,
    available_paper_ids: list[str] | None = None,
    paper_to_callout: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the generated conclusion against quality criteria.

    Checks cover the requirement's quality bars: not a simple recap
    (n-gram overlap with the body), connected to the body (cited papers are
    real), reference diversity across paragraphs, plus the existing vague-
    phrase / count / word-count checks.
    """
    issues: list[str] = []
    warnings: list[str] = []

    raw_paragraphs = result.get("paragraphs") or []
    if not isinstance(raw_paragraphs, list):
        issues.append("no_paragraphs: result missing paragraphs array")
        raw_paragraphs = []
    paragraphs = [
        paragraph
        for paragraph in raw_paragraphs
        if isinstance(paragraph, dict)
        and str(paragraph.get("content") or "").strip()
    ]

    # Check paragraph count
    if len(paragraphs) < 2:
        issues.append("too_few_paragraphs: expected 2-3 paragraphs")
    if len(paragraphs) > 3:
        issues.append("too_many_paragraphs: expected 2-3 paragraphs")

    # Check for vague language
    vague_phrases = [
        "more research is needed",
        "further studies are required",
        "future work should",
        "remains to be explored",
        "more work is needed",
    ]
    for para in paragraphs:
        if isinstance(para, dict):
            content = para.get("content", "")
            for phrase in vague_phrases:
                if phrase in content.lower():
                    warnings.append(f"vague_phrase_detected: '{phrase}' in paragraph {para.get('index', '?')}")

    # Collect references per paragraph
    all_refs: list[str] = []
    refs_per_para: list[set[str]] = []
    for para in paragraphs:
        if isinstance(para, dict):
            refs = para.get("referenced_papers") or []
            if isinstance(refs, list):
                cleaned = {str(r).strip() for r in refs if str(r).strip()}
                refs_per_para.append(cleaned)
                all_refs.extend(cleaned)
            else:
                refs_per_para.append(set())
    if not all_refs:
        issues.append("no_paper_references: conclusion does not reference any specific paper IDs")

    if paper_to_callout is not None:
        unknown = sorted({paper_id for paper_id in all_refs if paper_id not in paper_to_callout})
        if unknown:
            issues.append(f"unknown_conclusion_paper_ids: {', '.join(unknown)}")

    for paragraph_number, para in enumerate(paragraphs, start=1):
        if isinstance(para, dict) and re.search(r"P\d+", str(para.get("content") or "")):
            issues.append(f"raw_paper_id_in_content: paragraph {paragraph_number}")
        if isinstance(para, dict) and _NUMERIC_CITATION_RE.search(
            str(para.get("content") or "")
        ):
            issues.append(f"numeric_citation_in_content: paragraph {paragraph_number}")

    # Reference diversity: warn if every paragraph cites the same single paper,
    # or if the unique-paper count is below a small threshold for 2+ paragraphs.
    if len(paragraphs) >= 2 and refs_per_para:
        unique_all = set().union(*refs_per_para)
        if len(unique_all) == 1:
            warnings.append("low_reference_diversity: all paragraphs cite only one paper")
        elif len(unique_all) < len(paragraphs):
            warnings.append(f"low_reference_diversity: only {len(unique_all)} unique papers "
                            f"across {len(paragraphs)} paragraphs")

    # Body-connection: cited papers should be real (appear in available_paper_ids
    # or in the draft text). Warn on invented IDs.
    available = set(available_paper_ids or [])
    if available:
        invented = {r for r in all_refs if r not in available}
        if invented:
            warnings.append(f"cited_paper_not_in_review: {sorted(invented)[:10]} "
                            "not found among reviewed papers")

    # Recap detection: high n-gram overlap with the body text means the
    # conclusion is restating rather than abstracting/judging.
    draft_shingles = _word_shingles(draft_text, 5) if draft_text else set()
    if draft_shingles:
        for para in paragraphs:
            if not isinstance(para, dict):
                continue
            content = para.get("content", "")
            p_shingles = _word_shingles(content, 5)
            if not p_shingles:
                continue
            overlap = len(p_shingles & draft_shingles) / len(p_shingles)
            if overlap >= 0.40:
                warnings.append(
                    f"possible_recap: paragraph {para.get('index','?')} has "
                    f"{int(overlap*100)}% 5-gram overlap with body text "
                    "(should abstract/judge, not restate)"
                )

    # Check word count
    total_words = sum(
        len(re.findall(r"\b\w+\b", str(paragraph.get("content") or "")))
        for paragraph in paragraphs
    )
    if total_words < 200:
        issues.append(f"too_short: {total_words} words (minimum 200 recommended)")
    if total_words > 1200:
        warnings.append(f"too_long: {total_words} words (maximum 900 recommended)")

    return {
        "issues": issues,
        "warnings": warnings,
        "referenced_papers": all_refs,
        "paragraph_count": len(paragraphs),
        "total_words": total_words,
        "unique_paper_count": len(set(all_refs)),
        "passes_validation": len(issues) == 0,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    _load_dotenv_if_present(review_root)
    project = review_root / "review-projects" / args.project_id

    if not project.exists():
        print(f"ERROR: Project not found: {project}", file=__import__("sys").stderr)
        return 2

    # Collect all input sources
    matrix_path = project / "01_matrix_outline" / "literature_matrix.json"
    blueprint_path = project / "01_matrix_outline" / "section_blueprint.json"
    notes_path = project / "01_matrix_outline" / "paper_reading_notes.json"
    draft_path = project / "04_first_draft" / "first_draft.md"
    citations_path = project / "04_first_draft" / "citations.json"
    final_draft_path = project / "05_final_audit" / "final_draft.md"
    section_drafts_path = project / "02_section_drafting" / "section_drafts.md"

    mode = getattr(args, "mode", "orchestrated")
    if mode == "orchestrated":
        if not draft_path.exists():
            print(
                f"ERROR: Orchestrated draft not found: {draft_path}",
                file=__import__("sys").stderr,
            )
            return 2
        draft_text = read_text(draft_path)
    else:
        draft_text = ""
        for candidate in (final_draft_path, draft_path, section_drafts_path):
            candidate_text = read_text(candidate)
            if candidate_text:
                draft_text = candidate_text
                break
        if not draft_text:
            print("WARN: No draft text found in standalone fallback locations.")

    matrix = read_json(matrix_path) if matrix_path.exists() else {}
    blueprint = read_json(blueprint_path) if blueprint_path.exists() else {}
    notes = read_json(notes_path) if notes_path.exists() else {}
    paper_to_callout = load_paper_to_callout(citations_path) if citations_path.exists() else {}

    # Collect limitations, trends, claims
    limitations = collect_limitations(matrix, notes)
    trends = identify_trends(matrix)
    claims = collect_section_claims(blueprint)

    # Parse the full review structure so the conclusion is grounded in the
    # actual section hierarchy (not a truncated 8000-char excerpt).
    section_summaries = parse_review_structure(draft_text)

    # Collect the paper IDs actually available in the project so the LLM only
    # cites real papers.
    available_paper_ids = collect_available_paper_ids(matrix, claims)
    if paper_to_callout:
        available_paper_ids = [
            paper_id
            for paper_id in available_paper_ids
            if paper_id in paper_to_callout
        ]

    # Infer topic
    topic = blueprint.get("review_topic") or ""
    if not topic:
        # some blueprints store the topic per-section or under a different key
        topic = blueprint.get("topic") or blueprint.get("review_question") or ""
    if not topic:
        topic_input = project / "00_discovery" / "topic_input.md"
        if topic_input.exists():
            for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("# "):
                    topic = line.strip()[2:]
                    break
    if not topic and section_summaries:
        topic = section_summaries[0].get("heading", "this review")

    # Build context
    context = {
        "draft_text": draft_text,
        "claims": claims,
        "limitations": limitations,
        "trends": trends,
        "topic": topic or "this review",
        "section_summaries": section_summaries,
        "available_paper_ids": available_paper_ids,
    }

    # API key resolution
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    model = args.model or os.environ.get("REVIEW_CONCLUSION_MODEL", "gpt-5.4")

    if not api_key:
        out_dir = project / "04_first_draft"
        _invalidate_required_outputs(out_dir)
        print("WARN: No API key found. Writing context bundle and prompt for manual use.")
        print("Set OPENAI_API_KEY or pass --api-key to generate conclusion via LLM.")
        write_json(out_dir / "conclusion_context.json", context)
        prompt = build_conclusion_prompt(context)
        write_text(out_dir / "conclusion_prompt.txt", prompt)
        print(f"Wrote context: {out_dir / 'conclusion_context.json'}")
        print(f"Wrote prompt: {out_dir / 'conclusion_prompt.txt'}")
        print("Ready for manual LLM submission.")
        return 0

    # Build prompt and call LLM
    prompt = build_conclusion_prompt(context)
    print(f"Calling LLM ({model})...")

    try:
        raw_response = call_llm(prompt, api_key, base_url, model)
        result = json.loads(raw_response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        out_dir = project / "04_first_draft"
        _invalidate_required_outputs(out_dir)
        print(f"ERROR: LLM call failed: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        # Save context for manual use
        write_json(out_dir / "conclusion_context.json", context)
        write_text(out_dir / "conclusion_prompt.txt", prompt)
        print(f"Saved context and prompt for manual submission in {out_dir}")
        return 1

    # Validate
    validation = validate_conclusion(
        result,
        draft_text,
        available_paper_ids,
        paper_to_callout or None,
    )

    # Write outputs
    out_dir = project / "04_first_draft"

    # Write manuscript-only Markdown; provenance remains in the quality report.
    conclusion_md = render_conclusion_markdown(result, paper_to_callout)
    write_text(out_dir / "conclusion_generated.md", conclusion_md)

    # Write the structured result with validation
    output = {
        "project_id": args.project_id,
        "topic": topic,
        "model": model,
        "created_at": utc_now(),
        "paragraphs": result.get("paragraphs", []),
        "total_words": validation["total_words"],
        "quality_notes": result.get("quality_notes", []),
        "validation": validation,
        "context_summary": {
            "claims_count": len(claims),
            "limitations_count": len(limitations),
            "section_count": len(section_summaries),
            "available_paper_count": len(available_paper_ids),
            "citation_map_size": len(paper_to_callout),
            "trends": trends,
        },
    }
    write_json(out_dir / "conclusion_quality_report.json", output)

    # Print summary
    print(f"\nConclusion generated for {args.project_id}:")
    print(f"  Paragraphs: {validation['paragraph_count']}")
    print(f"  Total words: {validation['total_words']}")
    print(f"  Validation: {'PASS' if validation['passes_validation'] else 'ISSUES'}")
    if validation["issues"]:
        print("  Issues:")
        for issue in validation["issues"]:
            print(f"    - {issue}")
    if validation["warnings"]:
        print("  Warnings:")
        for w in validation["warnings"]:
            print(f"    - {w}")
    print(f"  Output: {out_dir / 'conclusion_generated.md'}")
    print(f"  Report: {out_dir / 'conclusion_quality_report.json'}")
    return 0 if validation["passes_validation"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate high-quality conclusion/challenges/insights for a review project."
    )
    parser.add_argument("--review-root", default=".",
                        help="Review project root (contains review-projects/). Default: cwd.")
    parser.add_argument("--project-id", required=True, help="Project ID (directory name under review-projects/)")
    parser.add_argument(
        "--mode",
        choices=("orchestrated", "standalone"),
        default="orchestrated",
        help="Draft selection mode. Default: orchestrated.",
    )
    parser.add_argument("--api-key", default="",
                        help="API key (or set OPENAI_API_KEY env var).")
    parser.add_argument("--base-url", default="",
                        help="API base URL (or set OPENAI_BASE_URL env var). "
                             "Default: https://api.openai.com")
    parser.add_argument("--model", default="",
                        help="Model name (or set REVIEW_CONCLUSION_MODEL env var). "
                             "Default: gpt-5.4")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
