"""Shared source, identity and relation checks for scientific fact cards.

These deterministic checks establish provenance and reject known contradictions;
they deliberately do not claim to replace source-grounded semantic verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from itertools import product
from typing import Any, Iterable, Mapping

from review_writer_core.evidence_integrity import (
    normalized_anchor_text,
    normalized_source_excerpt_text,
    source_contains_excerpt,
    unsupported_realization_anchors,
)


FACT_VALIDATION_VERSION = "fact-support/3"
ASSERTION_CEILING_ORDER = (
    "context_only",
    "abstract_report_only",
    "attributed_author_interpretation",
    "direct_report_with_local_context",
    "direct_source_report",
)
FACT_PROMPT_VERSION = "fact-extraction/7"

# One writing/comparison policy for Blueprint, Sections and Draft evaluation.
# This permits synthesis, not unverified aliases or new experimental facts.
REVIEW_COMPARISON_POLICY = (
    "Assess comparability for the particular conclusion, not for a whole pair of papers. "
    "Different names or source-local labels alone do not prove different objects or incompatibility. "
    "Use supplied source descriptions to distinguish aliases, related families and scientifically important differences; "
    "never infer structural identity from similar names or merge separate experiments. "
    "Source-supported family-level synthesis, method contrasts and reported-condition comparisons are allowed "
    "without identical test objects. Preserve each result's object, metric and conditions. "
    "A numerical superiority ranking or causal claim needs evidence for the relevant matching basis; "
    "similarity alone is not proof of equal reactivity or performance. "
    "Not yet assessed is not scientifically incomparable, and missing extraction is not absent reporting. "
    "Lead with a concrete supported takeaway. State only limitations that materially affect that takeaway; "
    "do not append a generic cannot-compare disclaimer to every paragraph. Keep unresolved extraction checks in diagnostics."
)


def normalize_assertion_ceiling(value: Any) -> str:
    """Fail closed when a current or legacy artifact omits its claim authority."""

    normalized = str(value or "").strip()
    return normalized if normalized in ASSERTION_CEILING_ORDER else "context_only"


def claim_assertion_ceiling(
    evidence: Iterable[Mapping[str, Any]],
    facts: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Use the weakest ceiling of the cited passages and selected fact cards.

    Callers resolve fact IDs against current evidence first. Unselected facts
    sharing a passage must not change this Claim's ceiling. Missing and unknown
    values fail closed to context-only.
    """
    ceilings = [normalize_assertion_ceiling(row.get("assertion_ceiling")) for row in evidence]
    ceilings.extend(
        normalize_assertion_ceiling(row.get("assertion_ceiling")) for row in facts
    )
    return min(
        ceilings,
        key=ASSERTION_CEILING_ORDER.index,
        default="context_only",
    )


def evidence_repair_has_changes(repair: Mapping[str, Any] | None) -> bool:
    """Both selected passages and verified fact supplements mutate Evidence."""
    return any((repair or {}).get(key) for key in (
        "added_evidence_count", "promoted_fact_count", "matrix_fact_promotion_count",
    ))


