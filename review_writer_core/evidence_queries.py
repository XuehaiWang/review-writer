"""Deterministic, discipline-neutral scientific evidence query plans."""

from __future__ import annotations

import re
from typing import Any


QUERY_WORD = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*")
QUERY_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "their", "this", "through", "to", "using",
    "review", "reviews", "section", "chapter", "study", "studies", "paper",
    "papers", "synthesis", "syntheses", "strategy", "strategies", "overview",
    "comparison", "conclusion", "introduction", "evidence", "current",
}
QUESTION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("object_input", ("substrate", "starting material", "input", "sample", "population", "dataset", "object")),
    ("method_conditions", ("method", "procedure", "protocol", "catalyst", "reaction condition", "experimental condition", "workflow")),
    ("quantitative_results", ("result", "yield", "selectivity", "performance", "accuracy", "conversion", "outcome")),
    ("scope", ("substrate scope", "functional group tolerance", "generality", "applicability", "scope")),
    ("mechanism", ("mechanism", "pathway", "intermediate", "control experiment", "explanation")),
    ("limitations", ("limitation", "constraint", "drawback", "challenge", "disadvantage")),
)


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
                *query_terms(topic_phrase, limit=5),
            ]
        )
    )[:8]
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
                for term in group[:10]
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
