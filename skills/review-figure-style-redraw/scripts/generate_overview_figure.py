#!/usr/bin/env python3
"""Generate a review overview figure by auto-matching the best template.

Usage:
    python skills/review-figure-style-redraw/scripts/generate_overview_figure.py \
        --review-root <path> \
        --project-id <id> \
        [--api-key <key>] \
        [--base-url <url>] \
        [--model <model>]

The script:
1. Reads the review outline and selected_discovery_results to extract structure.
2. Scores each template bundled under this skill's assets directory.
3. Selects the best-matching template.
4. Adapts the template prompt with review-specific content.
5. Calls the OpenAI-compatible image edit API with the template reference image.
6. Saves the generated overview figure.
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


TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
LANDSCAPE_OVERVIEW_IMAGE_SIZE = "1536x1024"
SQUARE_COMPATIBLE_IMAGE_SIZE = "1024x1024"

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
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{endpoint}"
    return f"{base}/v1{endpoint}"


def overview_image_size_candidates(base_url: str, preferred_size: str = "") -> list[str]:
    """Return image sizes in provider-compatible retry order.

    Geek2API-backed xiaoleai image routes reject the OpenAI landscape size
    before generation starts.  Use their square size directly instead of
    repeating a known-invalid request.  Other OpenAI-compatible providers keep
    the landscape request first and can still fall back to the widely supported
    square size.  ``OVERVIEW_IMAGE_SIZE`` and ``--size`` remain explicit
    operator overrides, while the compatible fallback is always retained.
    """
    configured = preferred_size.strip() or os.environ.get("OVERVIEW_IMAGE_SIZE", "").strip()
    provider_default = (
        SQUARE_COMPATIBLE_IMAGE_SIZE
        if "api.xiaoleai.team" in base_url.lower()
        else LANDSCAPE_OVERVIEW_IMAGE_SIZE
    )
    candidates = [configured, provider_default, SQUARE_COMPATIBLE_IMAGE_SIZE]
    return list(dict.fromkeys(size for size in candidates if size))


def prompt_for_overview_size(prompt: str, size: str) -> str:
    """Keep a landscape reading order when a provider only emits a square."""
    if size != SQUARE_COMPATIBLE_IMAGE_SIZE:
        return prompt
    return (
        prompt
        + " The image service uses a square canvas. Preserve the template's landscape reading order "
        "inside the square: keep every panel fully visible, use balanced white margins, do not crop, "
        "stretch, stack, or omit any title, category, reaction, label, legend, or conclusion block."
    )


def condense_overview_prompt(prompt: str, max_chars: int = 3900) -> str:
    """Trim the prompt to provider limits without losing the adaptation data.

    Providers such as micuapi gpt-image-2 reject prompts over ~4000 chars.
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
}
_ATOM_RADIUS = {"C": 20, "H": 12, "O": 19, "N": 19, "S": 22, "P": 22,
                "Si": 22, "B": 19, "R": 24}


# Generic SMILES -> accurate 3D geometry.  Any review topic can supply its
# core motif as SMILES (query_plan "skeleton_smiles"), otherwise a keyword map
# picks a built-in SMILES.  Geometry is exact by construction:
# rings = regular polygons, substituents grow with ideal hybridization angles
# (sp 180 / sp2 120 / sp3 109.5), cumulated double bonds get perpendicular
# planes (allene chirality).

_VALENCE = {"C": 4, "N": 3, "O": 2, "S": 2, "P": 3, "B": 3, "H": 1,
            "F": 1, "Cl": 1, "Br": 1, "I": 1, "Si": 4, "R": 0}