def attach_repair_fact_context(package, repair, *, selected_by_paragraph=None):
    """Attach verified supplements without claiming that they prove a Claim.

    Before rewriting all matching supplements are context for source checking.
    Publication additionally requires the paragraph checker to select the exact
    source keys. Corrections never silently replace facts used by other prose.
    """
    candidate = deepcopy(package or {})
    registry = {str(row.get("evidence_key")): row for row in candidate.get("evidence_registry") or []}
    sections = {str(row.get("section_id")): row for row in candidate.get("sections") or []}
    sources = {str(paper.get("paper_id")): {str(row.get("evidence_key")): row
               for row in paper.get("evidence_candidates") or []}
               for paper in repair.get("fact_repair_sources") or []}
    roots = repair.get("targets") or []
    supplements = candidate.setdefault("paragraph_fact_supplements", {})
    promoted, pending_corrections = [], []
    affected_paragraphs, affected_sections = set(), set()
    for paper in repair.get("papers") or []:
        paper_id = str(paper.get("paper_id") or "")
        owned = sources.get(paper_id, {})
        targets = [root for root in roots if root.get("paper_id") == paper_id]
        for fact in paper.get("facts") or []:
            if str(fact.get("fact_id")) in (repair.get("initial_fact_ids") or {}).get(paper_id, []):
                continue
            if fact.get("correction_of_fact_id"):
                pending_corrections.append({"paper_id": paper_id, "fact_id": fact.get("fact_id"),
                                            "correction_of_fact_id": fact["correction_of_fact_id"]})
                continue
            if (fact.get("validation_contract") != FACT_VALIDATION_VERSION
                    or not fact_is_usable(fact, purpose="detail")):
                continue
            spans = fact_support_spans(fact, owned)
            if not spans:
                continue
            keys = {ref["evidence_key"] for ref in spans}
            used = False
            for target in targets:
                for pid in target.get("paragraph_ids") or []:
                    if selected_by_paragraph is not None and not keys <= set(selected_by_paragraph.get(pid) or []):
                        continue
                    # Paragraph identifiers are generated from section IDs;
                    # use explicit target section when supplied by the plan.
                    sid = (target.get("paragraph_sections") or {}).get(pid) or str(pid).rsplit("-p", 1)[0]
                    section = sections.get(sid)
                    if section is None:
                        continue
                    permitted = set(map(str, [*(section.get("primary_papers") or []),
                                               *(section.get("supporting_papers") or []),
                                               *(section.get("allowed_papers") or [])]))
                    if permitted and paper_id not in permitted:
                        continue
                    entries = supplements.setdefault(pid, [])
                    for key in sorted(keys):
                        row = {**owned[key], "paper_id": paper_id, "claim_eligible": True,
                               "support_level": "direct", "match_type": "direct_match",
                               "source_channel": "fact_agent_supplement", "fact_ids": [fact["fact_id"]]}
                        registry.setdefault(key, row)
                        hits = section.setdefault("hits", [])
                        if not any(hit.get("evidence_key") == key for hit in hits):
                            hits.append(row)
                        if not any(entry.get("evidence_key") == key for entry in entries):
                            entries.append({"evidence_key": key, "paper_id": paper_id})
                    used = True
                    affected_paragraphs.add(pid)
                    affected_sections.add(sid)
            if used:
                promoted.append({"paper_id": paper_id, "fact": {**fact, "support_spans": spans, "evidence_refs": spans,
                    "origin_paragraph_id": next((pid for target in targets for pid in target.get("paragraph_ids") or []
                        if selected_by_paragraph is None or keys <= set(selected_by_paragraph.get(pid) or [])), ""),
                    "extraction": {**(fact.get("extraction") or {}), "method": "draft_shared_fact_agent"}}})
    candidate["evidence_registry"] = list(registry.values())
    return candidate, {"promoted_facts": promoted, "pending_corrections": pending_corrections,
                       "affected_paragraph_ids": sorted(affected_paragraphs),
                       "affected_section_ids": sorted(affected_sections)}
_METRIC = r"(?:yield|conversion|recovery|selectivity|ee|de|er|dr|rr|accuracy|precision|recall|sensitivity|specificity|auc)"
_VALUE = r"\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?\s*%?"
_METRIC_AFTER = re.compile(rf"(?P<value>{_VALUE})\s*(?P<metric>{_METRIC})\b", re.I)
_METRIC_BEFORE = re.compile(
    rf"\b(?P<metric>{_METRIC})\s*(?:(?:of|was|is|were|up to)\s*|[=:,(]\s*){{0,3}}(?P<value>{_VALUE})",
    re.I,
)
_CHEMICAL_LOCANT_NAME = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*|\d+(?:,\d+)+)"
    r"(?:-(?:\d+(?:,\d+)*|[A-Za-z][A-Za-z0-9]*))+\b"
)


