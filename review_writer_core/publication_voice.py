"""Deterministic detection of internal workflow language in manuscript prose."""

from __future__ import annotations

import re
from typing import Any


REFERENCES_HEADING = re.compile(r"(?im)^#{1,6}\s+references\s*$")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]+`")
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
