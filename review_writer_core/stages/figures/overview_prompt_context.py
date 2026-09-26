"""Small, source-addressable projections for Overview text calls.

The full contracts remain in the artifact and in local validation. Never send
passage payloads or the entire argument graph just to label a figure.
"""
from __future__ import annotations

import json


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def overview_prompt_context(features, *, claim_budget=32000):
    contract = features.get("overview_content_contract") or {}
    bindings = features.get("overview_evidence_bindings") or contract.get("evidence_bindings") or {}
    sections = (features.get("argument_execution") or {}).get("sections") or []
    wanted = {str(row.get("section_id")) for row in bindings.values() if isinstance(row, dict)}
    selected_sections = [s for s in sections if not wanted or str(s.get("section_id")) in wanted]
    # Give every represented section a share; an early, verbose section must
    # not consume the whole budget. Keep claims whole, including qualifications.
    per_section = claim_budget // max(1, len(selected_sections))
    claims = []
    for section in selected_sections:
        used, seen = 0, set()
        candidates = sorted(section.get("claims") or [],
                            key=lambda c: c.get("binding_level") != "source_passage")
        for claim in candidates:
            text = " ".join(str(claim.get("claim") or "").split())
            cid = str(claim.get("claim_id") or "")
            if not text or not cid or text in seen:
                continue
            row = {"section_id": section.get("section_id"), "claim_id": cid,
                   "claim": text, "paper_ids": claim.get("paper_ids") or [],
                   "binding_level": claim.get("binding_level") or ""}
            size = len(compact_json(row)) + 1
            if used + size > per_section:
                continue
            claims.append(row)
            seen.add(text)
            used += size
    structure = features.get("overview_structure_contract") or {}
    return {
        "contract": {key: contract[key] for key in (
            "title", "primary_axis", "modules", "performance_label_policy"
        ) if key in contract},
        "module_sections": {label: row.get("section_id") for label, row in bindings.items()
                            if isinstance(row, dict)},
        "claims": claims,
        # Preserve the exact confirmed structure, role, status and constraints;
        # bulky source passages remain available to validation, not the prompt.
        "structure": {key: structure[key] for key in (
            "schema_version", "status", "role", "smiles", "required_smarts",
            "fact_id", "reason", "evidence_sources"
        ) if key in structure},
    }
