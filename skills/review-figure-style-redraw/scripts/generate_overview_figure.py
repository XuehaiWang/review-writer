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
2. Scores each template in references/templates/overview_templates.json.
3. Selects the best-matching template.
4. Adapts the template prompt with review-specific content.
5. Calls the OpenAI-compatible image edit API with the template reference image.
6. Saves the generated overview figure.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        "metal_categories": [],
        "time_window": "",
        "reaction_type": "",
        "group_by": [],
        "review_title": "",
        "classification_rule": "",
        "product_keywords": [],
        "substrate_keywords": [],
        "catalyst_keywords": [],
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

        # Generic category extraction from section headings (for non-metal reviews)
        # Extract ## N. Title lines, skip Introduction and Conclusion
        section_titles = re.findall(r"^## \d+\.\s*(.+)$", text, re.MULTILINE)
        generic_cats = []
        skip_words = {"introduction", "conclusion", "outlook", "summary"}
        for title in section_titles:
            title_clean = title.strip()
            if title_clean.lower().split()[0] in skip_words or title_clean.lower() in skip_words:
                continue
            # Extract a short category label (first 2-3 meaningful words)
            # Remove common prefixes like "Allenylation via", "Synthesis from"
            short = re.sub(r"^(alleny?lation|synthesis|reactions?)\s+(via|from|of|through)\s+", "", title_clean, flags=re.IGNORECASE)
            # Also remove trailing generic words
            short = re.sub(r"\s+(leaving groups?|and other.*|\(.*\))$", "", short, flags=re.IGNORECASE)
            # If still contains 'via'/'from', the prefix wasn't stripped - take words after it
            if re.search(r"\b(via|from)\b", short, re.IGNORECASE):
                parts = re.split(r"\b(via|from)\b", short, flags=re.IGNORECASE)
                short = parts[-1].strip() if len(parts) > 1 else short
            # Take first 2 words max as the category label (keep it short for hexagon)
            words = short.split()[:2]
            label = " ".join(words)
            if label and len(label) <= 30:
                generic_cats.append(label)

        # If metal_categories is mostly empty or only has Organocatalysis,
        # use the generic categories from outline sections
        real_metals = [m for m in features["metal_categories"] if m != "Organocatalysis"]
        if len(real_metals) < 3 and generic_cats:
            features["metal_categories"] = generic_cats

    # Read discovery results for group stats
    sel_path = project_dir / "00_discovery" / "selected_discovery_results.json"
    if sel_path.exists():
        sel = read_json(sel_path)
        features["group_by"] = sel.get("group_by", features["group_by"])

    # Store project dir for multi-pass extraction in _build_metal_rows_text
    features["_project_dir"] = project_dir

    return features


def score_template(template: dict[str, Any], features: dict[str, Any]) -> float:
    """Score a template against review features. Higher = better match."""
    score = 0.0
    layout = template.get("layout_type", "")
    prompt = template.get("prompt", "").lower()
    desc = template.get("description", "").lower()

    # Bonus: template explicitly mentions "by catalyst center metal" or metal classification
    if "catalyst center metal" in prompt or "metal-centered" in prompt:
        score += 3.0
    if features["has_metal_classification"]:
        if "metal" in layout or "metal" in desc:
            score += 2.0

    # Bonus: template has 5 categories (matching our 5 sections)
    if "five" in prompt or "5 " in prompt:
        score += 1.0

    # Bonus: template has classification rule panel
    if "classification rule" in prompt or "classification" in desc:
        score += 2.0

    # Bonus: template has take-home messages / outlook section
    if "take-home" in prompt or "outlook" in prompt or "conclusion" in prompt:
        score += 1.0

    # Bonus: template has reaction scheme at top/center
    if "reaction" in prompt and ("scheme" in prompt or "top" in prompt or "center" in prompt):
        score += 1.0

    # Bonus: layout types that work well for metal-classified reviews
    preferred_layouts = [
        "dual-page-spread",           # Best: shows classification + metal rows
        "module-cards-crosscut-sidebar",  # Good: modular cards per metal
        "top-reaction-5col-table",    # Good: reaction + columns
        "route-map-start-strategy-result",  # OK: horizontal lanes
        "metal-x-dimension-matrix",   # OK: comparison matrix
    ]
    for i, pref in enumerate(preferred_layouts):
        if layout == pref:
            score += (5 - i) * 0.5
            break

    # Bonus: template mentions time window / recent years
    if "time window" in prompt or "recent" in prompt or "last five" in prompt:
        score += 1.0

    # Bonus: template has cross-cutting / shared themes sidebar
    if "cross-cut" in prompt or "shared" in prompt or "sidebar" in layout:
        score += 0.5

    # Penalty: if review has organocatalysis but template doesn't mention it
    # (not a big penalty since "Other metals" can cover it)

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

