"""Deterministic, discipline-neutral scientific evidence query plans."""

from __future__ import annotations

import re
from typing import Any


QUERY_WORD = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
QUERY_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "their", "this", "through", "to", "using",
    "with",
    "review", "reviews", "section", "chapter", "study", "studies", "paper",
    "papers", "synthesis", "syntheses", "strategy", "strategies", "overview",
    "comparison", "conclusion", "introduction", "evidence", "current",
}
TOPIC_INSTRUCTION_WORDS = {
    "access", "categorize", "categorized", "categorise", "categorised",
    "classify", "classified", "compare", "development", "different",
    "discuss", "focusing", "focused", "focus", "generate", "organize",
    "organized", "organise", "organised", "please", "prepare", "write",
}
QUESTION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("object_input", ("substrate", "starting material", "input", "sample", "population", "dataset", "object")),
    ("method_conditions", ("method", "procedure", "protocol", "catalyst", "reaction condition", "experimental condition", "workflow")),
    ("quantitative_results", ("result", "yield", "selectivity", "performance", "accuracy", "conversion", "outcome")),
    ("scope", ("substrate scope", "functional group tolerance", "generality", "applicability", "scope")),
    ("mechanism", ("mechanism", "pathway", "intermediate", "control experiment", "explanation")),
    ("limitations", ("limitation", "constraint", "drawback", "challenge", "disadvantage")),
    ("validation_evidence", ("validation", "verification", "characterization", "measurement", "statistical analysis", "control experiment")),
    ("scale_reproducibility", ("scale-up", "gram scale", "sample size", "replicate", "reproducibility", "external validation")),
    ("intervention_role", ("catalyst loading", "co-catalyst", "promoter", "stoichiometric reagent", "auxiliary", "intervention role", "dose")),
    ("safety_cost_sustainability", ("safety", "toxicity", "hazard", "cost", "sustainability", "environmental impact", "resource use")),
    ("specialized_metrics", ("absolute configuration", "stereochemistry", "effect size", "uncertainty", "statistical method", "domain-specific metric")),
)

COMPARISON_FIELD_IDS = tuple(question_id for question_id, _terms in QUESTION_TERMS)


def query_terms(value: Any, *, limit: int = 8) -> list[str]:
    output: list[str] = []
    for match in QUERY_WORD.finditer(str(value or "").casefold()):
        term = match.group(0)
        if term in QUERY_STOPWORDS or len(term) < 2:
            continue
        if term not in output:
            output.append(term)
        if len(output) >= limit:
            break
    return output


def query_phrase(value: Any) -> str:
    text = " ".join(str(value or "").replace('"', " ").split()).strip()
    return text[:120]


def _word_form_variants(term: str) -> list[str]:
    """Return conservative token variants without discipline-specific aliases."""

    normalized = str(term or "").casefold().strip()
    if not normalized:
        return []
    variants = [normalized]
    if re.fullmatch(r"[a-z][a-z0-9'-]{3,}", normalized):
        if normalized.endswith("ies") and len(normalized) > 4:
            variants.append(f"{normalized[:-3]}y")
        elif normalized.endswith("s") and not normalized.endswith(
            ("ss", "is", "us")
        ):
            variants.append(normalized[:-1])
        elif not normalized.endswith(("s", "x", "z", "ed")):
            variants.append(f"{normalized}s")
    return list(dict.fromkeys(variants))