_AROMATIC_EL = {"c": "C", "n": "N", "o": "O", "s": "S"}
_TWO_LETTER = ("Cl", "Br", "Si")
_LABEL_SMILES = [
    ("allenoate", "*C(*)=C=C(C(=O)O*)*"),
    ("biaryl", "*c1ccccc1-c2ccccc2*"),
    ("atropisomer", "*c1ccccc1-c2ccccc2*"),
    ("indole", "c1ccc2[nH]ccc2c1"),
    ("cyclopropane", "*C1(*)CC1"),
    ("propargyl", "OCC#C*"),
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


def parse_smiles(smiles: str):
    """Parse an organic-subset SMILES into atoms/bonds (aromatic bond = 1.5)."""
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
        if ch in "=#-":
            pending = {"=": 2.0, "#": 3.0, "-": 1.0}[ch]; i += 1; continue
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
        if ch == "[":
            j = smiles.index("]", i)
            tok = smiles[i + 1:j]
            m = re.match(r"([A-Za-z][a-z]?)", tok)
            raw = m.group(1) if m else "C"
            aromatic = raw in _AROMATIC_EL
            el = _AROMATIC_EL.get(raw, raw[0].upper() + raw[1:])
            hm = re.search(r"H(\d*)", tok)
            bracket_h = 1 if hm and hm.group(1) == "" else (int(hm.group(1)) if hm else 0)
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
        atoms.append({"el": el, "aromatic": aromatic, "h": bracket_h})
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


def _order_cycle(verts, adj):
    """Order a cycle's vertex set along actual bonds (2 neighbors each)."""
    vs = set(verts)
    emap = {v: [w for w in adj[v] if w in vs] for v in vs}
    if any(len(emap[v]) != 2 for v in vs):
        return None
    ordered, cur, prv = [], next(iter(vs)), None
    for _ in vs:
        ordered.append(cur)
        nxts = [w for w in emap[cur] if w != prv]
        if not nxts:
            return None
        prv, cur = cur, nxts[0]
    return ordered


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

    for ring in _find_rings(n, bonds):
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
            else:  # spiro-like single shared atom: perpendicular plane
                a = ring[k0]
                A = coords[a]
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

    if coords[0] is None:
        coords[0] = (0.0, 0.0, 0.0)
        frames[0] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

    def local_to_global(i, v):
        e1, e2, e3 = frames[i]
        return _vadd(_vadd(_vscale(e1, v[0]), _vscale(e2, v[1])), _vscale(e3, v[2]))

    def next_slot(i):
        for s in _SLOTS[hyb[i]]:
            g = local_to_global(i, s)
            if all(_vdot(g, u) < 0.9 for u in used[i]):
                return g
        for s in [(0, 1, 0), (0, 0, 1), (0, -1, 0), (0, 0, -1)]:
            g = local_to_global(i, s)
            if all(_vdot(g, u) < 0.9 for u in used[i]):
                return g
        return local_to_global(i, (0, 1, 0))

    q = deque(i for i in range(n) if coords[i] is not None)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if coords[v] is not None:
                continue
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


def render_smiles_ball_and_stick(smiles: str, output_path: Path,
                                 img_size: tuple[int, int] = (900, 640)) -> Path | None:
    """Render a SMILES string as a chemically accurate ball-and-stick PNG."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
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
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    scale = min(img_size) * 0.62 / span
    ox, oy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    px = [(img_size[0] / 2 + (p[0] - ox) * scale, img_size[1] / 2 + (p[1] - oy) * scale, p[2])
          for p in pts]

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
            offsets = {1.0: [0.0], 2.0: [-4.0, 4.0], 3.0: [-6.0, 0.0, 6.0],
                       1.5: [-3.0, 3.0]}[order]
            for off in offsets:
                draw.line([(x1 + nx * off, y1 + ny * off), (x2 + nx * off, y2 + ny * off)],
                          fill=(120, 120, 120), width=8)
        else:
            _, i, el = item
            r = _ATOM_RADIUS.get(el, 20)
            color = _CPK_COLORS.get(el, (150, 150, 150))
            x, y = px[i][0], px[i][1]
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color, outline=(20, 20, 20), width=2)
            draw.ellipse([x - r * 0.55, y - r * 0.65, x - r * 0.05, y - r * 0.15],
                         fill=tuple(min(255, c + 90) for c in color))
            if el == "R":
                r_idx += 1
                draw.text((x - 6, y - 8), f"R{r_idx}", fill=(255, 255, 255))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path


# Normalized (x0, y0, x1, y1) structure-panel regions per layout for
# pixel-exact programmatic skeleton compositing.  Layouts not listed keep the
# model-drawn molecule (extra reference image mode).
_COMPOSITE_REGIONS = {
    "why-strategy-what": (0.16, 0.31, 0.415, 0.65),
    "module-cards-crosscut-sidebar": (0.02, 0.17, 0.35, 0.52),
}


def composite_skeleton_into_figure(figure_path: Path, skeleton_path: Path, layout: str) -> bool:
    """Paste the exact ball-and-stick model into the layout's structure panel.

    Guarantees pixel-exact molecular geometry: the panel is cleared to white
    and the programmatically rendered model is centered into it.
    """
    box = _COMPOSITE_REGIONS.get(layout)
    if not box or not skeleton_path.exists():
        return False
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    with Image.open(figure_path) as fig:
        fig = fig.convert("RGB")
        W, H = fig.size
        x0, y0, x1, y1 = (int(W * box[0]), int(H * box[1]), int(W * box[2]), int(H * box[3]))
        draw = ImageDraw.Draw(fig)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=24, fill=(255, 255, 255))
        with Image.open(skeleton_path) as sk:
            sk = sk.convert("RGB")
            sk.thumbnail((x1 - x0 - 30, y1 - y0 - 30))
            sx = x0 + (x1 - x0 - sk.width) // 2
            sy = y0 + (y1 - y0 - sk.height) // 2
            fig.paste(sk, (sx, sy))
        fig.save(figure_path, format="PNG")
    return True


def render_skeleton_model(features: dict[str, Any], output_path: Path,
                          img_size: tuple[int, int] = (900, 640)) -> Path | None:
    """Render the review's core motif as an accurate ball-and-stick PNG.

    SMILES resolution order: explicit project override (query_plan
    "skeleton_smiles"/"smiles") -> product keyword map -> title/outline/draft
    keyword scan.  Returns None when no motif can be determined (caller then
    falls back to the text description).
    """
    smiles = features.get("skeleton_smiles", "") or ""
    if not smiles:
        products = features.get("product_keywords", [])
        smiles = _smiles_for_label(products[0] if products else "") or ""
    if not smiles:
        blob_parts = [features.get("review_title", ""), features.get("_outline_text", "")[:800]]
        project_dir = features.get("_project_dir")
        if project_dir:
            draft_path = Path(project_dir) / "04_first_draft" / "first_draft.md"
            if draft_path.exists():
                blob_parts.append(draft_path.read_text(encoding="utf-8", errors="ignore")[:1200])
        smiles = _smiles_for_label(" ".join(blob_parts)) or ""
    if not smiles:
        return None
    return render_smiles_ball_and_stick(smiles, output_path, img_size)


def resolve_api_key(cli_value: str, base_url: str) -> str:
    """Use the matching credential when text and image providers differ."""
    if cli_value:
        return cli_value
    image_key = os.environ.get("IMAGE_OPENAI_API_KEY", "")
    if image_key:
        return image_key
    if "api.xiaoleai.team" in str(base_url).lower():
        return os.environ.get("XIAOLEAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("XIAOLEAI_API_KEY", "")


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
        "classification_rule": "",
        "product_keywords": [],
        "substrate_keywords": [],
        "catalyst_keywords": [],
        "skeleton_smiles": "",
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
    # Read selected outline for section structure
    outline_path = project_dir / "01_matrix_outline" / "selected_outline.md"
    if outline_path.exists():
        text = outline_path.read_text(encoding="utf-8")
        features["_outline_text"] = text  # Store for later use by _build_metal_rows_text
        import re
        sections = re.findall(r"^## \d+\.", text, re.MULTILINE)
        features["num_sections"] = len(sections)
        if "catalyst" in text.lower() and "metal" in text.lower():
            features["has_metal_classification"] = True
        if "organocatal" in text.lower():
            features["has_organocatalysis"] = True
        metal_map = {
            "palladium": "Pd", "copper": "Cu", "nickel": "Ni",
            "cobalt": "Co", "gold": "Au", "rhodium": "Rh",
            "iridium": "Ir", "iron": "Fe",
        }
        for key, sym in metal_map.items():
            if key in text.lower():
                features["metal_categories"].append(sym)
        if features["has_organocatalysis"]:
            features["metal_categories"].append("Organocatalysis")

        # Theme-level signals used for template scoring
        outline_lower = text.lower()
        features["has_chirality"] = bool(
            re.search(r"chiral|enantio|asymmetric|atropisomer|stereoselect", outline_lower)
        )
        features["has_reaction_focus"] = bool(
            re.search(
                r"reaction|synthesis|synthetic|catalytic|cataly[sz]ed|coupling|functionalization",
                outline_lower,
            )
        )

        # Generic category extraction from section headings (for non-metal reviews)
        section_titles = re.findall(r"^## \d+\.\s*(.+)$", text, re.MULTILINE)
        generic_cats = []
        for title in section_titles:
            label = _heading_to_category(title)
            if label and label not in generic_cats:
                generic_cats.append(label)

        # If metal_categories is mostly empty or only has Organocatalysis,
        # use the generic categories from outline sections
        real_metals = [m for m in features["metal_categories"] if m != "Organocatalysis"]
        if len(real_metals) < 3 and generic_cats:
            features["metal_categories"] = generic_cats

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
    review_title = features.get("review_title", "")
    if review_title and not re.search(r"[\u4e00-\u9fff]", review_title):
        title_lower = review_title.strip().lower()
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


def build_adapted_prompt(template: dict[str, Any], features: dict[str, Any]) -> str:
    """Adapt the template prompt with review-specific content (fully generic)."""
    base_prompt = _retheme_base_prompt(template.get("prompt", ""), features)

    # Build English title: if original is non-English, construct from keywords
    gb = features.get("group_by", [])
    review_title = features.get("review_title", "Review")
    if re.search(r"[\u4e00-\u9fff]", review_title):
        products = features.get("product_keywords", [])
        prod = products[0] if products else "target compounds"
        time_w = features.get("time_window", "recent years")
        english_title = f"Recent Advances in {prod.title()} ({time_w})"
    else:
        english_title = review_title

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

    # Visual style description with dynamic term replacement
    visual_style_desc = _get_visual_style_description(template)
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

Banner title (English, for the navy banner): "{english_title}"
Original topic (for reference): "{review_title}"
Time window: {time_window}
Classification rule: "{classification_rule}"

{skeleton_desc}

{metal_rows_text}

{take_home_text}

CRITICAL RULES:
- ALL text in the figure MUST be in ENGLISH only. No Chinese or other non-English characters.
- If the review title (banner) is in Chinese or any non-English language, translate it to professional English before rendering.
- Do NOT draw a reaction equation (no arrow, no substrate-to-product transformation).
- The left-page structure area shows ONLY a single representative skeleton/motif.
- Keep all text concise. Category labels stay SHORT (symbols or max 2 words). No long names in hexagons.
- Right-page cells: max 2-3 words each. Use real metrics from the content above.
- Generate the figure with ALL text fields FILLED IN. No dotted placeholders.
- Every panel, arc, box, and cell in the layout MUST contain the provided text; absolutely no blank regions.
- Use the same visual style, layout, color scheme, and icon design as the reference template.

APPROVED TERMINOLOGY (use ONLY these exact phrases in the figure text):
{approved_terms}

FORBIDDEN BEHAVIOR (strictly enforced):
- Do not hyphenate a word across two lines.
- Do not replace letters with visually similar characters (e.g. 'l' for '1', 'O' for '0').
- Do not use placeholder-like pseudo-English or gibberish text.
- Do not combine two approved phrases into a new unlisted phrase.
- Do not repeat or truncate words.
- Every word in the figure must be a correctly spelled English word from the approved list or a standard chemical symbol/abbreviation (Pd, Cu, Ni, ee, R1, R2, etc.).
"""
    return base_prompt + adaptation


def _build_skeleton_description(features: dict[str, Any]) -> str:
    """Determine what 3D ball-and-stick model to draw based on product keywords."""
    products = features.get("product_keywords", [])
    prod_label = products[0] if products else "product"

    # Map product types to ball-and-stick model descriptions (generic, extensible)
    skeleton_map = {
        "axially chiral allenes": ("A 3D ball-and-stick model of an allene (C=C=C). Show three carbon spheres in a linear arrangement: two terminal carbons (black spheres) on opposite ends, one central carbon (black sphere) in the middle. The two terminal carbons each have two substituent spheres (colored, e.g. blue for R groups) arranged in PERPENDICULAR planes — one pair horizontal, one pair vertical — demonstrating axial chirality.", "axially chiral allene"),
        "allenes": ("A 3D ball-and-stick model of an allene (C=C=C). Three black carbon spheres in a line, with two colored substituent spheres on each terminal carbon. The substituent pairs are in perpendicular planes.", "allene"),
        "trisubstituted allenes": ("A 3D ball-and-stick model of a trisubstituted allene (C=C=C) with one H sphere (white) and two R group spheres on one terminal carbon, and two R spheres on the other.", "trisubstituted allene"),
        "tetrasubstituted allenes": ("A 3D ball-and-stick model of a tetrasubstituted allene (C=C=C) with four R group spheres on the two terminal carbons, in perpendicular planes.", "tetrasubstituted allene"),
        "allenoates": ("A 3D ball-and-stick model of an allenoate: C=C=C-C(=O)-O- with red oxygen spheres.", "allenoate"),
        "allenyl silanes": ("A 3D ball-and-stick model with C=C=C-Si, tan/gold silicon sphere.", "allenyl silane"),
        "allenyl boranes": ("A 3D ball-and-stick model with C=C=C-B, pink/orange boron sphere.", "allenyl borane"),
        "biaryl compounds": ("A 3D ball-and-stick model of two connected benzene rings (Ar-Ar) at an angle, showing axial chirality with restricted rotation.", "biaryl"),
        "atropisomers": ("A 3D ball-and-stick model of two aromatic rings at an angle with restricted rotation.", "atropisomer"),
        "cyclopropanes": ("A 3D ball-and-stick model of a three-membered carbon ring (triangle of black spheres).", "cyclopropane"),
        "alkenes": ("A 3D ball-and-stick model of an alkene C=C with four substituent spheres.", "alkene"),
        "alkynes": ("A 3D ball-and-stick model of an alkyne C≡C with two substituent spheres.", "alkyne"),
        "alpha-allenic alcohols": ("A 3D ball-and-stick model of an allene with an OH group (red oxygen sphere).", "alpha-allenic alcohol"),
    }

    # Find matching pattern
    skeleton = None
    label = prod_label
    prod_lower = prod_label.lower()
    for key, (desc, lbl) in skeleton_map.items():
        if key in prod_lower or prod_lower in key:
            skeleton = desc
            label = lbl
            break

    if not skeleton:
        skeleton = f"A 3D ball-and-stick model representing '{prod_label}'. Use black spheres for carbon, red for oxygen, blue for nitrogen, white for hydrogen, and colored spheres for R groups."
        label = prod_label

    if features.get("_composite_layout") in _COMPOSITE_REGIONS:
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

    return f"""LEFT-PAGE STRUCTURE AREA (center of left page):
Draw a SINGLE 3D ball-and-stick molecular model (NOT a reaction equation, NO arrow, NO 2D bond-line):
  Model: {skeleton}
  Label below: "{label}"

BALL-AND-STICK RENDERING RULES:
- *** CRITICAL: BOND ANGLES MUST BE CHEMICALLY CORRECT — sp2 ~120°, sp3 ~109°, sp (cumulated C=C=C) ~180°. ABSOLUTELY NO 90° ANGLES ANYWHERE IN THE MODEL. ***
- Render as a 3D BALL-AND-STICK model (not 2D bond-line notation)
- Atoms = colored spheres: Carbon=black, Hydrogen=white, Oxygen=red, Nitrogen=blue, Sulfur=yellow, Phosphorus=orange, Silicon=tan, Boron=pink
- Bonds = gray cylinders/sticks connecting spheres
- For allenes (C=C=C): the central carbon is sp (180°), terminal carbons are sp2 (120°). The two substituent pairs are at ~120° from each other on each terminal carbon, and the two planes are perpendicular (90° to each other in ORIENTATION, but bond angles within each plane are 120°).
- Double bonds = two parallel sticks; cumulated double bonds = consecutive pairs
- R-group substituents = colored spheres labeled R1, R2, R3, R4
- Use a clean 3D perspective view (slightly rotated) so the spatial arrangement is clear
- White or light gray background
- The model should look like a standard chemistry textbook 3D molecular model

STEREOCHEMISTRY (must be visually prominent):
- For AXIAL CHIRALITY (allenes): show the two substituent planes clearly PERPENDICULAR (one horizontal, one vertical) — this is the key visual feature
- The 3D perspective should make the chirality obvious at a glance

QUALITY CHECK:
- Verify ALL bond angles are ~120° (sp2) or ~180° (sp) — NO 90° angles allowed
- Verify the 3D arrangement clearly shows the molecular geometry
- Verify no atoms are missing or misplaced
- Verify the model is recognizable as the intended molecule"""


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
    model: str = "gpt-image-1",
    preferred_size: str = "",
    wire_api: str = "",
    request_metadata: dict[str, str] | None = None,
    extra_images: list[Path] | None = None,
) -> bytes:
    """Call image generation API. Supports both OpenAI-compatible and DashScope native format."""
    # Detect if this is an Alibaba Cloud / DashScope endpoint
    if "maas.aliyuncs.com" in base_url or "dashscope" in base_url:
        if request_metadata is not None:
            request_metadata.update({"endpoint": "dashscope-native", "image_size": "2K"})
        return _call_dashscope_native(api_key, base_url, reference_image, prompt, model, extra_images)

    # OpenAI-compatible endpoints.  Some New API relays expose gpt-image-2
    # only through multimodal /chat/completions.  When that transport is
    # configured, do not probe /images/*: doing so can trigger provider-side
    # access controls and can never reach the model assigned to the chat route.
    resolved_wire_api = normalize_image_wire_api(wire_api)
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
            "User-Agent": "review-writer-overview-figure/1.0",
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
            "User-Agent": "review-writer-overview-figure/1.0",
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
                         request_metadata: dict[str, str] | None = None) -> dict:
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
    return report


