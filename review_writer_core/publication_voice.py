"""Deterministic detection of internal workflow language in manuscript prose."""

from __future__ import annotations

import re
from typing import Any


REFERENCES_HEADING = re.compile(r"(?im)^#{1,6}\s+references\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
_PROTECTED = re.compile(r'<!--.*?-->|```.*?```|`[^`\n]+`|"[^"\n]*"|“[^”\n]*”', re.DOTALL)
_SOURCE_WORDING = re.compile(
    r"\b(?P<modifier>supplied|provided|retrieved)\s+(?P<noun>passages?|excerpts?)"
    r"(?=\s+(?:identify|describe|report|indicate|show|state|document|support|suggest|demonstrate)s?\b)", re.I)


def normalize_publication_voice(markdown: str) -> str:
    """Remove source-delivery wording without erasing evidence limitations.

    Only affirmative source-attribution subjects are projected. Statements
    about missing evidence need semantic repair and remain detectable.
    """
    parts = REFERENCES_HEADING.split(str(markdown or ""), maxsplit=1)
    body = parts[0]
    def prose(value: str) -> str:
        return _SOURCE_WORDING.sub(lambda m: (
            ("Cited" if m.group("modifier")[0].isupper() else "cited")
            + (" sources" if m.group("noun").lower().endswith("s") else " source")), value)
    protected = []
    def stash(match):
        protected.append(match.group(0))
        return f"\x00VOICE{len(protected)-1}\x00"
    body = _PROTECTED.sub(stash, body)
    body = "\n".join(line if line.lstrip().startswith((">", "#", "![", "|")) else prose(line) for line in body.split("\n"))
    for index, value in enumerate(protected):
        body = body.replace(f"\x00VOICE{index}\x00", value)
    return body + str(markdown or "")[len(parts[0]):]
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("evidence_package", re.compile(r"\b(?:supplied|provided) evidence\b", re.I)),
    (
        "retrieval_boundary_leak",
        re.compile(
            r"\b(?:available|provided|supplied) (?:excerpt|material|passage)s?\b|"
            r"\blocally bounded\b|\bselected matrix\b|\bindexed evidence\b",
            re.I,
        ),
    ),
    ("workflow_artifact", re.compile(r"\b(?:evidence|source|workflow) (?:package|artifact|registry)\b", re.I)),
    ("internal_gate", re.compile(r"\b(?:integrity|quality|review) gate\b", re.I)),
    ("model_instruction", re.compile(r"\b(?:the model|the prompt|the workflow) (?:must|should|was instructed)\b", re.I)),
    ("unsupported_internal_label", re.compile(r"\b(?:claim id|paragraph id|paper id|evidence key)\b", re.I)),
)


def publication_voice_issues(markdown: str) -> list[dict[str, Any]]:
    """Return prose leaks while ignoring references, comments, code and quotes."""

    body = str(markdown or "")
    reference = REFERENCES_HEADING.search(body)
    if reference:
        body = body[: reference.start()]
    body = FENCED_CODE.sub("", COMMENT.sub("", body))
    body = INLINE_CODE.sub("", body)
    issues: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(body.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith((">", "![", "|", "#")):
            continue
        for code, pattern in LEAK_PATTERNS:
            for match in pattern.finditer(line):
                issues.append(
                    {
                        "code": code,
                        "line": line_number,
                        "phrase": match.group(0),
                        "context": line[:500],
                    }
                )
    return issues