def _topic_concept_terms(review_topic: str, *, limit: int = 20) -> list[str]:
    """Expand user phrasing into portable lexical alternatives.

    Quoted text often contains a coined label or abbreviation rather than the
    wording used by every source paper.  Keep that phrase, but also split
    hyphenated compounds, retain the explicit focus clause, and add conservative
    singular/plural forms.  This stays discipline-neutral and does not require
    embeddings or a hard-coded scientific synonym table.
    """

    topic = str(review_topic or "")
    quoted = [
        query_phrase(item)
        for item in re.findall(r'["“”]([^"“”]{3,180})["“”]', topic)
        if query_phrase(item)
    ]
    focus = re.findall(
        r"(?:\bfocus(?:ing|ed)?\s+on\b|\bwith\s+emphasis\s+on\b|重点关注|聚焦于?)\s*"
        r"(.{3,320}?)(?=\b(?:organize|organise|categorize|categorise|"
        r"classify|group|separately\s+discuss)\b|[.;。；]|$)",
        topic,
        flags=re.I,
    )
    sources = [
        *quoted,
        *(" ".join(str(item or "").replace('"', " ").split()) for item in focus),
    ]
    if not sources:
        sources = [query_phrase(topic)]

    output: list[str] = []

    def add(value: str) -> None:
        cleaned = " ".join(str(value or "").casefold().split()).strip()
        if not cleaned or cleaned in output:
            return
        output.append(cleaned)

    for source in sources:
        if 3 <= len(source) <= 100:
            add(source)
        for raw_term in query_terms(source, limit=20):
            if raw_term in TOPIC_INSTRUCTION_WORDS:
                continue
            pieces = [
                part
                for part in re.split(r"[-_/]+", raw_term)
                if len(part) >= 2
                and part not in QUERY_STOPWORDS
                and part not in TOPIC_INSTRUCTION_WORDS
            ]
            if len(pieces) > 1:
                add(" ".join(pieces))
            for piece in pieces or [raw_term]:
                add(piece)
            if len(output) >= limit:
                return output[:limit]
    # Add morphology only after retaining the distinct concepts. This avoids
    # spending the bounded query budget on plural variants before later focus
    # terms have had a chance to enter the plan.
    for term in list(output):
        if " " in term:
            continue
        for variant in _word_form_variants(term):
            add(variant)
        if len(output) >= limit:
            break
    return output[:limit]


def build_question_query_plans(
    *,
    review_topic: str,
    heading: str = "",
    core_argument: str = "",
    section_role: str = "body",
    must_cover_points: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build short Boolean groups without turning prose into one long query."""

    quoted_topic = next(
        (
            query_phrase(item)
            for item in re.findall(r'["“”]([^"“”]{3,160})["“”]', review_topic)
            if query_phrase(item)
        ),
        "",
    )
    topic_phrase = quoted_topic or query_phrase(review_topic)
    heading_phrase = query_phrase(heading)
    core_terms = list(
        dict.fromkeys(
            [
                *query_terms(heading_phrase, limit=5),
                *_topic_concept_terms(review_topic, limit=20),
            ]
        )
    )[:22]
    core_group = list(
        dict.fromkeys(
            [
                phrase.casefold()
                for phrase in (heading_phrase, topic_phrase)
                if 3 <= len(phrase) <= 100
            ]
            + core_terms
        )
    )
    if not core_group:
        core_group = query_terms(core_argument, limit=6)
    if not core_group:
        return []

    role = str(section_role or "body").casefold()
    applicable = {
        "introduction": {"object_input", "method_conditions", "limitations"},
        "conclusion": {"quantitative_results", "scope", "limitations"},
    }.get(role, {item[0] for item in QUESTION_TERMS})
    definitions: list[tuple[str, list[str]]] = [("section_focus", [])]
    definitions.extend(
        (question_id, list(terms))
        for question_id, terms in QUESTION_TERMS
        if question_id in applicable
    )
    for index, point in enumerate(must_cover_points or [], start=1):
        terms = query_terms(point, limit=7)
        if terms:
            definitions.append((f"required_claim_{index:02d}", terms))

    plans: list[dict[str, Any]] = []
    for question_id, question_terms in definitions:
        groups = [core_group]
        if question_terms:
            groups.append(question_terms)
        query_parts = []
        for group in groups:
            alternatives = [
                f'"{term}"' if " " in term else term
                for term in group[:22]
            ]
            query_parts.append("(" + " OR ".join(alternatives) + ")")
        exact_phrases = list(
            dict.fromkeys(
                term
                for group in groups
                for term in group
                if " " in term and len(term) <= 100
            )
        )
        plans.append(
            {
                "question_id": question_id,
                "coverage_policy": (
                    "all_primary"
                    if question_id == "section_focus"
                    else "any_primary"
                    if question_id.startswith("required_claim_")
                    else "evidence_bearing"
                ),
                "required_for_section": bool(
                    question_id == "section_focus"
                    or question_id.startswith("required_claim_")
                ),
                "natural_query": (
                    f"Find evidence about {heading_phrase or topic_phrase}"
                    + (f" for {question_id.replace('_', ' ')}" if question_terms else "")
                ),
                "required_concept_groups": [core_group],
                "question_term_groups": [question_terms] if question_terms else [],
                "term_groups": groups,
                "exact_phrases": exact_phrases,
                "websearch_query": " ".join(query_parts),
                "excluded_terms": [],
                "expected_content_types": ["text", "merged_text", "markdown", "table"],
            }
        )
    return plans
