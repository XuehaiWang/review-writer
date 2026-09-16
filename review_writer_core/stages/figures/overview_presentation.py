"""Presentation-only contracts for compact overview figures."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


OVERVIEW_DISPLAY_SCHEMA_VERSION = 2
DEFAULT_MAX_MODULES = 5
DEFAULT_MAX_CLAIMS_PER_MODULE = 2
DEFAULT_MAX_UNASSIGNED_FINDINGS = 2
DEFAULT_MAX_WORDS = 12
DEFAULT_MAX_CHARS = 84

_SPACE_RE = re.compile(r"\s+")
_SOURCE_SUFFIX_RE = re.compile(r"\s*\(\s*source\s+stud(?:y|ies)\s*:[^)]*\)\s*$", re.I)
_CITATION_RE = re.compile(r"\s*\[(?:\d+(?:\s*[-,]\s*\d+)*)\]\s*")
_METRIC_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:%|°\s*C|h\b|min\b|equiv\b|mol\s*%|ee\b|dr\b|yield\b))",
    re.I,
)
_RESULT_RE = re.compile(
    r"\b(?:afford(?:ed|s)?|deliver(?:ed|s)?|furnish(?:ed|es)?|form(?:ed|s)?|"
    r"give|gave|gives|obtain(?:ed|s)?|reach(?:ed|es)?|preserv(?:ed|es)?|"
    r"enable(?:d|s)?|access(?:ed|es)?)\b",
    re.I,
)
_DANGLING_END = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "or", "the",
    "to", "under", "using", "via", "with", "without",
}
_SEMANTIC_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "gave",
    "gives", "in", "is", "of", "on", "the", "to", "under", "up", "use",
    "uses", "via", "with", "reached", "reach", "access", "accesses",
    "afforded", "affords", "furnished", "furnishes", "formation",
}


def _normalise(value: Any) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    text = _SOURCE_SUFFIX_RE.sub("", text)
    text = _CITATION_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip(" -;,")


def display_word_count(text: Any) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[./'-][A-Za-z0-9]+)*%?", str(text or "")))


def display_text_is_within_budget(
    text: Any,
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> bool:
    cleaned = _normalise(text)
    return bool(cleaned) and len(cleaned) <= max_chars and display_word_count(cleaned) <= max_words


def compact_claim_for_display(
    claim: Any,
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Create a bounded extractive display sentence without changing values."""

    cleaned = _normalise(claim)
    if not cleaned:
        return ""
    sentences = [part.strip(" -;,") for part in re.split(r"(?<=[.!?;])\s+", cleaned) if part.strip()]
    clauses: list[str] = []
    for sentence in sentences or [cleaned]:
        pieces = re.split(
            r"\s*[;:]\s*|,\s+(?=(?:which|whereas|while|but|and|giving|affording|yielding)\b)",
            sentence,
            flags=re.I,
        )
        clauses.extend(piece.strip(" -;,") for piece in pieces if piece.strip(" -;,"))

    def _score(text: str) -> tuple[int, int, int]:
        return (
            2 if _METRIC_RE.search(text) else 0,
            1 if _RESULT_RE.search(text) else 0,
            -display_word_count(text),
        )

    selected = max(clauses or [cleaned], key=_score)
    words = selected.split()
    if len(words) > max_words:
        metric_match = _METRIC_RE.search(selected)
        if metric_match:
            metric_word = len(selected[: metric_match.start()].split())
            start = max(0, min(metric_word - max_words // 2, len(words) - max_words))
            words = words[start : start + max_words]
        else:
            words = words[:max_words]
    while words and words[-1].strip(".,;:").casefold() in _DANGLING_END:
        words.pop()
    compact = " ".join(words).strip(" -;,")
    while compact and len(compact) > max_chars and len(words) > 1:
        words.pop()
        while words and words[-1].strip(".,;:").casefold() in _DANGLING_END:
            words.pop()
        compact = " ".join(words).strip(" -;,")
    return compact.rstrip(".;,")


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _compact_list(
    values: Any,
    *,
    limit: int,
    max_words: int,
    max_chars: int,
) -> list[str]:
    output: list[str] = []
    for value in values if isinstance(values, list) else []:
        compact = compact_claim_for_display(value, max_words=max_words, max_chars=max_chars)
        if compact and compact not in output:
            output.append(compact)
        if len(output) >= limit:
            break
    return output


def _semantic_tokens(value: Any) -> set[str]:
    """Return a small display-level fingerprint for cross-region de-duplication."""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9]+", _normalise(value).casefold()):
        if raw in _SEMANTIC_STOPWORDS or len(raw) == 1:
            continue
        token = raw[:-1] if len(raw) > 5 and raw.endswith("s") and not raw.endswith("ss") else raw
        tokens.add(token)
    return tokens


def display_texts_are_near_duplicates(left: Any, right: Any) -> bool:
    """Whether two short overview statements communicate the same pixel-level fact."""
    left_text = _normalise(left).casefold()
    right_text = _normalise(right).casefold()
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    # Different quantities or polarity are different claims, not duplicates.
    left_numbers = set(re.findall(r"\d+(?:\.\d+)?", left_text))
    right_numbers = set(re.findall(r"\d+(?:\.\d+)?", right_text))
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return False
    if bool(re.search(r"\b(?:not|no|without|fails|failed)\b", left_text)) != bool(re.search(r"\b(?:not|no|without|fails|failed)\b", right_text)):
        return False
    # Equal metrics from different experiments are not the same fact.
    left_tokens = _semantic_tokens(left_text)
    right_tokens = _semantic_tokens(right_text)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens.intersection(right_tokens))
    containment = overlap / min(len(left_tokens), len(right_tokens))
    jaccard = overlap / len(left_tokens.union(right_tokens))
    return overlap >= 2 and (containment >= 0.6 or jaccard >= 0.5)


