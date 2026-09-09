"""Bounded chemical-name resolution for optional overview structures.

This module is deliberately small and fail-closed.  It only talks to a fixed
public resolver, returns a proposed SMILES string, and leaves structural
validation to :mod:`review_writer_core.stages.figures.overview_structure`.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


_PUBCHEM_PROPERTY_URL = (
    "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
    "{name}/property/CanonicalSMILES,IsomericSMILES/JSON"
)
_LEADING_DESCRIPTORS = re.compile(
    r"^(?:(?:mono|di|tri|tetra)[-\s]?substituted|substituted|axially\s+chiral|"
    r"enantioenriched|optically\s+active|representative)\s+",
    re.IGNORECASE,
)
_UNSAFE_NAME = re.compile(r"[\r\n\x00-\x1f]|https?://|(?:ignore|system)\s+prompt", re.IGNORECASE)


def _singularize_last_word(value: str) -> str:
    words = value.split()
    if not words:
        return value
    word = words[-1]
    lowered = word.casefold()
    if lowered.endswith("ies") and len(word) > 4:
        word = f"{word[:-3]}y"
    elif lowered.endswith(("ses", "xes", "zes", "ches", "shes")) and len(word) > 4:
        word = word[:-2]
    elif lowered.endswith("s") and not lowered.endswith(("ss", "us")) and len(word) > 3:
        word = word[:-1]
    words[-1] = word
    return " ".join(words)


def chemical_name_candidates(value: Any) -> tuple[str, ...]:
    """Return a small, deterministic set of safe resolver candidates."""

    text = " ".join(str(value or "").split()).strip(" .,:;()[]{}")
    if not text or len(text) > 160 or _UNSAFE_NAME.search(text):
        return ()
    candidates: list[str] = []
    for candidate in (text, _LEADING_DESCRIPTORS.sub("", text)):
        candidate = candidate.strip(" .,:;()[]{}")
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        singular = _singularize_last_word(candidate)
        if singular and singular not in candidates:
            candidates.append(singular)
    return tuple(candidates[:4])


@lru_cache(maxsize=256)
def _resolve_pubchem(candidate: str, timeout_seconds: float) -> dict[str, Any] | None:
    url = _PUBCHEM_PROPERTY_URL.format(name=quote(candidate, safe=""))
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "review-writer/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    properties = ((payload.get("PropertyTable") or {}).get("Properties") or [])
    if not properties or not isinstance(properties[0], dict):
        return None
    row = properties[0]
    smiles = str(
        row.get("IsomericSMILES")
        or row.get("CanonicalSMILES")
        or row.get("ConnectivitySMILES")
        or ""
    ).strip()
    if not smiles:
        return None
    return {
        "resolver": "pubchem_pug_rest",
        "resolved_name": candidate,
        "smiles": smiles,
        "source_url": url,
    }


def resolve_chemical_name(value: Any, *, timeout_seconds: float = 4.0) -> dict[str, Any] | None:
    """Resolve a chemical name without ever raising on network/provider errors."""

    timeout = max(1.0, min(float(timeout_seconds or 4.0), 8.0))
    for candidate in chemical_name_candidates(value):
        resolved = _resolve_pubchem(candidate, timeout)
        if resolved:
            return resolved
    return None
