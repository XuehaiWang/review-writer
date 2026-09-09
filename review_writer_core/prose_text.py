"""Unicode-aware sentence boundaries and comparison keys for prose checks."""

import re
import unicodedata


def prose_sentences(text: str) -> list[str]:
    # Latin full stops require spacing; CJK sentence punctuation does not.
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|(?<=[。！？])\s*", str(text or "")) if part.strip()]


def prose_comparison_key(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFC", text).casefold()
                   if char.isalnum() or unicodedata.category(char).startswith("M"))
