"""Cheap manuscript wording clues, never a semantic or scientific verdict."""

from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import re
from typing import Any, Mapping


_CALLOUT = re.compile(r"\[(?:\d+\s*(?:[,;–-]\s*\d+\s*)*)\]")
_WORDS = re.compile(r"[a-z]+(?:[-'][a-z]+)*|\d+(?:\.\d+)?|[\u3400-\u9fff]+", re.I)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NEGATION = re.compile(r"\b(?:no|not|never|without|neither|none)\b|[无未不非]", re.I)


def _plain(value: Any) -> str:
    text = _CALLOUT.sub("", str(value or ""))
    text = re.sub(r"<!--.*?-->|!\[[^\]]*\]\([^)]*\)", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORDS.finditer(value)]


def _substantial(value: str, tokens: list[str]) -> bool:
    return len(value) >= 65 and (len(tokens) >= 10 or sum(len(token) for token in tokens if "\u3400" <= token[:1] <= "\u9fff") >= 28)


def _signature(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip(" \t\r\n.,;:!?。！？")


def scan_repetition_hints(
    paragraphs: list[Mapping[str, Any]],
    sections: list[Mapping[str, Any]] | None = None,
    *,
    max_hints: int = 12,
) -> dict[str, Any]:
    """Locate surface repetition in linear, bounded work.

    Shared papers, facts or words are not proof of a duplicate scientific
    proposition. Results are advisory and must never change manuscript text.
    """
    rows = [row for row in paragraphs if isinstance(row, Mapping) and str(row.get("text") or "").strip()]
    section_by_paragraph = {
        str(paragraph.get("paragraph_id") or ""): str(section.get("section_id") or "")
        for section in sections or [] if isinstance(section, Mapping)
        for paragraph in section.get("paragraphs") or [] if isinstance(paragraph, Mapping)
    }
    hints: list[dict[str, Any]] = []
    sentences: list[dict[str, Any]] = []
    exact: dict[str, int] = {}
    shingles: dict[tuple[str, ...], list[int]] = defaultdict(list)
    endings: dict[tuple[str, str], list[str]] = defaultdict(list)

    for row in rows:
        paragraph_id = str(row.get("paragraph_id") or "")
        parts = [_plain(piece) for piece in _SENTENCE_BOUNDARY.split(str(row.get("text") or ""))]
        parts = [piece for piece in parts if piece]
        if not parts:
            continue
        closing_words = _tokens(parts[-1])
        if len(closing_words) >= 5 and closing_words[0] not in {"the", "a", "an"}:
            endings[tuple(closing_words[:2])].append(paragraph_id)
        for piece in parts:
            tokens = _tokens(piece)
            if not _substantial(piece, tokens):
                continue
            normalized = _signature(piece)
            matching = exact.get(normalized)
            if matching is None and len(tokens) >= 10:
                candidates = Counter(
                    prior
                    for start in range(len(tokens) - 4)
                    for prior in shingles.get(tuple(tokens[start:start + 5]), ())
                )
                for prior, shared in candidates.most_common(4):
                    previous = sentences[prior]
                    if (shared < 3
                            or _NUMBER.findall(normalized) != previous["numbers"]
                            or _NEGATION.findall(normalized) != previous["negations"]):
                        continue
                    if SequenceMatcher(None, normalized, previous["normalized"], autojunk=False).ratio() >= .88:
                        matching = prior
                        break
            if matching is not None and len(hints) < max_hints:
                previous = sentences[matching]
                hints.append({
                    "kind": "wording_overlap", "paragraph_id": paragraph_id,
                    "other_paragraph_id": previous["paragraph_id"],
                    "section_id": section_by_paragraph.get(paragraph_id, ""),
                    "other_section_id": section_by_paragraph.get(previous["paragraph_id"], ""),
                    "excerpt": piece[:300], "other_excerpt": previous["text"][:300],
                    "basis": "surface_wording_only",
                })
            index = len(sentences)
            sentences.append({"paragraph_id": paragraph_id, "text": piece, "normalized": normalized,
                              "numbers": _NUMBER.findall(normalized), "negations": _NEGATION.findall(normalized)})
            exact.setdefault(normalized, index)
            if len(tokens) >= 10:
                for start in range(len(tokens) - 4):
                    key = tuple(tokens[start:start + 5])
                    if len(shingles[key]) < 8:
                        shingles[key].append(index)

    for words, paragraph_ids in endings.items():
        unique = list(dict.fromkeys(paragraph_ids))
        if len(unique) < 3 or len(hints) >= max_hints:
            continue
        hints.append({"kind": "repeated_closing", "paragraph_ids": unique[:12],
                      "phrase": " ".join(words), "basis": "surface_wording_only"})
    return {"status": "complete" if rows else "not_ready", "method": "surface_clues_v1",
            "covered_paragraph_count": len(rows), "total_paragraph_count": len(paragraphs),
            "hints": hints}
