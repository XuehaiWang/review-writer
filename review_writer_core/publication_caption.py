"""Non-blocking normalization for publication-facing figure captions.

MinerU captions are evidence and must remain immutable.  This module only
builds derived display fields for manuscripts, browsers, and exports.  Every
entry point is deliberately fail-open: malformed or unsupported TeX becomes
readable plain text (or the original text as a last resort) and never blocks a
workflow stage.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from review_writer_core.text_safety import make_xml_compatible


CAPTION_NORMALIZATION_VERSION = "publication-caption/3"

# These roles describe what a figure contributes to the review.  They are
# intentionally discipline-neutral; chemistry-specific labels remain accepted
# as legacy aliases, but are not the default for a new project.
ROLE_ALIASES = {
    "mechanism": "mechanism_model",
    "scope": "scope_samples",
    "paper_overview": "conceptual_overview",
}
CANONICAL_ROLES = {
    "workflow",
    "core_transformation",
    "mechanism_model",
    "scope_samples",
    "quantitative_results",
    "comparison_ablation",
    "conceptual_overview",
    "structure_image",
    "unknown",
}

_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mechanism_model", re.compile(r"\b(mechanis(?:m|tic)|catalytic cycle|transition state|energy profile|reaction pathway|proposed pathway|dft)\b", re.I)),
    ("scope_samples", re.compile(r"\b(substrate scope|sample scope|functional.group tolerance|generality|scope of substrates|representative substrates)\b", re.I)),
    ("workflow", re.compile(r"\b(workflow|study design|experimental design|research strategy|screening process|analysis pipeline)\b", re.I)),
    ("comparison_ablation", re.compile(r"\b(comparison|benchmark|ablation|versus|compared with|control experiment)\b", re.I)),
    ("quantitative_results", re.compile(r"\b(plot|curve|correlation|performance|kinetic|time course|quantitative|statistical)\b", re.I)),
    ("structure_image", re.compile(r"\b(crystal structure|x.ray|microscopy|micrograph|sem|tem|morphology|molecular structure)\b", re.I)),
    ("conceptual_overview", re.compile(r"\b(graphical abstract|conceptual overview|overview|schematic illustration|general concept)\b", re.I)),
    ("core_transformation", re.compile(r"\b(reaction|transformation|synthesis|synthetic route|reaction conditions|scheme)\b", re.I)),
)

_ROLE_FALLBACK_CAPTIONS = {
    "workflow": "Study workflow and major analysis steps reported in the source study.",
    "core_transformation": "Core transformation and representative reaction conditions reported in the source study.",
    "mechanism_model": "Proposed mechanistic pathway, key intermediates, or transition states reported in the source study.",
    "scope_samples": "Representative substrate or sample scope and corresponding outcomes reported in the source study.",
    "quantitative_results": "Key quantitative results and trends reported in the source study.",
    "comparison_ablation": "Comparison of representative methods, conditions, or controls reported in the source study.",
    "conceptual_overview": "Conceptual overview of the study and its main research strategy.",
    "structure_image": "Representative structural or imaging result reported in the source study.",
    "unknown": "Representative figure from the cited source study.",
}

_ROLE_ALT_TEXT = {
    "workflow": "Study workflow",
    "core_transformation": "Core transformation",
    "mechanism_model": "Proposed mechanistic pathway",
    "scope_samples": "Representative scope",
    "quantitative_results": "Quantitative result",
    "comparison_ablation": "Method comparison",
    "conceptual_overview": "Study overview",
    "structure_image": "Representative structure or image",
    "unknown": "Source study figure",
}

_HTML_TAG = re.compile(r"<[^>]+>")
_CAPTION_PREFIX = re.compile(
    r"^\s*(?:figure|fig\.?|scheme|table|chart|图|反应式|表)\s*"
    r"(?:[A-Za-z]?\d+[A-Za-z]?|[IVXLC]+)\s*[.:：\-]?\s*",
    re.IGNORECASE,
)
_TEX_COMMAND = re.compile(r"\\([A-Za-z]+)")
_TEX_WRAPPER = re.compile(
    r"\\(?:mathrm|mathsf|mathbf|mathit|text|operatorname|pmb|boldsymbol)"
    r"\s*\{([^{}]*)\}"
)
_SCRIPT_GROUP = {
    "_": re.compile(r"\s*_\s*\{([^{}]+)\}"),
    "^": re.compile(r"\s*\^\s*\{([^{}]+)\}"),
}
_SCRIPT_SINGLE = {
    "_": re.compile(r"\s*_\s*([0-9+\-=()])"),
    "^": re.compile(r"\s*\^\s*([0-9+\-=()])"),
}

# Conservative repairs for OCR word splits observed in chemistry figure
# captions.  These are intentionally curated instead of joining arbitrary
# adjacent words, which could silently alter valid scientific prose.
_OCR_WORD_SPLITS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcycloaddi\s+tion(?:s)?\b", re.IGNORECASE),
)

_SUBSCRIPT = str.maketrans(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
)
_SUPERSCRIPT = str.maketrans(
    "0123456789+-=()in",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ",
)
_KNOWN_COMMANDS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "theta": "θ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Omega": "Ω",
    "cdot": "·",
    "times": "×",
    "pm": "±",
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "neq": "≠",
    "equiv": "≡",
    "rightarrow": "→",
    "leftrightarrow": "↔",
    "prime": "′",
    "circ": "°",
}


@dataclass(frozen=True)
class PublicationCaption:
    source_text: str
    publication_text: str
    plain_text: str
    status: str
    warnings: tuple[str, ...]
    version: str = CAPTION_NORMALIZATION_VERSION
    representative_role: str = "unknown"
    alt_text: str = "Source study figure"
    quality_status: str = "ready"
    quality_warnings: tuple[str, ...] = ()
    word_count: int = 0
    sentence_count: int = 0

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "publication_caption_text": self.publication_text,
            "publication_caption_plain_text": self.plain_text,
            "caption_normalization_status": self.status,
            "caption_normalization_warnings": list(self.warnings),
            "caption_normalization_version": self.version,
            "alt_text": self.alt_text,
            "representative_role": self.representative_role,
            "caption_quality": {
                "status": self.quality_status,
                "warnings": list(self.quality_warnings),
                "word_count": self.word_count,
                "sentence_count": self.sentence_count,
                "human_modified": False,
            },
        }


def canonical_figure_role(value: Any) -> str:
    role = str(value or "").strip().casefold()
    role = ROLE_ALIASES.get(role, role)
    return role if role in CANONICAL_ROLES else "unknown"


def infer_figure_role(*values: Any, preferred: Any = "") -> str:
    """Infer a broad semantic role without claiming to understand the image."""

    explicit = canonical_figure_role(preferred)
    if explicit != "unknown":
        return explicit
    searchable = " ".join(str(value or "") for value in values)
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(searchable):
            return role
    return "unknown"


def _caption_quality(value: str, warnings: list[str]) -> tuple[str, list[str], int, int]:
    words = re.findall(r"\b[\w'’-]+\b", value, flags=re.UNICODE)
    sentences = [part for part in re.split(r"(?<=[.!?。！？])\s+", value) if part.strip()]
    quality_warnings = list(warnings)
    if len(words) > 100:
        quality_warnings.append("caption_too_long")
    if len(sentences) > 3:
        quality_warnings.append("too_many_sentences")
    if re.search(r"(?:\b(?:and|or|with|which|that|this|the|of|to|for|by)|[,;:—-])\s*$", value, re.I):
        quality_warnings.append("possibly_incomplete_ending")
    pairs = (("(", ")"), ("[", "]"))
    if any(value.count(left) != value.count(right) for left, right in pairs):
        quality_warnings.append("unbalanced_delimiter")
    severe = {
        "caption_too_long",
        "too_many_sentences",
        "possibly_incomplete_ending",
        "unbalanced_delimiter",
    }
    status = (
        "needs_review"
        if any(item in severe for item in quality_warnings)
        else "ready_with_normalization"
        if quality_warnings
        else "ready"
    )
    return status, list(dict.fromkeys(quality_warnings)), len(words), len(sentences)


def _script_text(value: str, marker: str) -> str:
    compact = re.sub(r"\s+", "", value)
    table = _SUBSCRIPT if marker == "_" else _SUPERSCRIPT
    translated = compact.translate(table)
    # ``str.translate`` leaves unsupported characters unchanged.  Mixed
    # output such as N² is still more readable than raw TeX and preserves all
    # source characters.
    return translated


def _readable_tex(value: str) -> tuple[str, list[str]]:
    text = value
    warnings: list[str] = []
    if text.count("$") % 2:
        warnings.append("unbalanced_math_delimiter")

    # Unwrap nested typography commands from the inside out.
    for _ in range(8):
        updated = _TEX_WRAPPER.sub(lambda match: match.group(1), text)
        if updated == text:
            break
        text = updated

    known_commands = set(_KNOWN_COMMANDS)
    wrapper_commands = {
        "mathrm", "mathsf", "mathbf", "mathit", "text", "operatorname",
        "pmb", "boldsymbol",
    }
    unknown = sorted(
        {
            match.group(1)
            for match in _TEX_COMMAND.finditer(text)
            if match.group(1) not in known_commands | wrapper_commands
        }
    )
    if unknown:
        warnings.append("unsupported_tex_commands:" + ",".join(unknown))

    text = _TEX_COMMAND.sub(
        lambda match: _KNOWN_COMMANDS.get(match.group(1), match.group(1)),
        text,
    )
    for marker in ("^", "_"):
        pattern = _SCRIPT_GROUP[marker]
        for _ in range(8):
            updated = pattern.sub(
                lambda match, token=marker: _script_text(match.group(1), token),
                text,
            )
            if updated == text:
                break
            text = updated
        text = _SCRIPT_SINGLE[marker].sub(
            lambda match, token=marker: _script_text(match.group(1), token),
            text,
        )

    text = re.sub(r"\\[,;:! ]", " ", text)
    text = text.replace("\\%", "%")
    text = text.replace("$", "").replace("{", "").replace("}", "")
    return text, warnings


def _typographic_cleanup(value: str) -> str:
    text = value
    text = re.sub(r"\s*([·×])\s*", r"\1", text)
    text = re.sub(r"(?<!\w)-(?=\d)", "−", text)
    text = text.replace("(-)", "(−)")
    text = re.sub(r"(\([RS]\)-\([−+]\))\s+-", r"\1-", text)
    text = re.sub(r"\s+([,;:.)])", r"\1", text)
    text = re.sub(r"([,;:])(?=[A-Za-z0-9(])", r"\1 ", text)
    text = re.sub(r"([−+]?\d+(?:\.\d+)?)\s*°\s*([CFK])\b", r"\1 °\2", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def repair_publication_ocr_splits(value: Any) -> str:
    """Repair a small allowlist of unambiguous scientific OCR word splits."""

    text = str(value or "")
    for pattern in _OCR_WORD_SPLITS:
        text = pattern.sub(
            lambda match: re.sub(r"\s+", "", match.group(0)),
            text,
        )
    return text


def normalize_publication_caption(
    value: Any,
    *,
    representative_role: Any = "",
    source_label: Any = "",
    context_title: Any = "",
) -> PublicationCaption:
    """Return a safe derived caption without modifying the source evidence."""

    source = str(value or "").strip()
    role = infer_figure_role(
        source, source_label, context_title, preferred=representative_role
    )
    if not source:
        fallback = _ROLE_FALLBACK_CAPTIONS[role]
        return PublicationCaption(
            "", fallback, fallback, "cleaned", (),
            representative_role=role,
            alt_text=_ROLE_ALT_TEXT[role],
            quality_status="generated_fallback",
            quality_warnings=("source_caption_missing",),
            word_count=len(fallback.split()),
            sentence_count=1,
        )
    try:
        safe, replaced = make_xml_compatible(source)
        warnings: list[str] = []
        if replaced:
            warnings.append(f"xml_control_characters_replaced:{replaced}")
        visible = html.unescape(_HTML_TAG.sub(" ", safe))
        visible, tex_warnings = _readable_tex(visible)
        warnings.extend(tex_warnings)
        visible = _CAPTION_PREFIX.sub("", visible)
        visible = repair_publication_ocr_splits(visible)
        visible = _typographic_cleanup(visible).strip().rstrip(".")
        status = "partial" if warnings else "cleaned"
        quality_status, quality_warnings, word_count, sentence_count = (
            _caption_quality(visible, warnings)
        )
        publication_text = visible
        # Never cut a scientific caption in the middle of a sentence.  When
        # the source is abnormally long/noisy, use an explicitly generic,
        # role-aware publication caption and retain the immutable source text
        # for evidence inspection.
        if quality_status == "needs_review":
            publication_text = _ROLE_FALLBACK_CAPTIONS[role]
            quality_status = "generated_fallback"
        return PublicationCaption(
            source_text=source,
            publication_text=publication_text,
            plain_text=publication_text,
            status=status,
            warnings=tuple(warnings),
            representative_role=role,
            alt_text=_ROLE_ALT_TEXT[role],
            quality_status=quality_status,
            quality_warnings=tuple(quality_warnings),
            word_count=word_count,
            sentence_count=sentence_count,
        )
    except Exception as exc:  # pragma: no cover - defensive workflow boundary
        fallback, _ = make_xml_compatible(source)
        return PublicationCaption(
            source_text=source,
            publication_text=fallback,
            plain_text=fallback,
            status="passthrough",
            warnings=(f"normalization_failed:{type(exc).__name__}",),
            representative_role=role,
            alt_text=_ROLE_ALT_TEXT[role],
            quality_status="needs_review",
            quality_warnings=("normalization_failed",),
            word_count=len(fallback.split()),
            sentence_count=1 if fallback else 0,
        )


def publication_caption_fields(
    value: Any,
    *,
    representative_role: Any = "",
    source_label: Any = "",
    context_title: Any = "",
) -> dict[str, Any]:
    """Convenience helper for figure manifests."""

    return normalize_publication_caption(
        value,
        representative_role=representative_role,
        source_label=source_label,
        context_title=context_title,
    ).manifest_fields()


def figure_rights_fields(source: dict[str, Any] | None) -> dict[str, Any]:
    """Return conservative rights fields for a Figure Manifest row.

    Attribution is not treated as permission.  A row is marked
    ``license_verified`` only when an explicit verification flag and a stored
    permission or licence record are both present.
    """

    row = dict(source or {})
    origin = str(
        row.get("figure_origin")
        or row.get("source_relationship")
        or row.get("render_mode")
        or ""
    ).casefold()
    if origin in {"original_synthesis", "generated_overview", "overview"} or bool(
        row.get("original_synthesis")
    ):
        return {
            "rights_status": "original_synthesis",
            "source_relationship": "original_synthesis",
            "permission_status": "not_required_for_source_reuse",
            "permission_record": None,
        }

    permission_record = str(
        row.get("permission_record")
        or row.get("permission_record_id")
        or row.get("license_record")
        or ""
    ).strip()
    if row.get("license_verified") is True and permission_record:
        return {
            "rights_status": "license_verified",
            "source_relationship": "source_attributed",
            "permission_status": "verified",
            "permission_record": permission_record,
        }

    attributed = bool(
        row.get("paper_id")
        or row.get("source_label")
        or row.get("source_artifact_id")
        or row.get("source_image_artifact_id")
    )
    return {
        "rights_status": "source_attributed" if attributed else "permission_unknown",
        "source_relationship": "source_attributed" if attributed else "unknown",
        "permission_status": "unknown",
        "permission_record": None,
    }
