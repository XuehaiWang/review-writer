"""Request-local source selection; never a replacement for scientific verification."""
from copy import deepcopy
import hashlib
import json
import re

QUOTE_FORMAT = "quotes/1"
UNIT_FORMAT = "source-units/1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_source_units(sources, *, request_scope=""):
    """Use paragraph boundaries, never punctuation-based chemical sentence splitting.

    Table-containing chunks stay intact so headers and qualifications stay visible.
    Offsets refer to the exact displayed input, not an unseen full document.
    """
    scope = digest({"version": UNIT_FORMAT, "sources": sources, "request_scope": request_scope})[:20]
    mapping, displayed = {}, []
    for index, source in enumerate(sources, 1):
        text = str(source.get("content") or "")
        boundaries = [] if "|" in text else list(re.finditer(r"\n[ \t]*\n", text))
        start, ranges = 0, []
        for boundary in boundaries:
            ranges.append((start, boundary.start()))
            start = boundary.end()
        ranges.append((start, len(text)))
        units = []
        for begin, end in ranges:
            if not text[begin:end].strip():
                continue
            uid = f"U{scope}-{index}-{len(units) + 1}"
            mapping[uid] = {"evidence_key": source["evidence_key"], "quote": text[begin:end],
                            "start": begin, "end": end, "text_sha256": digest(text)}
            units.append({"unit_id": uid, "text": text[begin:end]})
        row = deepcopy(source)
        row.pop("content", None)
        row["source_units"] = units
        displayed.append(row)
    return displayed, mapping


def unit_schema(schema):
    """Version the wire format only; normalized downstream references stay unchanged."""
    result = deepcopy(schema)
    def walk(node):
        if isinstance(node, dict):
            properties = node.get("properties", {})
            if "support_spans" in properties:
                properties["support_spans"]["items"] = {
                    "type": "object", "properties": {"unit_id": {"type": "string"}},
                    "required": ["unit_id"], "additionalProperties": False}
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(result)
    return result


def resolve_unit_response(response, mapping):
    """Normalize selected IDs once. Mixed/unknown IDs stay invalid for bounded repair."""
    result = deepcopy(response)
    def walk(node):
        if isinstance(node, dict):
            spans = node.get("support_spans")
            if isinstance(spans, list):
                normalized = []
                for span in spans:
                    unit = mapping.get(span.get("unit_id")) if isinstance(span, dict) and isinstance(span.get("unit_id"), str) else None
                    if not unit or set(span) != {"unit_id"}:
                        normalized.append({"evidence_key": "", "quote": ""})
                    else:
                        normalized.append({"evidence_key": unit["evidence_key"], "quote": unit["quote"]})
                node["support_spans"] = normalized
            for key, value in node.items():
                if key != "support_spans":
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(result)
    return result


def evidence_uses(raw, allowed):
    """Optional navigation, not evidence. Invalid entries never trigger a paid retry."""
    valid, issues = [], []
    if raw is None:
        return valid, issues
    if not isinstance(raw, list):
        return valid, ["invalid_evidence_uses"]
    for item in raw:
        refs = item.get("source_refs") if isinstance(item, dict) else None
        purpose = item.get("purpose") if isinstance(item, dict) else None
        if (not isinstance(refs, list) or not refs or not isinstance(purpose, str) or not purpose.strip()
                or any(not isinstance(ref, str) or ref not in allowed for ref in refs)):
            issues.append("invalid_evidence_use")
            continue
        entry = {"source_refs": list(dict.fromkeys(refs)), "purpose": purpose.strip()}
        if entry not in valid:
            valid.append(entry)
    return valid, issues
