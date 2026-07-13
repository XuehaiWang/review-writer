#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STOPWORDS = {
    "and",
    "the",
    "from",
    "with",
    "for",
    "into",
    "via",
    "section",
    "introduction",
    "conclusion",
    "outlook",
    "review",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in re.findall(r"[A-Za-z][A-Za-z0-9'′-]{2,}", text or "")
        if t.lower() not in STOPWORDS
    }


def value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(value_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(value_text(v) for v in value.values())
    return str(value)


def paper_value(paper: dict[str, Any], key: str) -> str:
    aliases = {
        # legacy field names (product/substrate/catalyst_or_method/reaction_type)
        # are kept as fallback aliases for compatibility with older matrices.
        "output": ["output", "output_class", "product", "product_class"],
        "input": ["input", "input_class", "substrate", "substrate_class"],
        "method": ["method", "method_logic", "catalyst_or_method", "catalyst_logic", "process_mode", "activation_mode"],
        "process_type": ["process_type", "reaction_type", "process_mode"],
        "limitation": ["limitation", "main_limitation"],
        "selectivity": ["selectivity", "selectivity_mode"],
    }
    for candidate in aliases.get(key, [key]):
        value = paper.get(candidate)
        if value:
            return str(value)
    structured = paper.get("structured_tags")
    if isinstance(structured, dict):
        value = structured.get(key)
        if value:
            return str(value)
    return ""


def parse_outline_sections(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    in_outline = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^#{1,3}\s+Outline\b", line, flags=re.I):
            in_outline = True
            continue
        if in_outline and re.match(r"^#{1,3}\s+", line):
            break
        match = re.match(r"^(?:[-*]\s*)?(\d+)[.)]\s+(.+?)\s*$", line)
        if match:
            title = match.group(2).strip()
            if title:
                sections.append({"section_id": f"sec{len(sections) + 1}", "title": title})
    if sections:
        return sections

    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^(?:[-*]\s*)?(\d+)[.)]\s+(.+?)\s*$", line)
        if match:
            title = match.group(2).strip()
            sections.append({"section_id": f"sec{len(sections) + 1}", "title": title})
    return sections


def parse_explicit_paper_assignments(text: str) -> dict[str, list[str]]:
    """Read explicit `Papers: P001, P002, ...` lines from an outline's Section Details.

    `selected_outline.md` commonly names, per section, exactly which papers belong
    there (see review-literature-matrix-outline's outline_options.md format). Without
    this, `select_papers()`'s keyword-overlap scoring is the only signal available,
    and it degrades badly once many papers share tightly clustered vocabulary (e.g.
    a whole review on one narrow reaction class) -- it scatters papers across
    sections almost arbitrarily. When the outline already states the assignment
    explicitly, honor it directly instead of re-deriving a worse guess.

    Returns a mapping of normalized section title -> ordered list of paper_ids.
    A line is associated with the nearest preceding section heading, matched by
    a line starting with `#`, `##`, `###`, or a plain `N. Title` / `N) Title` line.
    """
    assignments: dict[str, list[str]] = {}
    current_title: str | None = None
    heading_re = re.compile(r"^#{1,6}\s+(?:\d+[.)]\s+)?(.+?)\s*$")
    numbered_re = re.compile(r"^(?:[-*]\s*)?\d+[.)]\s+(.+?)\s*$")
    papers_line_re = re.compile(r"^Papers:\s*(.+)$", re.I)
    paper_id_re = re.compile(r"\bP\d{2,}\b")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading_match = heading_re.match(line)
        numbered_match = numbered_re.match(line) if not heading_match else None
        if heading_match:
            current_title = heading_match.group(1).strip().lower()
            continue
        if numbered_match:
            current_title = numbered_match.group(1).strip().lower()
            continue
        papers_match = papers_line_re.match(line)
        if papers_match and current_title:
            ids = paper_id_re.findall(papers_match.group(1))
            if ids:
                assignments[current_title] = ids
    return assignments


def load_matrix(path: Path) -> tuple[str, list[dict[str, Any]], list[str]]:
    data = read_json(path)
    if isinstance(data, dict):
        topic = str(data.get("review_topic") or data.get("topic") or "")
        papers = data.get("papers") if isinstance(data.get("papers"), list) else []
        axes = data.get("comparison_axes") if isinstance(data.get("comparison_axes"), list) else []
        return topic, [p for p in papers if isinstance(p, dict)], [str(a) for a in axes]
    if isinstance(data, list):
        return "", [p for p in data if isinstance(p, dict)], []
    return "", [], []


def load_notes(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = read_json(path)
    if not isinstance(data, list):
        return {}
    return {str(row.get("paper_id")): row for row in data if isinstance(row, dict) and row.get("paper_id")}


def select_rule_pack(skill_root: Path, topic: str) -> tuple[str, str]:
    manifest_path = skill_root / "references" / "rule_packs.json"
    try:
        manifest = read_json(manifest_path)
    except Exception:
        return "", ""
    default = str(manifest.get("default_rule_pack") or "")
    packs = manifest.get("rule_packs") if isinstance(manifest, dict) else {}
    if not isinstance(packs, dict):
        return "", ""
    topic_low = (topic or "").lower()
    for name, cfg in packs.items():
        if not isinstance(cfg, dict):
            continue
        signals = cfg.get("topic_signals")
        if isinstance(signals, list) and any(str(signal).lower() in topic_low for signal in signals):
            return str(name), str(cfg.get("path") or f"references/rule_packs/{name}")
    cfg = packs.get(default)
    if isinstance(cfg, dict):
        return default, str(cfg.get("path") or f"references/rule_packs/{default}")
    # No topic-signal match and no valid default pack configured: proceed with
    # no rule pack rather than silently forcing an unrelated one's writing rules.
    return "", ""


def paper_blob(paper: dict[str, Any], note: dict[str, Any] | None) -> str:
    fields = [
        "title",
        "input",
        "process_type",
        "output",
        "method",
        "selectivity",
        "limitation",
        "role_after_reading",
        "review_topic_relevance",
    ]
    blob = " ".join(paper_value(paper, k) or value_text(paper.get(k)) for k in fields)
    if note:
        blob += " " + value_text(note.get("why_relevant"))
        blob += " " + value_text(note.get("key_evidence"))
        blob += " " + value_text(note.get("limitations"))
    return blob


def score_paper(section_title: str, paper: dict[str, Any], note: dict[str, Any] | None) -> int:
    section_tokens = tokens(section_title)
    blob_tokens = tokens(paper_blob(paper, note))
    score = len(section_tokens & blob_tokens) * 3
    note = note or {}
    relevance = str(paper.get("review_topic_relevance") or note.get("review_topic_relevance") or "").lower()
    role = str(paper.get("role_after_reading") or note.get("role_after_reading") or "").lower()
    if relevance == "high":
        score += 2
    if role == "core":
        score += 2
    if role == "supporting":
        score += 1
    return score


def select_papers(
    section_title: str,
    papers: list[dict[str, Any]],
    notes: dict[str, dict[str, Any]],
    explicit_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    if explicit_ids:
        by_id = {str(p.get("paper_id")): p for p in papers if p.get("paper_id")}
        explicit_selected = [by_id[pid] for pid in explicit_ids if pid in by_id]
        if explicit_selected:
            return explicit_selected
    scored = []
    for paper in papers:
        pid = str(paper.get("paper_id") or "")
        if not pid:
            continue
        score = score_paper(section_title, paper, notes.get(pid))
        if score > 0:
            scored.append((score, pid, paper))
    scored.sort(key=lambda row: (-row[0], row[1]))
    selected = [paper for _, _, paper in scored[:8]]
    if not selected:
        def is_high_value(p: dict[str, Any]) -> bool:
            note = notes.get(str(p.get("paper_id") or "")) or {}
            relevance = str(p.get("review_topic_relevance") or note.get("review_topic_relevance") or "").lower()
            role = str(p.get("role_after_reading") or note.get("role_after_reading") or "").lower()
            return relevance == "high" or role == "core"

        selected = [p for p in papers if is_high_value(p)][:6]
    return selected


def infer_logic(title: str) -> str:
    low = title.lower()
    if any(w in low for w in ["radical", "photoredox", "cross-electrophile", "reductive"]):
        return "mechanistic_pathway"
    if any(w in low for w in ["carbonate", "ester", "alcohol", "bromide", "phosphate", "sulfide", "derivative"]):
        return "precursor_class"
    if any(w in low for w in ["stereo", "enantio", "chiral", "chirality", "selectivity"]):
        return "stereochemical_control"
    if any(w in low for w in ["application", "target", "useful"]):
        return "application"
    if any(w in low for w in ["outlook", "challenge", "conclusion"]):
        return "outlook"
    return "general_process"


def target_depth(title: str, selected_count: int) -> tuple[int, str]:
    low = title.lower()
    if any(word in low for word in ["introduction", "background"]):
        return 3, "500-800"
    if any(word in low for word in ["conclusion", "outlook", "future"]):
        return 3, "500-900"
    if selected_count >= 6:
        return 5, "1000-1500"
    return 4, "800-1200"


def infer_claim_type(title: str, index: int) -> str:
    low = title.lower()
    if index == 0 and any(w in low for w in ["foundational", "classical", "introduction"]):
        return "foundation"
    if any(w in low for w in ["mechanism", "radical", "photoredox"]):
        return "mechanism"
    if any(w in low for w in ["scope", "functionalized", "classes"]):
        return "scope"
    if any(w in low for w in ["challenge", "outlook", "limitation"]):
        return "limitation"
    return ["foundation", "extension", "contrast", "limitation"][min(index, 3)]


def common_values(papers: list[dict[str, Any]], key: str, limit: int = 3) -> list[str]:
    values: list[str] = []
    for paper in papers:
        raw = paper_value(paper, key).strip()
        if raw:
            values.append(raw)
    counts = Counter(values)
    return [value for value, _ in counts.most_common(limit)]


def join_values(values: list[str], fallback: str) -> str:
    if not values:
        return fallback
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f", and {values[-1]}"


def section_thesis(title: str, selected: list[dict[str, Any]], dominant_logic: str) -> str:
    # Cap at 2 values per axis so the thesis stays one readable sentence even
    # when many papers are assigned to the section; this is a draft skeleton,
    # not final prose, but it should still be legible before the LLM rewrites it.
    substrates = join_values(common_values(selected, "input", 2), "the assigned input classes")
    activations = join_values(common_values(selected, "process_type", 2), "the assigned methods or approaches")
    products = join_values(common_values(selected, "output", 2), "the target output classes")
    low = title.lower()
    if "introduction" in low:
        return f"Frame the review around how {substrates} lead to {products}, emphasizing why method choice and input variation define the field."
    if "outlook" in low or "challenge" in low or "conclusion" in low:
        return f"Synthesize the remaining limits across {substrates}, especially where {activations} still leave gaps in scope, reliability, or practicality."
    if dominant_logic == "mechanistic_pathway":
        return f"Compare how {activations} shape the {products} obtained from {substrates}, while separating well-evidenced explanations from proposed ones."
    if dominant_logic == "stereochemical_control":
        return f"Use the assigned papers to distinguish the control strategies at work, and where selectivity or control breaks down in producing {products}."
    if dominant_logic == "precursor_class":
        return f"Show how {substrates} function as distinct starting points rather than interchangeable variants, with {activations} setting the main comparison axis."
    return f"Explain how {activations} convert {substrates} into {products}, and define the scope and limitation boundaries that matter for this section."


def review_problem(title: str, selected: list[dict[str, Any]], dominant_logic: str) -> str:
    axes = {
        "mechanistic_pathway": "Which underlying pathway or mechanism changes the observed outcome, and how strong is the evidence for it?",
        "stereochemical_control": "Which control strategy is operating, and where does the method lose fidelity or generality?",
        "precursor_class": "What does this input class enable that closely related alternatives do not, and what boundary remains?",
        "application": "What practical value is demonstrated beyond method development?",
        "outlook": "Which limitations are common across the assigned methods, and which are specific to one approach or class?",
    }
    return axes.get(dominant_logic, "Which method, input class, or output class best explains the papers grouped in this section?")


def normalize_role(raw: str) -> str:
    low = (raw or "").lower()
    if "core" in low:
        return "strategic extension"
    if "support" in low:
        return "comparison source"
    if "background" in low:
        return "foundational method"
    return "comparison source"


def claim_from_papers(section_id: str, title: str, idx: int, papers: list[dict[str, Any]], axes: list[str]) -> dict[str, Any]:
    claim_type = infer_claim_type(title, idx)
    paper_refs = []
    for paper in papers[:4]:
        pid = str(paper.get("paper_id"))
        use_for = [
            k.replace("_", " ")
            for k in ["input", "process_type", "output", "selectivity", "limitation"]
            if paper_value(paper, k)
        ][:3]
        caveat = paper_value(paper, "limitation")
        paper_refs.append(
            {
                "paper_id": pid,
                "role": normalize_role(str(paper.get("role_after_reading") or "")),
                "use_for": use_for,
                "caveat": caveat,
            }
        )
    axis_values = [a.replace("_", " ") for a in axes[:3]] or ["input class", "method", "scope boundary"]
    substrates = join_values(common_values(papers, "input", 2), "the assigned input classes")
    activations = join_values(common_values(papers, "process_type", 2), "the assigned methods or approaches")
    products = join_values(common_values(papers, "output", 2), "the assigned output classes")
    limitations = common_values(papers, "limitation", 2)
    limitation_text = join_values(limitations, "the stated input and condition boundaries")
    selectivity = join_values(common_values(papers, "selectivity", 2), "the reported selectivity or control pattern")
    if claim_type == "foundation":
        claim = f"Establish {activations} of {substrates} as the baseline logic for producing {products}, while naming the key limitation that makes the section review-relevant."
    elif claim_type == "extension":
        claim = f"Show how the assigned papers extend the baseline toward {products}, especially through changes in input class, method logic, or an added component."
    elif claim_type == "contrast":
        claim = f"Contrast {activations} by how they control {selectivity}, rather than treating the papers as equivalent approaches."
    elif claim_type == "limitation":
        claim = f"Qualify the section's apparent generality by preserving the main boundaries: {limitation_text}."
    elif claim_type == "mechanism":
        claim = f"Separate well-evidenced claims from proposed rationales when discussing {activations} and their conversion of {substrates} to {products}."
    elif claim_type == "scope":
        claim = f"Compress scope around input and output classes: {substrates} leading to {products}, with boundaries stated explicitly."
    else:
        claim = f"Use the assigned papers to develop a bounded review claim about {title}, with explicit scope and limitation boundaries."
    return {
        "claim_id": f"{section_id}_c{idx + 1}",
        "claim": claim,
        "claim_type": claim_type,
        "supporting_papers": paper_refs,
        "logic_relationship": {
            "foundation": "foundation_to_extension",
            "extension": "limitation_repair",
            "contrast": "contrast",
            "limitation": "scope_boundary",
            "mechanism": "mechanistic_partition",
            "scope": "scope_boundary",
        }.get(claim_type, "contrast"),
        "comparison_axes": axis_values,
        "evidence_strength": "needs verification",
        "wording_constraints": [
            "Name the substrate or product class when making a scope claim.",
            "State proposed mechanisms as proposed unless the assigned paper reports direct evidence.",
            "Avoid one-paper-one-paragraph narration.",
        ],
    }


def build_section(
    section: dict[str, str],
    papers: list[dict[str, Any]],
    axes: list[str],
    notes: dict[str, dict[str, Any]],
    prev_title: str,
    next_title: str,
    explicit_ids: list[str] | None = None,
) -> dict[str, Any]:
    title = section["title"]
    selected = select_papers(title, papers, notes, explicit_ids)
    paper_ids = [str(p.get("paper_id")) for p in selected if p.get("paper_id")]
    claim_count = 2 if title.lower() in {"introduction", "conclusion"} else min(4, max(2, len(selected) // 2 or 2))
    claims = []
    for idx in range(claim_count):
        claim_papers = selected[idx * 2 : idx * 2 + 4] or selected[:4]
        claims.append(claim_from_papers(section["section_id"], title, idx, claim_papers, axes))
    dominant_logic = infer_logic(title)
    target_paragraphs, target_words = target_depth(title, len(selected))
    return {
        "section_id": section["section_id"],
        "title": title,
        "section_thesis": section_thesis(title, selected, dominant_logic),
        "review_problem": review_problem(title, selected, dominant_logic),
        "target_paragraphs": target_paragraphs,
        "target_words": target_words,
        "dominant_logic": dominant_logic,
        "major_papers": paper_ids,
        "review_claims": claims,
        "figure_or_table_needs": [
            {
                "type": "figure" if infer_logic(title) != "outlook" else "comparison table",
                "purpose": "Show the core method/process logic, representative input/output classes, or comparison axis that anchors this section.",
                "candidate_papers": paper_ids[:3],
            }
        ],
        "depth_requirements": [
            "Draft fully developed review prose, not a compact example or annotated bibliography.",
            "Use the approved matrix as a guide, but reopen Markdown/PDF evidence for section-level details.",
            "Each substantive paragraph should contain a claim, source-grounded technical detail, and a review-level interpretation.",
        ],
        "section_transition": {
            "from_previous": f"Connect from {prev_title}." if prev_title else "Open the review scope and organizing logic.",
            "to_next": f"Set up {next_title}." if next_title else "Close with unresolved limitations and future directions.",
        },
        "avoid_patterns": [
            "Do not summarize papers in chronological order unless chronology is the section logic.",
            "Do not collapse distinct activation modes into generic substitution language.",
            "Do not use broad/generic scope adjectives without substrate boundaries.",
        ],
    }


def write_plan(path: Path, blueprint: dict[str, Any]) -> None:
    lines = [
        "# Section Writing Plan",
        "",
        f"- Project ID: `{blueprint['project_id']}`",
        f"- Review topic: {blueprint.get('review_topic') or ''}",
        f"- Rule pack: `{blueprint.get('rule_pack')}` ({blueprint.get('rule_pack_path')})",
        f"- Created at: {blueprint.get('created_at')}",
        "",
    ]
    for section in blueprint["sections"]:
        lines.extend(
            [
                f"## {section['section_id']}. {section['title']}",
                "",
                f"Thesis: {section['section_thesis']}",
                "",
                f"Major papers: {', '.join(section['major_papers']) or 'TBD'}",
                "",
                "Claims:",
            ]
        )
        for claim in section["review_claims"]:
            papers = ", ".join(p["paper_id"] for p in claim["supporting_papers"])
            lines.append(f"- `{claim['claim_id']}` {claim['claim']} Papers: {papers or 'TBD'}")
        lines.extend(["", f"Figure/table need: {section['figure_or_table_needs'][0]['type']} - {section['figure_or_table_needs'][0]['purpose']}", ""])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    skill_root = Path(__file__).resolve().parents[1]
    if args.stage_dir:
        stage_dir = Path(args.stage_dir).resolve()
        out_dir = stage_dir
    else:
        project_dir = review_root / "review-projects" / args.project_id
        stage_dir = project_dir / "01_matrix_outline"
        out_dir = project_dir / "02_section_blueprint"
    out_dir.mkdir(parents=True, exist_ok=True)
    selected_outline = stage_dir / "selected_outline.md"
    matrix_path = stage_dir / "literature_matrix.json"
    notes_path = stage_dir / "paper_reading_notes.json"
    if not selected_outline.exists():
        raise SystemExit(f"selected_outline.md not found: {selected_outline}")
    if not matrix_path.exists():
        raise SystemExit(f"literature_matrix.json not found: {matrix_path}")

    outline_text = read_text(selected_outline)
    sections = parse_outline_sections(outline_text)
    if not sections:
        raise SystemExit("No numbered outline sections found in selected_outline.md")

    topic, papers, axes = load_matrix(matrix_path)
    rule_pack, rule_pack_path = select_rule_pack(skill_root, topic or outline_text)
    notes = load_notes(notes_path)
    explicit_assignments = parse_explicit_paper_assignments(outline_text)
    blueprint_sections = []
    for idx, section in enumerate(sections):
        prev_title = sections[idx - 1]["title"] if idx > 0 else ""
        next_title = sections[idx + 1]["title"] if idx + 1 < len(sections) else ""
        explicit_ids = explicit_assignments.get(section["title"].strip().lower())
        blueprint_sections.append(build_section(section, papers, axes, notes, prev_title, next_title, explicit_ids))

    blueprint = {
        "project_id": args.project_id,
        "review_topic": topic,
        "outline_source": str(selected_outline),
        "matrix_source": str(matrix_path),
        "rule_pack": rule_pack,
        "rule_pack_path": rule_pack_path,
        "created_at": utc_now(),
        "status": "draft_initialization_needs_semantic_review",
        "sections": blueprint_sections,
    }
    out_json = out_dir / "section_blueprint.json"
    out_md = out_dir / "section_writing_plan.md"
    write_json(out_json, blueprint)
    write_plan(out_md, blueprint)
    print(f"Wrote {out_json}")
    print(f"Wrote {out_md}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize section_blueprint.json from selected outline and literature matrix.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", default="")
    parser.add_argument("--stage-dir", default="", help="Override stage folder directly (skips review-root/project-id resolution)")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
