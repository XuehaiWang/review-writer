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


CAPTION_NORMALIZATION_VERSION = "publication-caption/2"

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

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "publication_caption_text": self.publication_text,
            "publication_caption_plain_text": self.plain_text,
            "caption_normalization_status": self.status,
            "caption_normalization_warnings": list(self.warnings),
            "caption_normalization_version": self.version,
        }


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


def normalize_publication_caption(value: Any) -> PublicationCaption:
    """Return a safe derived caption without modifying the source evidence."""

    source = str(value or "").strip()
    if not source:
        return PublicationCaption("", "", "", "cleaned", ())
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
        return PublicationCaption(
            source_text=source,
            publication_text=visible,
            plain_text=visible,
            status=status,
            warnings=tuple(warnings),
        )
    except Exception as exc:  # pragma: no cover - defensive workflow boundary
        fallback, _ = make_xml_compatible(source)
        return PublicationCaption(
            source_text=source,
            publication_text=fallback,
            plain_text=fallback,
            status="passthrough",
            warnings=(f"normalization_failed:{type(exc).__name__}",),
        )


def publication_caption_fields(value: Any) -> dict[str, Any]:
    """Convenience helper for figure manifests."""

    return normalize_publication_caption(value).manifest_fields()
