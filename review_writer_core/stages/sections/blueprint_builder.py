"""Pure Section Blueprint construction shared by Web and Skill entry points."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from review_writer_core.classification_axes import canonical_classification_contract
from review_writer_core.review_structure import infer_section_role
from review_writer_core.section_narrative_contracts import (
    apply_single_paper_policy,
    derive_scientific_thesis,
    derive_section_depth_contract,
)


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
    "synthesis",
    "review",
    "chemistry",
}


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
        "product": ["product", "product_class"],
        "substrate": ["substrate", "substrate_class"],
        "catalyst_or_method": ["catalyst_or_method", "catalyst_logic", "activation_mode"],
        "reaction_type": ["reaction_type", "activation_mode"],
        "method": ["method", "approach", "catalyst_or_method", "reaction_type", "activation_mode"],
        "finding": ["main_finding", "key_findings", "contribution", "product", "product_class"],
        "evidence": ["key_evidence", "evidence", "abstract", "summary"],
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


def parse_outline_sections(text: str) -> list[dict[str, Any]]:
    """Extract major Markdown sections and their explicit Matrix paper assignments."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^(?:#{1,6}\s+)?(?:[-*]\s*)?(\d+)[.)]\s+(.+?)\s*$", line)
        if match:
            title = match.group(2).strip()
            if title:
                current = {
                    "section_id": f"sec{len(sections) + 1}",
                    "title": title,
                    "section_role": infer_section_role(title),
                    "assigned_papers": [],
                }
                sections.append(current)
            continue
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            title = match.group(1).strip()
            if title:
                current = {
                    "section_id": f"sec{len(sections) + 1}",
                    "title": title,
                    "section_role": infer_section_role(title),
                    "assigned_papers": [],
                }
                sections.append(current)
            continue
        if current and line.lower().startswith("section role:"):
            current["section_role"] = infer_section_role(
                current.get("title"), line.split(":", 1)[1].strip()
            )
            continue
        if current and line.lower().startswith("assigned papers:"):
            current["assigned_papers"] = re.findall(r"\b[A-Za-z]+\d+\b", line)
    return sections


