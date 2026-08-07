"""Shared Unicode and XML-compatible text validation helpers."""

from __future__ import annotations

from typing import Any


def is_xml_compatible_character(char: str) -> bool:
    codepoint = ord(char)
    return (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def make_xml_compatible(text: Any, replacement: str = "\uFFFD") -> tuple[str, int]:
    """Replace characters that cannot be represented in OOXML/XML 1.0."""
    value = str(text or "")
    replaced = 0
    output: list[str] = []
    for char in value:
        if is_xml_compatible_character(char):
            output.append(char)
        else:
            output.append(replacement)
            replaced += 1
    return "".join(output), replaced


def incompatible_character_count(text: Any) -> int:
    return sum(1 for char in str(text or "") if not is_xml_compatible_character(char))
