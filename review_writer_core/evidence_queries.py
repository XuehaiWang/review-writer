"""Deterministic, discipline-neutral scientific evidence query plans."""

from __future__ import annotations

import re
import hashlib
import json
from collections.abc import Iterable, Mapping
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
FACT_FIELD_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{0,79}$")
NON_FACT_QUESTION_IDS = frozenset({"abstract_summary", "coverage", "section_focus"})


def normalize_fact_field_id(value: Any) -> str:
    """Return one bounded field identity suitable for trusted task registries."""

    field_id = str(value or "").strip().casefold()
    if field_id in NON_FACT_QUESTION_IDS or not FACT_FIELD_ID.fullmatch(field_id):
        return ""
    return field_id


def registered_fact_field_ids(
    *,
    required_roles: Iterable[Any] = (),
    evidence_candidates: Iterable[Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    """Share the exact task-local field registry across extraction and audit."""

    values = [*required_roles]
    values.extend(
        field_id
        for candidate in evidence_candidates
        if isinstance(candidate, Mapping)
        for field_id in candidate.get("question_ids") or []
    )
    return tuple(
        dict.fromkeys(
            field_id
            for value in values
            if (field_id := normalize_fact_field_id(value))
        )
    )


def extraction_fact_field_ids(*, required_roles=(), evidence_candidates=()):
    """Extraction, verification and publication share available fields, not mandatory quotas."""
    return registered_fact_field_ids(required_roles=[*COMPARISON_FIELD_IDS, *required_roles],
                                     evidence_candidates=evidence_candidates)


def normalize_targeted_fact_gaps(
    gaps: Any,
    *,
    allowed_paper_ids: Iterable[Any],
    allowed_field_ids: Iterable[Any],
) -> dict[str, list[str]]:
    """Keep one bounded Blueprint gap map inside its section and field scope."""

    if not isinstance(gaps, Mapping):
        return {}
    allowed_fields = set(
        registered_fact_field_ids(required_roles=allowed_field_ids)
    )
    normalized: dict[str, list[str]] = {}
    for raw_paper_id in allowed_paper_ids:
        paper_id = str(raw_paper_id or "").strip()
        raw_fields = gaps.get(paper_id)
        if not paper_id or not isinstance(raw_fields, list):
            continue
        fields = [
            field_id
            for field_id in registered_fact_field_ids(required_roles=raw_fields)
            if field_id in allowed_fields
        ]
        if fields:
            normalized[paper_id] = fields
    return normalized


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


def boolean_query(groups: list[list[str]]) -> str:
    """One portable representation for the structured lexical contract."""
    return " ".join(
        "(" + " OR ".join('"' + query_phrase(term) + '"' for term in group if query_phrase(term)) + ")"
        for group in groups if any(query_phrase(term) for term in group)
    )


def normalize_fact_request(
    request: Any,
    *,
    allowed_field_ids: Iterable[Any] | None = None,
) -> dict[str, Any] | None:
    """Keep a question separate from source-stated lookup targets; accept old checkpoints."""
    if not isinstance(request, dict):
        return None
    field_id = normalize_fact_field_id(request.get("field_id"))
    allowed = set(COMPARISON_FIELD_IDS)
    if allowed_field_ids is not None:
        allowed.update(
            normalized
            for value in allowed_field_ids
            if (normalized := normalize_fact_field_id(value))
        )
    if not field_id or field_id not in allowed:
        return None
    query = " ".join(str(request.get("query") or "").split())[:600]
    if not query:
        return None
    targets = request.get("target_terms") or []
    keys = request.get("evidence_keys") or []
    reasons = request.get("reasons") if isinstance(request.get("reasons"), list) else []
    return {
        "field_id": field_id, "query": query,
        "target_terms": list(dict.fromkeys(query_phrase(term).casefold() for term in targets
            if isinstance(term, str) and query_phrase(term)))[:6] if isinstance(targets, list) else [],
        "experiment_id": query_phrase(request.get("experiment_id") or ""),
        "source_recovery": request.get("source_recovery") is True or bool({reason for reason in reasons
            if isinstance(reason, str)} & {
            "numeric_token_mismatch", "source_excerpt_mismatch", "source_damage"}),
        "evidence_keys": sorted(set(str(key) for key in keys if isinstance(key, str) and key))[:8]
            if isinstance(keys, list) else [],
    }


def fact_request_identity(
    request: dict[str, Any],
    *,
    allowed_field_ids: Iterable[Any] | None = None,
) -> str:
    """Deduplicate a problem, not a fact: distinct experiments keep distinct identities."""
    request = normalize_fact_request(request, allowed_field_ids=allowed_field_ids)
    if request is None:
        return ""
    payload = {key: request[key] for key in ("field_id", "experiment_id", "evidence_keys", "source_recovery")}
    payload["targets"] = sorted(request["target_terms"])
    if not payload["targets"] and not payload["experiment_id"] and not payload["evidence_keys"]:
        payload["targets"] = sorted(set(query_terms(request["query"], limit=40)) - TOPIC_INSTRUCTION_WORDS)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_fact_query_plans(
    request: dict[str, Any],
    *,
    allowed_field_ids: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """Bounded exact-to-broad recovery *inside* an already admitted paper/SI.

    Domain vocabulary comes from the request and the existing role definitions,
    never a paper ID, example result, or a topic-specific synonym catalogue.
    """
    request = normalize_fact_request(request, allowed_field_ids=allowed_field_ids)
    if request is None:
        return []
    role_terms = list(dict(QUESTION_TERMS).get(request["field_id"], ()))
    if not role_terms:
        role_terms = query_terms(request["field_id"].replace("_", " "), limit=8)
    targets = request["target_terms"] or [term for term in query_terms(request["query"], limit=24)
        if term not in TOPIC_INSTRUCTION_WORDS | {"provide", "identify", "identifying", "determine", "whether",
            "reported", "report", "surrounding", "text", "including", "contains", "all", "only", "actual",
            "when", "was", "were", "is", "are", "be", "it", "that", "may", "can", "could", "would", "observed"}][:8]
    identity = [request["experiment_id"].casefold()] if request["experiment_id"] else []
    variants = [([identity, targets] if identity else [targets, role_terms]),
                ([identity] if identity else [targets]), [role_terms]]
    plans, seen = [], set()
    for groups in variants:
        groups = [group for group in groups if group]
        query = boolean_query(groups)
        if not query or query in seen:
            continue
        seen.add(query)
        plans.append({"websearch_query": query, "term_groups": groups,
                      "exact_phrases": [term for group in groups for term in group if " " in term]})
    return plans


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
    research_questions: list[str] | None = None,
    section_role: str = "body",
    must_cover_points: list[Any] | None = None,
    scientific_claims: list[Any] | None = None,
    required_fact_roles: list[Any] | None = None,
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
    declared_roles = set(registered_fact_field_ids(required_roles=required_fact_roles or []))
    if declared_roles:
        applicable = declared_roles & set(COMPARISON_FIELD_IDS)
    definitions: list[dict[str, Any]] = [
        {
            "question_id": "section_focus",
            "terms": [],
            "coverage_policy": "all_primary",
            "required_for_section": True,
            "query_route": "focus",
        }
    ]
    definitions.extend(
        {
            "question_id": question_id,
            "terms": list(terms),
            "coverage_policy": "evidence_bearing",
            "required_for_section": False,
            "query_route": "fact_role",
        }
        for question_id, terms in QUESTION_TERMS
        if question_id in applicable
    )
    definitions.extend(
        {
            "question_id": field_id,
            "terms": query_terms(field_id.replace("_", " "), limit=8),
            "coverage_policy": "evidence_bearing",
            "required_for_section": False,
            "query_route": "fact_role",
        }
        for field_id in sorted(declared_roles - set(COMPARISON_FIELD_IDS))
    )

    definitions.extend({"question_id": f"writing_question_{index}", "terms": query_terms(question, limit=12),
                        "coverage_policy": "evidence_bearing", "required_for_section": False, "query_route": "writing_question"}
                       for index, question in enumerate(dict.fromkeys(research_questions or []), 1)
                       if str(question).strip())

    # Only explicitly structured scientific claims can create required Claim
    # queries.  Legacy prose instructions in ``must_cover_points`` used to be
    # treated as scientific propositions, which created impossible evidence
    # gaps such as searching source papers for "develop claim-centered
    # synthesis".  Structured legacy rows remain readable; plain strings are
    # deliberately ignored here and stay authoring constraints.
    claim_rows: list[dict[str, Any]] = []
    if scientific_claims is not None:
        claim_rows.extend(
            dict(item) if isinstance(item, dict) else {"proposition": str(item)}
            for item in scientific_claims
        )
    else:
        claim_rows.extend(
            dict(item)
            for item in must_cover_points or []
            if isinstance(item, dict)
            and (
                item.get("proposition")
                or item.get("scientific_proposition") is True
                or item.get("fact_ids")
                or item.get("evidence_refs")
                or item.get("required_fact_roles")
            )
        )

    limitation_terms = list(dict(QUESTION_TERMS).get("limitations") or ())
    for index, claim in enumerate(claim_rows, start=1):
        proposition = (
            claim.get("proposition")
            or claim.get("claim")
            or claim.get("text")
            or ""
        )
        terms = query_terms(proposition, limit=7)
        if not terms:
            continue
        claim_id = str(claim.get("claim_id") or f"claim_{index:02d}")
        required = bool(claim.get("required_for_section", True))
        definitions.append(
            {
                "question_id": f"required_claim_{index:02d}",
                "terms": terms,
                "coverage_policy": "any_primary",
                "required_for_section": required,
                "query_route": "support",
                "claim_id": claim_id,
                "proposition": str(proposition),
            }
        )
        definitions.append(
            {
                "question_id": f"claim_boundary_{index:02d}",
                "terms": list(dict.fromkeys([*terms, *limitation_terms]))[:14],
                "coverage_policy": "evidence_bearing",
                "required_for_section": False,
                "query_route": "boundary",
                "claim_id": claim_id,
                "proposition": str(proposition),
            }
        )

    plans: list[dict[str, Any]] = []
    for definition in definitions:
        question_id = str(definition["question_id"])
        question_terms = list(definition.get("terms") or [])
        groups = [core_group]
        if question_terms:
            groups.append(question_terms)
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
                **{
                    key: value
                    for key, value in definition.items()
                    if key not in {"terms"}
                },
                "question_id": question_id,
                "natural_query": (
                    f"Find evidence about {heading_phrase or topic_phrase}"
                    + (f" for {question_id.replace('_', ' ')}" if question_terms else "")
                ),
                "required_concept_groups": [core_group],
                "question_term_groups": [question_terms] if question_terms else [],
                "term_groups": groups,
                "exact_phrases": exact_phrases,
                "websearch_query": boolean_query([group[:22] for group in groups]),
                "excluded_terms": [],
                "expected_content_types": ["text", "merged_text", "markdown", "table"],
            }
        )
    return plans
