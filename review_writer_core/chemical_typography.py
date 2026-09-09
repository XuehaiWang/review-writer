"""Conservative chemical typography shared by manuscript views and exporters.

Only complete formula tokens are formatted. Source facts, identifiers, links,
code and explicit math are never inferred or repaired by this presentation pass.
"""

from __future__ import annotations

import re

_ELEMENTS = set("H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og".split())
_GROUPS = {"Me", "Et", "Pr", "Bu", "Ph", "Bn", "Boc", "TMS", "Tf", "Ts", "Ac", "dba", "acac", "bpy", "bipy", "phen", "dppe", "dppf", "cod", "COD"}
_SYMBOL = "(?:" + "|".join(sorted(_ELEMENTS | _GROUPS, key=lambda value: (-len(value), value))) + ")"
_PART = re.compile(_SYMBOL + r"|[()[\]]|\d+")
_TOKEN = re.compile(r"(?<![A-Za-z0-9_/])[\[(]*[A-Z][A-Za-z0-9()[\]·/]*(?:\^?[+-](?!\w))?(?![A-Za-z0-9_/]|\.[A-Za-z])")
_PROTECTED = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|<!--[\s\S]*?-->|`[^`\n]*`|\$\$[\s\S]*?\$\$|\$(?:\\.|[^$\n])*\$|https?://[^\s)]+|\b10\.\d{4,9}/\S+|\]\([^)]+\)")
SUBSCRIPT = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")
SUPERSCRIPT = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")
_MOLECULES = {"H2", "N2", "O2", "O3", "F2", "Cl2", "Br2", "I2", "P4", "S8", "C60", "C70"}


def _formula(token: str) -> str:
    if "/" in token:
        components = token.split("/")
        # Recognize a complete chemical combination, not a path or arbitrary ID.
        if any(not part or "".join(_PART.findall(part)) != part for part in components):
            return token
        return "/".join(_formula(part) for part in components)
    # Only explicit charge markup establishes that a minus is not a prose hyphen.
    if token.endswith(("^+", "^-")):
        return _formula(token[:-2]) + token[-1].translate(SUPERSCRIPT)
    if token.startswith("[") and token.endswith(("]+", "]-")):
        return _formula(token[:-1]) + token[-1].translate(SUPERSCRIPT)
    if "·" in token:
        components = []
        for part in token.split("·"):
            coefficient = re.match(r"\d+", part)
            prefix = coefficient[0] if coefficient else ""
            components.append(prefix + _formula(part[len(prefix):]))
        return "·".join(components)
    # Parenthetical prose punctuation can follow an otherwise complete formula.
    suffix = ""
    while token.endswith((")", "]")) and token.count(token[-1]) > token.count("(" if token[-1] == ")" else "["):
        suffix = token[-1] + suffix
        token = token[:-1]
    if suffix:
        return _formula(token) + suffix
    ion = re.fullmatch(r"([A-Z][a-z]?)([1-9]\d?)?([+-])", token)
    if ion and ion[3] == "+" and ion[1] in _ELEMENTS and token[:-1] not in _MOLECULES:
        return ion[1] + ((ion[2] or "") + ion[3]).translate(SUPERSCRIPT)
    complex_ion = re.fullmatch(r"(\[.+\])([1-9]\d?)([+-])", token)
    if complex_ion:
        return _formula(complex_ion[1]) + (complex_ion[2] + complex_ion[3]).translate(SUPERSCRIPT)
    charge = token[-1] if token.endswith(("+", "-")) else ""
    body = token[:-1] if charge else token
    parts = _PART.findall(body)
    if "".join(parts) != body or not any(part.isdigit() for part in parts):
        return token
    # A single element plus a number is frequently a compound/sample identifier.
    symbols = [part for part in parts if part in _ELEMENTS or part in _GROUPS]
    if len(symbols) < 2 and body not in _MOLECULES:
        return token
    stack: list[str] = []
    previous = ""
    for part in parts:
        if part in {"(", "["}:
            stack.append(part)
        elif part in {")", "]"}:
            if not stack or stack.pop() != ("(" if part == ")" else "["):
                return token
        elif part.isdigit():
            if part.startswith("0") or not previous or previous in {"(", "["}:
                return token
            # Plain SO42- is ambiguous; charge magnitudes require explicit math.
            if charge and part == parts[-1] and len(part) > 1:
                return token
        previous = part
    if stack:
        return token
    return "".join(part.translate(SUBSCRIPT) if part.isdigit() else part for part in parts) + (charge if charge == "-" else charge.translate(SUPERSCRIPT))


def normalize_chemical_typography(text: str) -> str:
    """Normalize recognized plain formulas, preserving explicit markup and data IDs."""
    text = str(text or "")
    result: list[str] = []
    cursor = 0
    for match in _PROTECTED.finditer(text):
        result.append(_TOKEN.sub(lambda token: _formula(token[0]), text[cursor:match.start()]))
        result.append(match.group(0))
        cursor = match.end()
    result.append(_TOKEN.sub(lambda token: _formula(token[0]), text[cursor:]))
    return "".join(result)
