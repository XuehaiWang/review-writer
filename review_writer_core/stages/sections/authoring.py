"""Small, local prose checks. Findings are repair hints, not plagiarism verdicts."""
from __future__ import annotations

import re
from copy import deepcopy
from difflib import SequenceMatcher


def normalized(value):
    return " ".join(str(value or "").split())


def organizational_transition(text):
    """Conservative allowance for document navigation, never arbitrary free prose.

    Other connective language can remain inside a source-bound prose span.
    This deliberately does not try to certify arbitrary uncited model text.
    """
    text = normalized(text)
    # Punctuation between exact source-bound spans is layout, not an assertion.
    if text and re.fullmatch(r"[\s,;:.!?，；：。！？—–()（）]+", text):
        return True
    if not text or len(text) > 180 or re.search(r"[\d$=<>%\[\]]", text):
        return False
    return bool(re.fullmatch(
        r"(?:We (?:next |now )?(?:consider|compare|discuss|examine)|"
        r"The following (?:section|discussion) (?:considers|compares|examines)) "
        r"(?:the )?(?:reported )?(?:methods|strategies|approaches|scope|conditions|limitations|results)"
        r"(?: and (?:their )?(?:scope|limitations|conditions|results))?[.:]?|"
        r"(?:下面|下文|接下来)(?:比较|讨论|考察)(?:这些|不同)?(?:方法|策略|条件|适用范围|局限性)[。：]?",
        text, re.I))


def prose_layout(text, claims):
    """Map exact, non-overlapping spans once; never pick an ambiguous first match.

    Legacy responses have no paragraph text and retain their existing order.
    On a malformed new mapping the caller retains bound spans, not unmapped prose.
    """
    text = normalized(text)
    if not text:
        return [], []
    layout, position = [], 0
    for claim in claims:
        span = normalized(claim.get("claim"))
        if not span or text.count(span) != 1:
            return [], ["ambiguous_or_missing_prose_span"]
        start = text.find(span)
        if start < position:
            return [], ["overlapping_or_unordered_prose_spans"]
        gap = text[position:start].strip()
        if gap:
            if not organizational_transition(gap):
                return [], ["unmapped_prose_requires_source_binding"]
            layout.append({"transition": gap})
        layout.append({"claim_id": claim["claim_id"]})
        position = start + len(span)
    gap = text[position:].strip()
    if gap:
        if not organizational_transition(gap):
            return [], ["unmapped_prose_requires_source_binding"]
        layout.append({"transition": gap})
    return layout, []


def bind_discourse_gaps(paragraph):
    """Retain exact prose, extending adjacent spans for mandatory semantic review.

    This is mapping, not evidence verification: no free-text gap is certified as
    navigation. Its scientific content must pass the existing source auditor.
    Ambiguous/overlapping spans remain explicit repair targets.
    """
    if not isinstance(paragraph, dict):
        return paragraph
    text = normalized(paragraph.get("text"))
    claims = paragraph.get("claims") or []
    if not text or not claims or any(not isinstance(c, dict) for c in claims):
        return paragraph
    if not prose_layout(text, [{"claim_id": str(i), "claim": c.get("text")} for i, c in enumerate(claims)])[1]:
        return paragraph
    positions = []
    end = 0
    for claim in claims:
        span = normalized(claim.get("text"))
        if not span or text.count(span) != 1:
            return paragraph
        start = text.find(span)
        if start < end:
            return paragraph
        positions.append((start, start + len(span)))
        end = start + len(span)
    result = deepcopy(paragraph)
    for i, claim in enumerate(result["claims"]):
        start, end = positions[i]
        left = 0 if i == 0 else start
        right = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        extended = text[left:right].strip()
        if extended != normalized(claim.get("text")):
            claim["text"] = extended
            claim["review_reasons"] = list(dict.fromkeys([
                *(claim.get("review_reasons") or []), "discourse_span_requires_source_review"]))
    return result


def retained_layout(layout, claim_ids):
    """Retain only navigation whose adjacent source spans both survived."""
    keep = set(claim_ids)
    result = []
    for index, row in enumerate(layout):
        if row.get("claim_id"):
            if row["claim_id"] in keep:
                result.append(row)
            continue
        left = next((r["claim_id"] for r in reversed(layout[:index]) if r.get("claim_id")), None)
        right = next((r["claim_id"] for r in layout[index + 1:] if r.get("claim_id")), None)
        if (left is None or left in keep) and (right is None or right in keep) and organizational_transition(row.get("transition")):
            result.append(row)
    return result


