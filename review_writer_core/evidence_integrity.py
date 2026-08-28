"""Deterministic anchors for checking realized scientific claims."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


QUANTITATIVE_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:19|20)\d{2}"
    r"|\d+(?:\.\d+)?(?:\s*[–—-]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:%|mol\s*%|°\s*C|K|h|min|s|equiv|eq\.?|M|mM|μM|uM|"
    r"bar|atm|MPa|GPa|mg|g|kg|mmol|mol|mL|μL|uL|L|nm|μm|um|cm)"
    r"|\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
METRIC_VALUE_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d+(?:\.\d+)?(?:\s*[–—-]\s*\d+(?:\.\d+)?)?\s*%?\s*"
    r"(?:yield|conversion|recovery|selectivity|ee|de|er|dr|rr|e\.e\.|d\.e\.)"
    r"|(?:yield|conversion|recovery|selectivity|ee|de|er|dr|rr|e\.e\.|d\.e\.)"
    r"\s*(?:of\s*)?\d+(?:\.\d+)?(?:\s*:\s*\d+(?:\.\d+)?)?\s*%?"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
FORMULA_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?=[A-Za-z0-9()]*[a-z0-9(])"
    r"[A-Z][a-z]?(?:\([A-Za-z0-9+\-]+\)\d*|[A-Z][a-z]?\d*)+"
    r"|[A-Z][a-z]?\([IVX]+\)"
    r")(?![A-Za-z0-9])"
)

LATEX_TEXT_WRAPPER_RE = re.compile(
    r"\\(?:mathrm|mathsf|mathbf|text|operatorname|ce)\s*\{([^{}]*)\}"
)
TECHNICAL_ROLE_SUFFIX_RE = re.compile(
    r"-?(?:mediated|cataly[sz]ed|promoted|assisted|enabled|derived|based)$",
    re.IGNORECASE,
)


def _plain_scientific_markup(value: Any) -> str:
    """Collapse common MinerU/LaTeX representations before anchor matching.

    MinerU can represent the same formula as ``ZnBr2``, ``ZnBr_{2}``, or
    ``\\mathrm{ZnBr}_{2}``.  Evidence validation must compare their visible
    scientific value rather than the extraction markup.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\\%", "%").replace("\\degree", "°")
    text = re.sub(r"\^\s*\{?\s*\\circ\s*\}?", "°", text)
    for _ in range(6):
        collapsed = LATEX_TEXT_WRAPPER_RE.sub(r"\1", text)
        if collapsed == text:
            break
        text = collapsed
    text = re.sub(r"[_^]\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"[_^]\s*([A-Za-z0-9+\-]+)", r"\1", text)
    text = re.sub(r"\\(?:,|;|!|:|\s)", "", text)
    text = re.sub(r"\\[A-Za-z]+", "", text)
    return text.replace("$", "").replace("{", "").replace("}", "")


def normalized_anchor_text(value: Any) -> str:
    text = _plain_scientific_markup(value).casefold()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = re.sub(r"\b([ed])\.\s*([ed])\.", r"\1\2", text)
    return re.sub(r"\s+", "", text)


def quantitative_anchors(value: Any) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(0).strip()
            for pattern in (QUANTITATIVE_ANCHOR_RE, METRIC_VALUE_ANCHOR_RE)
            for match in pattern.finditer(str(value or ""))
        )
    )


def _quantitative_anchor_supported(anchor: str, searchable: str) -> bool:
    normalized = normalized_anchor_text(anchor)
    if normalized in searchable:
        return True
    # Metric prose frequently reverses value/metric order, e.g. ``94-99% de``
    # versus ``very high de (94-99%)`` or ``42% yield`` versus ``yield ... 42%``.
    # Require both the exact numeric/unit component and the metric identity, but
    # do not require their surface word order to be identical.
    numeric_parts = [
        normalized_anchor_text(match.group(0))
        for match in QUANTITATIVE_ANCHOR_RE.finditer(anchor)
    ]
    metric_parts = [
        token.casefold().replace(".", "")
        for token in re.findall(
            r"\b(?:yield|conversion|recovery|selectivity|ee|de|er|dr|rr|e\.e\.|d\.e\.)\b",
            anchor,
            flags=re.IGNORECASE,
        )
    ]
    return bool(
        numeric_parts
        and metric_parts
        and all(part in searchable for part in numeric_parts)
        and all(part in searchable for part in metric_parts)
    )


def technical_entity_anchors(
    value: Any,
    *,
    domain_terms: Iterable[str] = (),
) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or ""))
    folded = text.casefold()
    anchors = [match.group(0).strip() for match in FORMULA_ANCHOR_RE.finditer(text)]
    for raw_term in domain_terms:
        term = " ".join(str(raw_term or "").split()).strip()
        if len(term) >= 3 and term.casefold() in folded:
            anchors.append(term)
    return list(dict.fromkeys(anchors))


def _technical_anchor_supported(anchor: str, searchable: str) -> bool:
    normalized = normalized_anchor_text(anchor)
    if normalized in searchable:
        return True
    # Taxonomy aliases often use adjectival forms such as ``CdI2-mediated``.
    # The role word is prose, not a second scientific entity; the cited chunk
    # only needs to contain the underlying named entity for this anchor check.
    base = TECHNICAL_ROLE_SUFFIX_RE.sub("", normalized)
    return bool(base and base != normalized and base in searchable)


def unsupported_realization_anchors(
    realization: Any,
    evidence_texts: Iterable[Any],
    *,
    domain_terms: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Return realized numerical/entity anchors absent from cited chunks."""

    searchable = normalized_anchor_text(
        " ".join(str(value or "") for value in evidence_texts)
    )
    return {
        "quantitative": [
            anchor
            for anchor in quantitative_anchors(realization)
            if not _quantitative_anchor_supported(anchor, searchable)
        ],
        "technical_entities": [
            anchor
            for anchor in technical_entity_anchors(
                realization, domain_terms=domain_terms
            )
            if not _technical_anchor_supported(anchor, searchable)
        ],
    }