def main():
    parser = argparse.ArgumentParser(description="Generate review overview figure from template")
    parser.add_argument("--review-root", required=True, help="Path to review-writer project root")
    parser.add_argument("--project-id", required=True, help="Project ID")
    parser.add_argument("--api-key", default="", help="API key (or set XIAOLEAI_API_KEY / OPENAI_API_KEY)")
    parser.add_argument("--base-url", default="", help="API base URL")
    parser.add_argument("--model", default="gpt-image-1", help="Image generation model")
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
    parser.add_argument("--output", default="", help="Output path for generated figure")
    parser.add_argument("--dry-run", action="store_true", help="Only show template matching, don't call API")
    args = parser.parse_args()

    review_root = Path(args.review_root).resolve()
    project_dir = review_root / "review-projects" / args.project_id
    load_dotenv(review_root)

    # Resolve API settings
    base_url = args.base_url or os.environ.get(
        "IMAGE_OPENAI_BASE_URL",
        os.environ.get("OPENAI_BASE_URL", "https://api.xiaoleai.team"),
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
    will_composite = False
    if render_skeleton_model(features, skeleton_png):
        features["_skeleton_image"] = skeleton_png
        will_composite = layout_type in _COMPOSITE_REGIONS
        if will_composite:
            features["_composite_layout"] = layout_type
        else:
            extra_images.append(skeleton_png)
        print(f"  Accurate ball-and-stick model rendered: {skeleton_png}")
    adapted_prompt = build_adapted_prompt(best_template, features)
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
    if not api_key:
        print("\nERROR: No API key available.", file=sys.stderr)
        print("  Set XIAOLEAI_API_KEY or OPENAI_API_KEY environment variable,", file=sys.stderr)
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
    if will_composite and composite_skeleton_into_figure(output_path, skeleton_png, layout_type):
        print("  Exact skeleton composited into the structure panel (pixel-exact).")
    print(f"\n  Overview figure saved to: {output_path}")
    print(f"  File size: {len(image_bytes):,} bytes")

    # Save match report
    report = _build_report(args, best_template, features, adapted_prompt,
                           reference_image, base_url, args.model,
                           status="success", output_path=str(output_path),
                           output_size=len(image_bytes), request_metadata=request_metadata)
    write_json(out_dir / "overview_template_match.json", report)
    print(f"  Match report saved to: {out_dir / 'overview_template_match.json'}")


if __name__ == "__main__":
    main()
