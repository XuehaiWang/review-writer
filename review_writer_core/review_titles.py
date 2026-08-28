"""Deterministic, discipline-neutral titles for generated review artifacts."""

from __future__ import annotations

import re
from typing import Any


REVIEW_REQUEST_RE = re.compile(
    r"\b(?:please\s+)?(?:write|prepare|provide|generate|create)\s+(?:an?\s+)?review\b",
    re.IGNORECASE,
)
OVERVIEW_INTERNAL_RESIDUE_RE = re.compile(
    r"(?:please\s+write\s+(?:an?\s+)?review|"
    r"module[-_ ]cards?|crosscut[-_ ]sidebar|modern[-_ ]survey|"
    r"review[-_ ]writer|(?:system|user)?\s*prompt\s*:|"
    r"\b(?:reaction_type|group_by|layout_type|taxonomy_profile|"
    r"catalyst_or_method|overview_axis_contract)\b)",
    re.IGNORECASE,
)
TITLE_SMALL_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "via",
        "with",
    }
)


def publication_title_case(value: Any) -> str:
    """Apply restrained academic title case while preserving abbreviations."""
    words = str(value or "").split()
    result: list[str] = []
    for index, raw_word in enumerate(words):
        prefix = ""
        suffix = ""
        word = raw_word
        while word and word[0] in "([{'\"“‘":
            prefix += word[0]
            word = word[1:]
        while word and word[-1] in ")]}.,:;!?\"”’":
            suffix = word[-1] + suffix
            word = word[:-1]
        low = word.casefold()
        if not word:
            converted = word
        elif word.isupper() or re.fullmatch(r"[A-Z]+\d*", word):
            converted = word
        elif low in TITLE_SMALL_WORDS and index > 0:
            converted = low
        else:
            converted = word[:1].upper() + word[1:].lower()
        result.append(prefix + converted + suffix)
    return " ".join(result)


