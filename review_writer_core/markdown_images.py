"""Small, deterministic parser for standalone Markdown image blocks.

The workflow emits one image per line.  A simple image regular expression is
not sufficient for scientific captions:
reaction names routinely contain nested brackets (for example ``[3+2]``),
and local image destinations can contain balanced parentheses.  Keeping the
parser here gives the Final validator, DOCX exporter, and PDF renderer one
definition of the supported syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_OPTIONAL_TITLE = re.compile(
    r'^(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\((?:\\.|[^)\\])*\))$'
)
_MARKDOWN_ESCAPED_PUNCTUATION = re.compile(r"\\([\\`*{}\[\]()#+.!_>\-])")


@dataclass(frozen=True)
class MarkdownImage:
    """A supported standalone Markdown image."""

    alt: str
    source: str
    title: str = ""


def _unescape(value: str) -> str:
    return _MARKDOWN_ESCAPED_PUNCTUATION.sub(r"\1", value)


def _destination(value: str) -> tuple[str, str] | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("<"):
        escaped = False
        closing = -1
        for index, character in enumerate(raw[1:], start=1):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == ">":
                closing = index
                break
        if closing < 0:
            return None
        source = raw[1:closing]
        remainder = raw[closing + 1 :].strip()
    else:
        escaped = False
        separator = -1
        for index, character in enumerate(raw):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character.isspace():
                separator = index
                break
        if separator < 0:
            source, remainder = raw, ""
        else:
            source = raw[:separator]
            remainder = raw[separator:].strip()
    if not source or (remainder and not _OPTIONAL_TITLE.fullmatch(remainder)):
        return None
    title = remainder[1:-1] if remainder else ""
    return _unescape(source), _unescape(title)


def parse_markdown_image(line: str) -> MarkdownImage | None:
    """Parse one standalone image line, including nested brackets in alt text."""

    text = str(line or "").strip()
    if not text.startswith("!["):
        return None

    depth = 1
    cursor = 2
    escaped = False
    alt_end = -1
    while cursor < len(text):
        character = text[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                alt_end = cursor
                break
        cursor += 1
    if alt_end < 0 or alt_end + 1 >= len(text) or text[alt_end + 1] != "(":
        return None

    depth = 1
    cursor = alt_end + 2
    escaped = False
    destination_end = -1
    while cursor < len(text):
        character = text[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                destination_end = cursor
                break
        cursor += 1
    if destination_end < 0 or text[destination_end + 1 :].strip():
        return None

    parsed_destination = _destination(text[alt_end + 2 : destination_end])
    if parsed_destination is None:
        return None
    source, title = parsed_destination
    return MarkdownImage(
        alt=_unescape(text[2:alt_end]),
        source=source,
        title=title,
    )


def malformed_markdown_image_lines(markdown: str) -> list[dict[str, object]]:
    """Return image-looking standalone lines the renderer cannot parse."""

    failures: list[dict[str, object]] = []
    for line_number, line in enumerate(str(markdown or "").splitlines(), start=1):
        if line.strip().startswith("![") and parse_markdown_image(line) is None:
            failures.append({"line": line_number, "text": line.strip()})
    return failures
