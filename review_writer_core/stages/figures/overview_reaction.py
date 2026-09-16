"""Evidence-aware reaction presentation helpers for overview figures."""
from __future__ import annotations

import json
from typing import Any, Mapping


REACTION_PRESENTATION_SCHEMA_VERSION = 1
EXACT_REACTION_MODE = "reaction"
GENERIC_REACTION_MODE = "reaction_generic"


def _strict_true(value: Any) -> bool:
    return value is True


def is_reaction_mode(mode: Any) -> bool:
    return str(mode or "") in {EXACT_REACTION_MODE, GENERIC_REACTION_MODE}


def reaction_evidence_excerpt(
    content_contract: Mapping[str, Any] | None,
    *,
    limit: int = 3500,
) -> str:
    """Return compact source-bound claims when the project provides them."""

    if not isinstance(content_contract, Mapping):
        return ""
    rows = content_contract.get("source_claim_bindings")
    if not isinstance(rows, list):
        return ""
    compact: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        claim = " ".join(str(raw.get("claim") or "").split()).strip()
        if not claim:
            continue
        compact.append(
            {
                "claim_id": str(raw.get("claim_id") or ""),
                "claim": claim,
                "paper_ids": [str(value) for value in raw.get("paper_ids") or []][:4],
                "evidence_refs": list(raw.get("evidence_refs") or [])[:4],
                "result_context": list(raw.get("result_context") or [])[:4],
            }
        )
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > limit:
            compact.pop()
            break
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":")) if compact else ""


def normalize_reaction_review(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize field-level review output and the legacy single boolean."""

    data = payload if isinstance(payload, Mapping) else {}
    legacy_supported = _strict_true(data.get("supported"))
    try:
        confidence = max(0, min(100, int(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return {
        "connectivity_supported": _strict_true(data.get("connectivity_supported", legacy_supported)),
        "product_supported": _strict_true(data.get("product_supported", legacy_supported)),
        "conditions_supported": _strict_true(data.get("conditions_supported", legacy_supported)),
        "confidence": confidence,
        "reason": " ".join(str(data.get("reason") or "").split())[:240],
    }


def choose_reaction_presentation(
    review: Mapping[str, Any] | None,
    *,
    exact_minimum: int = 70,
    generic_minimum: int = 45,
    skeleton_minimum: int = 40,
) -> dict[str, Any]:
    """Choose exact reaction, generic reaction, skeleton, or concept."""

    if not isinstance(review, Mapping):
        raise ValueError("Evidence review unavailable; do not publish a chemistry fallback.")
    normalized = normalize_reaction_review(review)
    confidence = normalized["confidence"]
    if (normalized["connectivity_supported"] and normalized["product_supported"]
            and normalized["conditions_supported"] and confidence >= exact_minimum):
        mode = EXACT_REACTION_MODE
        reason = normalized["reason"] or "Reaction connectivity is supported by bound evidence."
    elif normalized["connectivity_supported"] and normalized["product_supported"] and confidence >= generic_minimum:
        mode = GENERIC_REACTION_MODE
        reason = normalized["reason"] or "Generic transformation is supported; details remain incomplete."
    elif normalized["product_supported"] and confidence >= skeleton_minimum:
        mode = "skeleton"
        reason = normalized["reason"] or "Only the product motif is sufficiently supported."
    else:
        mode = "concept"
        reason = normalized["reason"] or "No sufficiently supported chemical relationship was found."
    return {
        **normalized,
        "mode": mode,
        "reason": reason,
        "reviewed_by": "rules+ai",
        "review_schema_version": REACTION_PRESENTATION_SCHEMA_VERSION,
    }


def scheme_for_presentation(
    scheme: Mapping[str, Any], decision: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove unsupported conditions from a generic reaction display."""

    output: dict[str, Any] = {
        key: str(scheme.get(key) or "")
        for key in (
            "substrate_smiles",
            "substrate_name",
            "product_smiles",
            "product_name",
            "catalyst_label",
            "reaction_name",
        )
    }
    if isinstance(scheme.get("r_group_mapping"), Mapping):
        output["r_group_mapping"] = dict(scheme["r_group_mapping"])
    if str(decision.get("mode") or "") == GENERIC_REACTION_MODE:
        if not _strict_true(decision.get("conditions_supported")):
            output["catalyst_label"] = ""
        if not output["reaction_name"]:
            output["reaction_name"] = "Representative transformation"
    return output


def map_reaction_r_groups(
    substrate_smiles: str, product_smiles: str
) -> tuple[str, str, dict[str, Any]]:
    """Assign stable wildcard atom-map numbers across both reaction sides."""

    try:
        from rdkit import Chem
    except ImportError:
        return substrate_smiles, product_smiles, {
            "status": "unavailable",
            "reason": "rdkit_missing",
        }
    substrate = Chem.MolFromSmiles(str(substrate_smiles or ""))
    product = Chem.MolFromSmiles(str(product_smiles or ""))
    if substrate is None or product is None:
        return substrate_smiles, product_smiles, {
            "status": "unavailable",
            "reason": "invalid_smiles",
        }

    product_atoms = [atom for atom in product.GetAtoms() if atom.GetAtomicNum() == 0]
    substrate_atoms = [atom for atom in substrate.GetAtoms() if atom.GetAtomicNum() == 0]

    def deduplicate_within_side(atoms: list[Any]) -> None:
        seen: set[int] = set()
        for atom in atoms:
            number = atom.GetAtomMapNum()
            if number > 0 and number in seen:
                atom.SetAtomMapNum(0)
            elif number > 0:
                seen.add(number)

    deduplicate_within_side(product_atoms)
    deduplicate_within_side(substrate_atoms)
    used = {
        atom.GetAtomMapNum()
        for atom in product_atoms + substrate_atoms
        if atom.GetAtomMapNum() > 0
    }

    def next_map() -> int:
        candidate = 1
        while candidate in used:
            candidate += 1
        used.add(candidate)
        return candidate

    for atom in product_atoms:
        if atom.GetAtomMapNum() <= 0:
            atom.SetAtomMapNum(next_map())
    product_maps = [atom.GetAtomMapNum() for atom in product_atoms]
    substrate_used = {
        atom.GetAtomMapNum()
        for atom in substrate_atoms
        if atom.GetAtomMapNum() > 0
    }
    reusable = [number for number in product_maps if number not in substrate_used]
    for atom in substrate_atoms:
        if atom.GetAtomMapNum() <= 0:
            atom.SetAtomMapNum(reusable.pop(0) if reusable else next_map())

    mapped_substrate = Chem.MolToSmiles(substrate, canonical=False, isomericSmiles=True)
    mapped_product = Chem.MolToSmiles(product, canonical=False, isomericSmiles=True)
    return mapped_substrate, mapped_product, {
        "status": "mapped",
        "method": "explicit_then_ordinal_wildcards",
        "substrate_r_groups": len(substrate_atoms),
        "product_r_groups": len(product_atoms),
        "product_map_numbers": product_maps,
    }