def numerical_tokens_supported(value: str, excerpt: str) -> bool:
    numeric_text = _CHEMICAL_LOCANT_NAME.sub(" ", str(value or ""))
    numbers = re.findall(r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?(?:\s*%)?", numeric_text)
    source = re.sub(r"(?<=\d)\s+(?=%)", "", normalized_source_excerpt_text(excerpt)).casefold()
    return all(re.search(r"(?<![\d.])" + re.escape(re.sub(r"\s+", "", number).casefold()) + r"(?!\d|\.\d)", source)
               for number in numbers)


def metric_values(text: Any) -> set[tuple[str, str]]:
    """Extract local metric/value associations, not a bag of all numbers."""
    text = normalized_source_excerpt_text(text).replace("\\%", "%").replace("$", "")
    return {
        (match.group("metric").casefold(), normalized_anchor_text(match.group("value")))
        for pattern in (_METRIC_AFTER, _METRIC_BEFORE)
        for match in pattern.finditer(text)
    }


def fact_relation_issues(value: Any, excerpts: Iterable[str]) -> list[str]:
    excerpts = list(excerpts)
    source_pairs = set().union(*(metric_values(text) for text in excerpts))
    by_metric: dict[str, set[str]] = {}
    for metric, number in source_pairs:
        by_metric.setdefault(metric, set()).add(number)
    # Only reject a deterministic mismatch. Unstructured/ambiguous tables still
    # need the Agent to read their headers and experiment context.
    return [
        f"metric_value_mismatch:{metric}:{number}"
        for metric, number in sorted(metric_values(value))
        if metric in by_metric and number not in by_metric[metric]
        # A value in an unstructured local passage is not disproved by a
        # different experiment's explicit metric. Semantic verification must
        # resolve that local association. An explicit *other* metric/value
        # association (e.g. recall versus accuracy) is not such an ambiguity.
        and not any(numerical_tokens_supported(number, text)
                    and not any(number == other for _metric, other in metric_values(text))
                    for text in excerpts)
    ]


def fact_support_spans(
    fact: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]], *, issues: list[str] | None = None
) -> list[dict[str, Any]]:
    """Validate each quote against its own registered source; fail closed.

    Legacy single-quote facts remain readable. Source coordinates are copied
    from the registry, never trusted from model output.
    """
    def reject(reason):
        if issues is not None:
            issues.append(reason)
        return []

    supplied = fact.get("support_spans")
    if supplied is None:
        refs = fact.get("evidence_refs") or [{"evidence_key": fact.get("evidence_key")}]
        supplied = [
            {**ref, "support_excerpt": ref.get("support_excerpt") or fact.get("support_excerpt")}
            for ref in refs if isinstance(ref, dict)
        ]
    if not isinstance(supplied, list) or not supplied:
        return reject("missing_support_spans")
    result: list[dict[str, Any]] = []
    for span in supplied:
        if not isinstance(span, dict):
            return reject("invalid_support_span")
        key = str(span.get("evidence_key") or "")
        source = sources.get(key)
        excerpt = " ".join(str(span.get("support_excerpt") or "").split())
        if source is None:
            return reject("unregistered_source")
        if not source_contains_excerpt(source.get("content"), excerpt):
            return reject("source_excerpt_mismatch")
        if (
            span.get("source_lineage_hash") and source.get("source_lineage_hash")
            and span["source_lineage_hash"] != source["source_lineage_hash"]
        ):
            return reject("source_lineage_mismatch")
        result.append({
            "evidence_key": key,
            "support_excerpt": excerpt,
            **{name: source.get(name) for name in (
                "paper_id", "source_file_id", "source_type", "chunk_id", "page_start", "page_end", "source_lineage_hash"
            )},
            "section_path": list(source.get("section_path") or []),
        })
    excerpts = [span["support_excerpt"] for span in result]
    if not numerical_tokens_supported(str(fact.get("value") or ""), " ".join(excerpts)):
        return reject("numeric_token_mismatch")
    relations = fact_relation_issues(fact.get("value"), excerpts)
    if relations:
        return reject(";".join(relations))
    return result