def build_adapted_prompt(template: dict[str, Any], features: dict[str, Any]) -> str:
    """Adapt the template prompt with review-specific content (fully generic)."""
    base_prompt = template.get("prompt", "")

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

    # Replace hardcoded 'metal' terms for non-metal classification schemes
    if gb and gb[0] != "catalyst_or_method":
        gb_word = gb[0].replace("_", " ")
        for old, new in [
            ("catalyst center metal", gb_word),
            ("catalytically active metal center", f"{gb_word} identity"),
            ("classification by metal centers", f"classification by {gb_word}"),
            ("metal-catalyzed", f"{gb_word}-based"),
            ("By catalyst center metal", features.get("classification_rule", "")),
        ]:
            base_prompt = base_prompt.replace(old, new)

    metals = features.get("metal_categories", [])
    if not metals:
        metals = ["Cat-1", "Cat-2", "Cat-3", "Cat-4", "Cat-5"]

    time_window = features.get("time_window", "recent years")
    classification_rule = features.get("classification_rule", "By category")

    # Build skeleton, rows, and take-home messages
    skeleton_desc = _build_skeleton_description(features)
    metal_rows_text = _build_metal_rows_text(features)
    take_home_text = _build_take_home_text(features)

    # Visual style description with dynamic term replacement
    visual_style_desc = _get_visual_style_description(template)
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
- Keep all text concise. Category labels use ONLY symbols (e.g. Pd, Cu). No full names in hexagons.
- Right-page cells: max 2-3 words each. Use real metrics from the content above.
- Generate the figure with ALL text fields FILLED IN. No dotted placeholders.
- Use the same visual style, layout, color scheme, and icon design as the reference template.

APPROVED TERMINOLOGY (use ONLY these exact phrases in the figure text):
allene, allenes, allene synthesis, allene framework, allene motif,
allenation, asymmetric allenation, enantioselective allenation,
propargylic alcohol, propargylic alcohols, propargylic substrates,
axial chirality, axially chiral allenes, chiral allenes,
enantioenriched allenes, asymmetric induction, chiral induction,
enantioselectivity, regioselectivity, chemoselectivity,
stereoselectivity, catalytic metal center,
classification by metal centers, metal catalysis,
transition-metal catalysis, metal-catalyzed allene synthesis,
catalytic system, representative catalytic system,
representative transformation, representative reaction,
substrate class, substrate scope, product class, ligand class,
chiral ligand, chiral amine, reaction mode, catalytic strategy,
key feature, key advantage, main limitation, key challenge,
recent advances, major advances, key developments,
research trends, future directions,
opportunities and challenges, broad substrate scope,
diverse substrate classes, high enantioselectivity,
functional-group tolerance, sustainable catalysis,
earth-abundant metal catalysis, synthetic utility.

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
    metals = features.get("metal_categories", [])
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

    for m in metals[:6]:
        d1 = pass1.get(m, {})
        d2 = pass2.get(m, {})
        d3 = pass3.get(m, {})
        # Multi-pass fallback: pass1 → pass2 → pass3 → derive from name → dash
        cell1 = (d1.get("strategy", "") or d2.get("strategy", "")
                 or d3.get("strategy", "") or _derive_strategy(m))
        cell2 = (d1.get("selectivity", "") or d2.get("selectivity", "")
                 or d3.get("selectivity", "") or _derive_selectivity(m))
        cell3 = (d1.get("highlight", "") or d2.get("highlight", "")
                 or d3.get("highlight", "") or _derive_highlight(m))
        lines.append(f"  [{m}] cell1=\"{cell1}\"  cell2=\"{cell2}\"  cell3=\"{cell3}\"")

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
        result[cat] = _match_patterns(combined, strategy_pats, selectivity_pats, highlight_pats)
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
            result[cat] = _match_patterns(combined, strategy_pats, selectivity_pats, highlight_pats)
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


