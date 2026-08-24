#!/usr/bin/env python3
"""Generate source-grounded review sections from Blueprint tasks and MinerU Markdown."""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
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
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_TEXT_MODEL,
    openai_endpoint as _shared_openai_endpoint,
    resolve_api_key as _shared_resolve_api_key,
)
from review_writer_core.text_safety import make_xml_compatible  # noqa: E402
from review_writer_core.model_gateway_client import (  # noqa: E402
    call_json_model as call_gateway_json,
    gateway_configured,
    parse_json_object_text as _parse_json_object_text,
)


def write_generation_progress(
    stage: Path,
    *,
    current: int,
    total: int,
    phase: str,
    current_section_id: str = "",
    current_heading: str = "",
    completed_sections: list[dict[str, str]] | None = None,
    failed_sections: list[dict[str, str]] | None = None,
    evidence_hit_count: int = 0,
    evidence_paper_count: int = 0,
) -> None:
    """Atomically expose chapter-level progress to the parent JobService."""

    destination = stage / "generation_progress.json"
    temporary = destination.with_name(f".{destination.name}.tmp")
    payload = {
        "schema_version": 1,
        "phase": str(phase),
        "current": max(0, int(current)),
        "total": max(0, int(total)),
        "current_section_id": str(current_section_id or ""),
        "current_heading": str(current_heading or ""),
        "completed_sections": list(completed_sections or []),
        "failed_sections": list(failed_sections or []),
        "evidence_hit_count": max(0, int(evidence_hit_count)),
        "evidence_paper_count": max(0, int(evidence_paper_count)),
        "updated_at_epoch": time.time(),
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_section_checkpoint(stage: Path, payload: dict[str, Any]) -> None:
    """Persist completed section objects so a retried job can resume safely."""

    destination = stage / "section_checkpoints.json"
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def openai_endpoint(base_url: str, endpoint: str) -> str:
    """Accept OpenAI-compatible base URLs with or without a trailing /v1."""
    return _shared_openai_endpoint(base_url, endpoint)


TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


def open_json_response(request: urllib.request.Request, *, label: str, timeout: int = 300) -> dict[str, Any]:
    """Open an API request with bounded transient retries and useful JSON errors."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
                raw = response.read()
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
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500].replace("\r", " ").replace("\n", " ")
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(f"{label} failed with HTTP {exc.code}: {body or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport failed: {exc}") from exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{label} failed after retries")


def resolve_api_key(cli_value: str, base_url: str, dotenv: dict[str, str] | None = None) -> str:
    del base_url
    return _shared_resolve_api_key(
        cli_value,
        env_names=(
            "REVIEW_WRITING_API_KEY",
            "OPENAI_API_KEY",
        ),
        dotenv=dotenv,
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dotenv(root: Path) -> dict[str, str]:
    path = root / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        # Match conventional dotenv behavior inside one file: the last
        # occurrence wins. Keep the values local so a dashboard process that
        # loaded an older duplicate cannot silently override this stage.
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_blueprint_rule_pack(_review_root: Path, blueprint: dict[str, Any]) -> str:
    """Load application-owned Blueprint rules, never user-workspace content."""
    # ``review_root`` points at per-user project storage in hosted mode.  Rule
    # packs are immutable application resources and live beside this script,
    # under the bootstrap root discovered above.
    skill_root = (_BOOTSTRAP_ROOT / "skills" / "review-section-blueprint").resolve()
    relative = str(
        blueprint.get("rule_pack_path")
        or "references/rule_packs/general"
    ).strip()
    candidate = (skill_root / relative).resolve()
    try:
        candidate.relative_to(skill_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Blueprint rule_pack_path escapes the blueprint skill: {relative}"
        ) from exc
    if not candidate.is_dir():
        fallback = skill_root / "references" / "rule_packs" / "general"
        if str(blueprint.get("rule_pack") or "") not in {"", "general"}:
            raise RuntimeError(f"Blueprint rule pack does not exist: {candidate}")
        candidate = fallback
    files = sorted(candidate.glob("*.md"))
    if not files:
        raise RuntimeError(f"Blueprint rule pack contains no Markdown rules: {candidate}")
    chunks: list[str] = []
    remaining = 14000
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        chunk = f"\n\n<!-- rule source: {path.name} -->\n{text}"[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "".join(chunks).strip()


def value(item: Any) -> Any:
    return item.get("value") if isinstance(item, dict) and "value" in item else item


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def paper_evidence(root: Path, rows: dict[str, dict[str, Any]], paper_id: str) -> dict[str, str]:
    row = rows.get(paper_id, {})
    metadata_path = root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    metadata = read_json(metadata_path) if metadata_path.exists() else {}
    sources = metadata.get("source_paths") if isinstance(metadata, dict) else {}
    markdown_path = Path(str((sources or {}).get("markdown") or ""))
    markdown = clean_markdown(markdown_path.read_text(encoding="utf-8", errors="ignore")) if markdown_path.exists() else ""
    fallback = clean_markdown(str(row.get("main_content") or row.get("abstract") or ""))
    return {
        "paper_id": paper_id,
        "title": str(row.get("title") or value(metadata.get("title")) or paper_id),
        "year": str(row.get("year") or value(metadata.get("year")) or ""),
        "evidence": (markdown or fallback)[:9000],
    }


def parse_json_object(text: Any, *, required_list: str = "") -> dict[str, Any]:
    parsed = _parse_json_object_text(
        str(text or ""),
        required_list=required_list,
        context="Section-writing model",
    )
    parsed = repair_model_unicode(parsed)
    return parsed


_TRUNCATED_GREEK_ESCAPE_RE = re.compile(r"\x03([0-9a-fA-F]{2})")
_KNOWN_TRUNCATED_UNICODE = {
    "\x02": "\u2032",  # U+2032 PRIME, seen in SN2\u2032
    "\x13": "\u2013",  # U+2013 EN DASH
    "\x14": "\u2014",  # U+2014 EM DASH
}


def repair_model_unicode(value: Any) -> Any:
    """Recover relay-truncated Unicode and reject no XML-incompatible text.

    Some OpenAI-compatible relays have returned ``\\u03b1`` as
    ``\\u0003b1`` and ``\\u2014`` as ``\\u0014`` inside an otherwise valid
    JSON response.  ``json.loads`` correctly decodes those malformed escapes,
    leaving control characters that later make a DOCX XML part invalid.
    """
    if isinstance(value, dict):
        return {str(key): repair_model_unicode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [repair_model_unicode(item) for item in value]
    if not isinstance(value, str):
        return value
    repaired = re.sub(r"(?<=C)\x03(?=C)", "\u2013", value)
    repaired = _TRUNCATED_GREEK_ESCAPE_RE.sub(
        lambda match: chr(int("03" + match.group(1), 16)),
        repaired,
    )
    repaired = "".join(_KNOWN_TRUNCATED_UNICODE.get(char, char) for char in repaired)
    return make_xml_compatible(repaired)[0]


def call_structured_llm(
    prompt: str,
    schema: dict[str, Any],
    api_key: str,
    base_url: str,
    model: str,
    wire_api: str = "responses",
    *,
    label: str,
    schema_name: str,
    required_list: str = "",
) -> dict[str, Any]:
    schema_prompt = (
        f"{prompt}\n\nReturn only one JSON object matching this JSON Schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    if gateway_configured():
        return repair_model_unicode(
            call_gateway_json(
                schema_prompt,
                label=label,
                required_list=required_list,
            )
        )
    wire = str(wire_api or "responses").strip().lower().replace("_", "-")
    if wire in {"chat", "chat-completion", "chat-completions"}:
        endpoint = openai_endpoint(base_url, "chat/completions")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": schema_prompt}],
            "response_format": {"type": "json_object"},
        }
    else:
        endpoint = openai_endpoint(base_url, "responses")
        payload = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
        }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # The configured relay rejects Python's default user agent.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    )
    data = open_json_response(request, label=label)
    if wire in {"chat", "chat-completion", "chat-completions"}:
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        text = message.get("content") if isinstance(message, dict) else ""
        if isinstance(text, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in text
                if isinstance(part, dict)
            )
    else:
        text = data.get("output_text") or ""
        if not text:
            text = "\n".join(
                content.get("text", "")
                for output in data.get("output", []) for content in output.get("content", [])
                if content.get("type") in {"output_text", "text"}
            )
    return parse_json_object(text, required_list=required_list)


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview_intent", "synthesis_summary", "components", "paragraphs"],
    "properties": {
        "overview_intent": {"type": "string"},
        "synthesis_summary": {"type": "string"},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["component_type", "purpose", "summary", "evidence_keys"],
                "properties": {
                    "component_type": {"type": "string"},
                    "purpose": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence_keys": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "paragraphs": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "theme", "argument_role", "objective", "reader_takeaway",
                    "positive_synthesis", "paper_ids", "claims"
                ],
                "properties": {
                    "theme": {"type": "string"},
                    "argument_role": {"type": "string"},
                    "objective": {"type": "string"},
                    "reader_takeaway": {"type": "string"},
                    "positive_synthesis": {"type": "string"},
                    "paper_ids": {"type": "array", "items": {"type": "string"}},
                    "claims": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "claim", "claim_kind", "synthesis_subtype",
                                "epistemic_status", "support_status",
                                "citation_group", "evidence_keys", "evidence_ceiling"
                            ],
                            "properties": {
                                "claim": {"type": "string"},
                                "claim_kind": {"type": "string"},
                                "synthesis_subtype": {"type": "string"},
                                "epistemic_status": {"type": "string"},
                                "support_status": {"type": "string"},
                                "citation_group": {"type": "array", "items": {"type": "string"}},
                                "evidence_keys": {"type": "array", "items": {"type": "string"}},
                                "evidence_ceiling": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}


WRITER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "paragraphs"],
    "properties": {
        "overview": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["paragraph_id", "claim_realizations"],
                "properties": {
                    "paragraph_id": {"type": "string"},
                    "claim_realizations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["claim_id", "text"],
                            "properties": {
                                "claim_id": {"type": "string"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}


CLAIM_KINDS = {
    "reported_finding",
    "reported_method",
    "cross_study_comparison",
    "mechanism_interpretation",
    "historical_transition",
    "review_synthesis",
    "future_direction",
}
EPISTEMIC_STATUSES = {
    "direct_source_report",
    "source_author_interpretation",
    "cross_source_inference",
    "review_hypothesis",
}
SUPPORT_STATUSES = {"supported", "partially_supported", "blocked"}
ARGUMENT_ROLES = {
    "definition", "foundation", "mechanism", "comparison", "extension",
    "limitation", "synthesis", "transition", "reported_evidence",
}


def compact_text(value: Any, *, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def build_matrix_comparison_table(
    section_id: str,
    paper_ids: list[str],
    rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Align source-addressable Matrix facts without inventing empty cells."""

    cells: list[dict[str, Any]] = []
    fields: list[str] = []
    for paper_id in paper_ids:
        for fact in rows.get(paper_id, {}).get("scientific_facts") or []:
            if not isinstance(fact, dict):
                continue
            field_id = str(fact.get("field_id") or "")
            refs = [
                ref
                for ref in fact.get("evidence_refs") or []
                if isinstance(ref, dict) and ref.get("evidence_key")
            ]
            if not field_id or not refs or not str(fact.get("value") or "").strip():
                continue
            if field_id not in fields:
                fields.append(field_id)
            cells.append(
                {
                    "paper_id": paper_id,
                    "field_id": field_id,
                    "value": compact_text(fact.get("value"), limit=1800),
                    "epistemic_status": fact.get("epistemic_status"),
                    "confidence": fact.get("confidence"),
                    "evidence_refs": refs,
                    "evidence_ceiling": fact.get("evidence_ceiling"),
                }
            )
    counts = {
        field_id: len(
            {cell["paper_id"] for cell in cells if cell["field_id"] == field_id}
        )
        for field_id in fields
    }
    return {
        "section_id": section_id,
        "paper_ids": paper_ids,
        "fields": fields,
        "comparable_fields": [field for field in fields if counts[field] >= 2],
        "single_source_fields": [field for field in fields if counts[field] == 1],
        "missing_cells": [
            {"paper_id": paper_id, "field_id": field_id, "status": "unresolved"}
            for field_id in fields
            for paper_id in paper_ids
            if not any(
                cell["paper_id"] == paper_id and cell["field_id"] == field_id
                for cell in cells
            )
        ],
        "cells": cells,
    }


def normalize_section_plan(
    *,
    section_id: str,
    role: str,
    primary: list[str],
    supporting: list[str],
    allowed: list[str],
    evidence: list[dict[str, Any]],
    retrieval_mode: str,
    generated: dict[str, Any],
    synthesis_requirements: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a model proposal into a deterministic evidence-bound contract."""

    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_key") or "")
        and bool(item.get("claim_eligible", True))
    }
    evidence_paper_by_key = {
        key: str(item.get("paper_id") or "")
        for key, item in evidence_by_key.items()
    }
    requirement_by_type = {
        str(item.get("component") or "").strip(): item
        for item in synthesis_requirements
        if isinstance(item, dict) and str(item.get("component") or "").strip()
    }
    components: list[dict[str, Any]] = []
    used_component_types: set[str] = set()
    for index, raw in enumerate(generated.get("components") or [], start=1):
        if not isinstance(raw, dict):
            continue
        component_type = compact_text(raw.get("component_type"), limit=80).casefold()
        if not component_type or component_type in used_component_types:
            continue
        if requirement_by_type and component_type not in requirement_by_type:
            continue
        keys = list(
            dict.fromkeys(
                str(key)
                for key in raw.get("evidence_keys") or []
                if str(key) in evidence_by_key
            )
        )
        if retrieval_mode == "lexical" and not keys:
            continue
        requirement = requirement_by_type.get(component_type, {})
        component_id = f"{section_id}-{component_type}-{index:02d}"
        components.append(
            {
                "component_id": component_id,
                "component_type": component_type,
                "necessity": str(requirement.get("necessity") or "recommended"),
                "purpose": compact_text(raw.get("purpose") or requirement.get("reason")),
                "status": "supported" if keys else "source_bounded_fallback",
                "summary": compact_text(raw.get("summary")),
                "evidence_keys": keys,
                "provenance": "evidence_first_planner",
            }
        )
        used_component_types.add(component_type)
    for component_type, requirement in requirement_by_type.items():
        if component_type in used_component_types:
            continue
        components.append(
            {
                "component_id": f"{section_id}-{component_type}-{len(components) + 1:02d}",
                "component_type": component_type,
                "necessity": str(requirement.get("necessity") or "recommended"),
                "purpose": compact_text(requirement.get("reason")),
                "status": "insufficient_evidence",
                "summary": "",
                "evidence_keys": [],
                "provenance": "deterministic_requirement_adapter",
            }
        )

    paragraph_plans: list[dict[str, Any]] = []
    claim_plans: list[dict[str, Any]] = []
    covered_primary: set[str] = set()
    for paragraph_index, raw_paragraph in enumerate(
        (generated.get("paragraphs") or [])[:8], start=1
    ):
        if not isinstance(raw_paragraph, dict):
            continue
        paragraph_id = f"{section_id}-p{paragraph_index}"
        paragraph_claim_ids: list[str] = []
        paragraph_papers: list[str] = []
        for claim_index, raw_claim in enumerate(
            (raw_paragraph.get("claims") or [])[:8], start=1
        ):
            if not isinstance(raw_claim, dict):
                continue
            support_status = compact_text(
                raw_claim.get("support_status"), limit=40
            ).casefold()
            if support_status not in SUPPORT_STATUSES:
                support_status = "partially_supported"
            # A blocked proposal is diagnostic input, not publishable content.
            if support_status == "blocked":
                continue
            keys = list(
                dict.fromkeys(
                    str(key)
                    for key in raw_claim.get("evidence_keys") or []
                    if str(key) in evidence_by_key
                )
            )
            key_papers = list(
                dict.fromkeys(
                    evidence_paper_by_key[key]
                    for key in keys
                    if evidence_paper_by_key.get(key) in allowed
                )
            )
            proposed_group = list(
                dict.fromkeys(
                    str(paper_id)
                    for paper_id in raw_claim.get("citation_group") or []
                    if str(paper_id) in allowed
                )
            )
            citation_group = key_papers if retrieval_mode == "lexical" else proposed_group
            if retrieval_mode == "lexical" and (not keys or not citation_group):
                continue
            if retrieval_mode != "lexical" and not citation_group:
                citation_group = [
                    str(item)
                    for item in raw_paragraph.get("paper_ids") or []
                    if str(item) in allowed
                ][:2]
            if not citation_group:
                continue
            claim_text = compact_text(raw_claim.get("claim"))
            if not claim_text:
                continue
            claim_kind = compact_text(raw_claim.get("claim_kind"), limit=80).casefold()
            if claim_kind not in CLAIM_KINDS:
                claim_kind = "reported_finding"
            epistemic_status = compact_text(
                raw_claim.get("epistemic_status"), limit=80
            ).casefold()
            if epistemic_status not in EPISTEMIC_STATUSES:
                epistemic_status = "direct_source_report"
            if retrieval_mode != "lexical":
                support_status = "partially_supported"
            claim_id = f"{paragraph_id}-C{claim_index:02d}"
            evidence_refs = [
                {
                    "evidence_id": evidence_by_key[key].get("evidence_id"),
                    "evidence_key": key,
                    "relationship": "supports",
                }
                for key in keys
            ]
            claim_plans.append(
                {
                    "claim_id": claim_id,
                    "paragraph_id": paragraph_id,
                    "sequence": len(paragraph_claim_ids) + 1,
                    "claim": claim_text,
                    "claim_kind": claim_kind,
                    "synthesis_subtype": compact_text(
                        raw_claim.get("synthesis_subtype"), limit=80
                    ),
                    "epistemic_status": epistemic_status,
                    "support_status": support_status,
                    "citation_group": citation_group,
                    "evidence_refs": evidence_refs,
                    "evidence_ceiling": compact_text(
                        raw_claim.get("evidence_ceiling")
                        or "Do not generalize beyond the cited source evidence."
                    ),
                    "semantic_constraints": [
                        "Do not introduce uncited quantitative, causal, or mechanistic detail.",
                        "Preserve source attribution and the declared evidence ceiling.",
                    ],
                }
            )
            paragraph_claim_ids.append(claim_id)
            paragraph_papers.extend(citation_group)
            covered_primary.update(set(citation_group) & set(primary))
        if not paragraph_claim_ids:
            continue
        argument_role = compact_text(
            raw_paragraph.get("argument_role"), limit=80
        ).casefold()
        if argument_role not in ARGUMENT_ROLES:
            argument_role = "synthesis" if len(set(paragraph_papers)) > 1 else "reported_evidence"
        knowledge_refs = [
            f"synthesis_state:{component['component_type']}:{component['component_id']}"
            for component in components
            if component.get("status") == "supported"
        ]
        paragraph_plans.append(
            {
                "paragraph_id": paragraph_id,
                "theme": compact_text(raw_paragraph.get("theme")),
                "argument_role": argument_role,
                "objective": compact_text(raw_paragraph.get("objective")),
                "target_words": {"min": 120, "max": 300},
                "primary_papers": [
                    paper_id for paper_id in dict.fromkeys(paragraph_papers)
                    if paper_id in primary
                ],
                "supporting_papers": [
                    paper_id for paper_id in dict.fromkeys(paragraph_papers)
                    if paper_id in supporting
                ],
                "opening_function": "Advance from the preceding analytical question.",
                "closing_function": "State the evidence boundary and next implication.",
                "reader_takeaway": compact_text(raw_paragraph.get("reader_takeaway")),
                "positive_synthesis": compact_text(raw_paragraph.get("positive_synthesis")),
                "caveat_policy": "diagnostic_only",
                "knowledge_component_refs": knowledge_refs,
                "claim_ids": paragraph_claim_ids,
            }
        )
    if not paragraph_plans:
        raise RuntimeError(f"The academic planner produced no supported paragraph for {section_id}.")
    missing_primary = [paper_id for paper_id in primary if paper_id not in covered_primary]
    if missing_primary:
        raise RuntimeError(
            f"The academic planner did not route every writeable primary paper into a supported Claim for {section_id}: "
            + ", ".join(missing_primary)
        )
    synthesis_section = {
        "section_id": section_id,
        "summary": compact_text(generated.get("synthesis_summary")),
        "components": components,
    }
    writing_section = {
        "section_id": section_id,
        "section_role": role,
        "route": "A" if len(paragraph_plans) == 1 else "B",
        "overview_intent": compact_text(generated.get("overview_intent")),
        "paragraphs": paragraph_plans,
        "claims": claim_plans,
    }
    return synthesis_section, writing_section


def prior_body_synthesis_context(
    section_specs: dict[str, dict[str, Any]],
    synthesis_sections: list[dict[str, Any]],
    writing_sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Build the evidence-bound body conclusions consumed by a conclusion.

    This deliberately transfers validated plans and evidence identities rather
    than draft prose, preventing the conclusion from inventing a second,
    disconnected interpretation of the corpus.
    """

    synthesis_by_id = {
        str(item.get("section_id") or ""): item
        for item in synthesis_sections
        if isinstance(item, dict) and str(item.get("section_id") or "")
    }
    context: list[dict[str, Any]] = []
    evidence_keys: set[str] = set()
    for writing in writing_sections:
        if not isinstance(writing, dict):
            continue
        section_id = str(writing.get("section_id") or "")
        spec = section_specs.get(section_id) or {}
        if str(spec.get("section_role") or "body").casefold() != "body":
            continue
        synthesis = synthesis_by_id.get(section_id) or {}
        claims: list[dict[str, Any]] = []
        for claim in writing.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            refs = [
                {
                    "evidence_key": str(ref.get("evidence_key") or ""),
                    "relationship": str(ref.get("relationship") or "supports"),
                }
                for ref in claim.get("evidence_refs") or []
                if isinstance(ref, dict) and str(ref.get("evidence_key") or "")
            ]
            evidence_keys.update(ref["evidence_key"] for ref in refs)
            claims.append(
                {
                    "claim": compact_text(claim.get("claim")),
                    "claim_kind": str(claim.get("claim_kind") or ""),
                    "epistemic_status": str(claim.get("epistemic_status") or ""),
                    "support_status": str(claim.get("support_status") or ""),
                    "citation_group": list(claim.get("citation_group") or []),
                    "evidence_refs": refs,
                    "evidence_ceiling": compact_text(claim.get("evidence_ceiling")),
                }
            )
        context.append(
            {
                "section_id": section_id,
                "title": str(spec.get("title") or section_id),
                "section_thesis": compact_text(spec.get("section_thesis")),
                "validated_synthesis_summary": compact_text(synthesis.get("summary")),
                "validated_claims": claims,
            }
        )
    return context, evidence_keys


def validate_and_realize_section(
    *,
    section_id: str,
    generated: dict[str, Any],
    writing_section: dict[str, Any],
    evidence: list[dict[str, Any]],
    citation_map: dict[str, int],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    plans = {
        str(item.get("paragraph_id") or ""): item
        for item in writing_section.get("paragraphs") or []
        if isinstance(item, dict)
    }
    claims = {
        str(item.get("claim_id") or ""): item
        for item in writing_section.get("claims") or []
        if isinstance(item, dict)
    }
    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_key") or "")
        and bool(item.get("claim_eligible", True))
    }
    raw_paragraphs = generated.get("paragraphs") or []
    realized_by_id = {
        str(item.get("paragraph_id") or ""): item
        for item in raw_paragraphs
        if isinstance(item, dict)
    }
    if set(realized_by_id) != set(plans):
        raise RuntimeError(
            f"The section writer changed the Paragraph Plan for {section_id}."
        )
    paragraphs: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    all_realized_claims: set[str] = set()
    for paragraph_id, paragraph_plan in plans.items():
        raw = realized_by_id[paragraph_id]
        realization_rows = [
            item for item in raw.get("claim_realizations") or []
            if isinstance(item, dict)
        ]
        expected_claims = list(paragraph_plan.get("claim_ids") or [])
        realized_claims = [str(item.get("claim_id") or "") for item in realization_rows]
        if realized_claims != expected_claims or len(set(realized_claims)) != len(realized_claims):
            raise RuntimeError(
                f"The section writer did not realize the current Claim Plan for {paragraph_id}."
            )
        realized_parts: list[str] = []
        claim_realizations: list[dict[str, Any]] = []
        paragraph_evidence: list[dict[str, Any]] = []
        paragraph_papers: list[str] = []
        for realization in realization_rows:
            claim_id = str(realization.get("claim_id") or "")
            claim_plan = claims.get(claim_id)
            if claim_plan is None or claim_plan.get("support_status") == "blocked":
                raise RuntimeError(f"The section writer referenced an unavailable Claim: {claim_id}.")
            sentence = compact_text(realization.get("text"), limit=2500)
            if not sentence:
                raise RuntimeError(f"The section writer returned an empty Claim realization: {claim_id}.")
            cited = [
                paper_id for paper_id in claim_plan.get("citation_group") or []
                if paper_id in citation_map
            ]
            if not cited:
                raise RuntimeError(f"Claim {claim_id} has no resolvable citation group.")
            callout = f"[{', '.join(str(citation_map[paper_id]) for paper_id in cited)}]"
            realized_parts.append(f"{sentence} {callout}")
            paragraph_papers.extend(cited)
            refs = [
                ref for ref in claim_plan.get("evidence_refs") or []
                if isinstance(ref, dict) and str(ref.get("evidence_key") or "") in evidence_by_key
            ]
            for paper_id in cited:
                chunks = list(
                    dict.fromkeys(
                        str(evidence_by_key[str(ref["evidence_key"])].get("chunk_id") or "")
                        for ref in refs
                        if str(evidence_by_key[str(ref["evidence_key"])].get("paper_id") or "") == paper_id
                        and str(evidence_by_key[str(ref["evidence_key"])].get("chunk_id") or "")
                    )
                )
                if chunks:
                    paragraph_evidence.append(
                        {
                            "paper_id": paper_id,
                            "chunk_ids": chunks,
                            "claim": claim_plan.get("claim"),
                            "claim_id": claim_id,
                        }
                    )
            claim_realizations.append(
                {
                    "claim_id": claim_id,
                    "text": sentence,
                    "citation_group": cited,
                    "evidence_refs": refs,
                }
            )
            all_realized_claims.add(claim_id)
        paragraph_text = " ".join(realized_parts)
        paragraphs.append(
            {
                "paragraph_id": paragraph_id,
                "paper_id": next(iter(dict.fromkeys(paragraph_papers)), ""),
                "cited_paper_ids": list(dict.fromkeys(paragraph_papers)),
                "text": paragraph_text,
                "evidence": paragraph_evidence,
                "claim_realizations": claim_realizations,
            }
        )
        validations.append(
            {
                "rule_id": "section.claim_plan_realization",
                "target_id": paragraph_id,
                "status": "pass",
                "claim_ids": expected_claims,
            }
        )
    if all_realized_claims != set(claims):
        raise RuntimeError(f"The section writer omitted planned Claims for {section_id}.")
    defensive_phrases = (
        "does not support", "not be interpreted as", "remains incomplete",
        "do not justify", "should not be used to",
    )
    defensive_count = sum(
        " ".join(item["text"] for item in paragraphs).casefold().count(phrase)
        for phrase in defensive_phrases
    )
    issues: list[dict[str, Any]] = []
    if defensive_count > max(1, len(paragraphs)):
        issues.append(
            {
                "type": "defensive_writing_repetition",
                "severity": "warning",
                "reason": "Repeated defensive templates may obscure the section's positive synthesis.",
            }
        )
    reviews = [
        {
            "iteration": 1,
            "decision": "PASS" if not issues else "PASS_WITH_WARNINGS",
            "target_ids": [section_id],
            "issues": issues,
            "preserve": ["validated Claim/Citation identities", "source evidence boundaries"],
            "repair_objective": "" if not issues else "Prefer a positive synthesis before necessary caveats.",
            "reviewer": "deterministic_evidence_review_v1",
        }
    ]
    overview = compact_text(generated.get("overview"), limit=3000)
    if not overview:
        raise RuntimeError(f"The section writer did not produce an overview for {section_id}.")
    return overview, paragraphs, validations, reviews


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate review sections with an OpenAI-compatible writing model.")
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--wire-api", default="")
    args = parser.parse_args()
    root = Path(args.review_root).resolve()
    dotenv = load_dotenv(root)
    base_url = (
        args.base_url
        or os.environ.get("REVIEW_WRITING_BASE_URL")
        or dotenv.get("REVIEW_WRITING_BASE_URL")
        or dotenv.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_OPENAI_BASE_URL
    )
    api_key = resolve_api_key(args.api_key, base_url, dotenv)
    if not api_key and not gateway_configured():
        raise SystemExit("The server text model is not configured for section generation.")
    model = (
        args.model
        or os.environ.get("REVIEW_WRITING_MODEL")
        or dotenv.get("REVIEW_WRITING_MODEL")
        or DEFAULT_TEXT_MODEL
    )
    wire_api = (
        args.wire_api
        or os.environ.get("REVIEW_WRITING_WIRE_API")
        or dotenv.get("REVIEW_WRITING_WIRE_API")
        or "responses"
    )
    project = root / "review-projects" / args.project_id
    stage = project / "02_section_drafting"
    tasks = read_json(stage / "section_tasks.json")
    evidence_package_path = stage / "section_evidence.json"
    evidence_package = (
        read_json(evidence_package_path)
        if evidence_package_path.exists()
        else {"sections": []}
    )
    evidence_sections = {
        str(item.get("section_id") or ""): item
        for item in (evidence_package.get("sections") or [])
        if isinstance(item, dict)
    }
    progress_total = len(tasks)
    task_ids = [str(task.get("section_id") or "") for task in tasks]
    checkpoint_path = stage / "section_checkpoints.json"
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {}
    checkpoint_entries = (
        checkpoint.get("entries")
        if isinstance(checkpoint, dict)
        and checkpoint.get("project_id") == args.project_id
        and checkpoint.get("task_ids") == task_ids
        else {}
    )
    if not isinstance(checkpoint_entries, dict):
        checkpoint_entries = {}
    checkpoint_entries = {
        section_id: entry
        for section_id, entry in checkpoint_entries.items()
        if section_id in task_ids
        and isinstance(entry, dict)
        and all(
            isinstance(entry.get(key), dict)
            for key in ("output", "synthesis", "writing")
        )
    }
    completed_progress: list[dict[str, str]] = [
        {
            "section_id": section_id,
            "heading": str((checkpoint_entries.get(section_id) or {}).get("heading") or section_id),
        }
        for section_id in task_ids
        if isinstance(checkpoint_entries.get(section_id), dict)
    ]
    write_generation_progress(
        stage,
        current=0,
        total=progress_total,
        phase="preparing",
        completed_sections=completed_progress,
    )
    matrix = read_json(project / "01_matrix_outline" / "literature_matrix.json")
    rows_list = matrix.get("rows") if isinstance(matrix, dict) else matrix
    rows = {str(row.get("paper_id")): row for row in rows_list or [] if isinstance(row, dict) and row.get("paper_id")}
    blueprint = read_json(project / "01_matrix_outline" / "section_blueprint.json")
    selected_outline_path = project / "01_matrix_outline" / "selected_outline.md"
    selected_outline = selected_outline_path.read_text(encoding="utf-8", errors="ignore")[:12000] if selected_outline_path.exists() else ""
    rules = load_blueprint_rule_pack(root, blueprint)
    section_specs = {str(item.get("section_id")): item for item in blueprint.get("sections", []) if isinstance(item, dict)}
    selected_papers = {
        str(pid)
        for task in tasks
        for pid in task.get("allowed_papers", [])
        if str(pid) in rows
    }
    paper_order = [
        str(row.get("paper_id"))
        for row in rows_list or []
        if isinstance(row, dict) and str(row.get("paper_id")) in selected_papers
    ]
    citation_map = {paper_id: index for index, paper_id in enumerate(dict.fromkeys(paper_order), start=1)}
    sections_dir = stage / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    output_sections = [
        dict(checkpoint_entries[section_id]["output"])
        for section_id in task_ids
        if isinstance(checkpoint_entries.get(section_id), dict)
        and isinstance(checkpoint_entries[section_id].get("output"), dict)
    ]
    synthesis_sections: list[dict[str, Any]] = [
        dict(checkpoint_entries[section_id]["synthesis"])
        for section_id in task_ids
        if isinstance(checkpoint_entries.get(section_id), dict)
        and isinstance(checkpoint_entries[section_id].get("synthesis"), dict)
    ]
    writing_sections: list[dict[str, Any]] = [
        dict(checkpoint_entries[section_id]["writing"])
        for section_id in task_ids
        if isinstance(checkpoint_entries.get(section_id), dict)
        and isinstance(checkpoint_entries[section_id].get("writing"), dict)
    ]
    failed_progress: list[dict[str, str]] = []

    def record_section_failure(section_id: str, heading: str, error: str) -> None:
        failed_progress.append(
            {
                "section_id": section_id,
                "heading": heading,
                "error": str(error)[:2000],
            }
        )
        write_generation_progress(
            stage,
            current=len(completed_progress) + len(failed_progress),
            total=progress_total,
            phase="continuing_after_failure",
            current_section_id=section_id,
            current_heading=heading,
            completed_sections=completed_progress,
            failed_sections=failed_progress,
        )
    for task in tasks:
        section_id = str(task.get("section_id"))
        if section_id in checkpoint_entries:
            continue
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="planning_claims",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
        )
        role = str(task.get("section_role") or "body").strip().casefold()
        body_synthesis_context: list[dict[str, Any]] = []
        body_synthesis_evidence_keys: set[str] = set()
        if role == "conclusion":
            body_synthesis_context, body_synthesis_evidence_keys = (
                prior_body_synthesis_context(
                    section_specs, synthesis_sections, writing_sections
                )
            )
        assigned_primary = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("primary_papers", [])
                if str(pid) in rows
            )
        )
        supporting = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("supporting_papers", [])
                if str(pid) in rows and str(pid) not in assigned_primary
            )
        )
        contextual = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("context_papers", [])
                if str(pid) in rows
                and str(pid) not in assigned_primary
                and str(pid) not in supporting
            )
        )
        allowed = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("allowed_papers", [*assigned_primary, *supporting])
                if str(pid) in rows
            )
        )
        section_evidence = evidence_sections.get(section_id, {})
        primary = list(
            dict.fromkeys(
                str(pid)
                for pid in (
                    section_evidence.get("writeable_primary_papers")
                    if "writeable_primary_papers" in section_evidence
                    else assigned_primary
                )
                or []
                if str(pid) in assigned_primary
            )
        )
        context_only_primary = [
            str(pid)
            for pid in section_evidence.get("context_only_primary_papers") or []
            if str(pid) in assigned_primary
        ]
        unresolved_primary = [
            str(pid)
            for pid in section_evidence.get("unresolved_primary_papers") or []
            if str(pid) in assigned_primary
        ]
        retrieval_mode = str(
            section_evidence.get("retrieval_mode") or "fixed_prefix_fallback"
        )
        if retrieval_mode == "lexical":
            evidence = [
                item
                for item in section_evidence.get("hits") or []
                if isinstance(item, dict)
                and str(item.get("paper_id") or "") in allowed
                and str(item.get("chunk_id") or "")
            ]
            if role == "conclusion" and body_synthesis_evidence_keys:
                evidence_by_key = {
                    str(item.get("evidence_key") or ""): item
                    for item in evidence
                    if isinstance(item, dict)
                    and str(item.get("evidence_key") or "")
                }
                for body_section_id, body_evidence in evidence_sections.items():
                    if (
                        str(
                            (section_specs.get(str(body_section_id)) or {}).get(
                                "section_role"
                            )
                            or "body"
                        ).casefold()
                        != "body"
                    ):
                        continue
                    for item in body_evidence.get("hits") or []:
                        if not isinstance(item, dict):
                            continue
                        key = str(item.get("evidence_key") or "")
                        if (
                            key in body_synthesis_evidence_keys
                            and key not in evidence_by_key
                            and str(item.get("paper_id") or "") in allowed
                        ):
                            evidence.append(item)
                            evidence_by_key[key] = item
        elif retrieval_mode == "abstract_only":
            evidence = [
                item
                for item in section_evidence.get("abstract_context") or []
                if isinstance(item, dict)
                and str(item.get("paper_id") or "") in allowed
                and str(item.get("evidence") or "").strip()
            ]
        elif retrieval_mode == "fixed_prefix_fallback":
            evidence = [paper_evidence(root, rows, paper_id) for paper_id in allowed]
        else:
            evidence = []
        has_evidence_text = any(
            str(
                item.get("content")
                if retrieval_mode == "lexical"
                else item.get("evidence")
                or ""
            ).strip()
            for item in evidence
            if isinstance(item, dict)
        )
        if not evidence or not has_evidence_text:
            message = (
                f"No usable indexed evidence for {section_id}."
                if retrieval_mode in {"lexical", "insufficient_evidence"}
                else f"No usable MinerU Markdown or matrix evidence for {section_id}."
            )
            record_section_failure(
                section_id, str(task.get("heading") or section_id), message
            )
            continue
        evidence_paper_count = len(
            {
                str(item.get("paper_id") or "")
                for item in evidence
                if str(item.get("paper_id") or "")
            }
        )
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="generating",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
            evidence_hit_count=len(evidence),
            evidence_paper_count=evidence_paper_count,
        )
        spec = section_specs.get(section_id, {})
        comparison_table = build_matrix_comparison_table(
            section_id, assigned_primary, rows
        )
        if role == "introduction":
            paragraph_instruction = """Write 2-4 claim-centered framing paragraphs. Define the problem, scope, terminology, organizing logic, and evidence landscape. Use the supporting papers only as brief representative anchors. Do not give any paper a standalone summary, and do not repeat detailed methods, conditions, datasets, results, yields, or limitations that belong in a body section."""
        elif role == "conclusion":
            paragraph_instruction = """Write 2-4 claim-centered synthesis paragraphs. Use the validated body-section synthesis supplied below as the analytical input, compare only the body conclusions that were actually completed, identify shared limitations and defensible future directions, and keep every conclusion tied to its inherited evidence identities. Do not replay the body as a paper-by-paper list and do not repeat full methods, conditions, datasets, or results."""
        elif primary:
            paragraph_instruction = f"""Write claim-centered review paragraphs, not one paragraph per paper. Every writeable primary paper must support at least one paragraph, but related studies should be compared or synthesized together when they address the same claim. A paragraph may cite one or several allowed papers. Discuss detailed study evidence only here, in the paper's primary section. Supporting papers may be used briefly for comparison, without repeating their full descriptions. Cover all {len(primary)} writeable primary papers. Do not force unresolved primary papers into prose."""
        elif context_only_primary:
            paragraph_instruction = """Write only a short, explicitly attributed background synthesis supported by the supplied abstracts. Do not state detailed methods, numerical results, mechanisms, scope boundaries, or limitations. The absence of full-text evidence is a writing boundary, not a scientific research gap."""
        else:
            paragraph_instruction = """Write 2-4 cross-cutting synthesis paragraphs using only the supporting evidence. Compare previously introduced findings from a new analytical angle, but do not repeat complete paper descriptions, methods, conditions, datasets, or results."""
        evidence_instruction = (
            "Every factual Claim must copy one or more `evidence_keys` exactly from the "
            "indexed evidence. Its citation_group must contain exactly the paper IDs "
            "resolved by those keys."
            if retrieval_mode == "lexical"
            else "Use only the allowed source paper IDs and the supplied bounded source text. "
            "Mark Claims partially_supported and do not exceed its evidence ceiling."
        )
        cross_section_input = (
            "Validated body-section synthesis (the conclusion may synthesize only these completed, evidence-bound claims):\n"
            + json.dumps(body_synthesis_context, ensure_ascii=False)
            if role == "conclusion"
            else ""
        )
        plan_prompt = f"""Plan one section of a source-grounded scientific review before prose is written.

Topic: {blueprint.get('review_topic') or project.name}
Selected review outline (preserve its ordering and heading intent):
{selected_outline}

Section title: {task.get('heading')}
Section role: {role}
Section thesis: {task.get('core_argument')}
Required claims: {json.dumps(task.get('must_cover_points') or [], ensure_ascii=False)}
Comparison axes and constraints: {json.dumps(spec.get('review_claims') or [], ensure_ascii=False)[:10000]}
Assigned primary paper IDs: {', '.join(assigned_primary) or 'none'}
Writeable primary paper IDs (the only papers that must be covered): {', '.join(primary) or 'none'}
Context-only primary paper IDs (optional broad attribution only): {', '.join(context_only_primary) or 'none'}
Unresolved primary paper IDs (do not force into prose): {', '.join(unresolved_primary) or 'none'}
Supporting paper IDs (brief comparison or synthesis only): {', '.join(supporting) or 'none'}
Context paper IDs (framing only; never substitute for primary evidence): {', '.join(contextual) or 'none'}
Allowed paper IDs only: {', '.join(allowed)}
Required synthesis components: {json.dumps(spec.get('synthesis_requirements') or [], ensure_ascii=False)}
Source-addressable Matrix comparison table (empty cells are unknown, never negative findings):
{json.dumps(comparison_table, ensure_ascii=False)}

Return an evidence-bound Synthesis and Writing Plan, not manuscript prose. First state the
section's positive synthesis. Then plan claim-centered paragraphs with one distinct academic
responsibility and a reader_takeaway each. Avoid one paragraph per paper when the evidence
supports comparison. Every planned Claim must separately declare claim_kind,
epistemic_status, support_status, citation_group, evidence_keys, and an evidence ceiling.

{paragraph_instruction}

{cross_section_input}

Evidence contract: {evidence_instruction}
Never invent conditions, yields, selectivities, structures, causal relations, or mechanistic
evidence. A limitation must follow a positive supported takeaway instead of replacing it.
Do not return blocked Claims as publishable content.

Academic rules:\n{rules}

Source evidence (only use claims supported here):\n{json.dumps(evidence, ensure_ascii=False)}
"""
        try:
            proposed_plan = call_structured_llm(
                plan_prompt,
                PLAN_SCHEMA,
                api_key,
                base_url,
                model,
                wire_api,
                label="section-academic-planning",
                schema_name="review_section_plan",
                required_list="paragraphs",
            )
            synthesis_section, writing_section = normalize_section_plan(
                section_id=section_id,
                role=role,
                primary=primary,
                supporting=supporting,
                allowed=allowed,
                evidence=evidence,
                retrieval_mode=retrieval_mode,
                generated=proposed_plan,
                synthesis_requirements=list(spec.get("synthesis_requirements") or []),
            )
            synthesis_section["comparison_table"] = comparison_table
        except RuntimeError as exc:
            message = str(exc)
            if "transport failed" in message.casefold():
                message = (
                    "Section-planning provider could not be reached after the server gateway exhausted its retries. "
                    f"Configured endpoint: {base_url}. Open API Settings from this deployment, "
                    "confirm that the displayed active workspace is correct, save the text provider again, "
                    "and retry the stage. "
                    f"Details: {message}"
                )
            record_section_failure(
                section_id, str(task.get("heading") or section_id), message
            )
            continue
        except urllib.error.HTTPError as exc:
            record_section_failure(
                section_id,
                str(task.get("heading") or section_id),
                f"Section-planning model request was rejected (HTTP {exc.code}). "
                "Check OPENAI_API_KEY, OPENAI_BASE_URL, and REVIEW_WRITING_MODEL.",
            )
            continue
        except urllib.error.URLError as exc:
            record_section_failure(
                section_id,
                str(task.get("heading") or section_id),
                f"Section-planning model is unreachable: {exc.reason}",
            )
            continue
        selected_evidence_keys = {
            str(ref.get("evidence_key") or "")
            for claim in writing_section.get("claims") or []
            for ref in claim.get("evidence_refs") or []
            if isinstance(ref, dict) and str(ref.get("evidence_key") or "")
        }
        selected_paper_ids = {
            str(paper_id)
            for claim in writing_section.get("claims") or []
            for paper_id in claim.get("citation_group") or []
        }
        writer_evidence = [
            item for item in evidence
            if isinstance(item, dict)
            and (
                str(item.get("evidence_key") or "") in selected_evidence_keys
                or (
                    not selected_evidence_keys
                    and str(item.get("paper_id") or "") in selected_paper_ids
                )
            )
        ]
        writer_prompt = f"""Realize a validated academic Writing Plan as fluent review prose.

Topic: {blueprint.get('review_topic') or project.name}
Section title: {task.get('heading')}
Section role: {role}

The plan below is an immutable contract for this call. Return every paragraph_id and every
claim_id exactly once and in plan order. Write one concise realization for each Claim. Do not
add, remove, merge, split, or reorder Claims; do not add citations, source IDs, paper IDs, or
headings because the workflow inserts citations after identity validation. Respect each
evidence_ceiling and use conditional attribution for author interpretations or mechanisms.
Lead with supported positive synthesis, then state necessary boundaries. Avoid reading-note
style and avoid one-paper-at-a-time narration unless the plan explicitly requires it.

Validated Synthesis slice:
{json.dumps(synthesis_section, ensure_ascii=False)}

Validated Writing Plan:
{json.dumps(writing_section, ensure_ascii=False)}

Selected source evidence only:
{json.dumps(writer_evidence, ensure_ascii=False)}

Writing rules:
{rules}
"""
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="drafting",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
            evidence_hit_count=len(evidence),
            evidence_paper_count=evidence_paper_count,
        )
        try:
            generated_draft = call_structured_llm(
                writer_prompt,
                WRITER_SCHEMA,
                api_key,
                base_url,
                model,
                wire_api,
                label="section-claim-realization",
                schema_name="review_claim_realization",
                required_list="paragraphs",
            )
            overview, paragraphs, validations, reviews = validate_and_realize_section(
                section_id=section_id,
                generated=generated_draft,
                writing_section=writing_section,
                evidence=evidence,
                citation_map=citation_map,
            )
        except RuntimeError as exc:
            record_section_failure(
                section_id,
                str(task.get("heading") or section_id),
                str(exc),
            )
            continue
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="reviewing",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
            evidence_hit_count=len(evidence),
            evidence_paper_count=evidence_paper_count,
        )
        markdown = [f"## {task.get('heading')}", "", overview, ""]
        for item in paragraphs:
            paragraph_id = str(item["paragraph_id"])
            text = str(item["text"])
            markdown.extend([
                text,
                "",
                f"<!-- paragraph_id: {paragraph_id} -->",
                "",
            ])
        section_text = make_xml_compatible("\n".join(markdown).strip() + "\n")[0]
        (sections_dir / f"{section_id}.md").write_text(section_text, encoding="utf-8")
        synthesis_sections.append(synthesis_section)
        writing_sections.append(writing_section)
        output_sections.append(
            {
                "section_id": section_id,
                "heading": task.get("heading"),
                "section_role": role,
                "writing_mode": task.get("writing_mode"),
                "primary_papers": primary,
                "supporting_papers": supporting,
                "overview": overview,
                "paragraphs": paragraphs,
                "draft_md": section_text,
                "validations": validations,
                "reviews": reviews,
                "repair_candidates": [],
                "planning_proposals": [],
            }
        )
        checkpoint_entries[section_id] = {
            "heading": str(task.get("heading") or section_id),
            "output": output_sections[-1],
            "synthesis": synthesis_section,
            "writing": writing_section,
        }
        write_section_checkpoint(
            stage,
            {
                "schema_version": 1,
                "project_id": args.project_id,
                "task_ids": task_ids,
                "entries": checkpoint_entries,
            },
        )
        completed_progress.append(
            {
                "section_id": section_id,
                "heading": str(task.get("heading") or section_id),
            }
        )
        write_generation_progress(
            stage,
            current=len(completed_progress) + len(failed_progress),
            total=progress_total,
            phase="planning_claims"
            if len(completed_progress) + len(failed_progress) < progress_total
            else "finalizing",
            completed_sections=completed_progress,
            failed_sections=failed_progress,
        )
    if failed_progress:
        write_generation_progress(
            stage,
            current=len(completed_progress) + len(failed_progress),
            total=progress_total,
            phase="failed_with_checkpoint",
            completed_sections=completed_progress,
            failed_sections=failed_progress,
        )
        failed_ids = ", ".join(item["section_id"] for item in failed_progress)
        raise SystemExit(
            f"Section generation completed {len(completed_progress)} section(s), but {len(failed_progress)} section(s) failed: {failed_ids}. Retry the job to resume only the failed sections."
        )
    primary_sections_by_paper: dict[str, list[str]] = {}
    supporting_sections_by_paper: dict[str, list[str]] = {}
    for task in tasks:
        section_id = str(task.get("section_id") or "")
        for paper_id in task.get("primary_papers") or []:
            primary_sections_by_paper.setdefault(str(paper_id), []).append(section_id)
        for paper_id in task.get("supporting_papers") or []:
            supporting_sections_by_paper.setdefault(str(paper_id), []).append(section_id)
    comparable_paragraphs = [
        paragraph
        for section in writing_sections
        for paragraph in section.get("paragraphs") or []
        if isinstance(paragraph, dict)
    ]
    comparison_paragraphs = [
        paragraph
        for paragraph in comparable_paragraphs
        if len({str(value) for value in paragraph.get("paper_ids") or [] if str(value)}) >= 2
    ]
    synthesis_diagnostics = {
        "schema_version": 1,
        "comparison_paragraph_count": len(comparison_paragraphs),
        "planned_paragraph_count": len(comparable_paragraphs),
        "comparison_coverage": round(
            len(comparison_paragraphs) / max(1, len(comparable_paragraphs)), 4
        ),
        "papers_with_multiple_primary_sections": {
            paper_id: section_ids
            for paper_id, section_ids in primary_sections_by_paper.items()
            if len(set(section_ids)) > 1
        },
        "paper_roles": {
            paper_id: {
                "primary_sections": primary_sections_by_paper.get(paper_id, []),
                "supporting_sections": supporting_sections_by_paper.get(paper_id, []),
            }
            for paper_id in sorted(
                set(primary_sections_by_paper) | set(supporting_sections_by_paper)
            )
        },
    }
    write_json(
        stage / "synthesis_state.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "planning_mode": "evidence_first_pre_draft",
            "source_evidence_registry": "sections/evidence_package.json",
            "synthesis_diagnostics": synthesis_diagnostics,
            "sections": synthesis_sections,
        },
    )
    write_json(
        stage / "writing_plan.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "planning_mode": "evidence_first_pre_draft",
            "source_evidence_registry": "sections/evidence_package.json",
            "sections": writing_sections,
        },
    )
    write_json(stage / "section_drafts.json", {"project_id": args.project_id, "sections": output_sections})
    (stage / "section_drafts.md").write_text("\n\n".join(section["draft_md"] for section in output_sections), encoding="utf-8")
    (stage / "section_drafting_report.md").write_text(
        f"# Section Drafting Report\n\nGenerated {len(output_sections)} source-grounded sections with evidence-first Synthesis, Paragraph, Claim/Citation, realization, and deterministic review contracts using model `{model}`.\n", encoding="utf-8"
    )
    write_generation_progress(
        stage,
        current=len(completed_progress),
        total=progress_total,
        phase="completed",
        completed_sections=completed_progress,
    )
    print(f"Generated {len(output_sections)} sections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