def _distinct_across_regions(
    values: Iterable[str], used: list[str]
) -> tuple[list[str], int]:
    output: list[str] = []
    omitted = 0
    for value in values:
        if any(display_texts_are_near_duplicates(value, prior) for prior in used + output):
            omitted += 1
            continue
        output.append(value)
    used.extend(output)
    return output, omitted


def _mapped_finding_indices(
    value: Any,
    *,
    module_label: str,
    finding_count: int,
) -> list[int]:
    """Read model-provided 1-based finding assignments for one exact module."""
    target = _normalise(module_label).casefold()
    for row in _rows(value):
        if _normalise(row.get("module")).casefold() != target:
            continue
        output: list[int] = []
        for raw_index in row.get("finding_indices") or []:
            try:
                index = int(raw_index) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= index < finding_count and index not in output:
                output.append(index)
        return output
    return []


def build_overview_display_contract(
    *,
    modules: Iterable[Any],
    argument_execution: Mapping[str, Any] | None,
    evidence_bindings: Mapping[str, Any] | None,
    content_pack: Mapping[str, Any] | None = None,
    max_modules: int = DEFAULT_MAX_MODULES,
    max_claims_per_module: int = DEFAULT_MAX_CLAIMS_PER_MODULE,
    max_words: int = DEFAULT_MAX_WORDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Build compact visible text while retaining stable evidence identifiers.

    Older projects may not provide an argument-execution contract. In that
    compatibility case, already-grounded content-pack findings are assigned to
    modules by an explicit model mapping when available. Unassigned findings
    remain visible as separate key findings instead of being silently dropped
    or attributed to the wrong module.
    """

    execution = argument_execution if isinstance(argument_execution, Mapping) else {}
    bindings = evidence_bindings if isinstance(evidence_bindings, Mapping) else {}
    pack = content_pack if isinstance(content_pack, Mapping) else {}
    fallback_findings = _compact_list(
        pack.get("key_findings"),
        limit=max_modules * max_claims_per_module,
        max_words=max_words,
        max_chars=max_chars,
    )
    sections = {
        str(row.get("section_id") or ""): row
        for row in _rows(execution.get("sections"))
        if str(row.get("section_id") or "")
    }
    output_modules: list[dict[str, Any]] = []
    omitted_claims = 0
    used_finding_indices: set[int] = set()
    seen_labels: set[str] = set()
    used_claim_ids: set[str] = set()
    used_module_text: list[str] = []
    for raw_label in modules:
        label = _normalise(raw_label)
        if not label or label.casefold() in seen_labels or len(output_modules) >= max_modules:
            continue
        seen_labels.add(label.casefold())
        binding = bindings.get(raw_label) or bindings.get(label) or {}
        binding = binding if isinstance(binding, Mapping) else {}
        section = sections.get(str(binding.get("section_id") or ""), {})
        claims = _rows(section.get("claims"))
        items: list[dict[str, Any]] = []
        summaries = pack.get("module_summaries")
        if isinstance(summaries, Mapping):
            summary = str(summaries.get(str(binding.get("section_id") or "")) or "")
            if summary and not display_text_is_within_budget(summary, max_words=max_words, max_chars=max_chars):
                raise ValueError("Validated overview summary exceeds display budget; rewrite before rendering.")
            if summary and not any(display_texts_are_near_duplicates(summary, old) for old in used_module_text):
                items.append({"text": summary, "claim_id": "", "paper_ids": [], "binding_level": "validated_section_summary"})
                used_module_text.append(summary)
        for claim in ([] if isinstance(summaries, Mapping) else claims[:max_claims_per_module]):
            display_text = compact_claim_for_display(
                claim.get("claim"), max_words=max_words, max_chars=max_chars
            )
            if not display_text:
                continue
            claim_id = str(claim.get("claim_id") or "")
            if ((claim_id and claim_id in used_claim_ids)
                    or any(display_texts_are_near_duplicates(display_text, old) for old in used_module_text)):
                continue
            used_claim_ids.add(claim_id)
            used_module_text.append(display_text)
            items.append(
                {
                    "text": display_text,
                    "claim_id": str(claim.get("claim_id") or ""),
                    "paper_ids": [str(value) for value in claim.get("paper_ids") or []][:3],
                    "binding_level": str(claim.get("binding_level") or ""),
                }
            )
        mapped_indices = _mapped_finding_indices(
            pack.get("module_findings"),
            module_label=label,
            finding_count=len(fallback_findings),
        )
        # Structured source claims remain authoritative. Fill remaining visual
        # capacity only with findings that the model explicitly assigned to
        # this exact module and that do not repeat an existing claim.
        for finding_index in mapped_indices:
            if len(items) >= max_claims_per_module:
                break
            finding = fallback_findings[finding_index]
            used_finding_indices.add(finding_index)
            if any(display_texts_are_near_duplicates(finding, old) for old in used_module_text):
                continue
            used_module_text.append(finding)
            items.append(
                {
                    "text": finding,
                    "claim_id": "",
                    "paper_ids": [],
                    "binding_level": "content_pack_module_mapping",
                }
            )
        # Unmapped findings stay unassigned: list position is not scientific attribution.
        omitted_claims += max(0, len(claims) - len(items))
        output_modules.append({"label": label, "items": items})
    visible_claims = [
        str(item.get("text") or "")
        for module in output_modules
        for item in _rows(module.get("items"))
        if str(item.get("text") or "").strip()
    ]
    unassigned_candidates = [
        finding
        for index, finding in enumerate(fallback_findings)
        if index not in used_finding_indices
    ]
    used_visible = list(visible_claims)
    distinct_unassigned, unassigned_duplicates = _distinct_across_regions(
        unassigned_candidates, used_visible
    )
    unassigned_findings = distinct_unassigned[:DEFAULT_MAX_UNASSIGNED_FINDINGS]
    unassigned_overflow = max(
        0, len(distinct_unassigned) - DEFAULT_MAX_UNASSIGNED_FINDINGS
    )
    # The helper considered every distinct candidate used. Reset the visible
    # fingerprint after applying the display cap so hidden overflow cannot
    # suppress a useful cross-cutting or take-home statement.
    used_visible = list(visible_claims) + list(unassigned_findings)
    cross_candidates = _compact_list(
        pack.get("cross_cutting"), limit=4, max_words=4, max_chars=48
    )
    take_home_candidates = _compact_list(
        pack.get("take_home"), limit=4, max_words=6, max_chars=60
    )
    cross_cutting, cross_duplicates = _distinct_across_regions(
        cross_candidates, used_visible
    )
    take_home, take_home_duplicates = _distinct_across_regions(
        take_home_candidates, used_visible
    )
    duplicate_omissions = (
        unassigned_duplicates + cross_duplicates + take_home_duplicates
    )
    return {
        "schema_version": OVERVIEW_DISPLAY_SCHEMA_VERSION,
        "modules": output_modules,
        "unassigned_findings": unassigned_findings,
        "cross_cutting": cross_cutting,
        "take_home": take_home,
        "visual_budget": {
            "max_modules": max_modules,
            "max_claims_per_module": max_claims_per_module,
            "max_words_per_claim": max_words,
            "max_chars_per_claim": max_chars,
        },
        "omitted_claim_count": omitted_claims + unassigned_overflow,
        "omitted_finding_count": unassigned_overflow,
        "unassigned_finding_count": len(unassigned_findings),
        "omitted_duplicate_count": duplicate_omissions,
        "audit_note": (
            "Full claims and source passages remain outside the pixels; only bounded extracts are visible."
        ),
    }


def display_contract_prompt_rows(contract: Mapping[str, Any] | None) -> str:
    """Serialize only approved visible strings, never full source claims."""

    if not isinstance(contract, Mapping):
        return ""
    lines = [
        "OVERVIEW DISPLAY CONTRACT (visible card copy; render exactly and do not expand):",
        "Each module below has at most TWO distinct evidence cards. Render every Card statement exactly once ",
        "in its own module; never copy it into another card or invent additional role rows.",
    ]
    has_item = False
    for module in _rows(contract.get("modules")):
        label = _normalise(module.get("label"))
        if not label:
            continue
        lines.append(f'Module: "{label}"')
        items = _rows(module.get("items"))
        if not items:
            lines.append("  No visible claim; show the module label only.")
            continue
        for item in items[:2]:
            text = _normalise(item.get("text"))
            if text:
                has_item = True
                lines.append(f'  Card statement: "{text}"')
    if not has_item:
        lines.append("No compact claim text is available; do not quote manuscript paragraphs.")
    lines.append(
        "Never print paper IDs, source-study lists, evidence excerpts, or result-context JSON inside the figure."
    )
    return "\n".join(lines)