def review_fingerprint(fact: Mapping[str, Any]) -> str:
    """Bind a verdict to exact scientific inputs, preserving formula whitespace."""
    keys = ("paper_id", "field_id", "fact_type", "source_channel", "value", "evidence_ceiling", "subject",
            "predicate", "normalized_value", "unit", "qualifiers", "experiment_id",
            "epistemic_status", "evidence_refs", "support_spans", "support_excerpt",
            "classification_axis_id", "classification_partition_id", "revision_assertion_ceiling")
    inputs = {key: fact.get(key) for key in keys}
    if "study_ownership" in fact:
        inputs["study_ownership"] = fact["study_ownership"]
    return hashlib.sha256(json.dumps(inputs,
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def verification_matches(fact: Mapping[str, Any]) -> bool:
    """Accept exact audits or proven source metadata hydration/legacy aliases.

    Never ignore provenance in the hash: reconstruct the old input and require
    its exact saved digest. Quotes, values, chunk IDs and lineage remain bound.
    This also reads historical artifacts without rewriting their verdicts.
    """
    expected = (fact.get("verification") or {}).get("input_fingerprint")
    if not expected or expected == review_fingerprint(fact):
        return True
    slots = [(collection, field) for collection in ("evidence_refs", "support_spans")
             for field in ("source_file_id", "source_type")
             if fact.get(collection) and all(
                 isinstance(ref, dict) and ref.get("evidence_key")
                 and ref.get("chunk_id") and ref.get("source_lineage_hash")
                 and ref.get(field) for ref in fact[collection])]
    # main_article was the legacy storage label for article, not an evidence
    # channel. source_channel, paper, chunk and source lineage remain unchanged.
    choices = [("keep", None, "main_article") if field == "source_type"
               and all(ref[field] == "article" for ref in fact[collection])
               else ("keep", None) for collection, field in slots]
    for values in product(*choices):
        if all(value == "keep" for value in values):
            continue
        previous = deepcopy(dict(fact))
        for (collection, field), value in zip(slots, values):
            if value != "keep":
                for ref in previous[collection]:
                    ref[field] = value
        if review_fingerprint(previous) == expected:
            return True
    return False


def fact_needs_verification(fact: Mapping[str, Any]) -> bool:
    if fact.get("superseded_by_fact_id"):
        return False
    verdict = fact.get("verification") or {}
    # Historical source-bound facts without a validation contract remain readable.
    if not fact.get("validation_contract") and not verdict:
        return False
    return (verdict.get("contract") != FACT_VALIDATION_VERSION
            or verdict.get("status") not in {"supported", "uncertain", "rejected"}
            or not verification_matches(fact))


def verify_plain_source_quote(fact: dict[str, Any], sources: Mapping[str, Mapping[str, Any]]) -> bool:
    """Admit a plain source quotation without a separate semantic model call.

    This checks source attribution, not a paraphrase, classification, numeric
    relation or scientific inference. Those keep their independent audit.
    """
    if ("study_ownership" in fact
            or fact.get("field_id") not in {"object_input", "scope", "research_question", "contribution"}
            or fact.get("fact_type") not in {"reported_object_or_input", "scope", "reported_fact", "study_description"}
            or fact_is_classification(fact)
            or fact.get("revision_of_fact_id") or fact.get("correction_of_fact_id")
            or fact.get("epistemic_status") not in {"direct_source_report", "abstract_level_report"}
            or fact.get("support_level") not in {"direct", "abstract_limited"}
            or fact.get("experiment_id") or fact.get("qualifiers")
            or (fact.get("verification") or {}).get("source_damage")
            or (fact.get("verification") or {}).get("status") in {"uncertain", "contradicted", "rejected"}):
        return False
    spans = fact_support_spans(fact, sources)
    if len(spans) != 1:
        return False
    quote = spans[0]["support_excerpt"]
    if " ".join(str(fact.get("value") or "").split()) != quote:
        return False
    source = sources[spans[0]["evidence_key"]]
    if (str(source.get("content_type") or "body").casefold() not in {"body", "text", "paragraph", "abstract"}
            or (source.get("paper_id") and source["paper_id"] != fact.get("paper_id"))):
        return False
    # Fail back to semantic audit for numbers, damaged text, comparisons,
    # causality, negatives or broad claims, even if labelled as plain scope.
    if re.search(r"[\d\ufffd<>=%]|\b(?:caus\w*|mechanis\w*|cataly\w*|inhibit\w*|promot\w*|"
                 r"lead\w*|result\w*|because|due|therefore|prove\w*|demonstrat\w*|compar\w*|"
                 r"hypothe\w*|infer\w*|suggest\w*|imply|implies|indicat\w*|may|might|could|cannot|"
                 r"associat\w*|correlat\w*|predict\w*|expect\w*|improv\w*|enabl\w*|"
                 r"than|better|worse|higher|lower|superior|equivalent|identical|same|"
                 r"increase\w*|decrease\w*|significant\w*|all|every|any|always|only|"
                 r"no|not|never|without|except|fail\w*|lack\w*)\b|"
                 r"因果|导致|机制|机理|催化|抑制|促进|优于|高于|低于|相同|等同|所有|均|未|不|无|可能|推测|假设|表明|相关", quote, re.I):
        return False
    if any(str(fact.get(key) or "") and not source_contains_excerpt(quote, fact[key])
           for key in ("subject", "predicate")):
        return False
    fact["verification"] = {"contract": FACT_VALIDATION_VERSION, "status": "supported",
        "method": "exact_source_quote", "reason": "Plain quotation matched its registered source; prose interpretation is checked when written.",
        "input_fingerprint": review_fingerprint(fact)}
    return True


def fact_identity(fact: Mapping[str, Any]) -> str:
    """Identify a proposition including its source, experiment and qualifiers."""
    payload = {
        key: fact.get(key) for key in (
            "paper_id", "field_id", "value", "subject", "predicate", "qualifiers",
            "experiment_id", "epistemic_status",
        )
    }
    payload["sources"] = sorted({
        (str(ref.get("evidence_key") or ""), str(ref.get("source_lineage_hash") or ""),
         normalized_source_excerpt_text(ref.get("support_excerpt") or fact.get("support_excerpt")))
        for ref in fact.get("support_spans") or fact.get("evidence_refs") or [] if isinstance(ref, dict)
    })
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return "MF-" + digest[:20].upper()


def merge_facts(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain distinct same-role facts; never deduplicate merely by field/paper."""
    result: dict[str, dict[str, Any]] = {}
    for group in groups:
        for fact in group:
            if not isinstance(fact, dict) or not fact.get("value"):
                continue
            identity = fact_identity(fact)
            previous = result.get(identity)
            # An extraction of the exact same proposition must not erase a
            # completed relation audit. Changed values/sources have another ID.
            if previous and (previous.get("verification") or {}).get("contract") == FACT_VALIDATION_VERSION and not fact.get("verification"):
                continue
            result[identity] = dict(fact)
    return list(result.values())


def fact_is_classification(fact: Mapping[str, Any]) -> bool:
    return bool(fact.get("fact_type") == "classification" or fact.get("field_id") == "topic_partition"
                or fact.get("classification_axis_id"))


def fact_usage(fact: Mapping[str, Any]) -> str:
    """One policy for planning, comparison, chapter binding and coverage.

    Legacy source-bound cards remain readable. Newly extracted cards require
    their relation audit. Classification evidence is never a scientific result.
    Abstract reports may support attribution, not detailed claims/comparisons.
    """
    verification = fact.get("verification") or {}
    if (fact.get("superseded_by_fact_id") or not fact.get("value") or not fact.get("evidence_refs")
            or fact.get("support_level") in {"context_only", "coverage_only", "neighbor_context"}
            or verification.get("status") in {"rejected", "pending", "unavailable", "uncertain"}
            or (fact.get("validation_contract") and verification.get("status") != "supported")):
        return "unusable"
    if fact_is_classification(fact):
        return "classification"
    if (fact.get("support_level") == "abstract_limited" or fact.get("source_channel") == "abstract"
            or fact.get("epistemic_status") == "abstract_level_report"
            or fact.get("assertion_ceiling") == "abstract_report_only"
            or any(ref.get("chunk_id") == "abstract" for ref in fact.get("evidence_refs") or [])):
        return "background"
    return "direct"


def fact_is_usable(fact: Mapping[str, Any], *, purpose: str = "background") -> bool:
    if purpose not in {"background", "detail"}:
        raise ValueError(f"Unknown fact use: {purpose}")
    usage = fact_usage(fact)
    return usage == "direct" or (purpose == "background" and usage == "background")


def fact_assertion_texts(fact: Mapping[str, Any]) -> list[str]:
    """Selected proposition and source-checked components, not adjacent results.

    Qualifiers and subject only expand the anchor set when present in the
    registered quote. Adjacent experiments in the source chunk are not inherited.
    """
    texts = [str(fact.get("value") or "")]
    quotes = [str(span.get("support_excerpt") or "") for span in
              fact.get("support_spans") or fact.get("evidence_refs") or [] if isinstance(span, dict)]
    if fact.get("support_excerpt"):
        quotes.append(str(fact["support_excerpt"]))
    # Pre-contract cards had only a value and a quote, without structured
    # experiment identity. Keep their existing bounded-quote compatibility;
    # newly extracted/audited cards never inherit this legacy allowance.
    if not any(fact.get(key) for key in ("validation_contract", "experiment_id", "subject", "qualifiers")):
        texts.extend(quotes)
    parts = [fact.get("subject"), fact.get("predicate"), fact.get("experiment_id")]
    if isinstance(fact.get("qualifiers"), dict):
        for value in fact["qualifiers"].values():
            parts.extend(value if isinstance(value, list) else [value])
    texts.extend(str(part) for part in parts if part and any(source_contains_excerpt(quote, part) for quote in quotes))
    return list(dict.fromkeys(text for text in texts if text))


def fact_claim_issues(claim: str, facts: Iterable[Mapping[str, Any]], *, claim_kind: str) -> list[str]:
    """Validate selected fact scope without mixing unselected experiments."""
    facts = list(facts)
    issues = []
    if any(not fact_is_usable(fact) for fact in facts):
        issues.append("fact_not_usable")
    texts = [text for fact in facts for text in fact_assertion_texts(fact)]
    anchors = unsupported_realization_anchors(claim, texts)
    issues.extend(f"unsupported_{kind}:{anchor}" for kind, values in anchors.items() for anchor in values)
    # Use verified propositions for downstream metric relations, not pooled
    # quotes containing adjacent experiments. Match explicit experiment/subject
    # identities within each clause before comparing metric/value associations.
    for clause in re.split(r";|(?<=[.!?])\s+|\b(?:whereas|while)\b", claim):
        explicit_experiment = any(fact.get("experiment_id") and re.search(
            r"(?<!\w)" + re.escape(str(fact["experiment_id"])) + r"(?!\w)", clause, re.I) for fact in facts)
        named = [fact for fact in facts if any(
            len(str(fact.get(key) or "")) >= 2 and re.search(
                r"(?<!\w)" + re.escape(str(fact[key])) + r"(?!\w)", clause, re.I)
            for key in ("experiment_id", "subject")
        )]
        selected = named or facts
        if any(fact_usage(fact) == "background" for fact in selected):
            direct = [text for fact in selected if fact_usage(fact) == "direct" for text in fact_assertion_texts(fact)]
            if ((not direct and claim_kind in {"reported_method", "mechanism_interpretation"})
                    or re.search(r"\b(?:outperform\w*|superior|inferior|better|worse|highest|lowest|caus(?:es|ed))\b", clause, re.I)
                    or unsupported_realization_anchors(clause, direct)["quantitative"]):
                issues.append("abstract_detail_requires_full_text")
        issues.extend(fact_relation_issues(clause, [str(fact.get("value") or "") for fact in selected]))
        if named:
            scoped = unsupported_realization_anchors(clause, [text for fact in selected for text in fact_assertion_texts(fact)])
            # A synthesis may name several systems although only one card's
            # subject matches verbatim. Entity presence was checked across its
            # selected premises above; the source audit checks their relations.
            # Keep local metric checks and explicit experiment bindings strict.
            issues.extend(f"unsupported_{kind}:{anchor}" for kind, values in scoped.items() for anchor in values
                          if not (kind == "technical_entities" and claim_kind == "provisional_synthesis" and not explicit_experiment))
    return list(dict.fromkeys(issues))


def attach_fact_to_evidence(evidence: dict[str, Any], fact: Mapping[str, Any]) -> None:
    """Keep all usable propositions sharing a source instead of last-write wins."""
    if not fact_is_usable(fact):
        return
    evidence["fact_ids"] = list(dict.fromkeys([
        *(evidence.get("fact_ids") or []), str(fact.get("fact_id") or fact_identity(fact))
    ]))
    values = list(dict.fromkeys([
        *(evidence.get("normalized_fact_values") or []),
        *([evidence["normalized_fact_value"]] if evidence.get("normalized_fact_value") and not evidence.get("normalized_fact_values") else []),
        str(fact["value"]),
    ]))
    evidence["normalized_fact_values"] = values
    # Compatibility view for existing claim consumers, not an experiment merge.
    evidence["normalized_fact_value"] = "\n".join(values)
    bindings = {item["fact_id"]: item for item in evidence.get("fact_bindings") or []}
    fid = str(fact.get("fact_id") or fact_identity(fact))
    bindings[fid] = {"fact_id": fid, **{key: fact.get(key) for key in (
        "paper_id", "field_id", "fact_type", "value", "subject", "predicate", "experiment_id", "qualifiers",
        "epistemic_status", "assertion_ceiling", "evidence_refs", "support_spans", "support_excerpt",
        "support_level", "source_channel", "verification", "validation_contract"
    )}}
    bindings[fid]["usage"] = fact_usage(fact)
    bindings[fid]["paper_id"] = fact.get("paper_id") or evidence.get("paper_id")
    evidence["fact_bindings"] = list(bindings.values())


def registered_fact_bindings(evidence: Iterable[Mapping[str, Any]], allowed_papers: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Resolve IDs through owned, current evidence; the model need not copy refs.

    All spans must be present in this section's registry and match their paper,
    source version and quotation. A partial multi-span binding is not accepted.
    """
    allowed = set(allowed_papers)
    sources = {str(row.get("evidence_key")): row for row in evidence
               if row.get("evidence_key") and str(row.get("paper_id")) in allowed}
    registry: dict[str, dict[str, Any]] = {}
    ambiguous = set()
    for source in sources.values():
        for binding in source.get("fact_bindings") or []:
            fid = str(binding.get("fact_id") or "")
            if not fid or not fact_is_usable(binding):
                continue
            paper = str(binding.get("paper_id") or source.get("paper_id") or "")
            refs = binding.get("evidence_refs") or []
            if any(str(ref.get("evidence_key")) not in sources
                   or str(sources[str(ref["evidence_key"])].get("paper_id")) != paper
                   or (ref.get("source_lineage_hash") and
                       ref["source_lineage_hash"] != sources[str(ref["evidence_key"])].get("source_lineage_hash"))
                   for ref in refs):
                continue
            if binding.get("support_excerpt") or binding.get("support_spans"):
                if not fact_support_spans(binding, sources):
                    continue
            canonical = {**binding, "paper_id": paper}
            if fid in registry and registry[fid] != canonical:
                ambiguous.add(fid)
            registry[fid] = canonical
    return {fid: value for fid, value in registry.items() if fid not in ambiguous}


def writable_evidence_keys(evidence: Iterable[Mapping[str, Any]], allowed_papers: Iterable[str]) -> set[str]:
    """Registered direct evidence or validated limited-use fact context.

    Background admission does not raise its ceiling; callers still apply the
    fact/claim checks. Neighbor text alone never becomes a writable source.
    """
    allowed = set(allowed_papers)
    sources = [row for row in evidence if str(row.get("paper_id") or "") in allowed]
    facts = registered_fact_bindings(sources, allowed)
    return {str(row["evidence_key"]) for row in sources
            if row.get("evidence_key") and row.get("claim_eligible", True)} | {
        str(ref["evidence_key"]) for fact in facts.values()
        for ref in fact.get("evidence_refs") or [] if ref.get("evidence_key")
    }


def build_fact_comparison(section_id, paper_ids, rows):
    """One versioned comparison view shared by planning and section writing."""
    cells = []
    for paper_id in paper_ids:
        for fact in merge_facts(rows.get(paper_id, {}).get("scientific_facts") or []):
            if not fact_is_usable(fact, purpose="detail") or not fact.get("field_id"):
                continue
            cells.append({
                **{key: fact.get(key) for key in ("field_id", "value", "epistemic_status", "confidence", "evidence_refs",
                                                "assertion_ceiling", "evidence_ceiling", "experiment_id", "subject", "qualifiers", "verification")},
                "paper_id": paper_id,
                "fact_ids": list(dict.fromkeys(str(value) for value in [fact.get("fact_id") or fact_identity(fact),
                                                                        *(fact.get("fact_ids") or [])] if value)),
            })
    fields = list(dict.fromkeys(cell["field_id"] for cell in cells))
    counts = {field: len({cell["paper_id"] for cell in cells if cell["field_id"] == field}) for field in fields}
    result = {
        "contract": "fact-comparison/3", "section_id": section_id, "paper_ids": paper_ids, "fields": fields,
        "comparable_fields": [field for field in fields if counts[field] >= 2],
        "single_source_fields": [field for field in fields if counts[field] == 1],
        "comparison_policy": REVIEW_COMPARISON_POLICY,
        "comparability": [{"field_id": field, "level": "source_grounded_synthesis",
                           "reason": "shared_reported_field", "ranking_status": "not_assessed",
                           "allowed_uses": ["reported_condition_comparison", "source_supported_qualitative_synthesis"],
                           "requires_verification": ["object_equivalence", "quantitative_ranking", "causal_attribution"]}
                          for field in fields if counts[field] >= 2],
        "missing_cells": [{"paper_id": paper, "field_id": field, "status": "unresolved"}
                          for field in fields for paper in paper_ids
                          if not any(cell["paper_id"] == paper and cell["field_id"] == field for cell in cells)],
        "cells": cells,
    }
    result["input_fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def is_additive_fact_repair(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Prove compatibility with old inputs, not merely ignore an artifact ID.

    Only Draft's additive, source-bound fact repairs qualify. Changes to any
    existing fact, paper, order, scope or classification remain incompatible.
    """
    before, after = dict(previous), dict(current)
    old_rows, new_rows = before.pop("rows", []), after.pop("rows", [])
    before.pop("fact_repair_history", None)
    history = after.pop("fact_repair_history", [])
    if before != after or not history or not old_rows or len(old_rows) != len(new_rows):
        return False
    if history[-1].get("operation") != "draft_targeted_fact_promotion":
        return False
    for old_row, new_row in zip(old_rows, new_rows):
        old, new = dict(old_row), dict(new_row)
        old_facts, new_facts = old.pop("scientific_facts", []), new.pop("scientific_facts", [])
        if old != new or any(fact not in new_facts for fact in old_facts):
            return False
        added = [fact for fact in new_facts if fact not in old_facts]
        if any(not fact_is_usable(fact) or (fact.get("field_id") != "claim_targeted_fact" and not (
                   fact.get("validation_contract") == FACT_VALIDATION_VERSION
                   and (fact.get("extraction") or {}).get("method") == "draft_shared_fact_agent"
                   and not fact.get("correction_of_fact_id")))
               or not fact.get("support_excerpt") or not fact.get("origin_paragraph_id") for fact in added):
            return False
    return True
