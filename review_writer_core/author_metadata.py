"""Shared, conservative normalization for publication author bylines.

The functions in this module deliberately prefer an unresolved author field to
publishing publisher chrome, affiliations, dates, or article prose as names.
They are used by MinerU extraction, the bounded bibliography agent, readiness
checks, and final reference rendering so those layers cannot disagree.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any


_HTML_TAG = re.compile(r"<[^>]+>")
_MARKER = re.compile(
    r"(?:\$?\^\{?[^}\s]{1,8}\}?\$?|\\(?:mathrm|mathsf|mathbf)\{[^}]*\}|"
    r"[†‡§¶*]+|(?<=\s)[a-z]\d*(?=\s|$))"
)
_NON_AUTHOR = re.compile(
    r"\b(?:received|revised|accepted|available online|cite this|read online|"
    r"article recommendations?|supporting information|copyright|submitted|"
    r"checked by|edited by|reviewed by|vol(?:ume)?\.?|no\.?|issue|pages?|pp?\.?|"
    r"doi|abstract|keywords?|correspond(?:ing|ence)|affiliations?)\b",
    re.I,
)
_AFFILIATION = re.compile(
    r"\b(?:university|institute|institution|department|laboratory|school|college|"
    r"academy|faculty|hospital|centre|center|research group|postal|road|street|"
    r"avenue|province|china|usa|japan|germany|france|italy|india)\b",
    re.I,
)
_NAME_TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-žΑ-ωА-я一-龥]")


def _plain(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("L€u", "Lü").replace("Lu€", "Lü")
    # Common MinerU rendering of the surname "Ma" followed by a corresponding
    # author star.  Preserve the surname while removing only the marker.
    text = re.sub(
        r"\$?\s*M\s*a\s*\^\s*\{\s*\\star\s*\}\s*\$?",
        "Ma",
        text,
        flags=re.I,
    )
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _MARKER.sub(" ", text)
    text = text.replace("\u00ad", "").replace("\\*", " ")
    return " ".join(text.split()).strip(" ,;|·")


def _raw_values(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return _raw_values(value.get("value"))
    if isinstance(value, (list, tuple, set)):
        return [
            part
            for item in value
            for part in re.split(r"\s+and\s+|\s+&\s+", str(item or ""), flags=re.I)
        ]
    text = str(value or "")
    if not text.strip():
        return []
    # MinerU bylines ordinarily use comma/semicolon/"and" separators.  A
    # canonical list remains preferable because it does not require guessing.
    return re.split(r"\s*;\s*|\s*,\s*|\s+and\s+|\s+&\s+", text, flags=re.I)


def clean_author_names(value: Any) -> list[str]:
    """Return only name-like author values, preserving first-seen order."""

    result: list[str] = []
    for raw in _raw_values(value):
        name = _plain(raw)
        name = re.sub(r"^(?:by|authors?)\s*[:：]?\s*", "", name, flags=re.I)
        name = re.sub(r"^(?:and|&)\s+", "", name, flags=re.I)
        name = re.sub(r"\s+(?:[a-z]|\d{1,3})$", "", name).strip(" ,;|·+*")
        if not 2 <= len(name) <= 100:
            continue
        if _NON_AUTHOR.search(name) or _AFFILIATION.search(name):
            continue
        if not _NAME_TOKEN.search(name) or re.search(r"\d", name):
            continue
        words = name.split()
        if len(words) > 8 or name.count(".") > 6:
            continue
        # Prose/title fragments are much more likely to contain sentence
        # punctuation or verbal boilerplate than a publication byline.
        if any(mark in name for mark in ("?", "!", ":", "=", "/")):
            continue
        key = name.casefold()
        if key not in {item.casefold() for item in result}:
            result.append(name)
    return result


def author_quality_issues(value: Any) -> list[str]:
    """Describe why an author field must not be considered publication-ready."""

    raw_values = _raw_values(value)
    raw_text = " ".join(str(item or "") for item in raw_values).strip()
    source = value.get("value") if isinstance(value, Mapping) else value
    source_items = (
        [str(item or "") for item in source]
        if isinstance(source, (list, tuple, set))
        else [str(source or "")]
    )
    cleaned = clean_author_names(value)
    issues: list[str] = []
    if not cleaned:
        issues.append("authors_missing_or_unreadable")
    if raw_text and (_HTML_TAG.search(raw_text) or _MARKER.search(raw_text)):
        issues.append("authors_contain_markup_or_affiliation_markers")
    if raw_text and (_NON_AUTHOR.search(raw_text) or _AFFILIATION.search(raw_text)):
        issues.append("authors_contain_non_author_text")
    if any(
        re.search(r"\s(?:and|&)\s|\+\s*$|[€�]", item, re.I)
        for item in source_items
    ):
        issues.append("authors_require_normalization")
    if raw_values and len(cleaned) < len([item for item in raw_values if str(item).strip()]):
        issues.append("authors_contain_rejected_items")
    return list(dict.fromkeys(issues))


def authors_are_publication_ready(value: Any) -> bool:
    return not author_quality_issues(value)