def paper_blob(paper: dict[str, Any], note: dict[str, Any] | None) -> str:
    fields = [
        "title",
        "substrate",
        "reaction_type",
        "product",
        "catalyst_or_method",
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
    relevance = str(paper.get("review_topic_relevance") or "").lower()
    role = str(paper.get("role_after_reading") or "").lower()
    if relevance == "high":
        score += 2
    if role == "core":
        score += 2
    if role == "supporting":
        score += 1
    return score


def select_papers(section_title: str, papers: list[dict[str, Any]], notes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
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
        selected = [
            p
            for p in papers
            if str(p.get("review_topic_relevance") or "").lower() == "high"
            or str(p.get("role_after_reading") or "").lower() == "core"
        ][:6]
    if not selected:
        selected = [paper for paper in papers if paper.get("paper_id")][:6]
    return selected


def infer_logic(title: str) -> str:
    low = title.lower()
    if any(w in low for w in ["mechanism", "pathway", "architecture", "workflow", "framework"]):
        return "mechanism_or_method"
    if any(w in low for w in ["class", "type", "taxonomy", "representation", "category"]):
        return "category_or_representation"
    if any(w in low for w in ["performance", "prediction", "evaluation", "benchmark", "comparison", "selectivity"]):
        return "comparative_performance"
    if any(w in low for w in ["application", "target", "useful"]):
        return "application"
    if any(w in low for w in ["outlook", "challenge", "conclusion", "limitation", "future"]):
        return "outlook"
    return "thematic_synthesis"


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
    if any(w in low for w in ["mechanism", "radical", "photoredox", "architecture", "framework", "workflow"]):
        return "mechanism"
    if any(w in low for w in ["scope", "functionalized", "classes", "taxonomy", "representation"]):
        return "scope"
    if any(w in low for w in ["performance", "prediction", "evaluation", "benchmark", "comparison"]):
        return "contrast"
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


def section_thesis(
    title: str,
    selected: list[dict[str, Any]],
    dominant_logic: str,
) -> str:
    low = title.lower()
    methods = join_values(common_values(selected, "method"), "the principal approaches in the assigned literature")
    findings = join_values(common_values(selected, "finding"), "the main findings reported by the assigned papers")
    limitations = join_values(common_values(selected, "limitation"), "their stated evidence and applicability limits")
    if "introduction" in low or "background" in low:
        return f"Define the review's scope and organizing question, then position {methods} as the evidence base for the sections that follow."
    if dominant_logic == "outlook":
        return f"Synthesize the unresolved limitations across {methods}, distinguishing shared gaps from method-specific constraints such as {limitations}."
    if dominant_logic == "comparative_performance":
        return f"Compare {methods} using explicit evidence and performance boundaries, connecting the comparison to {findings}."
    if dominant_logic == "category_or_representation":
        return f"Explain how the field is organized into meaningful categories or representations, and use {methods} to show why those distinctions affect {findings}."
    if dominant_logic == "mechanism_or_method":
        return f"Compare how {methods} operate, separating demonstrated mechanisms or design choices from interpretation and linking them to {findings}."
    return f"Synthesize how {methods} advance the section topic, while preserving the evidence boundaries and limitations behind {findings}."


def review_problem(
    title: str,
    selected: list[dict[str, Any]],
    dominant_logic: str,
) -> str:
    axes = {
        "mechanism_or_method": "How do the principal approaches differ in design or mechanism, and which differences are supported by evidence?",
        "category_or_representation": "Which classification or representation best explains meaningful differences across the assigned studies?",
        "comparative_performance": "Which evidence and evaluation boundaries support the reported performance differences?",
        "application": "What practical value is demonstrated beyond proof of concept, and under which conditions?",
        "outlook": "Which limitations recur across approaches, and which are specific to one method or evidence base?",
    }
    return axes.get(dominant_logic, "What shared question connects these papers, where do their findings agree or diverge, and what limits the resulting conclusion?")


def normalize_role(raw: str) -> str:
    low = (raw or "").lower()
    if "core" in low:
        return "strategic extension"
    if "support" in low:
        return "comparison source"
    if "background" in low:
        return "foundational method"
    return "comparison source"


def claim_from_papers(
    section_id: str,
    title: str,
    idx: int,
    papers: list[dict[str, Any]],
    axes: list[str],
) -> dict[str, Any]:
    claim_type = infer_claim_type(title, idx)
    paper_refs = []
    for paper in papers[:4]:
        pid = str(paper.get("paper_id"))
        evidence_keys = ["method", "finding", "evidence", "limitation"]
        use_for = [k.replace("_", " ") for k in evidence_keys if paper_value(paper, k)][:3]
        caveat = paper_value(paper, "limitation")
        paper_refs.append(
            {
                "paper_id": pid,
                "role": normalize_role(str(paper.get("role_after_reading") or "")),
                "use_for": use_for,
                "caveat": caveat,
            }
        )
    axis_values = [a.replace("_", " ") for a in axes[:3]] or [
        "approach", "evidence", "scope boundary"
    ]
    methods = join_values(common_values(papers, "method", 2), "the assigned approaches")
    findings = join_values(common_values(papers, "finding", 2), "the reported findings")
    limitations = join_values(common_values(papers, "limitation", 2), "the stated evidence and applicability limits")
    if claim_type == "foundation":
        claim = f"Establish {methods} as the section's baseline and define which evidence supports {findings}."
    elif claim_type == "extension":
        claim = f"Show how the assigned studies extend the baseline in method, scope, or evidence, and connect those extensions to {findings}."
    elif claim_type == "contrast":
        claim = f"Compare {methods} on explicit evidence and evaluation axes rather than treating their conclusions as interchangeable."
    elif claim_type == "limitation":
        claim = f"Qualify the section's apparent generality by preserving the main boundaries: {limitations}."
    elif claim_type == "mechanism":
        claim = f"Separate demonstrated method or mechanism claims from proposed interpretations when explaining {methods}."
    elif claim_type == "scope":
        claim = f"Define the evidence and applicability scope of {findings}, including where the assigned studies do not support broader claims."
    else:
        claim = f"Use the assigned papers to develop a bounded review claim about {title}, with explicit evidence and limitations."
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
            "Name the method, evidence type, or study scope when making a comparative claim.",
            "Distinguish demonstrated results from hypotheses or author interpretation.",
            "Avoid one-paper-one-paragraph narration.",
        ],
    }


