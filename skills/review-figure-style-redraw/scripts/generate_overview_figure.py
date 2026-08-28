#!/usr/bin/env python3
"""Generate a review overview figure by auto-matching the best template.

Usage:
    python skills/review-figure-style-redraw/scripts/generate_overview_figure.py \
        --review-root <path> \
        --project-id <id> \
        [--api-key <key>] \
        [--base-url <url>] \
        [--model <model>] \
        [--require-ai-skeleton]

The script:
1. Reads the review outline and selected_discovery_results to extract structure.
2. Scores each template bundled under this skill's assets directory.
3. Selects the best-matching template.
4. Adapts the template prompt with review-specific content.
5. Calls the OpenAI-compatible image edit API with the template reference image.
6. Composites the exact ball-and-stick skeleton into the figure's structure
   panel (calibrated regions first, automatic blank-panel detection for every
   other layout).
7. Saves the generated overview figure.

Chemistry reviews (allene taxonomy profile or an explicit ``skeleton_smiles``
in the query plan) run in strict *chemical* skeleton mode: an exact
programmatic skeleton is mandatory.  The optional ai3d style transfer is
retried up to three times and falls back to that exact programmatic skeleton
when its probabilistic style gate rejects every attempt.  ``--require-ai-skeleton``
keeps the stronger opt-in behavior and also requires the AI-styled rendering.
This guarantees a chemistry overview never fails merely because an aesthetic
style transfer was rejected, while still refusing a missing or uncomposited
chemical structure.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "review_writer_core").is_dir() and (parent / "skills").is_dir()
    ),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.providers import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    openai_endpoint as _shared_openai_endpoint,
    resolve_api_key as _shared_resolve_api_key,
)
from review_writer_core.model_gateway_client import (  # noqa: E402
    call_image_model as call_gateway_image,
    image_gateway_configured,
)
from review_writer_core.review_titles import (  # noqa: E402
    build_publication_review_title,
)
from review_writer_core.taxonomy import (  # noqa: E402
    suggest_taxonomy_profile as _suggest_taxonomy_profile,
)


TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
LANDSCAPE_OVERVIEW_IMAGE_SIZE = "1536x1024"
SQUARE_COMPATIBLE_IMAGE_SIZE = "1024x1024"

# Some compatible gateways reject the default Python urllib signature; every
# outbound request therefore carries an explicit application user agent.
USER_AGENT = "review-writer-overview-figure/1.0"

_ENGLISH_NUM_WORDS = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten",
}

# Category panel lists baked into the template prompts (allene-field originals)
_CATEGORY_PANEL_RE = re.compile(
    r"(?:-\s*|(?:(\d+)\.\s*))?Cu\s*\n(?:-\s*|(?:(\d+)\.\s*))?Pd\s*\n"
    r"(?:-\s*|(?:(\d+)\.\s*))?Au\s*\n(?:-\s*|(?:(\d+)\.\s*))?Rh/Ir\s*\n"
    r"(?:-\s*|(?:(\d+)\.\s*))?Other metals",
    re.IGNORECASE,
)
_INLINE_CATEGORIES_RE = re.compile(r"Cu\s*/\s*Pd\s*/\s*Au\s*/\s*Rh/Ir", re.IGNORECASE)

_TEXT_INTEGRITY_GUARD = (
    "TEXT INTEGRITY (strict): render every cell EXACTLY as given; never copy or repeat "
    "words between panels; no invented, gibberish, hyphen-split, or placeholder text; "
    "fill every panel, leave no blank areas unless specified above."
)


def normalize_image_wire_api(value: str = "") -> str:
    """Resolve the configured image transport without silently changing endpoints."""
    configured = (
        value.strip()
        or os.environ.get("IMAGE_OPENAI_WIRE_API", "images").strip()
    ).lower().replace("_", "-")
    aliases = {
        "chat": "chat-completions",
        "chat-completion": "chat-completions",
        "image": "images",
        "image-api": "images",
    }
    configured = aliases.get(configured, configured)
    return configured if configured in {"images", "chat-completions"} else "images"


def openai_api_url(base_url: str, endpoint: str) -> str:
    """Build an OpenAI-compatible endpoint without duplicating /v1."""
    return _shared_openai_endpoint(base_url, endpoint)


def overview_image_size_candidates(base_url: str, preferred_size: str = "") -> list[str]:
    """Return image sizes in provider-compatible retry order.

    Provider capabilities are configured explicitly instead of inferred from a
    hostname. ``OVERVIEW_IMAGE_SIZE``/``--size`` set the preferred size and
    ``IMAGE_SUPPORTED_SIZES`` may list supported values in retry order. The
    widely supported square size remains the final compatibility fallback.
    """
    del base_url
    configured = preferred_size.strip() or os.environ.get("OVERVIEW_IMAGE_SIZE", "").strip()
    supported = [
        item.strip()
        for item in os.environ.get("IMAGE_SUPPORTED_SIZES", "").split(",")
        if item.strip()
    ]
    provider_default = supported[0] if supported else LANDSCAPE_OVERVIEW_IMAGE_SIZE
    candidates = [configured, *supported, provider_default, SQUARE_COMPATIBLE_IMAGE_SIZE]
    return list(dict.fromkeys(size for size in candidates if size))


# Appended by prompt_for_overview_size when the provider only supports a square
# canvas; its length is reserved in the condensation budget (_PROMPT_MAX_CHARS).
_SQUARE_CANVAS_NOTE = (
    " The image service uses a square canvas. Preserve the template's landscape reading order "
    "inside the square: keep every panel fully visible, use balanced white margins, do not crop, "
    "stretch, stack, or omit any title, category, reaction, label, legend, or conclusion block."
)


def prompt_for_overview_size(prompt: str, size: str) -> str:
    """Keep a landscape reading order when a provider only emits a square."""
    if size != SQUARE_COMPATIBLE_IMAGE_SIZE:
        return prompt
    return prompt + _SQUARE_CANVAS_NOTE


# Condensation target leaves headroom for the square-canvas note appended by
# prompt_for_overview_size so the final prompt (note included) stays under
# the ~4000-char limit that some compatible image providers enforce.
_PROMPT_MAX_CHARS = 3900 - len(_SQUARE_CANVAS_NOTE)


def condense_overview_prompt(prompt: str, max_chars: int = _PROMPT_MAX_CHARS) -> str:
    """Trim the prompt to provider limits without losing the adaptation data.

    Some compatible image providers reject prompts over roughly 4000 chars.
    The default ``max_chars`` already reserves space for the square-canvas
    note that ``prompt_for_overview_size`` may append afterwards.
    Removal order (least to most important): FORBIDDEN BEHAVIOR,
    APPROVED TERMINOLOGY, BALL-AND-STICK rendering detail, then the
    reference template preamble before DOMAIN OVERRIDE.
    """
    if len(prompt) <= max_chars:
        return prompt

    def _drop(pattern: str, text: str) -> str:
        return re.sub(pattern, "", text, count=1, flags=re.DOTALL)

    condensed = prompt
    # Replace FORBIDDEN BEHAVIOR with a compact one-liner (never drop the
    # anti-repetition / anti-gibberish guard entirely)
    condensed = re.sub(
        r"FORBIDDEN BEHAVIOR \(strictly enforced\):.*",
        _TEXT_INTEGRITY_GUARD,
        condensed, flags=re.DOTALL,
    )
    if len(condensed) <= max_chars:
        return condensed
    condensed = _drop(r"APPROVED TERMINOLOGY.*?(?=TEXT INTEGRITY|CRITICAL|\Z)", condensed)
    if len(condensed) <= max_chars:
        return condensed
    condensed = _drop(r"BALL-AND-STICK RENDERING RULES:.*?(?=STEREOCHEMISTRY|QUALITY CHECK|\Z)", condensed)
    condensed = _drop(r"STEREOCHEMISTRY \(must be visually prominent\):.*?(?=QUALITY CHECK|\Z)", condensed)
    condensed = _drop(r"QUALITY CHECK:.*?(?=\n\n|\Z)", condensed)
    if len(condensed) <= max_chars:
        return condensed
    # Keep only the adaptation block (everything from the reference usage note on)
    idx = condensed.find("REFERENCE IMAGE USAGE")
    if idx > 0:
        condensed = condensed[idx:]
    if len(condensed) <= max_chars:
        return condensed
    # Last resort: hard-truncate but always keep the integrity guard
    guard = _TEXT_INTEGRITY_GUARD
    if guard in condensed:
        condensed = condensed.replace(guard, "").rstrip()
    return condensed[: max_chars - len(guard) - 2].rstrip() + "\n\n" + guard


def decode_json_object(raw: bytes, label: str) -> dict[str, Any]:
    if not raw.strip():
        raise RuntimeError(f"{label} returned an empty response body")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw.decode("utf-8", "replace")[:300].replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"{label} returned non-JSON content: {preview or '<empty>'}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} returned JSON {type(data).__name__}, expected an object")
    return data


def open_json_request(request: urllib.request.Request, label: str, timeout: int = 300) -> dict[str, Any]:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
                return decode_json_object(response.read(), label)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500].replace("\r", " ").replace("\n", " ")
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(f"{label} failed with HTTP {exc.code}: {body or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport failed: {exc}") from exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{label} failed after retries")


def load_dotenv(review_root: Path) -> None:
    path = review_root / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("'\""))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def overview_template_catalog_path() -> Path:
    """Return the skill-owned overview template catalog."""
    return Path(__file__).resolve().parents[1] / "assets" / "overview-templates" / "overview_templates.json"


def resolve_overview_template_image(templates_path: Path, template: dict[str, Any]) -> Path:
    """Resolve one catalog image without allowing it to escape the asset directory."""
    asset_root = templates_path.resolve().parent
    configured = Path(str(template.get("reference_image") or "").strip())
    if not configured.name:
        raise ValueError("Overview template is missing reference_image")
    candidate = configured if configured.is_absolute() else asset_root / configured
    candidate = candidate.resolve()
    try:
        candidate.relative_to(asset_root)
    except ValueError as exc:
        raise ValueError(f"Overview template image escapes the skill asset directory: {candidate}") from exc
    return candidate


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_layout_skeleton(reference_image: Path, output_path: Path, skeleton_width: int = 72) -> Path:
    """Create a layout-only version of the template reference image.

    Aggressively downscales then upscales so panel shapes, colors, and the
    overall layout survive, while ALL text and chemistry glyphs become
    unreadable.  The reference image then serves purely as a layout/style
    guide (round vs square panels, column structure, color scheme) and the
    model cannot copy template content into the generated overview.
    Falls back to the original image when Pillow is unavailable.
    """
    try:
        from PIL import Image
    except ImportError:
        return reference_image
    try:
        with Image.open(reference_image) as img:
            img = img.convert("RGB")
            width, height = img.size
            ratio = max(1, width // skeleton_width)
            small = img.resize((max(1, width // ratio), max(1, height // ratio)), Image.LANCZOS)
            skeleton = small.resize((width, height), Image.BILINEAR)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            skeleton.save(output_path, format="PNG")
        return output_path
    except Exception as exc:  # never block generation on preprocessing
        print(f"  WARNING: layout skeleton failed ({exc}); using original reference")
        return reference_image


# ---------------------------------------------------------------------------
# Chemically accurate ball-and-stick skeleton rendering.
# Coordinates are built from hybridization rules (sp 180°, sp2 120°, ring 120°,
# perpendicular allene planes), so geometry is correct BY CONSTRUCTION and no
# chemistry DLL (RDKit/OpenBabel) is required.
# ---------------------------------------------------------------------------

_CPK_COLORS = {
    "C": (35, 35, 35), "H": (245, 245, 245), "O": (220, 30, 30),
    "N": (40, 80, 220), "S": (230, 200, 40), "P": (250, 140, 30),
    "Si": (200, 170, 120), "B": (240, 150, 180), "R": (70, 130, 200),
    # Halogens
    "F": (80, 220, 80), "Cl": (30, 190, 30), "Br": (160, 50, 30),
    "I": (130, 0, 150),
    # Transition metals (CPK-inspired distinct colors for catalysis reviews)
    "Pd": (0, 105, 133), "Cu": (200, 120, 50), "Fe": (225, 100, 30),
    "Zn": (125, 125, 130), "Ni": (80, 208, 80), "Co": (240, 144, 160),
    "Au": (255, 209, 35), "Ag": (192, 192, 192), "Pt": (208, 208, 224),
    "Ir": (22, 153, 139), "Rh": (10, 122, 135), "Ru": (36, 144, 144),
    "Mn": (156, 122, 197), "Ti": (191, 194, 199), "Cr": (138, 153, 199),
    "V": (166, 166, 171), "Mo": (84, 181, 181), "W": (34, 148, 201),
    "Re": (38, 125, 125), "Os": (38, 102, 150),
    # Main group metals
    "Al": (191, 166, 165), "Sn": (102, 128, 128), "Li": (204, 128, 255),
    "Na": (171, 92, 242), "K": (143, 64, 212), "Mg": (138, 255, 0),
    "Ca": (61, 255, 0), "Se": (255, 161, 0), "Te": (212, 122, 0),
}
_ATOM_RADIUS = {
    "C": 20, "H": 12, "O": 19, "N": 19, "S": 22, "P": 22,
    "Si": 22, "B": 19, "R": 24,
    # Halogens
    "F": 14, "Cl": 20, "Br": 22, "I": 24,
    # Transition metals (slightly larger spheres)
    "Pd": 26, "Cu": 24, "Fe": 24, "Zn": 24, "Ni": 24, "Co": 24,
    "Au": 26, "Ag": 26, "Pt": 26, "Ir": 26, "Rh": 26, "Ru": 25,
    "Mn": 24, "Ti": 26, "Cr": 24, "V": 24, "Mo": 26, "W": 26,
    "Re": 26, "Os": 26,
    # Main group metals
    "Al": 24, "Sn": 26, "Li": 28, "Na": 30, "K": 32, "Mg": 26,
    "Ca": 28, "Se": 22, "Te": 24,
}


# Generic SMILES -> accurate 3D geometry.  Any review topic can supply its
# core motif as SMILES (query_plan "skeleton_smiles"), otherwise a keyword map
# picks a built-in SMILES.  Geometry is exact by construction:
# rings = regular polygons, substituents grow with ideal hybridization angles
# (sp 180 / sp2 120 / sp3 109.5), cumulated double bonds get perpendicular
# planes (allene chirality).

_VALENCE = {"C": 4, "N": 3, "O": 2, "S": 2, "P": 3, "B": 3, "H": 1,
            "F": 1, "Cl": 1, "Br": 1, "I": 1, "Si": 4, "R": 0,
            # Transition metals (common in catalytic chemistry reviews)
            "Pd": 2, "Cu": 2, "Fe": 2, "Zn": 2, "Ni": 2, "Co": 2,
            "Au": 1, "Ag": 1, "Pt": 2, "Ir": 3, "Rh": 3, "Ru": 2,
            "Mn": 2, "Ti": 4, "Al": 3, "Sn": 4, "Se": 2, "Te": 2,
            "Li": 1, "Na": 1, "K": 1, "Mg": 2, "Ca": 2}
_AROMATIC_EL = {"c": "C", "n": "N", "o": "O", "s": "S", "p": "P",
                "se": "Se", "te": "Te"}
_TWO_LETTER = ("Cl", "Br", "Si", "Fe", "Cu", "Zn", "Ni", "Co", "Au", "Ag",
               "Pt", "Ir", "Rh", "Ru", "Mn", "Ti", "Al", "Sn", "Se", "Te",
               "Li", "Na", "Mg", "Ca", "Pd")
# Ordered most-specific first: the first matching key wins, so narrow motif
# names must precede their generic parents (e.g. "allenoate" before "allene",
# "suzuki" before "alkene").  Keep every key unambiguous as a plain substring:
# short words that collide with common English ("heck" vs "checked") are
# deliberately excluded.
_LABEL_SMILES = [
    ("allenoate", "*C(*)=C=C(C(=O)O*)*"),
    ("biaryl", "*c1ccccc1-c2ccccc2*"),
    ("atropisomer", "*c1ccccc1-c2ccccc2*"),
    ("suzuki", "*c1ccccc1-c2ccccc2*"),
    ("cross-coupling", "*c1ccccc1-c2ccccc2*"),
    ("negishi", "*c1ccccc1-c2ccccc2*"),
    ("kumada", "*c1ccccc1-c2ccccc2*"),
    ("indole", "c1ccc2[nH]ccc2c1"),
    ("cyclopropane", "*C1(*)CC1"),
    ("sonogashira", "*C#C*"),
    ("propargyl", "OCC#C*"),
    ("diene", "*C=CC=C*"),
    ("enone", "*C(=O)C=C*"),
    ("imine", "*C(*)=N*"),
    ("nitrile", "*C#N"),
    ("boronic", "*B(O)O"),
    ("amide", "*C(=O)N(*)*"),
    ("alkyne", "*C#C*"),
    ("alkene", "*C(*)=C(*)*"),
    ("allene", "*C(*)=C=C(*)*"),
]


def _smiles_for_label(label: str) -> str | None:
    low = (label or "").lower()
    for key, smi in _LABEL_SMILES:
        if key in low:
            return smi
    return None


# ---------------------------------------------------------------------------
# RDKit fast-path: if available, parse SMILES and generate 3D coordinates
# using the industry-standard cheminformatics toolkit.  Falls back to the
# lightweight built-in parser below when RDKit is not installed.
# ---------------------------------------------------------------------------

def _try_rdkit_parse(smiles: str):
    """Attempt RDKit SMILES parsing; returns (atoms, bonds) or None."""
    try:
        from rdkit import Chem  # noqa: F811
    except ImportError:
        return None
    mol = Chem.MolFromSmiles(smiles, sanitize=True)
    if mol is None:
        # Retry without strict sanitization for exotic SMILES
        mol = Chem.MolFromSmiles(smiles, sanitize=False)
        if mol is None:
            return None
    atoms: list[dict[str, Any]] = []
    bonds: list[tuple[int, int, float]] = []
    for rd_atom in mol.GetAtoms():
        el = rd_atom.GetSymbol()
        aromatic = rd_atom.GetIsAromatic()
        num_hs = rd_atom.GetTotalNumHs()
        atoms.append({"el": el, "aromatic": aromatic, "h": num_hs})
    for rd_bond in mol.GetBonds():
        a = rd_bond.GetBeginAtomIdx()
        b = rd_bond.GetEndAtomIdx()
        bt = rd_bond.GetBondTypeAsDouble()
        # RDKit uses 1.0/2.0/3.0 for S/D/T and 1.5 for aromatic
        bonds.append((a, b, bt))
    return atoms, bonds


def _try_rdkit_3d(smiles: str):
    """Attempt RDKit 3D coordinate generation.

    Returns ``(coords, atoms, bonds)`` with explicit hydrogens, or ``None``
    on any failure (never raises).
    """
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        return None
    try:
        mol = Chem.MolFromSmiles(smiles, sanitize=False)
        if mol is None:
            return None
        try:
            Chem.SanitizeMol(mol)
        except Exception:
            pass
        mol = Chem.AddHs(mol)
        result = AllChem.EmbedMolecule(mol, randomSeed=42, useRandomCoords=True)
        if result == -1:
            return None
        AllChem.MMFFOptimizeMolecule(mol, maxIters=300)
        conf = mol.GetConformer()
        coords = []
        for i in range(mol.GetNumAtoms()):
            pos = conf.GetAtomPosition(i)
            coords.append((pos.x, pos.y, pos.z))
        # RDKit generates coords for all atoms including H; re-derive the
        # atom/bond lists from the same mol object so indices align.
        rd_atoms: list[dict[str, Any]] = []
        for rd_atom in mol.GetAtoms():
            rd_atoms.append({"el": rd_atom.GetSymbol(), "aromatic": False, "h": 0})
        rd_bonds: list[tuple[int, int, float]] = []
        for rd_bond in mol.GetBonds():
            rd_bonds.append(
                (rd_bond.GetBeginAtomIdx(), rd_bond.GetEndAtomIdx(),
                 rd_bond.GetBondTypeAsDouble())
            )
        return coords, rd_atoms, rd_bonds
    except Exception:
        return None


def parse_smiles(smiles: str):
    """Parse a SMILES string into atoms/bonds (aromatic bond = 1.5).

    Attempts RDKit first for maximum chemical accuracy, falls back to the
    built-in lightweight parser when RDKit is unavailable.
    """
    # --- RDKit fast-path (preferred) ---
    rdkit_result = _try_rdkit_parse(smiles)
    if rdkit_result is not None:
        atoms, bonds = rdkit_result
        if atoms:
            return atoms, bonds

    # --- Fallback: built-in lightweight parser ---
    return _parse_smiles_fallback(smiles)


def _parse_smiles_fallback(smiles: str):
    """Built-in organic-subset SMILES parser (aromatic bond = 1.5)."""
    atoms: list[dict[str, Any]] = []
    bonds: list[tuple[int, int, float]] = []
    stack: list[int] = []
    ring_open: dict[int, tuple[int, float]] = {}
    prev = -1
    pending = 0.0
    i, n = 0, len(smiles)
    while i < n:
        ch = smiles[i]
        if ch == "(":
            stack.append(prev); i += 1; continue
        if ch == ")":
            prev = stack.pop(); i += 1; continue
        if ch in "=#-/:\\":
            if ch == ":":
                pending = 1.5
            elif ch in "/\\":
                pending = 1.0  # stereo slashes; treat as single bond
            else:
                pending = {"=": 2.0, "#": 3.0, "-": 1.0}[ch]
            i += 1; continue
        num = None
        if ch == "%":
            num = int(smiles[i + 1:i + 3]); i += 2
        elif ch.isdigit():
            num = int(ch)
        if num is not None:
            if num in ring_open:
                a, o = ring_open.pop(num)
                bonds.append((a, prev, max(o, pending or 1.0)))
            else:
                ring_open[num] = (prev, pending or 1.0)
            pending = 0.0
            i += 1
            continue
        aromatic, bracket_h = False, 0
        charge = 0
        if ch == "[":
            j = smiles.index("]", i) if "]" in smiles[i:] else i + 1
            tok = smiles[i + 1:j]
            m = re.match(r"(\d*)([A-Za-z][a-z]?)(@{0,2})", tok)
            raw = m.group(2) if m and m.group(2) else "C"
            aromatic = raw in _AROMATIC_EL
            el = _AROMATIC_EL.get(raw, raw[0].upper() + raw[1:] if len(raw) > 1 else raw)
            hm = re.search(r"H(\d*)", tok)
            bracket_h = 1 if hm and hm.group(1) == "" else (int(hm.group(1)) if hm else 0)
            # Parse charge: +, -, ++, --, +2, -2, etc.
            cm = re.search(r"([+-]+|\+[0-9]+|-[0-9]+)", tok)
            if cm:
                c_str = cm.group(1)
                if c_str == "+": charge = 1
                elif c_str == "-": charge = -1
                elif c_str.startswith("+"): charge = int(c_str[1:]) if len(c_str) > 1 else len(c_str.rstrip("+"))
                elif c_str.startswith("-"): charge = -int(c_str[1:]) if len(c_str) > 1 else -len(c_str.rstrip("-"))
            i = j + 1
        elif ch in _AROMATIC_EL:
            el, aromatic = _AROMATIC_EL[ch], True
            i += 1
        elif smiles[i:i + 2] in _TWO_LETTER:
            el = smiles[i:i + 2]; i += 2
        elif ch in "CNOSPFIBH*":
            el = "R" if ch == "*" else ch
            i += 1
        else:
            i += 1
            continue
        atoms.append({"el": el, "aromatic": aromatic, "h": bracket_h, "charge": charge})
        idx = len(atoms) - 1
        if prev >= 0:
            order = pending or 1.0
            if order == 1.0 and aromatic and atoms[prev]["aromatic"]:
                order = 1.5
            bonds.append((prev, idx, order))
        pending = 0.0
        prev = idx
    for idx, a in enumerate(atoms):
        if a["el"] in ("R", "H"):
            continue
        order_sum = sum(o for (x, y, o) in bonds if x == idx or y == idx)
        a["h"] = max(a["h"], int(round(_VALENCE.get(a["el"], 4) - order_sum)))
    return atoms, bonds


def _assign_hybrid(atoms, bonds):
    hyb = []
    for i, a in enumerate(atoms):
        orders = [o for (x, y, o) in bonds if x == i or y == i]
        if a["el"] in ("H", "R"):
            hyb.append("sp3")
        elif 3.0 in orders or orders.count(2.0) >= 2:
            hyb.append("sp")
        elif a["aromatic"] or 1.5 in orders or 2.0 in orders:
            hyb.append("sp2")
        else:
            hyb.append("sp3")
    return hyb


def _vadd(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _vsub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _vscale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def _vdot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _vcross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _vnorm(a):
    ln = math.sqrt(_vdot(a, a))
    return (a[0] / ln, a[1] / ln, a[2] / ln) if ln > 1e-9 else None


def _regular_polygon(m, edge):
    r = edge / (2 * math.sin(math.pi / m))
    return [(r * math.cos(2 * math.pi * k / m), r * math.sin(2 * math.pi * k / m), 0.0)
            for k in range(m)]


def _find_rings(n, bonds):
    """Return ring cycles (3-7 members). Fused rings are recovered by
    reducing large perimeter cycles against accepted small rings via
    undirected edge-set symmetric difference."""
    from collections import deque
    adj = [[] for _ in range(n)]
    for a, b, _o in bonds:
        adj[a].append(b)
        adj[b].append(a)
    parent = [-1] * n
    seen = [False] * n
    raw = []
    for root in range(n):
        if seen[root]:
            continue
        seen[root] = True
        q = deque([root])
        while q:
            u = q.popleft()
            for v in adj[u]:
                if not seen[v]:
                    seen[v] = True
                    parent[v] = u
                    q.append(v)
                elif v != parent[u] and u != parent[v]:
                    anc = set()
                    x = u
                    while x != -1:
                        anc.add(x)
                        x = parent[x]
                    y = v
                    while y not in anc:
                        y = parent[y]
                    cyc, x = [], u
                    while x != y:
                        cyc.append(x)
                        x = parent[x]
                    cyc.append(y)
                    x = v
                    tail = []
                    while x != y:
                        tail.append(x)
                        x = parent[x]
                    cyc += list(reversed(tail))  # lca -> v direction
                    if len(cyc) >= 3:
                        raw.append(cyc)

    cycles: list[list[int]] = []
    seen_sets: set[frozenset] = set()
    for cyc in sorted(raw, key=len):
        if len(cyc) <= 7 and frozenset(cyc) not in seen_sets:
            seen_sets.add(frozenset(cyc))
            cycles.append(cyc)

    def _edge_set(cyc):
        es = {frozenset((cyc[i], cyc[(i + 1) % len(cyc)])) for i in range(len(cyc))}
        return es

    def _order(diff):
        emap: dict[int, list[int]] = {}
        for e in diff:
            x, y = tuple(e)
            emap.setdefault(x, []).append(y)
            emap.setdefault(y, []).append(x)
        verts = set(emap)
        ordered, cur, prv = [], next(iter(verts)), None
        for _ in verts:
            ordered.append(cur)
            nxts = [w for w in emap[cur] if w != prv]
            prv, cur = cur, nxts[0]
        return ordered

    for cyc in sorted(raw, key=len):
        if len(cyc) <= 7:
            continue
        big = _edge_set(cyc)
        for small in list(cycles):
            diff = big ^ _edge_set(small)
            verts = set()
            for e in diff:
                verts |= set(e)
            if not (3 <= len(verts) <= 7 and len(diff) == len(verts)):
                continue
            if all(sum(1 for e in diff if v in e) == 2 for v in verts) \
                    and frozenset(verts) not in seen_sets:
                seen_sets.add(frozenset(verts))
                cycles.append(_order(diff))
                break
    cycles.sort(key=len)
    return cycles


_SINGLE_LEN = {
    ("C", "C"): 1.50, ("C", "O"): 1.43, ("C", "N"): 1.47, ("C", "S"): 1.82,
    ("C", "F"): 1.35, ("C", "Cl"): 1.77, ("C", "Br"): 1.94, ("C", "I"): 2.14,
    ("C", "Si"): 1.86, ("C", "P"): 1.87, ("C", "H"): 1.09, ("O", "H"): 0.96,
    ("N", "H"): 1.01, ("N", "O"): 1.40, ("O", "O"): 1.48, ("N", "N"): 1.45,
    ("S", "O"): 1.58, ("C", "B"): 1.56,
}


def _bond_len(el_a, el_b, order):
    if "R" in (el_a, el_b):
        return 1.5
    key = (el_a, el_b) if (el_a, el_b) in _SINGLE_LEN else (el_b, el_a)
    base = _SINGLE_LEN.get(key, 1.5)
    if order >= 3.0:
        return base - 0.3
    if order == 2.0:
        return base - 0.2
    if order == 1.5:
        return 1.39 if {el_a, el_b} == {"C"} else base
    return base


_SLOTS = {
    "sp": [(1, 0, 0), (-1, 0, 0)],
    "sp2": [(1, 0, 0), (-0.5, 0.8660254, 0), (-0.5, -0.8660254, 0)],
    "sp3": [(1, 0, 0), (-0.3333, 0.9428, 0), (-0.3333, -0.4714, 0.8165),
            (-0.3333, -0.4714, -0.8165)],
}


def build_3d(atoms, bonds):
    """Deterministic exact-geometry 3D embedding (rings + hybridization growth)."""
    from collections import deque
    n = len(atoms)
    if n == 0:
        return []
    hyb = _assign_hybrid(atoms, bonds)
    coords: list = [None] * n
    frames: list = [None] * n
    used: list = [[] for _ in range(n)]
    order_of = {}
    adj = [[] for _ in range(n)]
    for a, b, o in bonds:
        order_of[(a, b)] = o
        order_of[(b, a)] = o
        adj[a].append(b)
        adj[b].append(a)

    def mark_used(i, d):
        nd = _vnorm(d)
        if nd:
            used[i].append(nd)

    rings = _find_rings(n, bonds)
    deferred: list[list[int]] = []

    def place_ring(ring):
        m = len(ring)
        aromatic = any(atoms[i]["aromatic"] for i in ring)
        edge = 1.39 if aromatic else (1.51 if m == 3 else 1.5)
        poly = _regular_polygon(m, edge)
        pair = None
        for k in range(m):
            a, b = ring[k], ring[(k + 1) % m]
            if coords[a] is not None and coords[b] is not None:
                pair = k
                break
        if pair is not None:  # fused ring: align polygon edge, keep plane
            a, b = ring[pair], ring[(pair + 1) % m]
            A, B = coords[a], coords[b]
            normal = frames[a][2]
            t = _vnorm(_vsub(B, A))
            w = _vnorm(_vcross(normal, t))
            pa, pb = poly[pair], poly[(pair + 1) % m]
            tl = _vnorm(_vsub(pb, pa))
            wl = _vcross((0.0, 0.0, 1.0), tl)
            old_placed = [coords[i] for i in range(n) if coords[i] is not None]
            mid = _vscale(_vadd(A, B), 0.5)

            def _place(wvec):
                out = {}
                for k, i in enumerate(ring):
                    if coords[i] is None:
                        q = _vsub(poly[k], pa)
                        out[i] = _vadd(A, _vadd(_vscale(t, _vdot(q, tl)), _vscale(wvec, _vdot(q, wl))))
                return out

            cand = _place(w)
            if not cand:  # ring already fully placed
                return
            new_c = [cand[i] for i in cand]
            old_c = old_placed
            def _centroid(pts):
                return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts),
                        sum(p[2] for p in pts) / len(pts))
            # new ring must extend AWAY from the already-placed structure
            if _vdot(_vsub(_centroid(new_c), mid), _vsub(_centroid(old_c), mid)) > 0:
                cand = _place(_vscale(w, -1.0))
            for i, p in cand.items():
                coords[i] = p
        else:
            k0 = next(k for k, i in enumerate(ring) if coords[i] is not None) if any(
                coords[i] is not None for i in ring) else 0
            if coords[ring[k0]] is None:  # first ring at origin, xy plane
                for k, i in enumerate(ring):
                    coords[i] = poly[k]
                normal = (0.0, 0.0, 1.0)
            else:
                a = ring[k0]
                A = coords[a]
                exo = [w for w in adj[a] if w not in ring and coords[w] is not None]
                if exo:  # pendant ring attached by a single bond (biaryl-like):
                    # ring plane contains the attaching bond and stands
                    # perpendicular to the parent ring plane (atropisomer-like)
                    b = _vnorm(_vsub(coords[exo[0]], A))
                    p = frames[exo[0]][2] if frames[exo[0]] else (0.0, 0.0, 1.0)
                    normal = _vnorm(_vcross(b, p)) or _vnorm(_vcross(b, (0.0, 0.0, 1.0))) or (0.0, 0.0, 1.0)
                    T = _vscale(b, -1.0)
                    W = _vnorm(_vcross(normal, T)) or _vnorm(p)
                    pa = poly[k0]
                    tl = _vnorm(_vscale(pa, -1.0))
                    wl = _vcross((0.0, 0.0, 1.0), tl)
                    for k, i in enumerate(ring):
                        if coords[i] is None:
                            q = _vsub(poly[k], pa)
                            coords[i] = _vadd(A, _vadd(_vscale(T, _vdot(q, tl)), _vscale(W, _vdot(q, wl))))
                else:  # spiro-like single shared atom: perpendicular plane
                    e3p = frames[a][2] if frames[a] else (0.0, 0.0, 1.0)
                    e1p = frames[a][0] if frames[a] else (1.0, 0.0, 0.0)
                    normal = _vnorm(e1p)
                    t = _vnorm(_vcross(normal, e3p)) or _vnorm(e3p)
                    w = _vnorm(_vcross(normal, t))
                    pa = poly[k0]
                    tl = _vnorm(_vsub(poly[(k0 + 1) % m], pa))
                    wl = _vcross((0.0, 0.0, 1.0), tl)
                    for k, i in enumerate(ring):
                        if coords[i] is None:
                            q = _vsub(poly[k], pa)
                            coords[i] = _vadd(A, _vadd(_vscale(t, _vdot(q, tl)), _vscale(w, _vdot(q, wl))))
        for k, i in enumerate(ring):
            nxt, prv = ring[(k + 1) % m], ring[(k - 1) % m]
            e1 = _vnorm(_vsub(coords[nxt], coords[i]))
            e3 = _vnorm(normal)
            frames[i] = (e1, _vcross(e3, e1), e3)
            mark_used(i, _vsub(coords[nxt], coords[i]))
            mark_used(i, _vsub(coords[prv], coords[i]))

    # Independent (non-fused) rings must wait until a connecting bond places an
    # anchor atom; otherwise they would all be dropped onto the origin and
    # overlap (e.g. biaryl).  They are placed in a second pass after BFS growth.
    for ring in rings:
        if any(coords[i] is not None for i in ring) or all(c is None for c in coords):
            place_ring(ring)
        else:
            deferred.append(ring)

    ring_of: dict[int, list[int]] = {}
    for ring in deferred:
        for i in ring:
            ring_of[i] = ring

    if all(c is None for c in coords):  # acyclic seed
        coords[0] = (0.0, 0.0, 0.0)
        frames[0] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

    def local_to_global(i, v):
        e1, e2, e3 = frames[i]
        return _vadd(_vadd(_vscale(e1, v[0]), _vscale(e2, v[1])), _vscale(e3, v[2]))

    def next_slot(i):
        hybrid = [local_to_global(i, s) for s in _SLOTS[hyb[i]]]
        if not used[i]:
            return hybrid[0]
        # a slot is acceptable only when clear of EVERY used direction; among
        # acceptable slots pick the most separated one (matters for strained
        # rings where several slots pass the threshold)
        def sep(g):
            return min(_vdot(g, u) for u in used[i])
        acceptable = [g for g in hybrid
                      if max(_vdot(g, u) for u in used[i]) < 0.9]
        if acceptable:
            return min(acceptable, key=sep)
        # crowded/strained: complete the tetrahedron opposite the used sum
        comp = _vnorm((
            -sum(u[0] for u in used[i]),
            -sum(u[1] for u in used[i]),
            -sum(u[2] for u in used[i]),
        ))
        if comp:
            return comp
        for s in [(0, 1, 0), (0, 0, 1), (0, -1, 0), (0, 0, -1)]:
            g = local_to_global(i, s)
            if max(_vdot(g, u) for u in used[i]) < 0.9:
                return g
        return local_to_global(i, (0, 1, 0))

    def bfs_grow():
        q = deque(i for i in range(n) if coords[i] is not None)
        while q:
            u = q.popleft()
            for v in adj[u]:
                if coords[v] is not None:
                    continue
                ring = ring_of.get(v)
                if ring is not None:
                    if any(coords[i] is not None for i in ring):
                        continue  # ring is placed as a polygon, not a chain
                    if not any(coords[w] is not None for w in adj[v]
                               if w not in ring):
                        continue  # only the connecting anchor enters early
                o = order_of[(u, v)]
                d = next_slot(u)
                coords[v] = _vadd(coords[u], _vscale(d, _bond_len(atoms[u]["el"], atoms[v]["el"], o)))
                mark_used(u, d)
                back = _vscale(d, -1.0)
                e1 = back
                e3p = frames[u][2]
                if o == 2.0 and hyb[u] == "sp":  # cumulated double bond: perpendicular plane
                    e3 = _vcross(e3p, d)
                    e3 = _vnorm(e3) if _vdot(e3, e3) > 1e-6 else e3p
                else:
                    e3 = e3p
                e3 = _vnorm(_vsub(e3, _vscale(e1, _vdot(e3, e1)))) or _vnorm(frames[u][0])
                frames[v] = (e1, _vcross(e3, e1), e3)
                mark_used(v, back)
                q.append(v)

    bfs_grow()
    for ring in list(deferred):
        if any(coords[i] is not None for i in ring):
            place_ring(ring)
            deferred.remove(ring)
    bfs_grow()
    for ring in deferred:  # disconnected ring fragments: translate them apart
        placed_before = [i for i in range(n) if coords[i] is not None]
        place_ring(ring)
        new_idx = [i for i in ring if coords[i] is not None]
        if placed_before:
            dx = max(coords[b][0] for b in placed_before) - min(coords[i][0] for i in new_idx) + 3.0
            for i in new_idx:
                coords[i] = (coords[i][0] + dx, coords[i][1], coords[i][2])
    if deferred:
        bfs_grow()
    return coords


def _expand_hydrogens(atoms, bonds):
    """Turn implicit H counts into explicit atoms so they render as spheres."""
    atoms = [dict(a) for a in atoms]
    bonds = list(bonds)
    for i, a in enumerate(list(atoms)):
        for _ in range(a.get("h", 0)):
            atoms.append({"el": "H", "aromatic": False, "h": 0})
            bonds.append((i, len(atoms) - 1, 1.0))
        a["h"] = 0
    return atoms, bonds


def _rotate(p, rx, ry):
    x, y, z = p
    cy, sy = math.cos(ry), math.sin(ry)
    x, z = cy * x + sy * z, -sy * x + cy * z
    cx, sx = math.cos(rx), math.sin(rx)
    y, z = cx * y - sx * z, sx * y + cx * z
    return (x, y, z)


def _cumulated_view_basis(atoms, bonds, coords):
    """Return screen axes (x', y', z') that showcase allene-type perpendicular
    substituent planes: the cumulated axis lies horizontal on screen while the
    view direction is tilted between the two terminal plane normals so one
    terminal reads face-on and the other edge-on.  Returns None for molecules
    without a cumulated (C=C=C) core, which keep the default view."""
    adj: dict[int, list[tuple[int, float]]] = {}
    for a, b, o in bonds:
        adj.setdefault(a, []).append((b, o))
        adj.setdefault(b, []).append((a, o))
    for i, a in enumerate(atoms):
        if a["el"] in ("H", "R"):
            continue
        dbl = [j for j, o in adj.get(i, []) if o == 2.0]
        if len(dbl) != 2:
            continue
        t1, t2 = dbl
        axis = _vnorm(_vsub(coords[t2], coords[t1]))
        if not axis:
            continue

        def _plane_normal(t, other):
            for s, _o in adj.get(t, []):
                if s in (i, other):
                    continue
                d = _vnorm(_vsub(coords[s], coords[t]))
                nrm = _vnorm(_vcross(axis, d)) if d else None
                if nrm:
                    return nrm
            return None

        n1 = _plane_normal(t1, t2)
        n2 = _plane_normal(t2, t1)
        if not n1 or not n2:
            continue
        z = _vnorm(_vadd(_vadd(n2, _vscale(n1, 0.45)), _vscale(axis, 0.25)))
        y = _vnorm(_vsub(axis, _vscale(z, _vdot(axis, z))))
        if not z or not y:
            continue
        return (_vcross(y, z), y, z)
    return None


def _label_font(size: int = 22):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 9.2 fallback
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Pseudo-3D skeleton rendering helpers (style="3d"); style="flat" keeps the
# original flat vector look as the rollback switch.
# ---------------------------------------------------------------------------

_SPHERE_SPRITE_CACHE: dict[tuple[tuple[int, int, int], int], Any] = {}

# Screen-space light direction (from upper-left, toward the viewer).
_LIGHT_X, _LIGHT_Y, _LIGHT_Z = -0.45, -0.55, 0.70


def _fog_color(color: tuple[int, int, int], depth01: float,
               strength: float = 0.30) -> tuple[int, int, int]:
    """Fade a color toward white for distant geometry (depth cue)."""
    t = strength * (1.0 - max(0.0, min(1.0, depth01)))
    return tuple(int(c + (255 - c) * t) for c in color)


def _sphere_sprite(color: tuple[int, int, int], radius: float) -> Any:
    """Deterministic Lambert-shaded sphere sprite with a specular highlight."""
    from PIL import Image
    r = max(2, int(round(radius)))
    key = (color, r)
    cached = _SPHERE_SPRITE_CACHE.get(key)
    if cached is not None:
        return cached
    size = 2 * r + 3
    sprite = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = sprite.load()
    c = (size - 1) / 2.0
    for y in range(size):
        for x in range(size):
            dx, dy = (x - c) / r, (y - c) / r
            d2 = dx * dx + dy * dy
            if d2 > 1.0:
                continue
            nz = math.sqrt(1.0 - d2)
            lam = max(0.0, dx * _LIGHT_X + dy * _LIGHT_Y + nz * _LIGHT_Z)
            spec = lam ** 8
            col = tuple(int(min(255, ch * (0.30 + 0.80 * lam) + 235 * spec)) for ch in color)
            dist = math.sqrt(d2) * r
            alpha = 255 if dist <= r - 0.5 else int(255 * max(0.0, r + 0.5 - dist))
            px[x, y] = (col[0], col[1], col[2], alpha)
    _SPHERE_SPRITE_CACHE[key] = sprite
    return sprite


def _draw_cylinder_bond(draw: Any, x1: float, y1: float, x2: float, y2: float,
                        width: int, color: tuple[int, int, int]) -> None:
    """Fake a lit cylinder: base stroke plus a highlight stripe toward the light."""
    draw.line([(x1, y1), (x2, y2)], fill=color, width=width)
    dx, dy = x2 - x1, y2 - y1
    ln = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / ln, dx / ln
    if nx * _LIGHT_X + ny * _LIGHT_Y < 0:
        nx, ny = -nx, -ny
    off = max(1.0, width * 0.25)
    highlight = tuple(min(255, c + 70) for c in color)
    draw.line([(x1 + nx * off, y1 + ny * off), (x2 + nx * off, y2 + ny * off)],
              fill=highlight, width=max(1, width // 3))


def render_smiles_ball_and_stick(smiles: str, output_path: Path,
                                 img_size: tuple[int, int] = (900, 640),
                                 style: str = "3d") -> Path | None:
    """Render a SMILES string as a chemically accurate ball-and-stick PNG.

    ``style="3d"`` adds shaded spheres, cylinder-lit bonds, mild perspective
    and depth fog for a three-dimensional look; ``style="flat"`` keeps the
    original flat vector rendering (rollback switch).
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    # --- Try RDKit 3D fast-path: more accurate geometry for complex molecules ---
    rdkit_3d_result = _try_rdkit_3d(smiles)
    if rdkit_3d_result is not None:
        coords, atoms, bonds = rdkit_3d_result
        if not any(c is None for c in coords) and atoms:
            print(f"  Using RDKit 3D coordinates for {smiles!r}")
        else:
            rdkit_3d_result = None
    # --- Fallback to built-in pipeline ---
    if rdkit_3d_result is None:
        try:
            atoms, bonds = parse_smiles(smiles)
            if not atoms:
                return None
            atoms, bonds = _expand_hydrogens(atoms, bonds)
            coords = build_3d(atoms, bonds)
            if any(c is None for c in coords):
                return None
        except Exception as exc:
            print(f"  WARNING: skeleton build failed for {smiles!r}: {exc}")
            return None
    pts = [_rotate(p, math.radians(18), math.radians(28)) for p in coords]
    basis = _cumulated_view_basis(atoms, bonds, coords)
    if basis:
        bx, by, bz = basis
        pts = [(_vdot(p, bx), _vdot(p, by), _vdot(p, bz)) for p in coords]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    scale = min(img_size) * 0.62 / span
    ox, oy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    if style == "flat":
        px = [(img_size[0] / 2 + (p[0] - ox) * scale, img_size[1] / 2 + (p[1] - oy) * scale, p[2])
              for p in pts]
    else:
        # Mild perspective: nearer atoms grow up to ~14%, farther ones shrink.
        px = []
        for p in pts:
            f = 8.0 / (8.0 - p[2] / span)
            px.append((img_size[0] / 2 + (p[0] - ox) * scale * f,
                       img_size[1] / 2 + (p[1] - oy) * scale * f,
                       p[2]))
    zs = [p[2] for p in px]
    zmin, zmax = min(zs), max(zs)

    def depth01(z: float) -> float:
        return (z - zmin) / max(zmax - zmin, 1e-6)

    img = Image.new("RGB", img_size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    items = []
    for i, j, order in bonds:
        items.append(((px[i][2] + px[j][2]) / 2, ("bond", i, j, order)))
    for i, a in enumerate(atoms):
        items.append((px[i][2] + 0.01, ("atom", i, a["el"])))
    items.sort(key=lambda t: t[0])
    r_idx = 0
    for _, item in items:
        if item[0] == "bond":
            _, i, j, order = item
            x1, y1, x2, y2 = px[i][0], px[i][1], px[j][0], px[j][1]
            dx, dy = x2 - x1, y2 - y1
            ln = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / ln, dx / ln
            # .get() guards against unconventional RDKit bond orders
            # (e.g. 0.0 for dative/coordinate bonds) — draw a single line.
            offsets = {1.0: [0.0], 2.0: [-5.0, 5.0], 3.0: [-7.0, 0.0, 7.0],
                       1.5: [-4.0, 4.0]}.get(order, [0.0])
            if style == "flat":
                for off in offsets:
                    draw.line([(x1 + nx * off, y1 + ny * off), (x2 + nx * off, y2 + ny * off)],
                              fill=(120, 120, 120), width=5 if order == 1.0 else 4)
            else:
                bond_color = _fog_color((110, 110, 110),
                                        depth01((px[i][2] + px[j][2]) / 2))
                for off in offsets:
                    _draw_cylinder_bond(draw, x1 + nx * off, y1 + ny * off,
                                        x2 + nx * off, y2 + ny * off,
                                        6 if order == 1.0 else 5, bond_color)
        else:
            _, i, el = item
            x, y = px[i][0], px[i][1]
            if style == "flat":
                r = _ATOM_RADIUS.get(el, 20)
                color = _CPK_COLORS.get(el, (150, 150, 150))
                draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(20, 20, 20), width=2)
                draw.ellipse([x - r * 0.55, y - r * 0.65, x - r * 0.05, y - r * 0.15],
                             fill=tuple(min(255, c + 90) for c in color))
            else:
                depth = depth01(px[i][2])
                r = _ATOM_RADIUS.get(el, 20) * (0.85 + 0.30 * depth)
                color = _fog_color(_CPK_COLORS.get(el, (150, 150, 150)), depth, 0.18)
                sprite = _sphere_sprite(color, r)
                img.paste(sprite, (int(round(x - sprite.width / 2)),
                                   int(round(y - sprite.height / 2))), sprite)
            if el == "R":
                r_idx += 1
                draw.text((x, y), f"R{r_idx}", fill=(255, 255, 255),
                          font=_label_font(), anchor="mm")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path


# Normalized (x0, y0, x1, y1) candidate structure-panel regions per layout for
# pixel-exact programmatic skeleton compositing.  Entries must be calibrated
# against the ACTUAL generated layout of the named template (verified
# visually).  The previous module-cards / why-strategy-what regions were
# calibrated against a mismatched artwork and pasted the molecule over
# category cards, so they were removed.
# The model is not fully deterministic: it alternates between a wide horizontal
# structure panel at the top-left and a tall vertical panel on the left.  Each
# layout therefore retains known regions as safe fallbacks after live panel
# detection has failed.
# Automatic blank-panel detection is always attempted first because the image
# provider can move or resize a panel while keeping the same named layout.
# These calibrated regions are deliberately only fallbacks for outputs whose
# borders/backgrounds prevent automatic detection.
_COMPOSITE_REGIONS: dict[str, list[tuple[float, float, float, float]]] = {
    "module-cards-crosscut-sidebar": [
        # Variant A: wide horizontal structure panel at the top-left
        # (caption excluded), calibrated against _composite_test.png.
        (0.02, 0.13, 0.78, 0.365),
        # Variant B: tall vertical structure panel on the left
        # (caption excluded), calibrated against the 2026-08-06 run.
        (0.016, 0.16, 0.198, 0.795),
    ],
}


def _panel_whiteness(fig: Any, box: tuple[int, int, int, int]) -> float:
    """Fraction of near-white pixels in a region (coarse sampling guard)."""
    x0, y0, x1, y1 = box
    # Force RGB: the stride-3 pixel walk below would misalign on RGBA/other modes.
    raw = fig.crop((x0, y0, x1, y1)).convert("RGB").resize((120, 48)).tobytes()
    white = sum(1 for i in range(0, len(raw), 3)
                if raw[i] > 225 and raw[i + 1] > 225 and raw[i + 2] > 225)
    return white / max(1, len(raw) // 3)


_DETECT_GRID_COLS = 24
_DETECT_GRID_ROWS = 16
_DETECT_CELL_WHITENESS = 0.90
_REFINE_STRIP_BAND = 3


def _white_ratio_strip(image: Any, box: tuple[int, int, int, int]) -> float:
    """Return the near-white ratio for a narrow RGB image strip."""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = image.crop((x0, y0, x1, y1)).convert("RGB")
    raw = crop.tobytes()
    white = sum(
        1
        for offset in range(0, len(raw), 3)
        if raw[offset] > 225 and raw[offset + 1] > 225 and raw[offset + 2] > 225
    )
    return white / max(1, len(raw) // 3)


def _refine_panel_box(
    fig: Any,
    box: tuple[int, int, int, int],
    *,
    max_dx: int,
    max_dy: int,
    whiteness_threshold: float,
) -> tuple[int, int, int, int]:
    """Refine a coarse blank-panel box without crossing nearby content.

    Blank-panel detection works on a deliberately coarse grid.  This bounded
    strip scan recovers white space lost at cell boundaries and trims dirty
    boundary strips, while never moving an edge by more than one grid cell.
    """
    scale = 2
    small = fig.convert("RGB").resize(
        (max(1, fig.width // scale), max(1, fig.height // scale))
    )
    x0, y0, x1, y1 = (int(round(value / scale)) for value in box)
    limit_x = max(1, int(round(max_dx / scale)))
    limit_y = max(1, int(round(max_dy / scale)))
    threshold = max(0.0, min(1.0, whiteness_threshold))
    band = _REFINE_STRIP_BAND

    def _steps(limit: int) -> list[int]:
        return [min(band, limit - offset) for offset in range(0, limit, band)]

    # Expand into adjacent white strips, but only within the coarse-grid error
    # budget so a page background cannot swallow neighbouring panels.
    for step in _steps(limit_x):
        candidate = max(0, x0 - step)
        if candidate == x0 or _white_ratio_strip(small, (candidate, y0, x0, y1)) < threshold:
            break
        x0 = candidate
    for step in _steps(limit_x):
        candidate = min(small.width, x1 + step)
        if candidate == x1 or _white_ratio_strip(small, (x1, y0, candidate, y1)) < threshold:
            break
        x1 = candidate
    for step in _steps(limit_y):
        candidate = max(0, y0 - step)
        if candidate == y0 or _white_ratio_strip(small, (x0, candidate, x1, y0)) < threshold:
            break
        y0 = candidate
    for step in _steps(limit_y):
        candidate = min(small.height, y1 + step)
        if candidate == y1 or _white_ratio_strip(small, (x0, y1, x1, candidate)) < threshold:
            break
        y1 = candidate

    # If the coarse rectangle included a border glyph or coloured edge, trim
    # only the affected boundary and keep the same one-cell movement cap.
    for step in _steps(limit_x):
        width = x1 - x0
        probe = min(band, max(1, width // 3))
        if width <= 2 or _white_ratio_strip(small, (x0, y0, x0 + probe, y1)) >= threshold:
            break
        x0 = min(x1 - 1, x0 + step)
    for step in _steps(limit_x):
        width = x1 - x0
        probe = min(band, max(1, width // 3))
        if width <= 2 or _white_ratio_strip(small, (x1 - probe, y0, x1, y1)) >= threshold:
            break
        x1 = max(x0 + 1, x1 - step)
    for step in _steps(limit_y):
        height = y1 - y0
        probe = min(band, max(1, height // 3))
        if height <= 2 or _white_ratio_strip(small, (x0, y0, x1, y0 + probe)) >= threshold:
            break
        y0 = min(y1 - 1, y0 + step)
    for step in _steps(limit_y):
        height = y1 - y0
        probe = min(band, max(1, height // 3))
        if height <= 2 or _white_ratio_strip(small, (x0, y1 - probe, x1, y1)) >= threshold:
            break
        y1 = max(y0 + 1, y1 - step)

    return (
        max(0, min(fig.width - 1, x0 * scale)),
        max(0, min(fig.height - 1, y0 * scale)),
        max(1, min(fig.width, x1 * scale)),
        max(1, min(fig.height, y1 * scale)),
    )


def _looks_like_page_margin(box: tuple[int, int, int, int], width: int, height: int) -> bool:
    """True for page-background strips that are never a structure panel.

    The maximal-rectangle search also sees the white spine between two pages
    and the outer margin bands: strips that span (almost) the full canvas in
    one axis while staying narrow in the other.  Pasting the molecule there
    covers the canvas centre, so such strips are rejected as paste targets.
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if h >= 0.8 * height and w <= 0.22 * width:
        return True
    return w >= 0.8 * width and h <= 0.15 * height


def _looks_like_structure_panel(
    box: tuple[int, int, int, int], width: int, height: int
) -> bool:
    """Reject ordinary whitespace that cannot be the reserved molecule panel.

    The overview provider sometimes leaves a shallow white strip above the
    footer or beside the final card.  The maximal-rectangle detector used to
    accept that strip and paste the molecule there, which produced an
    unframed model floating in the lower-right corner.  A real structure panel
    is intentionally large in at least one direction and substantial in the
    other, remains inside the page margins, and does not start in the footer.
    """
    x0, y0, x1, y1 = box
    panel_w = x1 - x0
    panel_h = y1 - y0
    if panel_w <= 0 or panel_h <= 0:
        return False
    if x0 <= 0.008 * width or y0 <= 0.008 * height:
        return False
    if x1 >= 0.992 * width or y1 >= 0.992 * height:
        return False
    if y0 >= 0.80 * height:
        return False
    wide_panel = panel_w >= 0.30 * width and panel_h >= 0.15 * height
    tall_panel = panel_w >= 0.15 * width and panel_h >= 0.28 * height
    return wide_panel or tall_panel


def detect_blank_panel(fig: Any, min_area_fraction: float = 0.03,
                       max_area_fraction: float = 0.55,
                       whiteness_threshold: float = 0.85,
                       ) -> tuple[int, int, int, int] | None:
    """Locate the largest mostly-blank panel of a generated overview figure.

    Uncalibrated layouts carry no hand-measured structure-panel coordinates.
    In composite mode the prompt instructs the model to leave exactly one
    large panel blank white and to fill every other region, so the largest
    all-blank rectangle is the paste target.  A coarse grid plus the classic
    histogram-stack maximal-rectangle search ranks every blank rectangle;
    candidates are validated largest-first so a full-height page spine or
    margin band (``_looks_like_page_margin``) never wins over the reserved
    panel.  Each survivor is re-validated at sampled resolution with the same
    whiteness guard used for calibrated candidates.  Returns the box or None
    when no region qualifies.
    """
    W, H = fig.size
    small = fig.convert("RGB").resize(
        (_DETECT_GRID_COLS * 10, _DETECT_GRID_ROWS * 10)
    )
    px = small.load()
    cw = small.size[0] // _DETECT_GRID_COLS
    ch = small.size[1] // _DETECT_GRID_ROWS
    grid: list[list[int]] = []
    for gy in range(_DETECT_GRID_ROWS):
        row: list[int] = []
        for gx in range(_DETECT_GRID_COLS):
            white = total = 0
            for y in range(gy * ch, (gy + 1) * ch):
                for x in range(gx * cw, (gx + 1) * cw):
                    r, g, b = px[x, y]
                    total += 1
                    if r > 225 and g > 225 and b > 225:
                        white += 1
            row.append(1 if total and white / total >= _DETECT_CELL_WHITENESS else 0)
        grid.append(row)

    candidates: list[tuple[int, int, int, int, int]] = []
    heights = [0] * _DETECT_GRID_COLS
    for gy in range(_DETECT_GRID_ROWS):
        for gx in range(_DETECT_GRID_COLS):
            heights[gx] = heights[gx] + 1 if grid[gy][gx] else 0
        stack: list[int] = []
        for gx in range(_DETECT_GRID_COLS + 1):
            h = heights[gx] if gx < _DETECT_GRID_COLS else 0
            while stack and heights[stack[-1]] > h:
                top_h = heights[stack.pop()]
                left = stack[-1] + 1 if stack else 0
                candidates.append(
                    (top_h * (gx - left), gy - top_h + 1, left, gy, gx - 1)
                )
            stack.append(gx)

    # Largest first so the reserved (biggest) blank panel wins, while smaller
    # candidates remain available when the larger ones are page margins.
    rejected_margins: list[tuple[int, int, int, int]] = []
    for area, top, left, bottom, right in sorted(set(candidates), reverse=True):
        fraction = area / (_DETECT_GRID_ROWS * _DETECT_GRID_COLS)
        if fraction < min_area_fraction:
            break
        if fraction > max_area_fraction:
            continue
        box = (
            int(W * left / _DETECT_GRID_COLS),
            int(H * top / _DETECT_GRID_ROWS),
            int(W * (right + 1) / _DETECT_GRID_COLS),
            int(H * (bottom + 1) / _DETECT_GRID_ROWS),
        )
        # Sub-rectangles of a rejected margin strip are the same strip;
        # skipping them stops a shortened gutter from dodging the shape guard.
        if any(
            m[0] <= box[0] and m[1] <= box[1] and box[2] <= m[2] and box[3] <= m[3]
            for m in rejected_margins
        ):
            continue
        if _looks_like_page_margin(box, W, H):
            rejected_margins.append(box)
            continue
        box = _refine_panel_box(
            fig,
            box,
            max_dx=max(1, W // _DETECT_GRID_COLS),
            max_dy=max(1, H // _DETECT_GRID_ROWS),
            whiteness_threshold=whiteness_threshold,
        )
        refined_fraction = ((box[2] - box[0]) * (box[3] - box[1])) / max(1, W * H)
        if refined_fraction < min_area_fraction or refined_fraction > max_area_fraction:
            continue
        if _looks_like_page_margin(box, W, H):
            rejected_margins.append(box)
            continue
        if not _looks_like_structure_panel(box, W, H):
            continue
        if _panel_whiteness(fig, box) < whiteness_threshold:
            continue
        return box
    return None


def composite_skeleton_into_figure(figure_path: Path, skeleton_path: Path, layout: str,
                                   ) -> tuple[bool, str, str]:
    """Paste the exact ball-and-stick model into the layout's structure panel.

    Guarantees pixel-exact molecular geometry: the panel is cleared to white
    and the programmatically rendered model is centered into it.  The actual
    blank panel is detected from every generated image first so layout drift
    cannot reuse stale hand-measured coordinates.  Calibrated regions are only
    fallbacks when automatic detection cannot identify a safe target.

    Returns ``(ok, reason, panel_source)`` where ``panel_source`` is
    ``"calibrated-fallback"``, ``"auto-detected"``, ``"appended-dock"``,
    or ``""``.
    If the image model ignored the reserved-blank-panel instruction, the
    generated overview is preserved pixel-for-pixel and a white structure
    dock is appended to its right.  This is deliberately safer than clearing
    a non-white region that may contain scientific text or diagram content.
    """
    if not skeleton_path.exists():
        return False, "skeleton_missing", ""
    try:
        from PIL import Image
    except ImportError:
        return False, "pillow_unavailable", ""
    with Image.open(figure_path) as fig:
        fig = fig.convert("RGB")
        W, H = fig.size
        target = detect_blank_panel(fig)
        panel_source = "auto-detected" if target is not None else ""
        if target is None:
            for box in _COMPOSITE_REGIONS.get(layout, []):
                x0, y0, x1, y1 = (
                    int(W * box[0]),
                    int(H * box[1]),
                    int(W * box[2]),
                    int(H * box[3]),
                )
                # Guard: only paste into a mostly-blank panel.  If the layout
                # drifted and the region contains cards/text/colors, preserve it.
                candidate = (x0, y0, x1, y1)
                if (
                    _looks_like_structure_panel(candidate, W, H)
                    and _panel_whiteness(fig, candidate) >= 0.965
                ):
                    target = (x0, y0, x1, y1)
                    panel_source = "calibrated-fallback"
                    break
        if target is None:
            # The provider occasionally fills the explicitly reserved panel
            # with its own approximate molecule.  Never overwrite a populated
            # region: extend the canvas and place the integrity-checked exact
            # skeleton in a dedicated white dock instead.  The original image
            # remains unscaled and uncropped, so no generated content is lost.
            try:
                with Image.open(skeleton_path) as sk:
                    sk = sk.convert("RGB")
                    mask = sk.convert("L").point(lambda p: 255 if p < 245 else 0)
                    bbox = _skeleton_content_bbox(mask)
                    if bbox:
                        sk = sk.crop(bbox)

                    dock_width = max(320, min(480, int(round(W * 0.38))))
                    padding = max(22, int(round(dock_width * 0.08)))
                    canvas = Image.new("RGB", (W + dock_width, H), "white")
                    canvas.paste(fig, (0, 0))

                    # A restrained separator/frame makes the appended area
                    # read as an intentional scientific inset without adding
                    # any model-generated labels or changing the source art.
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(canvas)
                    separator_x = W + max(8, padding // 3)
                    draw.line(
                        (separator_x, padding, separator_x, H - padding),
                        fill=(38, 75, 130),
                        width=max(2, W // 500),
                    )
                    title = "Representative substrate"
                    title_font = _label_font(max(18, min(28, dock_width // 15)))
                    title_box = draw.textbbox((0, 0), title, font=title_font)
                    title_width = title_box[2] - title_box[0]
                    draw.text(
                        (W + (dock_width - title_width) // 2, padding),
                        title,
                        fill=(24, 56, 102),
                        font=title_font,
                    )
                    title_height = max(44, (title_box[3] - title_box[1]) + padding)
                    inset = (
                        W + padding,
                        padding + title_height,
                        W + dock_width - padding,
                        H - padding,
                    )
                    draw.rounded_rectangle(
                        inset,
                        radius=max(12, padding // 2),
                        fill="white",
                        outline=(182, 199, 219),
                        width=max(2, W // 500),
                    )

                    available_w = max(1, inset[2] - inset[0] - 2 * padding)
                    available_h = max(1, inset[3] - inset[1] - 2 * padding)
                    sk.thumbnail((available_w, available_h))
                    sx = inset[0] + (inset[2] - inset[0] - sk.width) // 2
                    sy = inset[1] + (inset[3] - inset[1] - sk.height) // 2
                    canvas.paste(sk, (sx, sy))
                    canvas.save(figure_path, format="PNG")
                return True, "", "appended-dock"
            except Exception as exc:
                return False, f"append_dock_failed:{type(exc).__name__}", ""
        x0, y0, x1, y1 = target
        _clear_panel_specks(fig, (x0, y0, x1, y1))
        with Image.open(skeleton_path) as sk:
            sk = sk.convert("RGB")
            # Trim the skeleton's white margins so the molecule uses the panel space.
            mask = sk.convert("L").point(lambda p: 255 if p < 245 else 0)
            bbox = _skeleton_content_bbox(mask)
            if bbox:
                sk = sk.crop(bbox)

            # Draw a deterministic inset frame instead of relying on the
            # image provider to preserve a visible panel border.  The header
            # and padding make the molecule read as a deliberate scientific
            # element, never as a loose overlay.
            from PIL import ImageDraw
            draw = ImageDraw.Draw(fig)
            panel_w = x1 - x0
            panel_h = y1 - y0
            border_width = max(2, min(W, H) // 420)
            radius = max(10, min(panel_w, panel_h) // 14)
            inset = max(border_width + 2, min(panel_w, panel_h) // 35)
            framed_box = (x0 + inset, y0 + inset, x1 - inset, y1 - inset)
            draw.rounded_rectangle(
                framed_box,
                radius=radius,
                fill="white",
                outline=(182, 199, 219),
                width=border_width,
            )
            title = "Representative structure"
            title_font = _label_font(max(14, min(24, panel_w // 28)))
            title_box = draw.textbbox((0, 0), title, font=title_font)
            title_width = title_box[2] - title_box[0]
            title_height = title_box[3] - title_box[1]
            title_x = x0 + (panel_w - title_width) // 2
            title_y = y0 + inset + max(8, panel_h // 40)
            draw.text(
                (title_x, title_y),
                title,
                fill=(24, 56, 102),
                font=title_font,
            )
            inner_padding = max(14, min(panel_w, panel_h) // 16)
            content_box = (
                framed_box[0] + inner_padding,
                title_y + title_height + inner_padding,
                framed_box[2] - inner_padding,
                framed_box[3] - inner_padding,
            )
            available_w = max(1, content_box[2] - content_box[0])
            available_h = max(1, content_box[3] - content_box[1])
            sk.thumbnail((available_w, available_h))
            sx = content_box[0] + (available_w - sk.width) // 2
            sy = content_box[1] + (available_h - sk.height) // 2
            fig.paste(sk, (sx, sy))
        fig.save(figure_path, format="PNG")
    return True, "", panel_source


def _clear_panel_specks(fig: Any, box: tuple[int, int, int, int],
                        max_speck_px: int = 500) -> None:
    """White-out small stray marks inside the structure panel.

    The whiteness guard guarantees the panel is mostly blank, but the model can
    leave tiny residue (stray glyphs, dots).  Blanketing the whole region with
    white would erase the panel's own border stroke, so instead only small
    non-white connected components that do NOT touch the region boundary are
    cleared; border fragments (touching the boundary) survive.
    """
    from collections import deque
    x0, y0, x1, y1 = box
    px = fig.load()
    seen = [[False] * (x1 - x0) for _ in range(y1 - y0)]

    def is_ink(x: int, y: int) -> bool:
        r, g, b = px[x, y][:3]
        return r < 240 or g < 240 or b < 240

    for sy in range(y0, y1):
        for sx in range(x0, x1):
            lx, ly = sx - x0, sy - y0
            if seen[ly][lx] or not is_ink(sx, sy):
                continue
            comp: list[tuple[int, int]] = []
            touches_edge = False
            queue = deque([(lx, ly)])
            seen[ly][lx] = True
            while queue:
                cx, cy = queue.popleft()
                comp.append((cx, cy))
                if cx == 0 or cy == 0 or cx == x1 - x0 - 1 or cy == y1 - y0 - 1:
                    touches_edge = True
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < x1 - x0 and 0 <= ny < y1 - y0 and not seen[ny][nx] \
                            and is_ink(x0 + nx, y0 + ny):
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if not touches_edge and len(comp) <= max_speck_px:
                for cx, cy in comp:
                    px[x0 + cx, y0 + cy] = (255, 255, 255)


def _looks_like_smiles(token: str) -> bool:
    """Cheap structural pre-filter before expensive parse validation.

    Rejects plain English words early: a SMILES token must contain at least
    one atom letter AND one structural feature (bond symbol, ring digit,
    branch parenthesis, bracket, or R-group marker).
    """
    if not re.search(r"[BCNOPSFIbcnops]|[A-Z][a-z]", token):
        return False
    if not re.search(r"[=#()\[\]*0-9%]", token):
        return False
    return True


def _validate_smiles_strict(token: str) -> bool:
    """Strict SMILES validation: RDKit sanitization when available.

    Without RDKit, rejects mostly-lowercase letter runs (English words)
    and requires >= 3 atoms from the fallback parser.
    """
    try:
        from rdkit import Chem
        from rdkit import RDLogger
        RDLogger.DisableLog("rdApp.*")
        return Chem.MolFromSmiles(token, sanitize=True) is not None
    except ImportError:
        pass
    # No RDKit: English words are mostly lowercase; real SMILES carry
    # uppercase atoms, digits, or bracketed groups.
    letters = [c for c in token if c.isalpha()]
    if letters and all(c.islower() for c in letters) \
            and not re.search(r"[0-9\[\]*/]", token):
        return False
    try:
        atoms, _bonds = _parse_smiles_fallback(token)
        return bool(atoms) and len(atoms) >= 3
    except Exception:
        return False


def _extract_smiles_from_text(text: str) -> str:
    """Scan free text (manuscript/outline) for a representative SMILES string.

    Extracts candidate tokens bounded by non-word characters, applies a
    structural pre-filter, then strict validation (RDKit when installed).
    Markdown bold markers (``**``) around a candidate are stripped first.

    Selection is score-based, NOT longest-wins: review manuscripts are full
    of specific substrate/product SMILES in reaction tables, and the longest
    one is almost always an example compound rather than the representative
    core motif.  Scoring therefore prefers generic R-group motifs (``*``),
    repeated occurrences, and moderate length.
    """
    if not text:
        return ""
    raw_candidates = re.findall(
        r"(?<![\w.])"
        r"([*\[\]A-Za-z0-9@+\-=#:/\\()%.]{3,60})"
        r"(?![\w])",
        text,
    )
    # Normalize (strip markdown bold markers) and count occurrences
    cleaned: list[str] = []
    for cand in raw_candidates:
        while cand.startswith("**") and cand.endswith("**") and len(cand) > 4:
            cand = cand[2:-2]
        if len(cand) >= 3:
            cleaned.append(cand)
    counts: dict[str, int] = {}
    for cand in cleaned:
        counts[cand] = counts.get(cand, 0) + 1

    best = ""
    best_score = 0.0
    for cand, freq in counts.items():
        if not _looks_like_smiles(cand):
            continue
        if not _validate_smiles_strict(cand):
            continue
        score = 1.0
        # Generic R-group motifs represent the core family, not one example
        if "*" in cand:
            score += 5.0
        # Repeated occurrences suggest a recurring core structure
        score += min(freq, 3)
        # Short-to-moderate length suggests a motif; very long strings are
        # usually specific example compounds from tables/schemes.
        if len(cand) <= 25:
            score += 1.0
        elif len(cand) > 45:
            score -= 3.0
        if score > best_score:
            best_score = score
            best = cand
    return best


def resolve_skeleton_smiles(features: dict[str, Any]) -> str:
    """Resolve the review's core-motif SMILES.

    Resolution priority:
    1. Explicit ``skeleton_smiles`` / ``smiles`` from query_plan (optional override).
    2. Theme-driven match: review title + product keywords against
       ``_LABEL_SMILES``.  The review THEME defines the representative core
       motif for the overview; this is more reliable than scanning the
       manuscript, which is full of specific example compounds.
    3. Scored scan of the final manuscript / draft text (theme-unmatched only).
    4. Outline text scan and broad keyword blob fallback.

    Returns "" when no motif can be determined.
    """
    # Priority 1: explicit skeleton_smiles (optional override, not required)
    smiles = str(features.get("skeleton_smiles", "") or "").strip()
    if smiles:
        return smiles
    smiles = str(features.get("smiles", "") or "").strip()
    if smiles:
        return smiles

    # Only chemistry-context projects should attempt molecule extraction
    if not is_chemistry_context(features):
        return ""

    # Priority 2: theme-driven motif lookup (title + product keywords).
    # Example: "axially chiral allenes" -> *C(*)=C=C(*)* regardless of the
    # specific substrate/product SMILES scattered through the manuscript.
    products = features.get("product_keywords", [])
    theme_blob = " ".join(
        [str(features.get("review_title", "") or "")]
        + [str(p) for p in products]
    )
    smiles = _smiles_for_label(theme_blob) or ""
    if smiles:
        return smiles

    # Priority 3: scan manuscript/draft text (theme did not match a motif)
    project_dir = features.get("_project_dir")
    if project_dir:
        # Try final/first draft (most authoritative, generated before overview)
        for sub in ("04_first_draft", "02_section_drafting"):
            for name in ("first_draft.md", "section_drafts.md"):
                draft_path = Path(project_dir) / sub / name
                if draft_path.exists():
                    text = draft_path.read_text(encoding="utf-8", errors="ignore")
                    smiles = _extract_smiles_from_text(text)
                    if smiles:
                        return smiles

    # Priority 4: outline text scan and broad keyword blob fallback
    outline = str(features.get("_outline_text", "") or "")
    if outline:
        smiles = _extract_smiles_from_text(outline)
        if smiles:
            return smiles
        blob_parts = [features.get("review_title", ""), outline[:800]]
        if project_dir:
            draft_path = Path(project_dir) / "04_first_draft" / "first_draft.md"
            if draft_path.exists():
                blob_parts.append(draft_path.read_text(encoding="utf-8", errors="ignore")[:1200])
        smiles = _smiles_for_label(" ".join(blob_parts)) or ""
        if smiles:
            return smiles
    return ""


def render_skeleton_model(features: dict[str, Any], output_path: Path,
                          img_size: tuple[int, int] = (900, 640),
                          style: str = "3d") -> Path | None:
    """Render the review's core motif as an accurate ball-and-stick PNG."""
    smiles = resolve_skeleton_smiles(features)
    if not smiles:
        return None
    return render_smiles_ball_and_stick(smiles, output_path, img_size, style)


def skeleton_atom_counts(smiles: str) -> tuple[int, int] | None:
    """(rendered atom count, R-group count) used by the AI-redraw gate.

    The exact reference renderer expands implicit hydrogens into visible white
    spheres, and the AI prompt explicitly requires preserving those spheres.
    Counting only heavy atoms made a correct ``OCC#C*`` redraw look like nine
    blobs against an expectation of five and falsely rejected it.  The gate is
    deliberately only a catastrophic-hallucination check, so its expectation
    must match every atom actually present in the reference image.
    """
    try:
        atoms, bonds = parse_smiles(smiles)
        if not atoms:
            return None
        atoms, bonds = _expand_hydrogens(atoms, bonds)
    except Exception:
        return None
    visible = len(atoms)
    r_count = sum(1 for a in atoms if a["el"] == "R")
    return visible, r_count


# ---------------------------------------------------------------------------
# Form B: isolated AI style-transfer of the exact skeleton ("ai3d" style).
# The model only restyles; a programmatic sanity gate must accept the redraw
# before it replaces the deterministic skeleton, otherwise we fall back.
# ---------------------------------------------------------------------------

# Gate tuning knobs, calibrated 2026-08-06 against the programmatic flat/3D
# renders and an accepted AI redraw (all analyzed at _GATE_SMALL_SIZE):
_GATE_SMALL_SIZE = (450, 320)        # downscale size for gate analysis
_GATE_INK_THRESHOLD = 235            # pixels darker than this count as ink
_GATE_EMPTY_INK_RATIO = 0.005        # less ink than this => empty image
_GATE_INK_EROSION = 5                # MinFilter width that erases thin bonds
_GATE_ATOM_BLOB_MIN_PX = 8           # min surviving blob size (one atom)
_GATE_ATOM_BLOB_RANGE = (0.5, 1.6)   # accepted blob count vs expected atoms
_GATE_BLUE_MIN_PX = 30               # R spheres are large; no erosion needed
_GATE_BLUE_MERGE_PX = 24             # centroid distance that merges R-sphere fragments
_GATE_R_TOLERANCE = 1                # allowed |detected - expected| R labels

_AI_SKELETON_REDRAW_PROMPT = (
    "STYLE-TRANSFER TASK. The attached reference is an exact ball-and-stick diagram of ONE "
    "molecule. Re-render this EXACT molecule as a glossy photorealistic 3D ball-and-stick "
    "model on a pure white background: shaded spheres with specular highlights, cylindrical "
    "lit bonds, soft studio lighting, mild perspective.\n"
    "PRESERVE EXACTLY (chemistry must not change):\n"
    "- every atom: same count, same topology, same colors (black carbon, red oxygen, blue "
    "nitrogen, white hydrogen, blue labeled R-group spheres);\n"
    "- every bond order: single/double/triple exactly as shown (parallel lines = multiple bonds);\n"
    "- the perpendicular orientation of the two terminal substituent planes around the "
    "cumulated C=C=C core;\n"
    "- all label texts verbatim (R1, R2, ...).\n"
    "Do not add, remove, merge or relabel atoms. No caption, no border, no extra text; "
    "output only the molecule centered on white."
)

# The redraw is non-deterministic and the gate probabilistic, so a single
# rejection is not evidence the model cannot do the job: retry the full
# request before declaring the redraw failed.
_AI_SKELETON_REDRAW_ATTEMPTS = 3


def _blob_stats(mask: Any, min_px: int) -> list[tuple[float, float, float, int, int, int, int]]:
    """Return (size, cx, cy, x0, y0, x1, y1) for every connected 255-valued
    blob of at least min_px pixels (4-neighbor)."""
    from collections import deque
    w, h = mask.size
    px = mask.load()
    seen = bytearray(w * h)
    stats: list[tuple[float, float, float, int, int, int, int]] = []
    for y0 in range(h):
        row = y0 * w
        for x0 in range(w):
            idx = row + x0
            if seen[idx] or not px[x0, y0]:
                continue
            seen[idx] = 1
            stack = deque((idx,))
            points: list[int] = []
            while stack:
                i = stack.pop()
                points.append(i)
                x, y = i % w, i // w
                if x + 1 < w:
                    j = i + 1
                    if not seen[j] and px[x + 1, y]:
                        seen[j] = 1
                        stack.append(j)
                if x > 0:
                    j = i - 1
                    if not seen[j] and px[x - 1, y]:
                        seen[j] = 1
                        stack.append(j)
                if y + 1 < h:
                    j = i + w
                    if not seen[j] and px[x, y + 1]:
                        seen[j] = 1
                        stack.append(j)
                if y > 0:
                    j = i - w
                    if not seen[j] and px[x, y - 1]:
                        seen[j] = 1
                        stack.append(j)
            if len(points) < min_px:
                continue
            xs = [p % w for p in points]
            ys = [p // w for p in points]
            stats.append(
                (
                    float(len(points)),
                    sum(xs) / len(points),
                    sum(ys) / len(points),
                    min(xs),
                    min(ys),
                    max(xs),
                    max(ys),
                )
            )
    return stats


def _skeleton_content_bbox(mask: Any) -> tuple[int, int, int, int] | None:
    """Bound the molecular drawing while discarding isolated raster specks.

    The main connected component anchors the molecule.  Smaller components are
    retained when they are chemically meaningful in size or close enough to be
    a detached atom/label; tiny distant artefacts are excluded.  The returned
    maximum coordinates are exclusive, as required by ``PIL.Image.crop``.
    """
    stats = _blob_stats(mask, min_px=8)
    if not stats:
        return None
    main = max(stats, key=lambda item: item[0])
    main_area, main_cx, main_cy = main[:3]
    proximity = max(12.0, math.hypot(mask.width, mask.height) * 0.30)
    min_meaningful_area = max(8.0, main_area * 0.02)
    kept = [
        item
        for item in stats
        if item[0] >= min_meaningful_area
        or math.hypot(item[1] - main_cx, item[2] - main_cy) <= proximity
    ]
    if not kept:
        kept = [main]
    return (
        min(int(item[3]) for item in kept),
        min(int(item[4]) for item in kept),
        max(int(item[5]) for item in kept) + 1,
        max(int(item[6]) for item in kept) + 1,
    )


def _count_blobs(mask: Any, min_px: int) -> int:
    """Count connected 255-valued blobs of at least min_px pixels (4-neighbor)."""
    return len(_blob_stats(mask, min_px))


def _clustered_blob_count(
    stats: list[tuple[float, float, float, int, int, int, int]],
    merge_px: float,
) -> int:
    """Count blobs after merging fragments of one physical object.

    Glossy specular bands can split a single sphere into several disconnected
    mask blobs; fragments overlap on one axis with at most a merge_px-wide gap
    on the other (or have centroids within merge_px) and are treated as one
    object. Well-separated blobs stay distinct.
    """
    count = len(stats)
    parent = list(range(count))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(count):
        _, cx_i, cy_i, x0_i, y0_i, x1_i, y1_i = stats[i]
        for j in range(i + 1, count):
            _, cx_j, cy_j, x0_j, y0_j, x1_j, y1_j = stats[j]
            sep_x = max(x0_i, x0_j) - min(x1_i, x1_j)
            sep_y = max(y0_i, y0_j) - min(y1_i, y1_j)
            aligned = (
                (sep_x <= 0 or sep_y <= 0)
                and sep_x <= merge_px
                and sep_y <= merge_px
            )
            close = (cx_i - cx_j) ** 2 + (cy_i - cy_j) ** 2 <= merge_px * merge_px
            if aligned or close:
                parent[find(j)] = find(i)
    return len({find(i) for i in range(count)})


def _ai_redraw_gate(img: Any, expected_atoms: int, expected_r: int) -> tuple[bool, str]:
    """Sanity-check an AI skeleton redraw: atom-blob and R-label counts.

    Bonds are thin, atoms are thick: eroding the ink mask removes bonds so
    each surviving blob approximates one atom.  Not a proof of correctness --
    a probabilistic gate that catches catastrophic hallucinations only.
    """
    from PIL import Image, ImageFilter
    small = img.resize(_GATE_SMALL_SIZE)
    r_ch, g_ch, b_ch = small.split()
    rp, gp, bp = r_ch.load(), g_ch.load(), b_ch.load()
    ink = small.convert("L").point(lambda p: 255 if p < _GATE_INK_THRESHOLD else 0)
    blue = Image.new("L", small.size, 0)
    blp = blue.load()
    for y in range(small.size[1]):
        for x in range(small.size[0]):
            rv, gv, bv = rp[x, y], gp[x, y], bp[x, y]
            if bv >= rv + 25 and bv >= gv + 15 and bv > 90:
                blp[x, y] = 255
    ink_px = ink.histogram()[255]
    if ink_px < _GATE_EMPTY_INK_RATIO * small.size[0] * small.size[1]:
        return False, "empty_image"
    blobs = _count_blobs(ink.filter(ImageFilter.MinFilter(_GATE_INK_EROSION)),
                         _GATE_ATOM_BLOB_MIN_PX)
    lo, hi = _GATE_ATOM_BLOB_RANGE
    if not (lo * expected_atoms <= blobs <= hi * expected_atoms):
        return False, f"atom_blobs_{blobs}_expected_about_{expected_atoms}"
    # No erosion for the blue mask: specular highlights hole the spheres and
    # erosion would fragment them; R spheres are large, so a size floor suffices.
    # A highlight band can still split one sphere into disconnected fragments,
    # so nearby fragments are clustered back into single spheres before counting.
    blue_stats = _blob_stats(blue, _GATE_BLUE_MIN_PX)
    r_blobs = _clustered_blob_count(blue_stats, _GATE_BLUE_MERGE_PX)
    if abs(r_blobs - expected_r) > _GATE_R_TOLERANCE:
        return False, f"r_labels_{r_blobs}_expected_{expected_r}"
    return True, "gate_passed"


def attempt_ai_skeleton_redraw(features: dict[str, Any], reference_png: Path,
                               output_png: Path, api_key: str, base_url: str,
                               model: str, wire_api: str,
                               attempts: int = _AI_SKELETON_REDRAW_ATTEMPTS,
                               ) -> tuple[Path | None, str, list[str]]:
    """Ask the image model to restyle the exact skeleton into a 3D render.

    Returns ``(path, note, attempt_notes)``; ``path`` is None when every
    attempt failed or the sanity gate rejected each redraw (the caller then
    keeps the programmatic skeleton, or fails in strict mode).  The redraw is
    non-deterministic, so the full request is retried up to ``attempts``
    times before giving up.
    """
    smiles = resolve_skeleton_smiles(features)
    counts = skeleton_atom_counts(smiles) if smiles else None
    if counts is None:
        return None, "no_smiles_for_gate", ["no_smiles_for_gate"]
    attempt_notes: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        try:
            image_bytes = call_image_edit_api(
                api_key, base_url, reference_png, _AI_SKELETON_REDRAW_PROMPT,
                model=model, wire_api=wire_api)
        except Exception as exc:
            attempt_notes.append(f"attempt_{attempt}:api_error:{exc}"[:300])
            continue
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        except Exception as exc:
            attempt_notes.append(f"attempt_{attempt}:undecodable_image:{exc}"[:300])
            continue
        try:
            ok, note = _ai_redraw_gate(img, counts[0], counts[1])
        except Exception as exc:
            attempt_notes.append(f"attempt_{attempt}:gate_error:{exc}"[:300])
            continue
        if not ok:
            # Keep every rejected draft for human inspection / gate tuning.
            try:
                output_png.parent.mkdir(parents=True, exist_ok=True)
                suffix = "" if attempt == 1 else f"_{attempt}"
                output_png.with_name(
                    output_png.stem + f"_rejected{suffix}.png"
                ).write_bytes(image_bytes)
            except OSError:
                pass
            attempt_notes.append(f"attempt_{attempt}:gate_rejected:{note}")
            continue
        attempt_notes.append(f"attempt_{attempt}:gate_passed")
        output_png.parent.mkdir(parents=True, exist_ok=True)
        output_png.write_bytes(image_bytes)
        return output_png, "gate_passed", attempt_notes
    return None, attempt_notes[-1] if attempt_notes else "no_attempts", attempt_notes


def resolve_api_key(cli_value: str, base_url: str) -> str:
    """Use the matching credential when text and image providers differ."""
    del base_url
    return _shared_resolve_api_key(
        cli_value,
        env_names=("IMAGE_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )


def build_overview_display_title(features: dict[str, Any]) -> str:
    """Return the shared concise title used by overview and final outputs."""
    return build_publication_review_title(
        features.get("review_title") or "",
        manuscript_title=features.get("manuscript_title") or "",
    )


# ---------------------------------------------------------------------------
# Template matching logic
# ---------------------------------------------------------------------------

def extract_review_features(project_dir: Path) -> dict[str, Any]:
    """Extract structural features from the review project for template scoring."""
    features: dict[str, Any] = {
        "num_sections": 0,
        "has_metal_classification": False,
        "has_organocatalysis": False,
        "has_chirality": False,
        "has_reaction_focus": False,
        "metal_categories": [],
        "time_window": "",
        "reaction_type": "",
        "group_by": [],
        "review_title": "",
        "manuscript_title": "",
        "display_title": "",
        "classification_rule": "",
        "product_keywords": [],
        "substrate_keywords": [],
        "catalyst_keywords": [],
        "skeleton_smiles": "",
        "overview_axis_contract": {},
    }

    # Read query plan for group_by, filters, keywords, and topic
    qp_path = project_dir / "00_discovery" / "query_plan.draft.json"
    if qp_path.exists():
        qp = read_json(qp_path)
        features["group_by"] = qp.get("group_by", [])
        features["review_title"] = qp.get("topic", "")
        filters = qp.get("filters", {})
        y_from = filters.get("year_from", "")
        y_to = filters.get("year_to", "")
        if y_from and y_to:
            features["time_window"] = f"{y_from}-{y_to}"
        # Extract keywords by category
        for kw_entry in qp.get("keywords", []):
            cat = kw_entry.get("category", "")
            word = kw_entry.get("keyword", "")
            if cat == "product":
                features["product_keywords"].append(word)
            elif cat == "substrate":
                features["substrate_keywords"].append(word)
            elif cat == "catalyst_or_method":
                features["catalyst_keywords"].append(word)
        # Derive classification rule from group_by
        gb = features["group_by"]
        if gb:
            rule_map = {
                "catalyst_or_method": "By catalyst center metal",
                "substrate": "By substrate class",
                "reaction_type": "By reaction type",
                "product": "By product class",
                "leaving_group": "By leaving group type",
                "ligand_or_chiral_source": "By ligand class",
                "organometallic_partner": "By organometallic partner",
                "document_scope": "By document type",
            }
            features["classification_rule"] = rule_map.get(gb[0], f"By {gb[0]}")
        features["skeleton_smiles"] = str(qp.get("skeleton_smiles", "") or qp.get("smiles", "") or "")

    # The confirmed Planning contract is authoritative for the overview's
    # primary grouping. Discovery query hints must not silently retheme the
    # final overview to a different taxonomy.
    blueprint_path = project_dir / "01_matrix_outline" / "section_blueprint.json"
    if blueprint_path.exists():
        blueprint = read_json(blueprint_path)
        basis = blueprint.get("classification_basis") if isinstance(blueprint, dict) else {}
        if isinstance(basis, dict):
            axis = str(basis.get("overview_axis") or basis.get("primary_axis") or "").strip()
            axis_map = {
                "substrate_classes": "substrate",
                "catalyst_or_method": "catalyst_or_method",
                "reaction_strategy": "reaction_type",
                "user_defined": "document_scope",
            }
            if axis:
                features["overview_axis_contract"] = basis
                features["group_by"] = [axis_map.get(axis, axis)]
                features["classification_rule"] = str(
                    basis.get("description") or f"By {axis.replace('_', ' ')}"
                )

    # Fallback: recover the review topic from PostgreSQL artifacts that the
    # native final-overview compatibility workspace actually materializes,
    # because the discovery query plan is not available there.
    if not features.get("review_title"):
        _recover_review_title(project_dir, features)

    # Read selected outline for section structure
    outline_path = project_dir / "01_matrix_outline" / "selected_outline.md"
    if outline_path.exists():
        _apply_outline_signals(outline_path.read_text(encoding="utf-8"), features)

    # Fallback: the native final-overview compatibility workspace does not
    # materialize the selected outline, so reuse the section-draft markdown
    # and the first draft, which carry the same heading structure and theme
    # signals.
    if not features.get("_outline_text"):
        drafts_md_path = project_dir / "02_section_drafting" / "section_drafts.md"
        if drafts_md_path.exists():
            _apply_outline_signals(
                drafts_md_path.read_text(encoding="utf-8"), features
            )
    if not features.get("_outline_text"):
        first_draft_path = project_dir / "04_first_draft" / "first_draft.md"
        if first_draft_path.exists():
            _apply_outline_signals(
                first_draft_path.read_text(encoding="utf-8", errors="ignore"), features
            )

    # Infer the classification dimension when the query plan is unavailable
    # but the recovered outline/draft clearly organizes by catalyst metals.
    if not features.get("group_by") and features.get("has_metal_classification"):
        features["group_by"] = ["catalyst_or_method"]
        features["classification_rule"] = "By catalyst center metal"

    # Backfill categories from first_draft.md section headings when the
    # outline is too sparse to fill the figure (avoids empty panels)
    real_cats = [c for c in features["metal_categories"] if c != "Organocatalysis"]
    if len(real_cats) < 3:
        draft_cats = _categories_from_draft(project_dir)
        if len(draft_cats) > len(real_cats):
            features["metal_categories"] = draft_cats

    # Read discovery results for group stats
    sel_path = project_dir / "00_discovery" / "selected_discovery_results.json"
    if sel_path.exists():
        sel = read_json(sel_path)
        features["group_by"] = sel.get("group_by", features["group_by"])

    # Resolve the taxonomy profile for cross-domain prompt adaptation.
    # Without this, non-chemistry reviews inherit allene-centric prompts.
    _resolve_taxonomy_profile(project_dir, features)
    features["display_title"] = build_overview_display_title(features)

    # Store project dir for multi-pass extraction in _build_metal_rows_text
    features["_project_dir"] = project_dir

    return features


def _heading_to_category(title: str) -> str:
    """Convert a section heading into a short, clean category label."""
    title_clean = " ".join(title.split())
    low = title_clean.lower()
    skip_words = {
        "introduction", "conclusion", "conclusions", "outlook", "summary",
        "abstract", "keywords", "references", "acknowledgment", "acknowledgments",
    }
    words_low = low.split()
    if not words_low or words_low[0] in skip_words or low in skip_words:
        return ""
    if "comparison" in low or "landscape" in low or "method selection" in low:
        return ""
    # Normalize catch-all sections to a single clean label
    if words_low[0] == "other":
        return "Others"
    # Remove common prefixes like "Allenylation via", "Synthesis from"
    short = re.sub(r"^(alleny?lation|synthesis|reactions?)\s+(via|from|of|through)\s+", "", title_clean, flags=re.IGNORECASE)
    # Remove trailing generic words
    short = re.sub(r"\s+(leaving groups?|and other.*|\(.*\))$", "", short, flags=re.IGNORECASE)
    # If still contains 'via'/'from', take the words after it
    if re.search(r"\b(via|from)\b", short, flags=re.IGNORECASE):
        parts = re.split(r"\b(via|from)\b", short, flags=re.IGNORECASE)
        short = parts[-1].strip() if len(parts) > 1 else short
    # 'X and Y' compound headings: keep the first chunk
    short = re.split(r"\s+and\s+", short, flags=re.IGNORECASE)[0].strip()
    # Take first 2 words max (keep it short for a hexagon label)
    label = " ".join(short.split()[:2])
    return label if label and len(label) <= 30 else ""


def _categories_from_draft(project_dir: Path) -> list[str]:
    """Extract category labels from first_draft.md headings when the outline
    is too sparse, so the overview figure always has enough panels."""
    if not project_dir:
        return []
    draft_path = project_dir / "04_first_draft" / "first_draft.md"
    if not draft_path.exists():
        return []
    text = draft_path.read_text(encoding="utf-8", errors="ignore")
    cats: list[str] = []
    for title in re.findall(r"^##\s+(?:\d+\.\s*)?(.+)$", text, re.MULTILINE):
        label = _heading_to_category(title)
        if label and label not in cats:
            cats.append(label)
    return cats


def _recover_review_title(project_dir: Path, features: dict[str, Any]) -> None:
    """Recover the review topic from materialized PostgreSQL artifacts.

    The native final-overview compatibility workspace does not carry the
    discovery query plan, but it always materializes the section blueprint
    and literature matrix, both of which store ``review_topic``.
    """
    for relative in (
        "01_matrix_outline/section_blueprint.json",
        "01_matrix_outline/literature_matrix.json",
    ):
        path = project_dir / relative
        if not path.exists():
            continue
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            topic = str(data.get("review_topic") or "").strip()
            if topic:
                features["review_title"] = topic
                return


def _apply_outline_signals(text: str, features: dict[str, Any]) -> None:
    """Populate section-level features from markdown outline or draft text.

    The selected outline is the preferred source, but the section-draft
    markdown and the first draft are equally usable when the native
    compatibility workspace does not materialize the discovery/outline
    files.  Signals are merged with OR/max semantics so multiple sources
    only strengthen the result.
    """
    text = str(text or "")
    if not text.strip():
        return
    manuscript_heading = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    if manuscript_heading and not features.get("manuscript_title"):
        heading = " ".join(manuscript_heading.group(1).split()).strip()
        if heading and heading.casefold() not in {"draft", "first draft", "review"}:
            features["manuscript_title"] = heading
    if not features.get("_outline_text"):
        features["_outline_text"] = text
    low = text.lower()
    headings = re.findall(r"^##\s+(?:\d+[.:]\s*)?(.+)$", text, re.MULTILINE)
    features["num_sections"] = max(
        int(features.get("num_sections") or 0), len(headings)
    )
    if "catalyst" in low and "metal" in low:
        features["has_metal_classification"] = True
    if "organocatal" in low:
        features["has_organocatalysis"] = True
    metal_map = {
        "palladium": "Pd", "copper": "Cu", "nickel": "Ni",
        "cobalt": "Co", "gold": "Au", "rhodium": "Rh",
        "iridium": "Ir", "iron": "Fe",
    }
    for key, sym in metal_map.items():
        if key in low and sym not in features["metal_categories"]:
            features["metal_categories"].append(sym)
    if (
        features.get("has_organocatalysis")
        and "Organocatalysis" not in features["metal_categories"]
    ):
        features["metal_categories"].append("Organocatalysis")
    if not features.get("has_chirality"):
        features["has_chirality"] = bool(
            re.search(r"chiral|enantio|asymmetric|atropisomer|stereoselect", low)
        )
    if not features.get("has_reaction_focus"):
        features["has_reaction_focus"] = bool(
            re.search(
                r"reaction|synthesis|synthetic|catalytic|cataly[sz]ed|coupling|functionalization",
                low,
            )
        )
    generic_cats: list[str] = []
    for title in headings:
        label = _heading_to_category(title)
        if label and label not in generic_cats:
            generic_cats.append(label)
    real_metals = [m for m in features["metal_categories"] if m != "Organocatalysis"]
    if len(real_metals) < 3 and len(generic_cats) > len(real_metals):
        features["metal_categories"] = generic_cats


def _resolve_taxonomy_profile(project_dir: Path, features: dict[str, Any]) -> None:
    """Set the taxonomy profile for cross-domain prompt adaptation.

    Resolution order: explicit ``REVIEW_TAXONOMY_PROFILE`` override →
    persisted ``project_config.json`` → topic-signal inference from the
    review title.  Without this, the generic-project adaptation path in
    ``build_adapted_prompt`` is dead code and non-chemistry reviews inherit
    allene-centric template prompts and skeleton geometry.
    """
    if features.get("taxonomy_profile"):
        return
    env_profile = os.environ.get("REVIEW_TAXONOMY_PROFILE", "").strip()
    if env_profile:
        features["taxonomy_profile"] = env_profile
        return
    config_path = project_dir / "project_config.json"
    if config_path.exists():
        try:
            config = read_json(config_path)
        except (OSError, ValueError):
            config = {}
        if isinstance(config, dict):
            configured = str(config.get("taxonomy_profile") or "").strip()
            if configured:
                features["taxonomy_profile"] = configured
                return
    features["taxonomy_profile"] = _suggest_taxonomy_profile(
        features.get("review_title") or ""
    )


def score_template(template: dict[str, Any], features: dict[str, Any]) -> float:
    """Score a template against review features. Higher = better match.

    The score is driven by theme-dependent features (classification
    dimension, category count, chirality, reaction focus, section count)
    so that different review topics select different templates instead of
    always converging on one layout.
    """
    score = 0.0
    layout = template.get("layout_type", "")
    prompt = template.get("prompt", "").lower()
    desc = template.get("description", "").lower()

    group_by = (features.get("group_by") or [""])[0]
    categories = features.get("metal_categories", [])
    num_categories = len(categories)
    num_sections = features.get("num_sections", 0)
    has_metal = bool(features.get("has_metal_classification"))
    has_chirality = bool(features.get("has_chirality"))
    has_reaction_focus = bool(features.get("has_reaction_focus"))

    # 1. Classification dimension: match layout semantics to group_by
    if group_by == "catalyst_or_method":
        if ("catalyst center metal" in prompt or "metal-centered" in prompt
                or "metal" in layout or "catal" in prompt):
            score += 4.0
    elif group_by in {"substrate", "leaving_group", "product", "ligand_or_chiral_source",
                      "organometallic_partner", "reaction_type", "document_scope"}:
        # Non-catalyst dimensions: penalize metal-centric framing, reward
        # layouts that generalize to arbitrary category families
        if "metal-centered" in prompt or "metal" in layout:
            score -= 2.5
        if layout in {"module-cards-crosscut-sidebar", "route-map-start-strategy-result",
                      "tree-metal-classification", "mosaic-infographic",
                      "metal-x-dimension-matrix"}:
            score += 2.0
    else:
        score += 0.5

    # 2. Category count fit: templates are drawn with 5 slots; layouts that
    # flex to other counts are rewarded when the topic has != 5 categories
    if num_categories:
        if num_categories == 5:
            score += 1.0
        else:
            flexible = {"metal-x-dimension-matrix", "mosaic-infographic",
                        "module-cards-crosscut-sidebar", "route-map-start-strategy-result",
                        "asymmetric-ring-right-sidebar", "why-strategy-what"}
            if layout in flexible:
                score += 1.5
            else:
                score -= 1.0

    # 3. Chirality / selectivity emphasis
    selectivity_heavy = ("enantio" in prompt or "chiral" in prompt
                         or "axial induction" in prompt)
    if has_chirality:
        if selectivity_heavy:
            score += 1.5
    else:
        if selectivity_heavy:
            score -= 1.5
        if layout in {"mosaic-infographic", "metal-x-dimension-matrix", "why-strategy-what"}:
            score += 1.0

    # 4. Reaction focus: reward reaction-strip layouts only when the review
    # is actually about reactions/synthesis
    if "reaction" in prompt and ("scheme" in prompt or "top" in prompt or "center" in prompt):
        score += 1.0 if has_reaction_focus else -1.0

    # 5. Section count: dense layouts for many sections, compact ones for few
    if num_sections >= 7:
        if layout in {"metal-x-dimension-matrix", "mosaic-infographic",
                      "module-cards-crosscut-sidebar", "dual-page-spread"}:
            score += 1.0
    elif 0 < num_sections <= 4:
        if layout in {"center-radial-classification", "tree-metal-classification"}:
            score += 1.0

    # 6. Structural bonuses (theme-independent quality signals)
    if has_metal and ("metal" in layout or "metal" in desc):
        score += 1.0
    if "classification rule" in prompt or "classification" in desc:
        score += 1.5
    if "take-home" in prompt or "outlook" in prompt or "conclusion" in prompt:
        score += 1.0
    if "time window" in prompt or "recent" in prompt or "last five" in prompt:
        score += 0.5
    if "cross-cut" in prompt or "shared" in prompt or "sidebar" in layout:
        score += 0.5

    return score


def select_best_template(templates: list[dict[str, Any]], features: dict[str, Any]) -> dict[str, Any]:
    """Select the best-matching template based on scoring."""
    scored = [(score_template(t, features), t) for t in templates]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_template = scored[0]
    print(f"  Template scoring results:")
    for s, t in scored[:5]:
        print(f"    [{s:.1f}] id={t['id']} name={t['name']} ({t['layout_type']})")
    print(f"  Selected: template_{best_template['id']} (score={best_score:.1f})")
    return best_template


# ---------------------------------------------------------------------------
# Prompt adaptation
# ---------------------------------------------------------------------------

def _clean_categories(categories: list[str]) -> list[str]:
    """Normalize category labels: collapse whitespace, drop empties."""
    cleaned: list[str] = []
    for cat in categories:
        label = " ".join(str(cat).split())
        if label and label not in cleaned:
            cleaned.append(label)
    return cleaned


def _retheme_base_prompt(base_prompt: str, features: dict[str, Any]) -> str:
    """Replace the allene-field content baked into template prompts with the
    current review's categories, counts, and classification dimension."""
    categories = _clean_categories(features.get("metal_categories", []))
    cats = categories[:5] if categories else ["Category 1", "Category 2",
                                              "Category 3", "Category 4", "Category 5"]

    def _panel_replacement(match: re.Match) -> str:
        numbered = any(match.groups())
        if numbered:
            return "\n".join(f"{i}. {c}" for i, c in enumerate(cats, 1))
        return "\n".join(f"- {c}" for c in cats)

    base_prompt = _CATEGORY_PANEL_RE.sub(_panel_replacement, base_prompt)
    base_prompt = _INLINE_CATEGORIES_RE.sub(" / ".join(cats), base_prompt)

    # Adjust slot counts ("five columns/lanes/cards...") to the real count
    n = len(cats)
    if n != 5:
        word = _ENGLISH_NUM_WORDS.get(n, str(n))
        base_prompt = re.sub(r"\bfive\b", word, base_prompt, flags=re.IGNORECASE)

    # Replace metal-centric wording for non-catalyst classification schemes
    gb = features.get("group_by", [])
    if gb and gb[0] != "catalyst_or_method":
        gb_word = gb[0].replace("_", " ")
        classification_rule = features.get("classification_rule", f"By {gb_word}")
        for old, new in [
            ("catalyst center metal", gb_word),
            ("catalytically active metal center", f"{gb_word} identity"),
            ("classification by metal centers", f"classification by {gb_word}"),
            ("By catalyst center metal", classification_rule),
            ("metal-catalyzed", f"{gb_word}-based"),
            ("Metal-centered classification", classification_rule),
            ("metal-centered", f"{gb_word}-centered"),
            ("metal families", f"{gb_word} families"),
            ("metal-family", f"{gb_word}-family"),
            ("metal nodes", "category nodes"),
            ("metal strategies", f"{gb_word} strategies"),
            ("catalyst-family", f"{gb_word}-family"),
        ]:
            base_prompt = base_prompt.replace(old, new)
    return base_prompt


def _build_approved_terminology(features: dict[str, Any]) -> str:
    """Build the approved-terminology list from the review's own keywords
    plus generic academic phrasing, instead of a fixed allene wordlist."""
    terms: list[str] = []
    for key in ("product_keywords", "substrate_keywords", "catalyst_keywords"):
        for kw in features.get(key, []):
            kw = kw.strip().lower()
            if kw and kw not in terms:
                terms.append(kw)
    display_title = str(
        features.get("display_title") or build_overview_display_title(features)
    )
    if display_title and not re.search(r"[\u4e00-\u9fff]", display_title):
        title_lower = display_title.strip().lower()
        if title_lower and title_lower not in terms:
            terms.append(title_lower)
    generic = [
        "recent advances", "key developments", "research trends", "future directions",
        "substrate scope", "substrate class", "product class", "reaction mode",
        "catalytic strategy", "key feature", "key advantage", "main limitation",
        "key challenge", "selectivity", "enantioselectivity", "regioselectivity",
        "diastereoselectivity", "chemoselectivity", "stereoselectivity",
        "mild conditions", "broad scope", "representative transformation",
        "representative reaction", "classification rule", "opportunities and challenges",
    ]
    for g_term in generic:
        if g_term not in terms:
            terms.append(g_term)
    return ", ".join(terms) + "."


def is_chemistry_skeleton_project(features: dict[str, Any]) -> bool:
    """Whether the overview must embed an exact molecular skeleton.

    Detection strategy (B2: explicit SMILES is the primary signal):
    1. Explicit ``skeleton_smiles`` in the query plan → mandatory skeleton.
    2. Profile == "allene" (domain-rules taxonomy) → mandatory skeleton.
    3. Profile starts with "chemistry" but no explicit SMILES → NOT mandatory
       (generic chemistry reviews may lack a single core molecule; skeleton
       is rendered only when a SMILES is available).

    Generic academic reviews (profile "general_academic" or unmatched) never
    trigger skeleton mode.
    """
    # Primary signal: explicit skeleton SMILES from query plan
    if str(features.get("skeleton_smiles") or "").strip():
        return True

    profile = str(features.get("taxonomy_profile") or "").strip().casefold()

    # Allene profile: domain-rules chemistry with mandatory skeleton
    if profile == "allene":
        return True

    return False


def is_chemistry_context(features: dict[str, Any]) -> bool:
    """Whether the review operates in a chemistry context (broader check).

    This determines whether chemistry-aware rendering features (molecular
    colors, bond rendering rules, element symbols) should be activated,
    even if a mandatory skeleton is not required.

    The taxonomy_profile is authoritative: "general_academic" always means
    non-chemistry, regardless of product keywords.
    """
    if is_chemistry_skeleton_project(features):
        return True
    profile = str(features.get("taxonomy_profile") or "").strip().casefold()
    # Explicit non-chemistry profile overrides keyword detection
    if profile == "general_academic":
        return False
    if profile.startswith("chemistry") or profile == "allene":
        return True
    # Check product keywords for chemistry signals (only when profile is unset)
    if not profile:
        products = features.get("product_keywords", [])
        if products:
            prod_text = " ".join(str(p) for p in products).lower()
            chemistry_signals = (
                "allene", "alkene", "alkyne", "biaryl", "atropisomer", "indole",
                "cyclopropane", "amide", "imine", "nitrile", "boronic", "diene",
                "enone", "allenoate", "catalyst", "ligand", "substrate",
                "enantioselective", "asymmetric", "chiral",
            )
            if any(sig in prod_text for sig in chemistry_signals):
                return True
    return False


_COMMON_FIGURE_ELEMENT_SYMBOLS = frozenset(
    {
        "Li", "Na", "K", "Mg", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co",
        "Ni", "Cu", "Zn", "Y", "Zr", "Nb", "Mo", "Ru", "Rh", "Pd", "Ag", "Cd",
        "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Al", "Ga", "In",
        "Sn", "Bi", "B", "Si", "P", "S", "Se", "La", "Ce",
    }
)


def _approved_figure_symbols(features: dict[str, Any]) -> list[str]:
    """Return only project-supported symbols that the image model may render."""
    approved: list[str] = []
    for category in _clean_categories(features.get("metal_categories", [])):
        token = str(category).strip()
        if token in _COMMON_FIGURE_ELEMENT_SYMBOLS and token not in approved:
            approved.append(token)

    if is_chemistry_context(features):
        for token in ("ee", "R1", "R2", "R3", "R4"):
            if token not in approved:
                approved.append(token)
    return approved


def build_adapted_prompt(template: dict[str, Any], features: dict[str, Any],
                         composite_mode: bool = False) -> str:
    """Adapt the template prompt with review-specific content (fully generic).

    ``composite_mode`` switches the structure-panel rule: the exact skeleton
    is pasted programmatically after generation, so the model must leave that
    panel blank white instead of drawing the molecule itself.
    """
    # Chemistry projects retheme the template (replace hardcoded categories);
    # generic academic projects skip retheming since templates may not apply.
    chemistry_project = is_chemistry_context(features)
    base_prompt = _retheme_base_prompt(template.get("prompt", ""), features) if chemistry_project else ""

    # Build English title: if original is non-English, construct from keywords
    gb = features.get("group_by", [])
    display_title = str(
        features.get("display_title") or build_overview_display_title(features)
    )
    if re.search(r"[\u4e00-\u9fff]", display_title):
        products = features.get("product_keywords", [])
        prod = products[0] if products else "target compounds"
        time_w = features.get("time_window", "recent years")
        english_title = f"Recent Advances in {prod.title()} ({time_w})"
    else:
        english_title = display_title

    metals = _clean_categories(features.get("metal_categories", []))
    if not metals:
        metals = ["Cat-1", "Cat-2", "Cat-3", "Cat-4", "Cat-5"]

    time_window = features.get("time_window", "recent years")
    classification_rule = features.get("classification_rule", "By category")

    # Build skeleton, rows, and take-home messages
    skeleton_desc = _build_skeleton_description(features)
    metal_rows_text = _build_metal_rows_text(features)
    take_home_text = _build_take_home_text(features)
    approved_terms = _build_approved_terminology(features)
    approved_symbols = _approved_figure_symbols(features)
    symbol_clause = (
        " or one of these project-approved symbols: " + ", ".join(approved_symbols)
        if approved_symbols
        else ""
    )

    # Visual style description with dynamic term replacement
    visual_style_desc = _get_visual_style_description(template)
    if not chemistry_project:
        visual_style_desc = (
            f"Use the abstract panel geometry and reading order of the {template.get('layout_type', 'overview')} "
            "reference, but discard all source-domain objects and wording. Render only the current project's "
            "title, categories, evidence cells, and take-home messages with crisp vector-like typography."
        )
    n_words = _ENGLISH_NUM_WORDS.get(len(metals[:5]), str(len(metals[:5])))
    if len(metals[:5]) != 5:
        visual_style_desc = re.sub(r"\bFive\b", n_words.capitalize(), visual_style_desc)
        visual_style_desc = re.sub(r"\bfive\b", n_words, visual_style_desc)
    if gb and gb[0] != "catalyst_or_method":
        gb_word = gb[0].replace("_", " ")
        gb_display_map = {
            "catalyst or method": "catalyst families", "leaving group": "leaving group families",
            "substrate": "substrate families", "reaction type": "reaction type families",
            "product": "product families", "ligand or chiral source": "ligand families",
        }
        gb_display = gb_display_map.get(gb_word, f"{gb_word} families")
        replacements = [
            ("metal families", gb_display), ("metal symbol", "category symbol"),
            ("metal-centered", f"{gb_word.replace(' ', '-')}-centered"),
            ("Metal-centered", f"{gb_word.title().replace(' ', '-')}-centered"),
            ("metal hexagons", "category hexagons"),
            ("catalytically active metal center", f"{gb_word} identity"),
            ("classification by metal centers", f"classification by {gb_word}"),
        ]
        for old, new in replacements:
            visual_style_desc = visual_style_desc.replace(old, new)

    if composite_mode:
        structure_rule = (
            "- Leave the large structure/molecule panel COMPLETELY EMPTY (plain white, "
            "no molecule, no drawing): an exact ball-and-stick model is inserted "
            "programmatically after generation. Only the short caption below it is drawn."
        )
        blank_rule = (
            "- Every panel, arc, box, and cell in the layout MUST contain the provided "
            "text, EXCEPT the structure panel which must stay blank white."
        )
    else:
        structure_rule = (
            "- The left-page structure area shows ONLY a single representative "
            "skeleton/motif."
        )
        blank_rule = (
            "- Every panel, arc, box, and cell in the layout MUST contain the provided "
            "text; absolutely no blank regions."
        )
    adaptation = f"""
REFERENCE IMAGE USAGE (read first):
The reference image is intentionally blurred: it is ONLY a layout guide
(panel shapes, round/square icons, column structure, color scheme).
Do NOT copy ANY object, label, symbol, molecule, or text visible in it.
Fill this layout EXCLUSIVELY with the categories, title, and cell text
given below for the current review topic.

VISUAL STYLE REQUIREMENTS (match the reference template exactly):
{visual_style_desc}

ADAPTATION INSTRUCTIONS FOR THIS REVIEW:

Banner title (English, for the navy banner; render EXACTLY this concise title and no request text): "{english_title}"
Time window: {time_window}
Classification rule: "{classification_rule}"

{skeleton_desc}

{metal_rows_text}

{take_home_text}

CRITICAL RULES:
- ALL text in the figure MUST be in ENGLISH only. No Chinese or other non-English characters.
- If the review title (banner) is in Chinese or any non-English language, translate it to professional English before rendering.
- Never print the user's search/query instruction (for example, "Please write a review...") anywhere in the figure.
- Do NOT draw a reaction equation (no arrow, no substrate-to-product transformation).
{structure_rule}
- Keep all text concise. Category labels stay SHORT (symbols or max 2 words). No long names in hexagons.
- Right-page cells: max 2-3 words each. Use real metrics from the content above.
- Generate the figure with ALL text fields FILLED IN. No dotted placeholders.
{blank_rule}
- Use the same visual style, layout, color scheme, and icon design as the reference template.

APPROVED TERMINOLOGY (use ONLY these exact phrases in the figure text):
{approved_terms}

FORBIDDEN BEHAVIOR (strictly enforced):
- Do not hyphenate a word across two lines.
- Do not replace letters with visually similar characters (e.g. 'l' for '1', 'O' for '0').
- Do not use placeholder-like pseudo-English or gibberish text.
- Do not combine two approved phrases into a new unlisted phrase.
- Do not repeat or truncate words.
- Every word in the figure must be a correctly spelled English word from the approved list{symbol_clause}.
- Do not render a metal name, element symbol, or category that is absent from the project categories and row labels above.
"""
    return base_prompt + adaptation


def _build_skeleton_description(features: dict[str, Any]) -> str:
    """Determine what to draw in the left-page structure/concept area.

    For chemistry reviews with a resolved SMILES: use composite/skeleton image.
    For non-chemistry reviews: use a generic concept illustration.
    """
    products = features.get("product_keywords", [])
    prod_label = products[0] if products else "product"

    topic = str(
        features.get("display_title") or build_overview_display_title(features)
    ).strip()

    # Non-chemistry reviews: concept illustration instead of molecule
    if not is_chemistry_context(features):
        return f"""LEFT-PAGE CONCEPT AREA (center of left page):
Draw one clean scientific concept illustration for the current review topic: "{topic or prod_label}".
Use only concepts and labels supported by the supplied project outline. Do not introduce a molecule,
reaction, catalyst, material, organism, or dataset that is not named in the current project evidence.

GENERAL RENDERING RULES:
- Prefer a simple domain-neutral icon, network, workflow, or evidence map appropriate to the topic.
- Keep all elements inside the designated panel with balanced white space.
- Use crisp vector-like edges, readable labels, and the template's established color palette.
- Do not reuse subject matter from the reference template."""

    # Chemistry reviews: determine label from product keywords
    label = prod_label

    # Composite mode pastes the exact skeleton for EVERY layout (calibrated
    # regions first, auto-detected blank panel otherwise), so the model must
    # leave the reserved structure panel blank white regardless of whether
    # the layout carries calibrated coordinates.
    if features.get("_composite_layout"):
        return f"""LEFT-PAGE STRUCTURE AREA (center of left page):
Draw an EMPTY white rounded panel here — NO molecule, NO atoms, NO bonds.
A chemically accurate ball-and-stick model will be inserted into this panel afterwards.
Label below: "{label}"."""

    if features.get("_skeleton_image"):
        return f"""LEFT-PAGE STRUCTURE AREA (center of left page):
The SECOND attached image is a chemically accurate 3D ball-and-stick model of "{label}".
Reproduce it EXACTLY in the structure area: identical atoms, bond angles, bond orders,
colors, and orientation. Do NOT redraw, modify, extend, or substitute its geometry.
Label below: "{label}"."""

    # No skeleton image available: provide generic 3D ball-and-stick instructions
    skeleton_desc = f"A 3D ball-and-stick model representing '{label}'. Use standard CPK coloring."

    return f"""LEFT-PAGE STRUCTURE AREA (center of left page):
Draw a SINGLE 3D ball-and-stick molecular model (NOT a reaction equation, NO arrow, NO 2D bond-line):
  Model: {skeleton_desc}
  Label below: "{label}"

BALL-AND-STICK RENDERING RULES:
- Bond angles must be chemically correct: sp3 ~109°, sp2 ~120°, sp ~180°. NO 90° angles.
- Render as a 3D BALL-AND-STICK model (not 2D bond-line notation)
- Atoms = colored spheres (CPK): C=dark gray, H=white, O=red, N=blue, S=yellow, P=orange, metals=distinct colors
- Bonds = gray cylinders/sticks connecting spheres
- Double bonds = two parallel sticks; triple bonds = three parallel sticks
- R-group substituents = colored spheres labeled R1, R2, R3, R4
- Use a clean 3D perspective view (slightly rotated) so the spatial arrangement is clear
- White or light gray background

QUALITY CHECK:
- Verify ALL bond angles are chemically plausible
- Verify the 3D arrangement clearly shows the molecular geometry
- Verify no atoms are missing or misplaced"""


def _build_metal_rows_text(features: dict[str, Any]) -> str:
    """Build right-page category rows using fully dynamic multi-pass extraction."""
    metals = _clean_categories(features.get("metal_categories", []))
    if not metals:
        metals = ["Cat-1", "Cat-2", "Cat-3", "Cat-4", "Cat-5"]

    outline_text = features.get("_outline_text", "")
    project_dir = features.get("_project_dir")

    # Pass 1: Extract from outline subsection titles
    pass1 = _parse_outline_sections(outline_text, metals)

    # Pass 2: Extract from first_draft.md (richer text with keywords)
    pass2 = _extract_from_draft(project_dir, metals) if project_dir else {}

    # Pass 3: Extract from paper titles in selected_discovery_results.json
    pass3 = _extract_from_paper_titles(project_dir, metals) if project_dir else {}

    lines = ["Right page rows (use ONLY these as hexagonal labels, in order):"]
    for i, m in enumerate(metals[:6], 1):
        lines.append(f"  Row {i}: {m}")
    if len(metals) > 6:
        lines.append(f"  (merge remaining: {', '.join(metals[6:])})")

    lines.append("")
    lines.append("For each row, fill 3 cells. Use EXACTLY the text below (do NOT invent):")
    lines.append("  Cell 1 = strategy (the catalytic approach, max 3 words)")
    lines.append("  Cell 2 = selectivity (ee level or selectivity type, max 2 words)")
    lines.append("  Cell 3 = highlight (one distinguishing feature, max 2 words)")
    lines.append("")

    used: dict[str, set[str]] = {"strategy": set(), "selectivity": set(), "highlight": set()}
    derives = {
        "strategy": _derive_strategy,
        "selectivity": _derive_selectivity,
        "highlight": _derive_highlight,
    }
    for m in metals[:6]:
        # Merge ranked candidates from all passes (pass1 > pass2 > pass3)
        candidates: dict[str, list[str]] = {"strategy": [], "selectivity": [], "highlight": []}
        for src in (pass1.get(m, {}), pass2.get(m, {}), pass3.get(m, {})):
            for dim in candidates:
                for lbl in src.get(dim, []):
                    if lbl and lbl not in candidates[dim]:
                        candidates[dim].append(lbl)
        # Greedy assignment: prefer a label not yet used by another row so
        # rows do not collapse into identical text (visual duplication)
        row: dict[str, str] = {}
        for dim in ("strategy", "selectivity", "highlight"):
            chosen = ""
            for lbl in candidates[dim]:
                if lbl not in used[dim]:
                    chosen = lbl
                    break
            if not chosen and candidates[dim]:
                chosen = candidates[dim][0]
            if not chosen:
                chosen = derives[dim](m)
            used[dim].add(chosen)
            row[dim] = chosen
        lines.append(f"  [{m}] cell1=\"{row['strategy']}\"  cell2=\"{row['selectivity']}\"  cell3=\"{row['highlight']}\"")

    lines.append("")
    lines.append("Column headers above row 1: \"Strategy\"  \"Selectivity\"  \"Highlight\"")
    lines.append("")
    lines.append("RULES:")
    lines.append("- Render each cell EXACTLY as given. Do not modify, add, or remove text.")
    lines.append("- If a cell value is \"—\", draw an empty rounded box.")
    lines.append("- Hexagonal label = ONLY the short category name. No brackets, no extra text.")
    return "\n".join(lines)


# Symbol-to-fullname mapping for section matching (shared by multiple functions)
_SYMBOL_NAMES = {
    "pd": ["pd", "palladium"], "cu": ["cu", "copper"],
    "ni": ["ni", "nickel"], "co": ["co", "cobalt"],
    "au": ["au", "gold"], "rh": ["rh", "rhodium"],
    "ir": ["ir", "iridium"], "fe": ["fe", "iron"],
    "organocatalysis": ["organocatal", "organocatalysis", "organocatalytic", "metal-free"],
}


def _extract_from_draft(project_dir, categories: list[str]) -> dict[str, dict[str, str]]:
    """Pass 2: Extract keywords from first_draft.md section text."""
    result: dict[str, dict[str, str]] = {}
    if not project_dir:
        return result
    draft_path = project_dir / "04_first_draft" / "first_draft.md"
    if not draft_path.exists():
        return result
    draft_text = draft_path.read_text(encoding="utf-8")

    # Reuse the same patterns from _parse_outline_sections
    strategy_pats, selectivity_pats, highlight_pats = _get_extraction_patterns()

    for cat in categories:
        cat_lower = cat.lower()
        search_terms = _build_search_terms(cat)
        section_body = ""
        for term in search_terms:
            pattern = re.compile(
                rf"^##\s*[^\n]*\b{re.escape(term)}\b[^\n]*\n(.*?)(?=^##\s|\Z)",
                re.MULTILINE | re.DOTALL | re.IGNORECASE
            )
            m = pattern.search(draft_text)
            if m:
                section_body = m.group(1)
                break
        if not section_body:
            result[cat] = {}
            continue
        combined = section_body.lower()
        result[cat] = _match_patterns_ranked(combined, strategy_pats, selectivity_pats, highlight_pats)
    return result


def _extract_from_paper_titles(project_dir, categories: list[str]) -> dict[str, dict[str, str]]:
    """Pass 3: Extract keywords from paper titles in selected_discovery_results."""
    result: dict[str, dict[str, str]] = {}
    if not project_dir:
        return result
    sel_path = project_dir / "00_discovery" / "selected_discovery_results.json"
    if not sel_path.exists():
        return result
    sel = read_json(sel_path)
    papers = sel.get("local_papers", [])

    strategy_pats, selectivity_pats, highlight_pats = _get_extraction_patterns()

    # Build a mapping from paper_id to title
    pid_to_title = {p.get("paper_id", ""): (p.get("title", "") or "") for p in papers}

    for cat in categories:
        search_terms = _build_search_terms(cat)
        # Collect titles that mention this category
        matched_titles = []
        for title in pid_to_title.values():
            title_lower = title.lower()
            if any(t in title_lower for t in search_terms[:3]):
                matched_titles.append(title_lower)
        combined = " ".join(matched_titles)
        if combined:
            result[cat] = _match_patterns_ranked(combined, strategy_pats, selectivity_pats, highlight_pats)
        else:
            result[cat] = {}
    return result


def _get_extraction_patterns() -> tuple[list, list, list]:
    """Return (strategy, selectivity, highlight) pattern lists."""
    strategy_pats = [
        (r"\bpd\b|palladium|pi[- ]?allyl|π[- ]?allyl", "Pd / pi-allyl"),
        (r"\bcu\b|copper|sn2'|organocopper", "Cu / SN2'"),
        (r"\bni\b|nickel|reductive cross", "Ni / reductive"),
        (r"\bco\b|cobalt", "Co catalysis"),
        (r"\bau\b|\bgold\b", "Au catalysis"),
        (r"\brh\b|rhodium|1,6[- ]?addition", "Rh / 1,6-addition"),
        (r"organocatal|brønsted acid|bronsted acid|chiral phosphoric|cpa", "organocatalysis"),
        (r"photoredox|photo[- ]?induced|visible light", "photoredox"),
        (r"electrochem", "electrochemistry"),
        (r"mechanochem|ball.?mill", "mechanochemistry"),
        (r"cooperative|dual catal|co[- ]?catal", "cooperative"),
        (r"remote|1,\d+[- ]?addition", "remote control"),
        (r"dehydrative|in situ activ|direct c[- ]?o", "direct activation"),
        (r"alleneamination", "alleneamination"),
        (r"carboetherification", "carboetherification"),
        (r"three[- ]?component|multicomponent", "multicomponent"),
        (r"ligand[- ]?free", "ligand-free"),
        (r"decarboxylative", "decarboxylative"),
        (r"\bflow\b|continuous", "flow chemistry"),
        (r"reductive|cross[- ]?coupling", "reductive coupling"),
        (r"carboxylation", "carboxylation"),
        (r"sulfonylation|sulfonyl", "sulfonylation"),
        (r"borylation|borane", "borylation"),
        (r"silylation|silane", "silylation"),
        (r"phosphorylation|phosphine", "phosphorylation"),
        (r"cyclization|cascade", "cyclization"),
        (r"reduction", "reduction"),
    ]
    selectivity_pats = [
        (r"enantioselective|asymmetric|chiral|ee|\d+%\s*ee", "enantioselective"),
        (r"regioselective|regio[- ]?control|regiodivergent", "regioselective"),
        (r"diastereoselective|\bdr\b", "diastereoselective"),
        (r"chemoselective", "chemoselective"),
        (r"stereoselective|stereospecific|enantiospecific", "stereoselective"),
        (r"enantioconvergent|enantiodivergent", "enantioselective"),
    ]
    highlight_pats = [
        (r"mild|room temp|ambient", "mild conditions"),
        (r"green|sustainable|earth[- ]?abundant", "sustainable"),
        (r"broad|diverse|wide scope", "broad scope"),
        (r"direct|atom[- ]?econom|no pre[- ]?activ", "atom-economical"),
        (r"remote|long[- ]?range", "remote control"),
        (r"divergent|switchable|tunable", "divergent"),
        (r"modular|versatile|flexible", "modular"),
        (r"scalable|gram[- ]?scale|flow", "scalable"),
        (r"metal[- ]?free|organocatal", "metal-free"),
        (r"emerging|new|novel|unprecedented", "emerging"),
        (r"cooperative|synergistic|dual", "cooperative"),
        (r"cost[- ]?effective|cheap|inexpensive", "cost-effective"),
        (r"ligand[- ]?free", "ligand-free"),
        (r"electrochem", "electrochemical"),
        (r"photo|visible light", "photoredox"),
    ]
    return strategy_pats, selectivity_pats, highlight_pats


def _build_search_terms(cat: str) -> list[str]:
    """Build search terms for a category label."""
    cat_lower = cat.lower()
    terms = [cat_lower, cat_lower.split()[0]]
    if cat_lower in _SYMBOL_NAMES:
        terms.extend(_SYMBOL_NAMES[cat_lower])
    if len(cat_lower) > 6:
        terms.append(cat_lower[:6])
    return terms


def _match_patterns_ranked(text: str, strategy_pats, selectivity_pats, highlight_pats) -> dict[str, list[str]]:
    """Match text against pattern lists; return ALL hits in priority order.

    Ranked results let the row assembler pick a distinct label per category
    instead of every category converging on the same first-hit label.
    """
    result: dict[str, list[str]] = {"strategy": [], "selectivity": [], "highlight": []}
    for pats, dim in ((strategy_pats, "strategy"), (selectivity_pats, "selectivity"),
                      (highlight_pats, "highlight")):
        for pat, label in pats:
            if re.search(pat, text) and label not in result[dim]:
                result[dim].append(label)
    return result


def _derive_strategy(cat: str) -> str:
    """Final fallback: derive a strategy label from the category name itself."""
    cat_lower = cat.lower()
    # Catch-all categories
    if cat_lower in ("other", "others", "miscellaneous"):
        return "diverse methods"
    # Metal symbols → "X catalysis"
    metal_symbols = {"pd", "cu", "ni", "co", "au", "rh", "ir", "fe", "ru", "zn", "cr"}
    if cat_lower in metal_symbols:
        return f"{cat} catalysis"
    # Organocatalysis
    if "organicat" in cat_lower or "metal-free" in cat_lower:
        return "organocatalysis"
    # Substrate/LG types → "X-based"
    if any(w in cat_lower for w in ("acetate", "carbonate", "halide", "phosphate", "ether", "mesylate")):
        return f"{cat_lower}-based"
    # Other: use the name directly
    return f"{cat}-based"


def _derive_selectivity(cat: str) -> str:
    """Final fallback: derive a selectivity label."""
    cat_lower = cat.lower()
    if cat_lower in ("other", "others", "miscellaneous"):
        return "mixed"
    if "organicat" in cat_lower or "metal-free" in cat_lower:
        return "enantioselective"
    if cat_lower in ("pd", "au", "rh"):
        return "high ee"
    if cat_lower in ("cu", "ni", "co"):
        return "moderate"
    return "—"


def _derive_highlight(cat: str) -> str:
    """Final fallback: derive a highlight label."""
    cat_lower = cat.lower()
    if cat_lower in ("other", "others", "miscellaneous"):
        return "emerging"
    if "organicat" in cat_lower or "metal-free" in cat_lower:
        return "metal-free"
    if cat_lower in ("ni", "co", "fe"):
        return "earth-abundant"
    if cat_lower == "cu":
        return "cost-effective"
    if cat_lower == "pd":
        return "broad scope"
    if cat_lower == "au":
        return "mild conditions"
    if any(w in cat_lower for w in ("acetate", "carbonate")):
        return "versatile"
    if "halide" in cat_lower:
        return "flow-compatible"
    if "free" in cat_lower or "propargyl" in cat_lower:
        return "atom-economical"
    return "—"


def _parse_outline_sections(outline_text: str, categories: list[str]) -> dict[str, dict[str, list[str]]]:
    """Parse outline to extract ranked candidate labels per category.

    Returns {category: {dimension: [labels in priority order]}}. Sources are
    merged in priority order: subsections mentioning this category first,
    then the first subsection, then all subsections + section body.
    """
    result: dict[str, dict[str, list[str]]] = {}
    if not outline_text:
        return result

    strategy_pats, selectivity_pats, highlight_pats = _get_extraction_patterns()
    dims = ("strategy", "selectivity", "highlight")

    def _merge(found: dict[str, list[str]], hits: dict[str, list[str]]) -> None:
        for dim in dims:
            for lbl in hits.get(dim, []):
                if lbl not in found[dim]:
                    found[dim].append(lbl)

    for cat in categories:
        search_terms = _build_search_terms(cat)
        section_body = ""
        for term in search_terms:
            pattern = re.compile(
                rf"^##\s*\d+\.\s*[^\n]*\b{re.escape(term)}\b[^\n]*\n(.*?)(?=^##\s|\Z)",
                re.MULTILINE | re.DOTALL | re.IGNORECASE
            )
            m = pattern.search(outline_text)
            if m:
                section_body = m.group(1)
                break

        if not section_body:
            result[cat] = {}
            continue

        section_lower = section_body.lower()
        subsec_lines = re.findall(r"^(?:###\s*(.+)|-\s*\d+\.\d+\s+(.+))$", section_body, re.MULTILINE)
        subsec_texts = [a or b for a, b in subsec_lines]
        first_subsec = subsec_texts[0].lower() if subsec_texts else ""
        combined = " ".join(subsec_texts).lower() + " " + section_lower
        cat_terms = set(_build_search_terms(cat))

        found: dict[str, list[str]] = {"strategy": [], "selectivity": [], "highlight": []}
        # Priority 1: subsections that mention THIS category
        for subsec_text in subsec_texts:
            subsec_lower = subsec_text.lower()
            if any(t in subsec_lower for t in cat_terms):
                _merge(found, _match_patterns_ranked(subsec_lower, strategy_pats, selectivity_pats, highlight_pats))
        # Priority 2: first subsection, then all subsections + body
        if first_subsec:
            _merge(found, _match_patterns_ranked(first_subsec, strategy_pats, selectivity_pats, highlight_pats))
        _merge(found, _match_patterns_ranked(combined, strategy_pats, selectivity_pats, highlight_pats))
        result[cat] = found
    return result


def _build_take_home_text(features: dict[str, Any]) -> str:
    """Build take-home messages dynamically from theme features."""
    time_window = features.get("time_window", "recent years")
    classification_rule = features.get("classification_rule", "")
    has_chirality = bool(features.get("has_chirality"))
    has_reaction_focus = bool(features.get("has_reaction_focus"))

    # Extract short keyword from classification rule
    class_short = "Classified"
    if classification_rule:
        parts = classification_rule.replace("By ", "").split()
        class_short = parts[0] if parts else "Classified"

    messages = [
        f"1. Timely overview ({time_window})",
        f"2. {class_short}-centered",
    ]
    if has_chirality:
        messages.append("3. High enantioselectivity")
    else:
        messages.append("3. Distinct category features")
    messages.append("4. Diverse substrate scope" if has_reaction_focus else "4. Diverse categories")
    messages.append("5. Emerging sustainable methods" if has_reaction_focus else "5. Emerging approaches")
    messages.append("6. Future directions")
    return "Take-home messages (bottom band, 6 items):\n" + "\n".join(messages)


def _get_visual_style_description(template: dict[str, Any]) -> str:
    """Return a visual style description based on the template layout type."""
    layout = template.get("layout_type", "")
    descriptions = {
        "dual-page-spread": """Layout: Two-page spread (left page + right page) with a unified bottom band.
Left page: Title 'Concept & classification' at top. Below it, a dark navy rounded banner with the review topic. Below the banner, a SINGLE 3D ball-and-stick molecular model (NO reaction arrow, NO equation — just one 3D molecule model with colored atom spheres and gray bond sticks). One short label below the model. Below that, a 'Classification rule' box with a periodic-table-style icon and the rule text. A summary sentence at the bottom of left page.
Right page: Title 'Representative metal families' (or category families) at top. Five horizontal rows, each with a colored hexagonal category symbol icon on the left and three info columns (case, selectivity, note) with small icons (clipboard, target, pencil).
Bottom band: Full-width cream/beige band titled 'Take-home messages' with 6 icon-and-text modules in a row.
Color scheme: Navy blue headers, white background, colored category hexagons, cream bottom band. Clean sans-serif typography. Rounded corners on all panels. Thin gray borders.
Icons: Simple line-art style icons (calendar, molecule, target, arrows, lightbulb, chart).""",
        "top-reaction-5col-table": """Layout: Top reaction scheme strip + 5 vertical columns below + bottom ribbon.
Top: A reaction scheme showing generic propargylic substrate → allene product with catalyst label.
Middle: Five equal-width vertical columns, each with a colored header (one per metal family). Each column has 4 sub-blocks: Catalyst/ligand, Representative case, Key selectivity, Synthetic note.
Bottom: Full-width ribbon with 4 icon modules (scope, mechanism, utility, future).
Style: Clean white background, colored column headers, thin borders, sans-serif font.""",
        "center-radial-classification": """Layout: Central panel with 5 radiating branches to outer panels.
Center: Rounded rectangle with 'APA mini-review' title, time window, and a reaction scheme.
Branches: 5 colored lines radiating to 5 outer panels (one per metal). Each panel has 3 rows with icons (flask, arrow, star).
Corners: 4 global theme callouts connected by dashed oval boundary.
Style: Symmetric radial layout, colored branch lines matching metal colors, white background.""",
        "route-map-start-strategy-result": """Layout: Horizontal lanes (left-to-right flow) + right outcome panel + bottom band.
Center: 5 horizontal lanes (one per metal), each with 3 sequential boxes connected by chevrons.
Right: 'Outcome' panel with product schematic and 3 result boxes.
Bottom: 'Challenges & outlook' band with 4 modules.
Style: Left-to-right reading flow, colored lane headers, chevron connectors.""",
        "metal-x-dimension-matrix": """Layout: Grid matrix with metal columns × dimension rows.
Columns: 5 metal families as column headers.
Rows: 4 information dimensions (Catalyst system, Representative chemistry, Selectivity, Utility).
Bottom: 'Shared conclusions' footer with 4 summary blocks.
Style: Clean table/grid layout, alternating row shading, colored column headers.""",
        "module-cards-crosscut-sidebar": """Layout: 5 tall rounded cards in a row + narrow right sidebar.
Cards: Each card has a colored header (metal name) and 4 mini-sections inside.
Sidebar: 'Cross-cutting issues' with 4 stacked modules.
Bottom: Abbreviations line.
Style: Card-based modular layout, colored headers, rounded corners.""",
        "tree-metal-classification": """Layout: Tree diagram with central node branching to metal nodes.
Root: Dark rounded node 'Metal-centered classification'.
Branches: 5 branches to circular metal nodes, each with 3 leaf boxes below.
Bottom: Shared 'General lessons' conclusion box.
Style: Scientific taxonomy tree, colored nodes, hierarchical layout.""",
        "why-strategy-what": """Layout: 3-section horizontal flow (Why → Strategies → What).
Section 1: Motivation/importance.
Section 2: 5 horizontal metal modules with 3 columns each.
Section 3: Outcomes with product motifs and summary blocks.
Bottom: Shared summary band.
Style: Left-to-right narrative flow, bold section arrows.""",
        "asymmetric-ring-right-sidebar": """Layout: Asymmetric ring/arc segments + right vertical sidebar.
Ring: 5 arc segments (one per metal) with catalyst icons and info boxes.
Sidebar: 'Shared insights' with 4 stacked panels.
Style: Intentionally asymmetric ring, modern infographic feel.""",
        "mosaic-infographic": """Layout: Mosaic of 5 aligned tiles + 2 support tiles + bottom legend.
Main: 5 tiles in a grid, each with 4 zones (Catalyst, Chemistry, Selectivity, Highlight).
Support: 'Key trends' tile (lower left) and 'Outlook' tile (lower right).
Bottom: Compact legend with abbreviations.
Style: Mosaic/tile-based, visually distinctive, publication-friendly.""",
    }
    return descriptions.get(layout, "Clean scientific infographic style with colored sections and icons.")


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def build_multipart_form(fields: dict[str, Any], file_fields: list[tuple[str, Path]]) -> tuple[str, bytes]:
    boundary = f"----OverviewBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, path in file_fields:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'.encode("utf-8")
        )
        body.extend(b"Content-Type: image/png\r\n\r\n")
        body.extend(path.read_bytes())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def call_image_edit_api(
    api_key: str,
    base_url: str,
    reference_image: Path,
    prompt: str,
    model: str = DEFAULT_IMAGE_MODEL,
    preferred_size: str = "",
    wire_api: str = "",
    request_metadata: dict[str, str] | None = None,
    extra_images: list[Path] | None = None,
) -> bytes:
    """Call image generation API. Supports both OpenAI-compatible and DashScope native format."""
    if image_gateway_configured():
        image_inputs = [
            (_gateway_image_mime(reference_image), reference_image.read_bytes()),
            *[
                (_gateway_image_mime(path), path.read_bytes())
                for path in (extra_images or [])
            ],
        ]
        image_bytes, gateway_metadata = call_gateway_image(
            condense_overview_prompt(prompt),
            label="final-overview-image",
            images=image_inputs,
            operation="edit",
            quality="high",
            background="opaque",
            output_format="png",
            size=preferred_size,
        )
        if request_metadata is not None:
            request_metadata.update(
                {
                    "endpoint": "internal-image-gateway",
                    "wire_api": "internal",
                    "image_size": preferred_size or "provider-controlled",
                    "gateway_request_id": str(gateway_metadata.get("request_id") or ""),
                }
            )
        return image_bytes
    # Detect if this is an Alibaba Cloud / DashScope endpoint
    if "maas.aliyuncs.com" in base_url or "dashscope" in base_url:
        if request_metadata is not None:
            request_metadata.update({"endpoint": "dashscope-native", "image_size": "2K"})
        return _call_dashscope_native(api_key, base_url, reference_image, prompt, model, extra_images)

    # OpenAI-compatible endpoints. Some relays expose image generation only
    # only through multimodal /chat/completions.  When that transport is
    # configured, do not probe /images/*: doing so can trigger provider-side
    # access controls and can never reach the model assigned to the chat route.
    resolved_wire_api = normalize_image_wire_api(wire_api)
    # Some compatible providers reject prompts over roughly 4000 chars;
    # condense once here.  The default max_chars already reserves headroom
    # for the square-canvas note that prompt_for_overview_size appends below.
    prompt = condense_overview_prompt(prompt)
    if resolved_wire_api == "chat-completions":
        size = overview_image_size_candidates(base_url, preferred_size)[0]
        sized_prompt = prompt_for_overview_size(prompt, size)
        image = _try_chat_completions_image_edit(
            base_url,
            api_key,
            reference_image,
            sized_prompt,
            model,
            extra_images,
        )
        if request_metadata is not None:
            request_metadata.update(
                {
                    "endpoint": "/chat/completions",
                    "wire_api": resolved_wire_api,
                    "image_size": "provider-controlled",
                    "requested_layout_size": size,
                }
            )
        return image

    # Standard OpenAI Images endpoints.
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"

    errors: list[str] = []
    for size in overview_image_size_candidates(base_url, preferred_size):
        sized_prompt = prompt_for_overview_size(prompt, size)
        try:
            image = _try_images_edits(base, api_key, reference_image, sized_prompt, model, size, extra_images)
            if request_metadata is not None:
                request_metadata.update({"endpoint": "/images/edits", "image_size": size})
            return image
        except Exception as edit_err:
            errors.append(f"/images/edits size={size}: {edit_err}")
            print(f"  /images/edits size={size} failed ({edit_err})")

        try:
            image = _try_images_generations_text_only(base, api_key, sized_prompt, model, size)
            if request_metadata is not None:
                request_metadata.update({"endpoint": "/images/generations", "image_size": size})
            return image
        except Exception as generation_err:
            errors.append(f"/images/generations size={size}: {generation_err}")
            print(f"  /images/generations size={size} failed ({generation_err})")

    summary = "; ".join(errors[-4:])
    raise RuntimeError(f"All overview image generation routes failed: {summary}")


def _gateway_image_mime(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.casefold(), "image/png")


def _data_uri_image_item(path: Path, detail: str | None = None) -> dict[str, Any]:
    """Build a chat-completions image_url content item from a local file."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    suffix = path.suffix.lower()
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/png")
    url_item: dict[str, Any] = {"url": f"data:{mime};base64,{encoded}"}
    if detail:
        url_item["detail"] = detail
    return {"type": "image_url", "image_url": url_item}


def _try_chat_completions_image_edit(
    base_url: str,
    api_key: str,
    reference_image: Path,
    prompt: str,
    model: str,
    extra_images: list[Path] | None = None,
) -> bytes:
    """Generate the overview through a multimodal Chat Completions relay."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    _data_uri_image_item(reference_image, detail="high"),
                ] + [_data_uri_image_item(p) for p in (extra_images or [])],
            }
        ],
        "stream": True,
    }
    request = urllib.request.Request(
        openai_api_url(base_url, "/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    result = _open_chat_completion_request(request)
    return _extract_chat_completion_image_bytes(result)


def _open_chat_completion_request(
    request: urllib.request.Request,
    timeout: int = 600,
) -> dict[str, Any]:
    """Read either a normal JSON response or an OpenAI-compatible SSE stream."""
    label = "Overview Chat Completions image request"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=timeout,
            ) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    return decode_json_object(response.read(), label)
                content_parts: list[str] = []
                image_items: list[Any] = []
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event_text = line[5:].strip()
                    if not event_text or event_text == "[DONE]":
                        continue
                    try:
                        event = json.loads(event_text)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices") if isinstance(event, dict) else None
                    if isinstance(choices, list) and choices:
                        delta = (choices[0] or {}).get("delta") or {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str):
                            content_parts.append(content)
                        elif isinstance(content, list):
                            image_items.extend(content)
                        images = delta.get("images") if isinstance(delta, dict) else None
                        if isinstance(images, list):
                            image_items.extend(images)
                    if isinstance(event, dict) and isinstance(event.get("delta"), str):
                        content_parts.append(event["delta"])
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(content_parts),
                }
                if image_items:
                    message["images"] = image_items
                return {"choices": [{"message": message}]}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500].replace("\r", " ").replace("\n", " ")
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(
                    f"{label} failed with HTTP {exc.code}: {body or exc.reason}"
                ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport failed: {exc}") from exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{label} failed after retries")


_DATA_IMAGE_PATTERN = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)",
    re.IGNORECASE,
)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", re.IGNORECASE)
_HTTP_IMAGE_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _valid_image_bytes(raw: bytes) -> bool:
    return any(
        (
            raw.startswith(b"\x89PNG\r\n\x1a\n"),
            raw.startswith(b"\xff\xd8\xff"),
            raw.startswith((b"GIF87a", b"GIF89a")),
            raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
        )
    )


def _decode_image_base64(value: str) -> bytes | None:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 32:
        return None
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    return raw if _valid_image_bytes(raw) else None


def _image_reference_from_node(node: Any) -> tuple[str, Any] | None:
    if isinstance(node, str):
        data_match = _DATA_IMAGE_PATTERN.search(node)
        if data_match:
            raw = _decode_image_base64(data_match.group(1))
            if raw:
                return "bytes", raw
        markdown_match = _MARKDOWN_IMAGE_PATTERN.search(node)
        if markdown_match:
            return "url", markdown_match.group(1)
        url_match = _HTTP_IMAGE_PATTERN.search(node)
        if url_match:
            return "url", url_match.group(0).rstrip(".,;)")
        raw = _decode_image_base64(node)
        return ("bytes", raw) if raw else None
    if isinstance(node, list):
        for item in node:
            found = _image_reference_from_node(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    for key in (
        "b64_json",
        "image_base64",
        "base64",
        "result",
        "message",
        "delta",
        "image_url",
        "url",
        "image",
        "images",
        "content",
    ):
        if key not in node:
            continue
        found = _image_reference_from_node(node[key])
        if found:
            return found
    return None


def _extract_chat_completion_image_bytes(result: dict[str, Any]) -> bytes:
    reference = _image_reference_from_node(result.get("data"))
    if not reference:
        reference = _image_reference_from_node(result.get("choices"))
    if not reference:
        reference = _image_reference_from_node(result.get("output"))
    if not reference:
        raise RuntimeError("Overview Chat Completions response did not contain an image")
    kind, value = reference
    if kind == "bytes":
        return value
    request = urllib.request.Request(
        str(value),
        headers={
            "Accept": "image/*",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(),
            timeout=180,
        ) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not download the overview image: {exc}") from exc
    if not _valid_image_bytes(raw):
        raise RuntimeError("Overview Chat Completions returned a URL that was not a valid image")
    return raw


def _call_dashscope_native(
    api_key: str, base_url: str, reference_image: Path, prompt: str, model: str,
    extra_images: list[Path] | None = None,
) -> bytes:
    """Call Alibaba Cloud DashScope native multimodal-generation API."""
    # Derive the native API URL from the base URL
    # User's base: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    # Native API:  https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    native_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/services/aigc/multimodal-generation/generation"
    print(f"  Using DashScope native API: {native_url}")

    # Encode reference image as base64
    img_b64 = base64.b64encode(reference_image.read_bytes()).decode("ascii")
    img_data_uri = f"data:image/png;base64,{img_b64}"

    # Build DashScope request body
    payload = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": img_data_uri},
                    ] + [
                        {"image": f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode('ascii')}"}
                        for p in (extra_images or [])
                    ] + [
                        {"text": prompt},
                    ],
                }
            ]
        },
        "parameters": {
            "size": "2K",
            "n": 1,
            "watermark": False,
        },
    }

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    req = urllib.request.Request(native_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "replace")
        print(f"  DashScope API error {exc.code}: {error_body[:500]}")
        # Fallback: try without reference image (text-only)
        print("  Retrying without reference image (text-only)...")
        payload["input"]["messages"][0]["content"] = [{"text": prompt}]
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(native_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))

    # Extract image from DashScope response
    return _extract_dashscope_image(result)


def _extract_dashscope_image(result: dict) -> bytes:
    """Extract image bytes from DashScope API response."""
    # DashScope response format: {"output": {"choices": [{"message": {"content": [{"image": "url"}]}}]}}
    output = result.get("output", {})
    choices = output.get("choices", [])
    if choices:
        content = choices[0].get("message", {}).get("content", [])
        for item in content:
            if "image" in item:
                img_url = item["image"]
                print(f"  Downloading generated image from: {img_url[:80]}...")
                with urllib.request.urlopen(img_url, timeout=120) as resp:
                    return resp.read()
    # Alternative format: {"output": {"results": [{"url": "..."}]}}
    results = output.get("results", [])
    if results:
        img_url = results[0].get("url", "")
        if img_url:
            print(f"  Downloading generated image from: {img_url[:80]}...")
            with urllib.request.urlopen(img_url, timeout=120) as resp:
                return resp.read()
    raise RuntimeError(f"Unexpected DashScope response: {json.dumps(result)[:800]}")


def _try_images_edits(
    base: str,
    api_key: str,
    reference_image: Path,
    prompt: str,
    model: str,
    size: str,
    extra_images: list[Path] | None = None,
) -> bytes:
    """Try the /images/edits endpoint."""
    url = f"{base}/images/edits"
    fields = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": "high",
        "output_format": "png",
    }
    file_fields = [("image", reference_image)] + [("image[]", p) for p in (extra_images or [])]
    content_type, body = build_multipart_form(fields, file_fields)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    result = open_json_request(req, "Overview image edit request")
    return _extract_image_bytes(result)


def _try_images_generations_text_only(
    base: str,
    api_key: str,
    prompt: str,
    model: str,
    size: str,
) -> bytes:
    """Try /images/generations with text-only prompt (no reference image)."""
    url = f"{base}/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "b64_json",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    result = open_json_request(req, "Overview image generation request")
    return _extract_image_bytes(result)


def _extract_image_bytes(result: dict) -> bytes:
    """Extract image bytes from API response."""
    data_list = result.get("data", [])
    if not data_list:
        raise RuntimeError(f"API returned no image data: {json.dumps(result)[:500]}")
    img_data = data_list[0]
    if "b64_json" in img_data:
        return base64.b64decode(img_data["b64_json"])
    elif "url" in img_data:
        with urllib.request.urlopen(img_data["url"], timeout=60) as resp:
            return resp.read()
    else:
        raise RuntimeError(f"Unexpected API response format: {json.dumps(img_data)[:300]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_report(args, template: dict, features: dict, prompt: str,
                         reference_image: Path, base_url: str, model: str,
                         status: str = "pending", output_path: str = "",
                         output_size: int = 0, error: str = "",
                         request_metadata: dict[str, str] | None = None,
                         composite: dict[str, Any] | None = None,
                         skeleton: dict[str, Any] | None = None) -> dict:
    """Build a single report dict (replaces 3 duplicate blocks in main)."""
    report = {
        "project_id": args.project_id,
        "selected_template_id": template["id"],
        "selected_template_name": template["name"],
        "reference_image": str(reference_image),
        "score": score_template(template, features),
        "features": {k: v for k, v in features.items() if not k.startswith("_")},
        "adapted_prompt": prompt,
        "api_base_url": base_url,
        "wire_api": normalize_image_wire_api(args.wire_api),
        "model": model,
        "image_size_candidates": overview_image_size_candidates(base_url, args.size),
        "status": status,
    }
    if request_metadata:
        report["image_request"] = dict(request_metadata)
    if output_path:
        report["output_path"] = output_path
        report["output_size_bytes"] = output_size
    if error:
        report["error"] = error
    if composite is not None:
        report["composite"] = dict(composite)
    if skeleton is not None:
        report["skeleton"] = dict(skeleton)
    return report


def main():
    parser = argparse.ArgumentParser(description="Generate review overview figure from template")
    parser.add_argument("--review-root", required=True, help="Path to review-writer project root")
    parser.add_argument("--project-id", required=True, help="Project ID")
    parser.add_argument("--api-key", default="", help="API key (or set IMAGE_OPENAI_API_KEY / OPENAI_API_KEY)")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL, help="Image generation model")
    parser.add_argument(
        "--wire-api",
        default="",
        choices=["", "images", "chat-completions"],
        help="Image transport; defaults to IMAGE_OPENAI_WIRE_API or images.",
    )
    parser.add_argument(
        "--size",
        default="",
        help="Preferred image size; incompatible providers automatically fall back to a supported size.",
    )
    parser.add_argument(
        "--skeleton-style",
        default="ai3d",
        choices=["3d", "flat", "ai3d"],
        help="Ball-and-stick skeleton rendering style; 'flat' restores the original 2D vector look; "
             "'ai3d' asks the image model to restyle the exact skeleton into a 3D render, gated by a "
             "programmatic sanity check with automatic fallback to the programmatic 3D skeleton.",
    )
    parser.add_argument(
        "--require-ai-skeleton",
        action="store_true",
        help="Stronger opt-in strict mode: require both the exact chemical skeleton and "
             "an AI-styled 3D skeleton. Chemistry reviews always require the exact "
             "chemical skeleton, but normally fall back to the exact programmatic 3D "
             "render when AI style transfer is rejected.",
    )
    parser.add_argument("--output", default="", help="Output path for generated figure")
    parser.add_argument("--dry-run", action="store_true", help="Only show template matching, don't call API")
    args = parser.parse_args()

    review_root = Path(args.review_root).resolve()
    project_dir = review_root / "review-projects" / args.project_id
    load_dotenv(review_root)

    # Resolve API settings
    base_url = args.base_url or os.environ.get(
        "IMAGE_OPENAI_BASE_URL",
        os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
    )
    api_key = resolve_api_key(args.api_key, base_url)
    wire_api = normalize_image_wire_api(args.wire_api)

    # Load templates
    templates_path = overview_template_catalog_path()
    if not templates_path.exists():
        print(f"ERROR: Templates file not found: {templates_path}", file=sys.stderr)
        sys.exit(1)
    templates = read_json(templates_path)
    print(f"Loaded {len(templates)} templates from {templates_path}")

    # Extract review features
    print(f"\nAnalyzing review project: {args.project_id}")
    features = extract_review_features(project_dir)
    print(f"  Sections: {features['num_sections']}")
    print(f"  Metal classification: {features['has_metal_classification']}")
    print(f"  Metal categories: {features['metal_categories']}")
    print(f"  Time window: {features['time_window']}")
    print(f"  Group by: {features['group_by']}")

    # Select best template
    print(f"\nMatching templates...")
    best_template = select_best_template(templates, features)
    template_id = best_template["id"]
    reference_image = resolve_overview_template_image(templates_path, best_template)
    print(f"\n  Best match: template_{template_id} ({best_template['name']})")
    print(f"  Reference image: {reference_image}")

    if not reference_image.exists():
        print(f"ERROR: Reference image not found: {reference_image}", file=sys.stderr)
        sys.exit(1)

    # Build adapted prompt
    out_dir = project_dir / "03_figure_redraw"
    out_dir.mkdir(parents=True, exist_ok=True)
    extra_images: list[Path] = []
    skeleton_png = out_dir / "skeleton_model.png"
    layout_type = best_template["layout_type"]
    program_style = "3d" if args.skeleton_style == "ai3d" else args.skeleton_style
    ai_style_required = bool(args.require_ai_skeleton)
    strict_skeleton = ai_style_required or is_chemistry_skeleton_project(features)
    smiles = resolve_skeleton_smiles(features)
    skeleton_attempts: list[str] = []
    ai_redraw_note = ""

    def _fail_skeleton(status: str, error: str) -> None:
        """Strict mode: refuse to ship an overview without the exact molecule."""
        print(f"\nERROR: {error}", file=sys.stderr)
        report = _build_report(
            args, best_template, features, "", reference_image, base_url, args.model,
            status=status, error=error,
            skeleton={
                "strict": True,
                "ai_style_required": ai_style_required,
                "style": args.skeleton_style,
                "smiles": smiles,
                "attempts": skeleton_attempts,
            },
        )
        write_json(out_dir / "overview_template_match.json", report)
        print(f"  Report saved to: {out_dir / 'overview_template_match.json'}", file=sys.stderr)
        sys.exit(4)

    if strict_skeleton and not smiles:
        _fail_skeleton(
            "skeleton_smiles_missing",
            "Strict skeleton mode: no core-motif SMILES could be resolved for this "
            "chemistry review. Set 'skeleton_smiles' in the discovery query plan "
            "(or include a recognizable motif keyword) so the overview can embed "
            "an exact molecule.",
        )
    skeleton_rendered = render_skeleton_model(features, skeleton_png,
                                              style=program_style)
    skeleton_source = "programmatic"
    if strict_skeleton and not skeleton_rendered:
        _fail_skeleton(
            "skeleton_render_failed",
            f"Strict skeleton mode: the ball-and-stick renderer rejected SMILES {smiles!r}.",
        )
    if skeleton_rendered and args.skeleton_style == "ai3d" and not args.dry_run:
        ai_png, ai_redraw_note, skeleton_attempts = attempt_ai_skeleton_redraw(
            features, skeleton_png, out_dir / "skeleton_model_ai3d.png",
            api_key, base_url, args.model, wire_api)
        if ai_png is not None:
            skeleton_png = ai_png
            skeleton_source = "ai_redraw"
            print(f"  AI 3D skeleton redraw accepted by the sanity gate: {ai_png}")
        elif ai_style_required:
            _fail_skeleton(
                "skeleton_redraw_failed",
                "Strict skeleton mode: the AI 3D skeleton redraw failed every "
                f"attempt ({'; '.join(skeleton_attempts)}). Refusing to ship a "
                "degraded overview; re-run the overview generation to retry.",
            )
        else:
            skeleton_source = "programmatic_fallback"
            print(
                f"  WARNING: AI 3D skeleton redraw not used ({ai_redraw_note}); "
                "using the exact programmatic 3D skeleton.",
                file=sys.stderr,
            )
    # Composite whenever an exact skeleton exists: calibrated layouts use the
    # measured regions, every other layout auto-detects its blank panel, so
    # the molecule is always pixel-exact instead of model-drawn.
    will_composite = bool(skeleton_rendered)
    if skeleton_rendered:
        features["_skeleton_image"] = skeleton_png
        if will_composite:
            features["_composite_layout"] = layout_type
        # always provide the exact model as an extra reference: the model draws
        # a faithful fallback in case the guarded compositing later skips
        extra_images.append(skeleton_png)
        print(f"  Accurate ball-and-stick model rendered: {skeleton_png}")
    adapted_prompt = build_adapted_prompt(best_template, features,
                                          composite_mode=will_composite)
    print(f"\n  Adapted prompt length: {len(adapted_prompt)} chars")

    if args.dry_run:
        print("\n[DRY RUN] Would generate figure with above settings.")
        print(f"  Template: {template_id}")
        print(f"  Reference: {reference_image}")
        endpoint = "/chat/completions" if wire_api == "chat-completions" else "/images/edits"
        print(f"  API: {openai_api_url(base_url, endpoint)}")
        print(f"  Wire API: {wire_api}")
        print(f"  Model: {args.model}")
        out_dir = project_dir / "03_figure_redraw"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _build_report(args, best_template, features, adapted_prompt,
                               reference_image, base_url, args.model, status="dry_run")
        write_json(out_dir / "overview_template_match.json", report)
        print(f"\n  Matching report saved to: {out_dir / 'overview_template_match.json'}")
        return

    # Call API
    if not api_key and not image_gateway_configured():
        print("\nERROR: No API key available.", file=sys.stderr)
        print("  Set IMAGE_OPENAI_API_KEY or OPENAI_API_KEY environment variable,", file=sys.stderr)
        print("  create a .env file, or pass --api-key.", file=sys.stderr)
        print("\n  Saving template match report for later use...", file=sys.stderr)
        out_dir = project_dir / "03_figure_redraw"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _build_report(args, best_template, features, adapted_prompt,
                               reference_image, base_url, args.model, status="pending_api_key")
        write_json(out_dir / "overview_template_match.json", report)
        print(f"  Report saved to: {out_dir / 'overview_template_match.json'}", file=sys.stderr)
        sys.exit(2)

    print(f"\nCalling image edit API...")
    print(f"  Base URL: {base_url}")
    print(f"  Wire API: {wire_api}")
    print(f"  Model: {args.model}")

    # The reference image is a LAYOUT guide only: skeletonize it so the model
    # can copy shapes/colors but not the template's text content
    out_dir = project_dir / "03_figure_redraw"
    out_dir.mkdir(parents=True, exist_ok=True)
    upload_image = build_layout_skeleton(reference_image, out_dir / "template_skeleton.png")
    if upload_image != reference_image:
        print(f"  Layout-skeleton reference: {upload_image}")

    request_metadata: dict[str, str] = {
        "reference_mode": "layout-skeleton" if upload_image != reference_image else "original",
    }
    try:
        image_bytes = call_image_edit_api(
            api_key,
            base_url,
            upload_image,
            adapted_prompt,
            args.model,
            preferred_size=args.size,
            wire_api=wire_api,
            request_metadata=request_metadata,
            extra_images=extra_images,
        )
    except Exception as exc:
        print(f"\nERROR: API call failed: {exc}", file=sys.stderr)
        out_dir = project_dir / "03_figure_redraw"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _build_report(args, best_template, features, adapted_prompt,
                               reference_image, base_url, args.model, status="api_error", error=str(exc),
                               request_metadata=request_metadata)
        write_json(out_dir / "overview_template_match.json", report)
        sys.exit(3)

    # Save output
    out_dir = project_dir / "03_figure_redraw"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else (out_dir / "overview_figure.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    composite_report: dict[str, Any] = {
        "enabled": will_composite,
        "status": "not_applicable",
        "reason": "",
        "panel_source": "",
    }
    if not will_composite:
        composite_report["reason"] = "skeleton_render_failed"
    else:
        composited, skip_reason, panel_source = composite_skeleton_into_figure(
            output_path, skeleton_png, layout_type)
        if composited:
            composite_report["status"] = "success"
            composite_report["panel_source"] = panel_source
            print(f"  Exact skeleton composited into the structure panel "
                  f"(pixel-exact, panel source: {panel_source}).")
        else:
            composite_report["status"] = "skipped"
            composite_report["reason"] = skip_reason
            print(
                "  WARNING: skeleton compositing was SKIPPED by the safety guard "
                f"(reason: {skip_reason}).\n"
                "  In composite mode the model was told to leave the structure panel blank,\n"
                "  so this overview figure may contain NO molecule at all. Inspect the PNG\n"
                "  manually; if the panel is empty, calibrate a new candidate region.",
                file=sys.stderr,
            )
    composite_report["skeleton_source"] = skeleton_source
    if args.skeleton_style == "ai3d":
        composite_report["ai_redraw_gate"] = ai_redraw_note or "not_attempted"
    skeleton_report = {
        "strict": strict_skeleton,
        "ai_style_required": ai_style_required,
        "style": args.skeleton_style,
        "source": skeleton_source,
        "smiles": smiles,
        "attempts": skeleton_attempts,
    }
    if composite_report["status"] == "skipped" and strict_skeleton:
        # In composite mode the model was told to leave the panel blank, so a
        # skipped compositing pass can leave the overview without ANY molecule.
        # Strict mode refuses to publish that degraded result.
        report = _build_report(args, best_template, features, adapted_prompt,
                               reference_image, base_url, args.model,
                               status="composite_skipped",
                               error=str(composite_report["reason"]),
                               output_path=str(output_path),
                               output_size=output_path.stat().st_size,
                               request_metadata=request_metadata,
                               composite=composite_report,
                               skeleton=skeleton_report)
        write_json(out_dir / "overview_template_match.json", report)
        print(
            "\nERROR: Strict skeleton mode: skeleton compositing was skipped "
            f"({composite_report['reason']}); the overview may lack the molecule. "
            "Re-run the overview generation to retry.",
            file=sys.stderr,
        )
        sys.exit(5)
    print(f"\n  Overview figure saved to: {output_path}")
    print(f"  File size: {output_path.stat().st_size:,} bytes")

    # Save match report
    report = _build_report(args, best_template, features, adapted_prompt,
                           reference_image, base_url, args.model,
                           status="success", output_path=str(output_path),
                           output_size=output_path.stat().st_size,
                           request_metadata=request_metadata,
                           composite=composite_report,
                           skeleton=skeleton_report)
    write_json(out_dir / "overview_template_match.json", report)
    print(f"  Match report saved to: {out_dir / 'overview_template_match.json'}")


if __name__ == "__main__":
    main()