def _match_patterns(text: str, strategy_pats, selectivity_pats, highlight_pats) -> dict[str, str]:
    """Match text against pattern lists and return first hits."""
    result = {}
    for pat, label in strategy_pats:
        if re.search(pat, text):
            result["strategy"] = label
            break
    for pat, label in selectivity_pats:
        if re.search(pat, text):
            result["selectivity"] = label
            break
    for pat, label in highlight_pats:
        if re.search(pat, text):
            result["highlight"] = label
            break
    return result


def _derive_strategy(cat: str) -> str:
    """Final fallback: derive a strategy label from the category name itself."""
    cat_lower = cat.lower()
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


def _parse_outline_sections(outline_text: str, categories: list[str]) -> dict[str, dict[str, str]]:
    """Parse outline to extract per-category strategy, selectivity, and highlight.

    Uses shared patterns from _get_extraction_patterns(). Strategy extraction
    prioritizes the FIRST subsection title, then expands to all subsections + body.
    """
    result: dict[str, dict[str, str]] = {}
    if not outline_text:
        return result

    strategy_pats, selectivity_pats, highlight_pats = _get_extraction_patterns()

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
        # Extract subsection titles (### headings or - N.N bullet format)
        subsec_lines = re.findall(r"^(?:###\s*(.+)|-\s*\d+\.\d+\s+(.+))$", section_body, re.MULTILINE)
        subsec_texts = [a or b for a, b in subsec_lines]
        first_subsec = subsec_texts[0].lower() if subsec_texts else ""
        all_subsec = " ".join(subsec_texts).lower()
        combined = all_subsec + " " + section_lower

        # Build the set of category-matching terms for consistency check
        cat_terms = set(_build_search_terms(cat))

        # Strategy: iterate subsections, prioritize ones that mention THIS category
        found = {}
        # Pass 1: find a subsection that mentions this category AND matches a strategy pattern
        for subsec_text in subsec_texts:
            subsec_lower = subsec_text.lower()
            # Check if this subsection mentions the current category
            mentions_cat = any(t in subsec_lower for t in cat_terms)
            if mentions_cat:
                for pat, label in strategy_pats:
                    if re.search(pat, subsec_lower):
                        found["strategy"] = label
                        break
                if "strategy" in found:
                    break
        # Pass 2: fall back to first subsection (original behavior)
        if "strategy" not in found:
            for pat, label in strategy_pats:
                if re.search(pat, first_subsec):
                    found["strategy"] = label
                    break
        # Pass 3: fall back to all subsections + body
        if "strategy" not in found:
            for pat, label in strategy_pats:
                if re.search(pat, combined):
                    found["strategy"] = label
                    break
        # Selectivity: try category-matching subsection first, then all
        if "selectivity" not in found:
            for subsec_text in subsec_texts:
                subsec_lower = subsec_text.lower()
                mentions_cat = any(t in subsec_lower for t in cat_terms)
                if mentions_cat:
                    for pat, label in selectivity_pats:
                        if re.search(pat, subsec_lower):
                            found["selectivity"] = label
                            break
                    if "selectivity" in found:
                        break
        if "selectivity" not in found:
            for pat, label in selectivity_pats:
                if re.search(pat, combined):
                    found["selectivity"] = label
                    break
        # Highlight: try category-matching subsection first, then all
        if "highlight" not in found:
            for subsec_text in subsec_texts:
                subsec_lower = subsec_text.lower()
                mentions_cat = any(t in subsec_lower for t in cat_terms)
                if mentions_cat:
                    for pat, label in highlight_pats:
                        if re.search(pat, subsec_lower):
                            found["highlight"] = label
                            break
                    if "highlight" in found:
                        break
        if "highlight" not in found:
            for pat, label in highlight_pats:
                if re.search(pat, combined):
                    found["highlight"] = label
                    break
        result[cat] = found
    return result