def topic_subject(raw_topic: Any) -> str:
    """Extract a scientific subject from a Topic instruction paragraph."""
    raw = " ".join(str(raw_topic or "").replace("\n", " ").split()).strip()
    if not raw:
        return "Review Overview"
    quoted = re.search(r"[\"“‘]([^\"”’]{3,180})[\"”’]", raw)
    subject = quoted.group(1).strip() if quoted else raw
    if not quoted:
        subject = re.sub(
            r"^.*?\breview\s+(?:on|of|about)\s+(?:the\s+topic\s+)?",
            "",
            subject,
            count=1,
            flags=re.IGNORECASE,
        )
        subject = re.sub(
            r"^.*?\btopic\s*[:：]?\s*", "", subject, count=1, flags=re.IGNORECASE
        )
        subject = re.split(
            r"\s*(?:[,;.]\s*)?(?:focusing\s+on|with\s+(?:a\s+)?focus\s+on|"
            r"categorized\s+by|organize(?:d)?\s+(?:the\s+review\s+)?by|"
            r"covering|with\s+emphasis\s+on|separately\s+discuss)\b",
            subject,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
    # Convert slug-like lowercase chains while preserving scientific bonds and
    # established forms such as C–H, Cu-catalyzed, and α-allenes.
    subject = re.sub(r"(?<=[a-z]{2})[-‐‑–—](?=[a-z]{2})", " ", subject)
    subject = re.sub(r"\s+", " ", subject).strip(" \t,.;:—–-")
    subject = re.sub(
        r"^syntheses\s+of\s+the\s+", "Synthesis of ", subject, flags=re.IGNORECASE
    )
    subject = re.sub(
        r"^syntheses\s+of\s+", "Synthesis of ", subject, flags=re.IGNORECASE
    )
    return publication_title_case(subject or "Review Overview")


def build_publication_review_title(
    raw_topic: Any,
    *,
    manuscript_title: Any = "",
    max_chars: int = 110,
) -> str:
    """Build a concise title without copying a full search instruction.

    A genuine manuscript title is retained. If it merely repeats the quoted
    search subject while the Topic contains instructions, the organizational
    dimensions are appended so the result functions as a publication title.
    """
    raw = " ".join(str(raw_topic or "").split()).strip()
    candidate = " ".join(str(manuscript_title or "").split()).strip("# \t")
    candidate_is_title = bool(
        candidate
        and not REVIEW_REQUEST_RE.search(candidate)
        and len(candidate) <= 140
        and len(candidate.split()) <= 20
    )
    raw_subject = topic_subject(raw)
    if candidate_is_title:
        normalized_candidate = topic_subject(candidate)
        repeats_instruction_subject = bool(
            REVIEW_REQUEST_RE.search(raw)
            and normalized_candidate.casefold() == raw_subject.casefold()
        )
        if not repeats_instruction_subject:
            return normalized_candidate[:max_chars].rstrip(" ,;:-")
        subject = normalized_candidate
    else:
        subject = raw_subject

    raw_lower = raw.casefold()
    dimensions: list[str] = []
    dimension_signals = (
        (r"reaction\s+(?:type|class|mode)s?", "Reaction Classes"),
        (r"cataly(?:tic|st|sis)|promot(?:er|ing)", "Catalytic Strategies"),
        (r"substrate\s+(?:class|scope)|different\s+substrates", "Substrate Scope"),
        (r"mechanis", "Mechanistic Insights"),
        (r"application", "Synthetic Applications"),
        (
            r"enantioselect|asymmetric|stereoselect|\bchiral\b|\bracemic\b",
            "Stereochemical Control",
        ),
    )
    subject_lower = subject.casefold()
    for pattern, label in dimension_signals:
        if re.search(pattern, raw_lower) and label.casefold() not in subject_lower:
            dimensions.append(label)
        if len(dimensions) == 2:
            break
    if dimensions and ":" not in subject:
        candidate_with_scope = f"{subject}: {' and '.join(dimensions)}"
        if len(candidate_with_scope) <= max_chars:
            subject = candidate_with_scope
        elif len(dimensions) > 1:
            candidate_with_scope = f"{subject}: {dimensions[0]}"
            if len(candidate_with_scope) <= max_chars:
                subject = candidate_with_scope
    return subject[:max_chars].rstrip(" ,;:-")


def generated_title_is_acceptable(value: Any) -> bool:
    """Whether an LLM-proposed title is safe to publish as front matter."""
    title = " ".join(str(value or "").split()).strip("# \t")
    return bool(
        title
        and not REVIEW_REQUEST_RE.search(title)
        and len(title) <= 140
        and 3 <= len(title.split()) <= 20
        and not title.endswith(("?", "。"))
    )


def generated_title_needs_rewrite(value: Any, raw_topic: Any = "") -> bool:
    """Detect generated/default titles that still expose the complete Topic."""
    title = " ".join(str(value or "").split()).strip("# \t")
    raw = " ".join(str(raw_topic or "").split()).strip()
    if not generated_title_is_acceptable(title):
        return True
    return bool(raw and title.casefold() == raw.casefold() and len(raw) > 70)


def _overview_dimension_phrases(
    raw_topic: Any,
    *,
    group_by: Any = (),
    classification_rule: Any = "",
    has_chirality: bool = False,
    has_reaction_focus: bool = False,
) -> list[str]:
    """Return publication prose for the scientific axes shown in an overview.

    The mapping is deliberately discipline-neutral. Internal enum values are
    interpreted here but are never copied into a manuscript caption.
    """

    raw = " ".join(str(raw_topic or "").split()).casefold()
    rule = " ".join(str(classification_rule or "").split()).casefold()
    values = group_by if isinstance(group_by, (list, tuple, set)) else [group_by]
    axes = {
        str(value or "").strip().casefold()
        for value in values
        if str(value or "").strip()
    }
    phrases: list[str] = []

    def add(value: str) -> None:
        if value and value not in phrases:
            phrases.append(value)

    if (
        "reaction_type" in axes
        or "reaction_strategy" in axes
        or "reaction" in rule
        or re.search(r"reaction\s+(?:type|class|mode)s?", raw)
    ):
        add("reaction class")
    if (
        "catalyst_or_method" in axes
        or "catalyst" in rule
        or "method" in rule
        or re.search(r"cataly(?:tic|st|sis)|promot(?:er|ing)", raw)
    ):
        add("catalytic or promoting system")
    if (
        "substrate" in axes
        or "substrate_classes" in axes
        or "substrate" in rule
        or re.search(r"substrate\s+(?:class|scope)|different\s+substrates", raw)
    ):
        add("substrate scope")
    if (
        "product" in axes
        or "product" in rule
        or re.search(
            r"product\s+(?:class|scope)|\b(?:mono|di|tri|tetra)[ -]?substitut",
            raw,
        )
    ):
        add("product class")
    if has_chirality or re.search(
        r"enantioselect|asymmetric|stereoselect|stereochem|\bchiral\b|\bracemic\b",
        raw,
    ):
        add("stereochemical control")
    if "mechanis" in raw or "mechanis" in rule:
        add("mechanistic evidence")
    if "application" in raw or "application" in rule:
        add("synthetic applications")
    if not phrases and has_reaction_focus:
        add("transformation strategy")
    if not phrases:
        add("major approaches")
        add("evidence-supported comparisons")
    return phrases[:5]


def _joined_academic_list(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def build_publication_overview_text(
    raw_topic: Any,
    *,
    manuscript_title: Any = "",
    group_by: Any = (),
    classification_rule: Any = "",
    has_chirality: bool = False,
    has_reaction_focus: bool = False,
) -> dict[str, Any]:
    """Build a concise, prompt-free caption model for a review overview.

    Overview captions describe what the figure organizes; they do not expose
    the user's request, a rendering template, query-plan enums, or agent state.
    """

    title = build_publication_review_title(
        raw_topic,
        manuscript_title=manuscript_title,
        max_chars=110,
    )
    if title.casefold() == "review overview":
        title = "Conceptual overview of the reviewed field"
    dimensions = _overview_dimension_phrases(
        raw_topic,
        group_by=group_by,
        classification_rule=classification_rule,
        has_chirality=has_chirality,
        has_reaction_focus=has_reaction_focus,
    )
    subtitle = (
        "The figure organizes the reviewed literature by "
        f"{_joined_academic_list(dimensions)}."
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "labels": [],
        "caption_schema": "publication-overview-v1",
    }


def overview_text_needs_rewrite(value: Any, raw_topic: Any = "") -> bool:
    """Detect prompt, template, or workflow residue in an overview caption."""

    if not isinstance(value, dict):
        return True
    title = " ".join(str(value.get("title") or "").split()).strip()
    subtitle = " ".join(str(value.get("subtitle") or "").split()).strip()
    labels = [
        " ".join(str(item).split()).strip()
        for item in value.get("labels") or []
        if str(item).strip()
    ]
    combined = " ".join([title, subtitle, *labels]).strip()
    raw = " ".join(str(raw_topic or "").split()).strip()
    return bool(
        not title
        or len(title) > 150
        or OVERVIEW_INTERNAL_RESIDUE_RE.search(combined)
        or (raw and len(raw) > 70 and raw.casefold() in combined.casefold())
    )
