"""Conservative title recovery and bounded front-matter evidence helpers.

MinerU sometimes uses an extraction folder name such as ``p001`` as the
document title while the real article title remains an H2 Markdown heading.
The helpers in this module deliberately inspect only the beginning of the
document and reject common section/boilerplate headings.  They are shared by
Library metadata preparation and Discovery so the two stages do not implement
different title heuristics.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping


_MARKDOWN_HEADING = re.compile(r"(?m)^\s{0,3}(#{1,3})\s+(.+?)\s*$")
_PLACEHOLDER_TITLE = re.compile(
    r"^(?:"
    r"p(?:age)?[-_ ]?\d{1,6}|part[-_ ]?\d{1,6}|"
    r"document[-_ ]?\d*|article[-_ ]?\d*|main(?:\s+document)?|"
    r"full[-_ ]?text|untitled(?:\s+document)?"
    r")$",
    re.I,
)
_BOILERPLATE_HEADINGS = (
    re.compile(r"^(?:working\s+with\s+)?hazardous\s+chemicals?$", re.I),
    re.compile(r"^(?:general\s+)?(?:experimental\s+)?procedures?$", re.I),
    re.compile(r"^(?:abstract|summary|keywords?|introduction|background)$", re.I),
    re.compile(r"^(?:results?(?:\s+and\s+discussion)?|discussion|conclusions?)$", re.I),
    re.compile(r"^(?:notes?|references?|bibliography|supporting\s+information)$", re.I),
    re.compile(r"^(?:article\s+info(?:rmation)?|graphical\s+abstract)$", re.I),
)


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, Mapping) and "value" in value else value


def clean_markdown_heading(value: Any) -> str:
    """Return visible heading text without Markdown/HTML presentation residue."""

    text = html.unescape(str(value or ""))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\$([^$]+)\$", r"\1", text)
    text = re.sub(r"(?:\\[A-Za-z]+|[†‡*]+|<sup>.*?</sup>)", " ", text, flags=re.I)
    text = text.replace("\u00ad", "")
    # PDF text layers often split chemical names with typographic hyphens
    # (for example ``2,3-‐butadien-‐1-‐ol``). Canonicalize them before title
    # validation and scientific-family matching.
    text = re.sub(r"[-‐‑‒–—﹘﹣－]+", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n#|_")
    return text


def looks_like_placeholder_title(value: Any) -> bool:
    """Identify extraction slugs and generic PDF titles, including ``p001``."""

    title = clean_markdown_heading(_field_value(value))
    if not title:
        return True
    if _PLACEHOLDER_TITLE.fullmatch(title):
        return True
    lowered = title.casefold()
    return lowered.startswith("doi:") or lowered in {
        "microsoft word",
        "main document",
        "article",
    }


def title_field_needs_repair(field: Any) -> bool:
    """Return whether a stored title is absent or is a low-confidence fallback."""

    if not isinstance(field, Mapping):
        return looks_like_placeholder_title(field)
    value = field.get("value")
    source = str(field.get("source") or "").strip().casefold()
    try:
        confidence = float(field.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if looks_like_placeholder_title(value):
        return True
    return source in {"slug_fallback", "filename_fallback"} and confidence < 0.6


def _title_heading_allowed(value: str) -> bool:
    if not 8 <= len(value) <= 320 or looks_like_placeholder_title(value):
        return False
    compact = re.sub(r"[\s:.-]+", " ", value).strip()
    if any(pattern.fullmatch(compact) for pattern in _BOILERPLATE_HEADINGS):
        return False
    if re.fullmatch(r"(?:volume|vol|issue|page|chapter)\s+\w+", compact, re.I):
        return False
    if re.fullmatch(r"[\W\d_]+", compact):
        return False
    return True


def extract_markdown_title(markdown: Any, *, limit: int = 30_000) -> dict[str, Any] | None:
    """Recover the first credible H1-H3 title from bounded MinerU Markdown."""

    front = str(markdown or "")[: max(1, int(limit))]
    for match in _MARKDOWN_HEADING.finditer(front):
        value = clean_markdown_heading(match.group(2))
        if not _title_heading_allowed(value):
            continue
        level = len(match.group(1))
        confidence = {1: 0.9, 2: 0.86, 3: 0.8}[level]
        return {
            "value": value,
            "source": f"mineru_markdown_h{level}_front_matter",
            "confidence": confidence,
            "human_checked": False,
        }
    return None


def _section_text(markdown: str, names: Iterable[str], *, max_chars: int) -> str:
    names_pattern = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"(?ims)^\s*#{{1,4}}\s*(?:{names_pattern})\s*$\s*(.+?)(?=^\s*#{{1,4}}\s+|\Z)",
        markdown,
    )
    return match.group(1)[:max_chars] if match else ""


def bounded_admission_text(
    markdown: Any,
    *,
    scientific_facts: Iterable[Any] = (),
    limit: int = 30_000,
) -> dict[str, str]:
    """Build a small evidence package for missing-title/abstract admission.

    The package contains a recovered front-page heading, a bounded
    abstract/introduction window, the first actual procedure/discussion window,
    and already extracted reaction facts.  It never includes references or the
    unrestricted paper body.
    """

    front = str(markdown or "")[: max(1, int(limit))]
    references = re.search(
        r"(?im)^\s*#{1,4}\s*(?:references?|bibliography)\s*$", front
    )
    if references:
        front = front[: references.start()]
    title = extract_markdown_title(front)
    overview = _section_text(front, ("abstract", "introduction", "background"), max_chars=6_000)
    reaction = _section_text(
        front,
        (
            "procedure",
            "general procedure",
            "experimental procedure",
            "results and discussion",
            "discussion",
        ),
        max_chars=8_000,
    )
    fact_texts: list[str] = []
    for fact in scientific_facts:
        if isinstance(fact, Mapping):
            value = (
                fact.get("fact")
                or fact.get("statement")
                or fact.get("text")
                or fact.get("evidence")
            )
        else:
            value = fact
        normalized = " ".join(str(value or "").split()).strip()
        if normalized:
            fact_texts.append(normalized[:1_500])
        if len(fact_texts) >= 12:
            break
    return {
        "title": str((title or {}).get("value") or ""),
        "overview": overview,
        "reaction": reaction,
        "scientific_facts": " ".join(fact_texts),
    }