def paragraph_parts(plan, parts):
    """Use the same ordering/transition validation in generation and publication."""
    layout = plan.get("prose_layout") or []
    if not layout:
        return list(parts.values())
    ids = [row.get("claim_id") for row in layout if row.get("claim_id")]
    if ids != list(parts):
        raise ValueError("Paragraph prose/source mapping has changed.")
    output = []
    for row in layout:
        if row.get("claim_id"):
            output.append(parts[row["claim_id"]])
        elif set(row) == {"transition"} and organizational_transition(row["transition"]):
            output.append((row["transition"], "", "organization"))
        else:
            raise ValueError("Unbound paragraph text is not an organizational transition.")
    return output


def review_reasons(claim, registry):
    """Concrete conservative triggers; not a promise of complete semantic detection."""
    reasons = list(claim.get("review_reasons") or [])
    kind = str(claim.get("claim_kind") or "").casefold()
    if any(value in kind for value in ("mechanistic_inference", "causal_inference", "historical_priority", "superiority")):
        reasons.append("strong_scientific_inference")
    sources = [registry[ref["evidence_key"]] for ref in claim["evidence_refs"]]
    if kind in {"reported_finding", "direct_source_report"} and any(
        row.get("study_ownership") in {"prior_work", "other_study", "unclear"} for row in sources
    ):
        reasons.append("study_attribution_unclear")
    # A ranking clue plus a multi-study comparison is actionable, not the word alone.
    if len(claim.get("citation_group") or []) > 1 and re.search(
        r"\b(?:superior|outperform\w*|best|more effective|better than)\b|优于|最优|更有效",
        claim["claim"], re.I
    ):
        reasons.append("cross_study_ranking")
    return list(dict.fromkeys(reasons))


def paragraph_is_current(plan, paragraph):
    """New authoring layouts cannot carry extra unbound text into publication."""
    if "prose_layout" not in plan:  # Old artifacts remain readable.
        return True
    from review_writer_core.draft_bibliography import CALLOUT_RE
    rows = paragraph.get("claim_realizations") or []
    if [row.get("claim_id") for row in rows] != plan.get("claim_ids"):
        return False
    try:
        expected = " ".join(part[0] for part in paragraph_parts(plan, {
            row["claim_id"]: (row.get("text", ""), "", "") for row in rows}))
    except (ValueError, KeyError, TypeError):
        return False
    return normalized(CALLOUT_RE.sub("", expected)) == normalized(CALLOUT_RE.sub("", paragraph.get("text") or ""))


def style_findings(claims):
    """Bound-source overlap and source-matched repetition; hints, never deletion."""
    findings, seen = {}, {}
    for claim in claims:
        text = normalized(claim["claim"])
        words = re.findall(r"\w+", text.casefold())
        issues = []
        if len(words) >= 22:
            for ref in claim["evidence_refs"]:
                source_words = re.findall(r"\w+", ref["quote"].casefold())
                match = SequenceMatcher(None, words, source_words, autojunk=False).find_longest_match()
                if match.size >= 22 and sum(w.isalpha() and len(w) > 2 for w in words[match.a:match.a + match.size]) >= 12:
                    issues.append("long_source_wording_overlap")
                    break
        sources = tuple(sorted({str(ref.get("paper_id") or ref["evidence_key"])
                                for ref in claim["evidence_refs"]}))
        if len(words) >= 10:
            for previous_text, previous_paragraph in seen.get(sources, []):
                # Numbers and polarity differences may express essential contrasts.
                if (re.findall(r"\d+(?:\.\d+)?", previous_text) != re.findall(r"\d+(?:\.\d+)?", text.casefold())
                        or set(re.findall(r"\b(?:no|not|without|never)\b", previous_text))
                        != set(re.findall(r"\b(?:no|not|without|never)\b", text.casefold()))):
                    continue
                if SequenceMatcher(None, previous_text, text.casefold(), autojunk=False).ratio() >= .9:
                    issues.append("repeated_wording_in_paragraph" if previous_paragraph == claim["paragraph_id"]
                                  else "repeated_wording_across_paragraphs")
                    break
            seen.setdefault(sources, []).append((text.casefold(), claim["paragraph_id"]))
        if issues:
            findings[claim["claim_id"]] = issues
    return findings