def _build_take_home_text(features: dict[str, Any]) -> str:
    """Build take-home messages dynamically."""
    time_window = features.get("time_window", "recent years")
    classification_rule = features.get("classification_rule", "")

    # Extract short keyword from classification rule
    class_short = "Classified"
    if classification_rule:
        parts = classification_rule.replace("By ", "").split()
        class_short = parts[0] if parts else "Classified"

    messages = [
        f"1. Timely overview ({time_window})",
        f"2. {class_short}-centered",
        "3. High enantioselectivity",
        "4. Diverse substrate scope",
        "5. Emerging sustainable methods",
        "6. Future directions",
    ]
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
) -> bytes:
    """Call image generation API. Supports both OpenAI-compatible and DashScope native format."""
    # Detect if this is an Alibaba Cloud / DashScope endpoint
    if "maas.aliyuncs.com" in base_url or "dashscope" in base_url:
        return _call_dashscope_native(api_key, base_url, reference_image, prompt, model)

    # OpenAI-compatible endpoints
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"

    # Strategy 1: Try /images/edits
    try:
        return _try_images_edits(base, api_key, reference_image, prompt, model)
    except Exception as edit_err:
        print(f"  /images/edits failed ({edit_err}), trying /images/generations...")

    # Strategy 2: Text-only /images/generations
    return _try_images_generations_text_only(base, api_key, prompt, model)


def _call_dashscope_native(
    api_key: str, base_url: str, reference_image: Path, prompt: str, model: str
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


def _try_images_edits(base: str, api_key: str, reference_image: Path, prompt: str, model: str) -> bytes:
    """Try the /images/edits endpoint."""
    url = f"{base}/images/edits"
    fields = {
        "model": model,
        "prompt": prompt,
        "size": "1536x1024",
        "quality": "high",
        "output_format": "png",
    }
    file_fields = [("image", reference_image)]
    content_type, body = build_multipart_form(fields, file_fields)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return _extract_image_bytes(result)


def _try_images_generations_text_only(base: str, api_key: str, prompt: str, model: str) -> bytes:
    """Try /images/generations with text-only prompt (no reference image)."""
    url = f"{base}/images/generations"
    payload = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1536x1024",
        "response_format": "b64_json",
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))
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
                         output_size: int = 0, error: str = "") -> dict:
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
        "model": model,
        "status": status,
    }
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

    # Load templates
    templates_path = review_root / "references" / "templates" / "overview_templates.json"
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
    reference_image = review_root / best_template["reference_image"]
    print(f"\n  Best match: template_{template_id} ({best_template['name']})")
    print(f"  Reference image: {reference_image}")

    if not reference_image.exists():
        print(f"ERROR: Reference image not found: {reference_image}", file=sys.stderr)
        sys.exit(1)

    # Build adapted prompt
    adapted_prompt = build_adapted_prompt(best_template, features)
    print(f"\n  Adapted prompt length: {len(adapted_prompt)} chars")

    if args.dry_run:
        print("\n[DRY RUN] Would generate figure with above settings.")
        print(f"  Template: {template_id}")
        print(f"  Reference: {reference_image}")
        print(f"  API: {base_url}/v1/images/edits")
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
    print(f"  Model: {args.model}")

    try:
        image_bytes = call_image_edit_api(api_key, base_url, reference_image, adapted_prompt, args.model)
    except Exception as exc:
        print(f"\nERROR: API call failed: {exc}", file=sys.stderr)
        out_dir = project_dir / "03_figure_redraw"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _build_report(args, best_template, features, adapted_prompt,
                               reference_image, base_url, args.model, status="api_error", error=str(exc))
        write_json(out_dir / "overview_template_match.json", report)
        sys.exit(3)

    # Save output
    out_dir = project_dir / "03_figure_redraw"
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else (out_dir / "overview_figure.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    print(f"\n  Overview figure saved to: {output_path}")
    print(f"  File size: {len(image_bytes):,} bytes")

    # Save match report
    report = _build_report(args, best_template, features, adapted_prompt,
                           reference_image, base_url, args.model,
                           status="success", output_path=str(output_path),
                           output_size=len(image_bytes))
    write_json(out_dir / "overview_template_match.json", report)
    print(f"  Match report saved to: {out_dir / 'overview_template_match.json'}")


if __name__ == "__main__":
    main()