def build_section(
    section: dict[str, Any],
    papers: list[dict[str, Any]],
    axes: list[str],
    notes: dict[str, dict[str, Any]],
    prev_title: str,
    next_title: str,
) -> dict[str, Any]:
    title = section["title"]
    role = infer_section_role(title, section.get("section_role"))
    primary_source = (
        section.get("primary_papers")
        if "primary_papers" in section
        else section.get("assigned_papers")
    )
    assigned_ids = [
        str(paper_id)
        for paper_id in primary_source or []
    ]
    supporting_ids = [
        str(paper_id) for paper_id in section.get("supporting_papers") or []
    ]
    papers_by_id = {str(paper.get("paper_id")): paper for paper in papers if paper.get("paper_id")}
    selected = [papers_by_id[paper_id] for paper_id in assigned_ids if paper_id in papers_by_id]
    supporting = [
        papers_by_id[paper_id]
        for paper_id in supporting_ids
        if paper_id in papers_by_id
    ]
    explicit_assignment = any(
        key in section for key in ("primary_papers", "assigned_papers", "supporting_papers")
    )
    if not explicit_assignment and not selected and not supporting and role == "body":
        selected = select_papers(title, papers, notes)
    evidence_papers = selected or supporting
    paper_ids = [str(p.get("paper_id")) for p in selected if p.get("paper_id")]
    claim_count = 2 if title.lower() in {"introduction", "conclusion"} else min(4, max(2, len(selected) // 2 or 2))
    claims = []
    for idx in range(claim_count):
        claim_papers = evidence_papers[idx * 2 : idx * 2 + 4] or evidence_papers[:4]
        claims.append(claim_from_papers(section["section_id"], title, idx, claim_papers, axes))
    dominant_logic = infer_logic(title)
    target_paragraphs, target_words = target_depth(title, len(selected))
    legacy_thesis = section_thesis(title, evidence_papers, dominant_logic)
    axis_contract = canonical_classification_contract(
        [
            {
                "axis_id": str(axis),
                "axis_role": (
                    "primary_organization" if index == 0 else "comparison_dimension"
                ),
            }
            for index, axis in enumerate(axes)
            if str(axis or "").strip()
        ],
        primary_axis_hint=str(axes[0] if axes else ""),
        source="legacy_blueprint_script",
    )
    thesis_contract = derive_scientific_thesis(
        {
            **section,
            "section_role": role,
            "purpose": legacy_thesis,
            "primary_papers": paper_ids,
            "supporting_papers": supporting_ids,
            "target_words": target_words,
        },
        papers_by_id,
        axis_contract,
    )
    if role == "body" and not (
        (thesis_contract.get("components") or {}).get("source_backed_paper_ids")
    ):
        thesis_contract["text"] = legacy_thesis
        thesis_contract["status"] = "provisional"
        thesis_contract["source"] = "legacy_matrix_metadata_pending_fact_retrieval"
    thesis = str(thesis_contract.get("text") or legacy_thesis)
    depth_contract = derive_section_depth_contract(
        {
            "section_role": role,
            "primary_papers": paper_ids,
            "target_words": target_words,
        }
    )
    return apply_single_paper_policy({
        "section_id": section["section_id"],
        "title": title,
        "section_role": role,
        "section_thesis": thesis,
        "scientific_thesis": thesis_contract,
        "thesis_status": str(thesis_contract.get("status") or "provisional"),
        "review_problem": review_problem(title, evidence_papers, dominant_logic),
        "target_paragraphs": target_paragraphs,
        "target_words": target_words,
        "depth_contract": depth_contract,
        "dominant_logic": dominant_logic,
        "major_papers": paper_ids,
        "primary_papers": paper_ids,
        "supporting_papers": supporting_ids,
        "review_claims": claims,
        # These legacy claims describe how the author should synthesize the
        # assigned literature ("establish", "compare", "qualify", etc.).
        # They must not become required full-text search propositions.
        "scientific_claims": [],
        "writing_requirements": [
            {
                "requirement_id": f"WR-{section['section_id']}-{index:02d}",
                "type": "cross_study_synthesis",
                "instruction": str(claim.get("claim") or ""),
                "source": "legacy_blueprint_script",
            }
            for index, claim in enumerate(claims, start=1)
            if str(claim.get("claim") or "").strip()
        ],
        "figure_or_table_needs": [
            {
                "type": "comparison table" if dominant_logic == "outlook" else "conceptual diagram or comparison table",
                "purpose": "Show the principal approaches, evidence relationships, or comparison axes that anchor this section.",
                "candidate_papers": paper_ids[:3],
            }
        ],
        "depth_requirements": [
            "Draft fully developed review prose, not a compact example or annotated bibliography.",
            "Use the approved matrix as a guide, but reopen Markdown/PDF evidence for section-level details.",
            "Each substantive paragraph should contain a claim, source-grounded evidence, and a review-level interpretation.",
        ],
        "section_transition": {
            "from_previous": f"Connect from {prev_title}." if prev_title else "Open the review scope and organizing logic.",
            "to_next": f"Set up {next_title}." if next_title else "Close with unresolved limitations and future directions.",
        },
        "avoid_patterns": [
            "Do not summarize papers in chronological order unless chronology is the section logic.",
            "Do not repeat a paper-level description already owned by another section.",
            "Do not organize prose as one title or one summary block per paper.",
            "Do not collapse distinct methods, evidence types, or study populations into one generic category.",
            "Do not use broad scope or performance adjectives without explicit evidence boundaries.",
        ],
    })
