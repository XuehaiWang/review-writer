#!/usr/bin/env python3
"""Generate source-grounded review sections from Blueprint tasks and MinerU Markdown."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import RLock
from copy import deepcopy
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
from review_writer_core.scientific_facts import (  # noqa: E402
    REVIEW_COMPARISON_POLICY,
    build_fact_comparison as build_matrix_comparison_table,
    claim_assertion_ceiling, fact_claim_issues, registered_fact_bindings, writable_evidence_keys,
)
from review_writer_core.academic_contracts import mechanism_evidence_types  # noqa: E402
from review_writer_core.stages.sections.fact_routing import (  # noqa: E402
    FACT_ROUTING_CONTRACT, FACT_ROUTING_INSTRUCTION, fact_routing_report, unselected_semantic_fact_ids,
)
from review_writer_core.evidence_integrity import (  # noqa: E402
    normalize_retrieval_mode,
    unsupported_realization_anchors,
)
from review_writer_core.writing_contracts import (  # noqa: E402
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    derive_writing_scope_contract,
    writing_scope_prompt_block,
    section_constraint_prompt_block,
)
from review_writer_core.model_gateway_client import (  # noqa: E402
    GatewayRequestError,
    call_json_model as call_gateway_json,
    gateway_configured,
    parse_json_object_text as _parse_json_object_text,
)
from review_writer_core.review_fact_readiness import (  # noqa: E402
    negative_claim_eligibility,
)
from review_writer_core.claim_contracts import (  # noqa: E402
    claim_planning_prompt_block,
    FACT_GROUNDED_BLUEPRINT_SCHEMA_VERSION,
    claim_is_executable,
    argument_projection,
    claim_support_coverage,
    derive_section_readiness,
)
from review_writer_core.draft_bibliography import format_citation_group  # noqa: E402
from review_writer_core.paragraph_citations import render_paragraph_citations  # noqa: E402
from review_writer_core.stages.sections.authoring import paragraph_parts
from review_writer_core.section_narrative_contracts import (  # noqa: E402
    CANONICAL_PARAGRAPH_ROLES,
    canonical_argument_role,
    derive_narrative_diagnostics,
    resolve_section_depth_contract,
)
from review_writer_core.stages.sections.rule_packs import (  # noqa: E402
    RULE_PACK_PROMPT_VERSION, load_rule_pack_text,
)
from review_writer_core.stages.sections.plan_repair import (  # noqa: E402
    merge_plan_repair,
    repair_prompt,
    repair_schema,
)
from review_writer_core.stages.sections.evidence_resolution import pending_markdown, resolution_record
from review_writer_core.stages.sections.execution import run_sections, chapter_responsibilities
from review_writer_core.stages.sections.source_writing import CONTRACT as SOURCE_CONTRACT, AUTHORING_VERSION, write_from_sources, valid_source_claim, passage_eligible
from review_writer_core.source_attribution import contribution_context
from review_writer_core.publication_tables import paper_presentation_outcomes
from review_writer_core.stages.sections.coverage import (  # noqa: E402
    claim_fact_identity_gaps,
    direct_claim_papers,
    missing_primary_papers,
    required_primary_papers,
    reusable_section_entries,
    supported_scientific_claim_ids,
    section_input_fingerprints, matching_section_inputs,
)


NEGATIVE_SOURCE_STATEMENT_RE = re.compile(
    r"\b(?:the\s+(?:study|paper|report|article|source)\s+)?"
    r"(?:does\s+not|did\s+not|doesn't|didn't|was\s+not|were\s+not)\s+"
    r"(?:report|provide|specify|define|describe|disclose)\b|"
    r"\b(?:not|never)\s+(?:reported|provided|specified|defined|described|disclosed)\b",
    re.I,
)


def write_generation_progress(
    stage: Path,
    *,
    current: int,
    total: int,
    phase: str,
    current_section_id: str = "",
    current_heading: str = "",
    completed_sections: list[dict[str, Any]] | None = None,
    failed_sections: list[dict[str, Any]] | None = None,
    active_sections: list[dict[str, Any]] | None = None,
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
        "active_sections": list(active_sections or []),
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
    return load_rule_pack_text(_BOOTSTRAP_ROOT, blueprint)


def load_cross_study_synthesis_skill() -> str:
    """Load the single reusable synthesis policy used by planning and prose."""

    path = (
        _BOOTSTRAP_ROOT
        / "skills"
        / "review-cross-study-synthesis"
        / "SKILL.md"
    )
    if not path.is_file():
        raise RuntimeError(f"Cross-study synthesis skill is missing: {path}")
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    return text.strip()[:6000]


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
        # Evidence quotations must retain the exact registered source bytes.
        # XML cleanup belongs to rendered prose, not source identity matching.
        return {str(key): item if key in {"quote", "support_excerpt"} else repair_model_unicode(item)
                for key, item in value.items()}
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
                                "claim_id", "claim", "claim_kind", "synthesis_subtype",
                                "epistemic_status", "support_status",
                                "citation_group", "evidence_keys", "evidence_ceiling", "fact_ids"
                            ],
                            "properties": {
                                "claim_id": {"type": "string"},
                                "claim": {"type": "string"},
                                "claim_kind": {"type": "string"},
                                "synthesis_subtype": {"type": "string"},
                                "epistemic_status": {"type": "string"},
                                "support_status": {"type": "string"},
                                "citation_group": {"type": "array", "items": {"type": "string"}},
                                "evidence_keys": {"type": "array", "items": {"type": "string"}},
                                "fact_ids": {"type": "array", "items": {"type": "string"}},
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
    *CANONICAL_PARAGRAPH_ROLES,
}


def compact_text(value: Any, *, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def manuscript_word_count(value: Any) -> int:
    """Count Latin tokens and CJK characters without counting Markdown syntax."""

    return len(
        re.findall(
            r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]",
            str(value or ""),
        )
    )


def target_word_floor(value: Any) -> int:
    """Read a conservative lower depth bound from current and legacy specs."""

    if isinstance(value, dict):
        for key in ("min", "minimum", "lower"):
            try:
                candidate = int(value.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return candidate
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(float(value) * 0.7))
    match = re.search(
        r"\b(\d{2,6})\s*(?:[-–—~]|to)\s*\d{2,6}\b",
        str(value or ""),
        re.I,
    )
    if match:
        return int(match.group(1))
    try:
        return max(0, int(str(value or "").strip()) * 7 // 10)
    except ValueError:
        return 0


def bounded_evidence_payload(
    evidence: list[dict[str, Any]],
    *,
    char_budget: int = 70_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project once; truncate text only if the complete projection cannot fit."""

    rows = [item for item in evidence if isinstance(item, dict)]
    char_budget = max(2, int(char_budget))
    per_content = max(160, min(1800, char_budget // max(1, len(rows)) - 360))
    compacted: list[dict[str, Any]] = []
    for row in rows:
        content = re.sub(r"\s+", " ", str(row.get("content") or row.get("evidence") or "")).strip()
        compacted.append(
            {
                "evidence_id": row.get("evidence_id"),
                "evidence_key": row.get("evidence_key"),
                "paper_id": row.get("paper_id"),
                "paper_title": compact_text(row.get("paper_title") or row.get("title"), limit=180),
                "chunk_id": row.get("chunk_id"),
                "page_start": row.get("page_start"),
                "page_end": row.get("page_end"),
                "section_path": list(row.get("section_path") or [])[:5],
                "source_channel": row.get("source_channel"),
                "support_level": row.get("support_level"),
                "claim_eligible": bool(row.get("claim_eligible", True)),
                "question_ids": list(row.get("question_ids") or []),
                "fact_routes": [
                    {key: route.get(key) for key in (
                        "fact_id", "original_field_id", "canonical_field_id", "status",
                    )}
                    for route in row.get("fact_routes") or []
                    if route.get("method") != "canonical_field"
                ],
                "fact_ids": list(row.get("fact_ids") or []),
                "fact_bindings": [{
                    **{key: binding.get(key) for key in (
                        "fact_id", "paper_id", "field_id", "value", "subject", "predicate", "experiment_id",
                        "qualifiers", "epistemic_status", "assertion_ceiling", "usage")},
                    "evidence_refs": [{"evidence_key": ref.get("evidence_key"),
                                       "support_excerpt": ref.get("support_excerpt") or binding.get("support_excerpt") or ""}
                                      for ref in binding.get("evidence_refs") or []],
                } for binding in row.get("fact_bindings") or []],
                "epistemic_status": row.get("epistemic_status"),
                "normalized_fact_value": compact_text(
                    row.get("normalized_fact_value"), limit=500
                ),
                "assertion_ceiling": compact_text(
                    row.get("assertion_ceiling"), limit=80
                ),
                "evidence_ceiling": compact_text(row.get("evidence_ceiling"), limit=240),
                "content": content,
            }
        )
    compacted = [{key: value for key, value in row.items()
                  if value is not None and value != "" and value != []} for row in compacted]
    full_size = len(json.dumps(compacted, ensure_ascii=False))
    if full_size <= char_budget:
        return compacted, {
            "input_hit_count": len(rows), "output_hit_count": len(compacted),
            "omitted_hit_count": 0, "truncated_content_hit_count": 0,
            "serialized_characters": full_size, "content_chars_per_hit": None,
            "content_characters": sum(len(row.get("content", "")) for row in compacted),
            "char_budget": char_budget, "compacted": False,
        }
    truncated = 0
    for row in compacted:
        content = row.get("content", "")
        truncated += len(content) > per_content
        if content:
            row["content"] = compact_text(content, limit=per_content)
    # Budget the serialized payload, not only content strings. Fact bindings
    # used to bypass the cap by repeating full quotes and audit records.
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in compacted:
        row = {key: value for key, value in row.items() if value is not None and value != "" and value != []}
        groups.setdefault(str(row.get("paper_id") or ""), []).append(row)
    selected, used = [], 2
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for group in groups.values():
            if index >= len(group):
                continue
            row = group[index]
            size = len(json.dumps(row, ensure_ascii=False)) + (2 if selected else 0)
            if used + size <= char_budget:
                selected.append(row)
                used += size
    return selected, {
        "input_hit_count": len(rows),
        "output_hit_count": len(selected),
        "omitted_hit_count": len(compacted) - len(selected),
        "truncated_content_hit_count": truncated,
        "serialized_characters": used,
        "content_chars_per_hit": per_content,
        "content_characters": sum(
            len(str(item.get("content") or "")) for item in selected
        ),
        "char_budget": char_budget,
        "compacted": len(selected) < len(rows) or bool(truncated),
    }


def request_body_budget_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "request_body_budget_exhausted",
            "request body",
            "payload too large",
            "prompt too long",
            "context length",
            "http 413",
        )
    )


def effective_retrieval_mode(section_evidence: dict[str, Any]) -> str:
    """Authorize the legacy prefix reader only for explicitly marked old indexes."""

    mode = normalize_retrieval_mode(section_evidence.get("retrieval_mode"))
    if mode == "fixed_prefix_fallback" and not bool(
        section_evidence.get("legacy_fallback_authorized")
    ):
        return "insufficient_evidence"
    return mode




def build_mechanism_evidence_table(
    section_id: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or not item.get("claim_eligible", True):
            continue
        types = list(item.get("mechanism_evidence_types") or [])
        if not types:
            types = mechanism_evidence_types(item.get("content"))
        if not types:
            continue
        rows.append(
            {
                "paper_id": str(item.get("paper_id") or ""),
                "evidence_key": str(item.get("evidence_key") or ""),
                "evidence_level": str(item.get("evidence_level") or "reported_result"),
                "evidence_types": types,
                "source_channel": str(item.get("source_channel") or "body"),
            }
        )
    return {
        "section_id": section_id,
        "rows": rows,
        "paper_count": len({row["paper_id"] for row in rows if row["paper_id"]}),
        "evidence_types": list(
            dict.fromkeys(
                value for row in rows for value in row["evidence_types"]
            )
        ),
    }


def synthesis_contract_gaps(
    writing_section: dict[str, Any],
    synthesis_section: dict[str, Any],
    requirements: list[dict[str, Any]],
    comparison_table: dict[str, Any],
    mechanism_table: dict[str, Any],
    depth_contract: dict[str, Any] | None = None,
) -> list[str]:
    required = {
        str(item.get("component") or "")
        for item in requirements
        if isinstance(item, dict) and str(item.get("necessity") or "") == "required"
    }
    claims = [
        item for item in writing_section.get("claims") or [] if isinstance(item, dict)
    ]
    diagnostics = synthesis_section.get("normalization_diagnostics") or {}
    gaps: list[str] = [f"primary_paper_unrouted:{paper}" for paper in diagnostics.get("missing_primary_papers") or []]
    if "comparison" in required and comparison_table.get("comparable_fields"):
        comparison_claim = any(
            str(claim.get("claim_kind") or "")
            in {"cross_study_comparison", "review_synthesis"}
            and len(set(claim.get("citation_group") or [])) >= 2
            for claim in claims
        )
        if not comparison_claim:
            gaps.append("required_cross_study_comparison_missing")
    if "mechanism" in required and mechanism_table.get("rows"):
        mechanism_claim = any(
            str(claim.get("claim_kind") or "") == "mechanism_interpretation"
            for claim in claims
        )
        if not mechanism_claim:
            gaps.append("required_mechanism_evidence_synthesis_missing")
    supported_components = {
        str(item.get("component_type") or "")
        for item in synthesis_section.get("components") or []
        if isinstance(item, dict) and str(item.get("status") or "") == "supported"
    }
    for component in required:
        if component == "comparison" and not comparison_table.get("comparable_fields"):
            continue
        if component == "mechanism" and not mechanism_table.get("rows"):
            continue
        if component not in supported_components:
            gaps.append(f"required_{component}_component_not_supported")
    narrative = derive_narrative_diagnostics(writing_section, depth_contract)
    for missing in narrative.get("missing_requirements") or []:
        gaps.append(f"narrative_{missing}_missing")
    writing_section["depth_contract"] = dict(depth_contract or {})
    writing_section["narrative_diagnostics"] = narrative
    return list(dict.fromkeys(gaps))


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
    declared_claims: list[dict[str, Any]] | None = None,
    depth_contract: dict[str, Any] | None = None,
    strict: bool = True,
    paragraph_start_index: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a model proposal into a deterministic evidence-bound contract."""

    retrieval_mode = normalize_retrieval_mode(retrieval_mode)
    if retrieval_mode == "unsupported_retrieval_mode":
        raise RuntimeError(f"Unsupported section retrieval mode for {section_id}.")
    enforce_declared_claims = role == "body" and declared_claims is not None
    declared_by_id = {
        str(claim.get("claim_id") or ""): dict(claim)
        for claim in declared_claims or []
        if isinstance(claim, dict)
        and str(claim.get("claim_id") or "")
        and claim_is_executable(claim)
    }
    assigned_declared_claim_ids: set[str] = set()
    fact_registry = registered_fact_bindings(evidence, allowed)
    writable_keys = writable_evidence_keys(evidence, allowed)
    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_key") or "")
        and str(item.get("paper_id") or "") in allowed
        and str(item["evidence_key"]) in writable_keys
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
    rejected_claims: list[dict[str, Any]] = []
    # Every returned/added slot must be validated. Silently slicing the plan
    # loses primary-paper coverage and can discard targeted repairs at its end.
    raw_paragraphs = [
        item
        for item in (generated.get("paragraphs") or [])
        if isinstance(item, dict)
    ]
    for paragraph_index, raw_paragraph in enumerate(raw_paragraphs, start=paragraph_start_index):
        if not isinstance(raw_paragraph, dict):
            continue
        paragraph_id = f"{section_id}-p{paragraph_index}"
        paragraph_claim_ids: list[str] = []
        paragraph_papers: list[str] = []
        for claim_index, raw_claim in enumerate(
            (raw_paragraph.get("claims") or []), start=1
        ):
            if not isinstance(raw_claim, dict):
                continue
            proposed_claim_id = compact_text(
                raw_claim.get("claim_id"), limit=120
            )
            blueprint_claim = declared_by_id.get(proposed_claim_id)
            claim_id = (
                proposed_claim_id
                if enforce_declared_claims and proposed_claim_id
                else f"{paragraph_id}-C{claim_index:02d}"
            )
            def reject(*reasons):
                rejected_claims.append({
                    "paragraph_id": paragraph_id, "claim_id": claim_id,
                    "claim": compact_text(raw_claim.get("claim")), "reasons": list(reasons),
                    "fact_ids": list(raw_claim.get("fact_ids") or []),
                    "evidence_keys": list(raw_claim.get("evidence_keys") or []),
                })
            if enforce_declared_claims and blueprint_claim is None:
                reject("unregistered_blueprint_claim_id")
                continue
            if enforce_declared_claims and claim_id in assigned_declared_claim_ids:
                reject("duplicate_blueprint_claim_id")
                continue
            source_claim = blueprint_claim or raw_claim
            if source_claim.get("argument_basis") and not claim_is_executable(source_claim):
                reject("argument_verification_stale_or_missing")
                continue
            support_status = compact_text(
                source_claim.get("support_status"), limit=40
            ).casefold()
            if support_status not in SUPPORT_STATUSES:
                support_status = "partially_supported"
            # A blocked proposal is diagnostic input, not publishable content.
            if support_status == "blocked":
                reject("planner_marked_blocked")
                continue
            requested_fact_ids = list(dict.fromkeys(
                str(fid) for fid in source_claim.get("fact_ids") or [] if fid
            ))
            if requested_fact_ids and set(requested_fact_ids) - fact_registry.keys():
                reject("unregistered_or_ineligible_fact_selection")
                continue
            selected_bindings = [fact_registry[fid] for fid in requested_fact_ids]
            proposed_keys = ([ref["evidence_key"] for binding in selected_bindings for ref in binding["evidence_refs"]]
                             if requested_fact_ids else raw_claim.get("evidence_keys") or [])
            keys = list(
                dict.fromkeys(
                    str(key)
                    for key in proposed_keys
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
                    for paper_id in (
                        [
                            *(source_claim.get("primary_papers") or []),
                            *(source_claim.get("comparison_papers") or []),
                        ]
                        if blueprint_claim is not None
                        else source_claim.get("citation_group") or []
                    )
                    if str(paper_id) in allowed
                )
            )
            citation_group = key_papers if retrieval_mode == "lexical" else proposed_group
            if retrieval_mode == "lexical" and (not keys or not citation_group):
                reject("no_registered_source")
                continue
            if retrieval_mode != "lexical" and not citation_group:
                citation_group = [
                    str(item)
                    for item in raw_paragraph.get("paper_ids") or []
                    if str(item) in allowed
                ][:2]
            if not citation_group:
                reject("no_allowed_citation")
                continue
            claim_text = compact_text(
                source_claim.get("proposition")
                or source_claim.get("claim")
            )
            if not claim_text:
                reject("empty_claim")
                continue
            if NEGATIVE_SOURCE_STATEMENT_RE.search(claim_text) and not any(
                negative_claim_eligibility(
                    evidence_by_key[key].get("fact_state"),
                    evidence_by_key[key].get("checked_sources") or [],
                )
                for key in keys
            ):
                # Retrieval misses are workflow diagnostics, not evidence that
                # a publication omitted a scientific fact.  Dropping the
                # proposal sends the plan through its normal repair path.
                reject("source_absence_not_verified")
                continue
            claim_kind = compact_text(
                source_claim.get("claim_kind")
                or source_claim.get("claim_type"),
                limit=80,
            ).casefold()
            claim_kind = {
                "reported_result": "reported_finding",
                "comparison": "cross_study_comparison",
                "cross_study_comparison": "cross_study_comparison",
                "mechanism": "mechanism_interpretation",
                "scope": "reported_finding",
                "limitation": "reported_finding",
            }.get(claim_kind, claim_kind)
            if claim_kind not in CLAIM_KINDS:
                claim_kind = "reported_finding"
            epistemic_status = compact_text(
                source_claim.get("epistemic_status"), limit=80
            ).casefold()
            if epistemic_status not in EPISTEMIC_STATUSES:
                epistemic_status = "direct_source_report"
            if retrieval_mode != "lexical":
                support_status = "partially_supported"
            evidence_refs = [
                {
                    "evidence_id": evidence_by_key[key].get("evidence_id"),
                    "evidence_key": key,
                    "relationship": "supports",
                }
                for key in keys
            ]
            fact_ids = list(
                dict.fromkeys(
                    str(fact_id)
                    for key in keys
                    for fact_id in evidence_by_key[key].get("fact_ids") or []
                    if str(fact_id)
                )
            )
            normalized_fact_values = list(
                dict.fromkeys(
                    compact_text(evidence_by_key[key].get("normalized_fact_value"), limit=800)
                    for key in keys
                    if compact_text(
                        evidence_by_key[key].get("normalized_fact_value"), limit=800
                    )
                )
            )
            if requested_fact_ids:
                invalid_fact_ids = claim_fact_identity_gaps(
                    {
                        "fact_ids": requested_fact_ids,
                        "evidence_refs": evidence_refs,
                    },
                    evidence_by_key,
                )
                if invalid_fact_ids:
                    reject(
                        "fact_identity_outside_selected_evidence:"
                        + ",".join(sorted(invalid_fact_ids))
                    )
                    continue
                fact_ids = requested_fact_ids
                normalized_fact_values = [str(binding["value"]) for binding in selected_bindings]
                issues = fact_claim_issues(claim_text, selected_bindings, claim_kind=claim_kind)
                if issues:
                    reject(*issues)
                    continue
            elif any(evidence_by_key[key].get("fact_bindings") for key in keys):
                # Do not evade experiment-level checks by omitting fact_ids.
                reject("registered_fact_selection_required")
                continue
            elif any(not evidence_by_key[key].get("claim_eligible", True) for key in keys):
                reject("background_requires_bound_fact")
                continue
            program_ceiling = claim_assertion_ceiling(
                [evidence_by_key[key] for key in keys],
                [fact_registry[fid] for fid in fact_ids if fid in fact_registry],
            )
            ceiling_explanation = compact_text(
                source_claim.get("evidence_ceiling")
                or " ".join(
                    str(evidence_by_key[key].get("evidence_ceiling") or "")
                    for key in keys
                )
                or "Do not generalize beyond the cited source evidence."
            )
            if source_claim.get("argument_basis") and (
                program_ceiling != source_claim.get("assertion_ceiling")
                or {r.get("evidence_key") for r in source_claim.get("evidence_refs") or []} - set(keys)
            ):
                reject("argument_premises_changed")
                continue
            coverage_report = claim_support_coverage(
                {
                    "proposition": claim_text,
                    "source": source_claim.get("source"),
                    "paper_ids": citation_group,
                    "fact_ids": fact_ids,
                    "coverage": dict(source_claim.get("coverage") or {}),
                    "evidence_refs": [
                        {"evidence_key": key} for key in keys
                    ],
                },
                evidence_texts=[
                    " ".join(
                        str(value or "")
                        for value in (
                            evidence_by_key[key].get("content")
                            or evidence_by_key[key].get("evidence")
                            or "",
                            evidence_by_key[key].get("normalized_fact_value") or "",
                        )
                    )
                    for key in keys
                ],
                available_fact_ids=fact_ids,
                evidence_paper_ids=[
                    evidence_by_key[key].get("paper_id") for key in keys
                ],
            )
            if (
                support_status == "supported"
                and coverage_report["support_status"] != "supported"
            ):
                support_status = "partially_supported"
            claim_plans.append(
                {
                    "claim_id": claim_id,
                    "paragraph_id": paragraph_id,
                    "sequence": len(paragraph_claim_ids) + 1,
                    "claim": claim_text,
                    "claim_kind": claim_kind,
                    "synthesis_subtype": compact_text(
                        source_claim.get("synthesis_subtype"), limit=80
                    ),
                    "epistemic_status": epistemic_status,
                    "support_status": support_status,
                    "citation_group": citation_group,
                    "evidence_refs": evidence_refs,
                    "fact_ids": fact_ids,
                    "allowed_assertion": compact_text(
                        source_claim.get("allowed_assertion"), limit=4000
                    )
                    or " ".join(normalized_fact_values)
                    or claim_text,
                    "fact_binding_status": (
                        "explicit_fact_selection"
                        if requested_fact_ids
                        else "source_bounded_context"
                    ),
                    "assertion_ceiling": program_ceiling,
                    "ceiling_explanation": ceiling_explanation,
                    "evidence_ceiling": ceiling_explanation,
                    "semantic_constraints": list(dict.fromkeys([
                        *(source_claim.get("semantic_constraints") or []),
                        "Do not introduce uncited quantitative, causal, or mechanistic detail.",
                        "Preserve source attribution and the declared evidence ceiling.",
                    ])),
                    "coverage": coverage_report["coverage"],
                    "failed_coverage_fields": coverage_report[
                        "failed_coverage_fields"
                    ],
                }
            )
            claim_plans[-1].update(argument_projection(source_claim))
            claim_plans[-1]["required_for_section"] = source_claim.get("required_for_section", True)
            paragraph_claim_ids.append(claim_id)
            if enforce_declared_claims:
                assigned_declared_claim_ids.add(claim_id)
            paragraph_papers.extend(citation_group)
            covered_primary.update((direct_claim_papers(claim_plans[-1], evidence_by_key, fact_registry)
                                    if retrieval_mode == "lexical" else set(citation_group)) & set(primary))
        if not paragraph_claim_ids:
            continue
        argument_role = compact_text(
            raw_paragraph.get("argument_role"), limit=80
        ).casefold()
        paragraph_claim_kinds = {
            str(claim.get("claim_kind") or "")
            for claim in claim_plans
            if claim.get("claim_id") in paragraph_claim_ids
        }
        argument_role = canonical_argument_role(
            argument_role,
            claim_kinds=paragraph_claim_kinds,
            paper_count=len(set(paragraph_papers)),
            paragraph_index=paragraph_index - 1,
            paragraph_count=len(raw_paragraphs),
            section_role=role,
        )
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
                "target_words": {
                    "min": CASE_PARAGRAPH_MIN_WORDS,
                    "max": CASE_PARAGRAPH_MAX_WORDS,
                },
                "primary_papers": [
                    paper_id for paper_id in dict.fromkeys(paragraph_papers)
                    if paper_id in primary
                ],
                "supporting_papers": [
                    paper_id for paper_id in dict.fromkeys(paragraph_papers)
                    if paper_id in supporting
                ],
                "paper_ids": list(dict.fromkeys(paragraph_papers)),
                "opening_function": "Advance from the preceding analytical question.",
                "closing_function": "State the evidence boundary and next implication.",
                "reader_takeaway": compact_text(raw_paragraph.get("reader_takeaway")),
                "positive_synthesis": compact_text(raw_paragraph.get("positive_synthesis")),
                "caveat_policy": "diagnostic_only",
                "knowledge_component_refs": knowledge_refs,
                "claim_ids": paragraph_claim_ids,
            }
        )
    if not paragraph_plans and strict:
        raise RuntimeError(f"The academic planner produced no supported paragraph for {section_id}.")
    # Filtering unsupported paragraphs must not turn a surviving experiment
    # into an introduction. Missing responsibilities are repaired
    # explicitly; position alone is not proof of a paragraph's function.
    # Repairs append new paragraphs to preserve all existing claim IDs. Order
    # their presentation only after assigning IDs, without rewriting content.
    paragraph_plans.sort(key=lambda item: {
        "section_frame": 0, "section_synthesis_exit": 2,
    }.get(item["argument_role"], 1))
    missing_primary = [paper_id for paper_id in primary if paper_id not in covered_primary]
    missing_declared_claim_ids = [
        claim_id
        for claim_id in declared_by_id
        if claim_id not in assigned_declared_claim_ids and declared_by_id[claim_id].get("required_for_section", True)
    ]
    if missing_primary and strict:
        raise RuntimeError(
            f"The academic planner did not route every writeable primary paper into a supported Claim for {section_id}: "
            + ", ".join(missing_primary)
        )
    if missing_declared_claim_ids and strict:
        raise RuntimeError(
            f"The academic planner omitted registered Blueprint Claims for {section_id}: "
            + ", ".join(missing_declared_claim_ids)
        )
    synthesis_section = {
        "section_id": section_id,
        "summary": compact_text(generated.get("synthesis_summary")),
        "components": components,
        "normalization_diagnostics": {
            "rejected_claims": rejected_claims,
            "proposed_claim_count": sum(len(p.get("claims") or []) for p in raw_paragraphs),
            "accepted_claim_count": len(claim_plans),
            "missing_primary_papers": missing_primary,
            "missing_blueprint_claim_ids": missing_declared_claim_ids,
            "missing_blueprint_claims": [
                declared_by_id[claim_id]
                for claim_id in missing_declared_claim_ids
            ],
            "unsupported_components": [item["component_type"] for item in components if item["status"] != "supported"],
        },
    }
    writing_section = {
        "section_id": section_id,
        "section_role": role,
        "route": "A" if len(paragraph_plans) == 1 else "B",
        "overview_intent": compact_text(generated.get("overview_intent")),
        "paragraphs": paragraph_plans,
        "claims": claim_plans,
        "depth_contract": dict(depth_contract or {}),
    }
    writing_section["narrative_diagnostics"] = derive_narrative_diagnostics(
        writing_section,
        depth_contract,
    )
    return synthesis_section, writing_section


def validate_and_realize_section(
    *,
    section_id: str,
    generated: dict[str, Any],
    writing_section: dict[str, Any],
    evidence: list[dict[str, Any]],
    citation_map: dict[str, int],
    domain_terms: list[str] | None = None,
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
    fact_registry = registered_fact_bindings(evidence, citation_map)
    fact_keys = {str(ref["evidence_key"]) for fact in fact_registry.values() for ref in fact["evidence_refs"]}
    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_key") or "")
        and (passage_eligible(item) or str(item["evidence_key"]) in fact_keys)
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
    anchor_failures: list[str] = []
    narrowed_claims: list[dict[str, Any]] = []
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
        realized_parts: list[tuple[str, str, str]] = []
        claim_realizations: list[dict[str, Any]] = []
        paragraph_evidence: list[dict[str, Any]] = []
        paragraph_papers: list[str] = []
        for realization in realization_rows:
            claim_id = str(realization.get("claim_id") or "")
            claim_plan = claims.get(claim_id)
            if claim_plan is None or claim_plan.get("support_status") == "blocked":
                raise RuntimeError(f"The section writer referenced an unavailable Claim: {claim_id}.")
            sentence = (" ".join(str(realization.get("text") or "").split())
                        if writing_section.get("evidence_mode") == SOURCE_CONTRACT
                        else compact_text(realization.get("text"), limit=2500))
            if not sentence:
                raise RuntimeError(f"The section writer returned an empty Claim realization: {claim_id}.")
            cited = [
                paper_id for paper_id in claim_plan.get("citation_group") or []
                if paper_id in citation_map
            ]
            if not cited:
                raise RuntimeError(f"Claim {claim_id} has no resolvable citation group.")
            callout = format_citation_group(citation_map[paper_id] for paper_id in cited)
            realized_parts.append((sentence, callout, str(claim_plan.get("claim_kind") or "")))
            paragraph_papers.extend(cited)
            refs = [
                ref for ref in claim_plan.get("evidence_refs") or []
                if isinstance(ref, dict) and str(ref.get("evidence_key") or "") in evidence_by_key
            ]
            if writing_section.get("evidence_mode") == SOURCE_CONTRACT and not valid_source_claim(claim_plan, evidence_by_key, text=sentence):
                raise RuntimeError(f"Claim {claim_id} source support changed after its used-claim check.")
            if refs and claim_plan.get("assertion_ceiling"):
                expected_ceiling = claim_assertion_ceiling(
                    [evidence_by_key[str(ref["evidence_key"])] for ref in refs],
                    [fact_registry[fid] for fid in claim_plan.get("fact_ids") or [] if fid in fact_registry],
                )
                if claim_plan["assertion_ceiling"] != expected_ceiling:
                    raise RuntimeError(
                        f"Claim {claim_id} assertion ceiling does not match its selected evidence "
                        f"(expected {expected_ceiling}, got {claim_plan['assertion_ceiling']})."
                    )
            cited_evidence_texts = [
                " ".join(
                    str(value or "")
                    for value in (
                        evidence_by_key[str(ref["evidence_key"])].get("content")
                        or evidence_by_key[str(ref["evidence_key"])].get("evidence")
                        or "",
                        evidence_by_key[str(ref["evidence_key"])].get(
                            "normalized_fact_value"
                        )
                        or "",
                    )
                )
                for ref in refs
            ]
            unsupported_anchors = unsupported_realization_anchors(
                sentence,
                cited_evidence_texts,
                domain_terms=domain_terms or [],
            )
            if claim_plan.get("fact_binding_status") == "explicit_fact_selection":
                selected_ids = claim_plan.get("fact_ids") or []
                fact_issues = (["unregistered_fact_selection"] if set(selected_ids) - fact_registry.keys()
                               else fact_claim_issues(sentence, [fact_registry[fid] for fid in selected_ids],
                                                      claim_kind=str(claim_plan.get("claim_kind") or "reported_finding")))
                if fact_issues:
                    anchor_failures.append(f"Claim {claim_id} introduced unsupported evidence anchors in its selected facts: "
                                           + ", ".join(fact_issues) + ".")
            if any(unsupported_anchors.values()):
                details = "; ".join(
                    f"{key}={', '.join(values)}"
                    for key, values in unsupported_anchors.items()
                    if values
                )
                anchor_failures.append(
                    f"Claim {claim_id} introduced unsupported evidence anchors: {details}."
                )
            # Planning coverage describes the original proposed Claim.  It is
            # not an immutable verdict on a safer realization.  In particular,
            # a plan may record ``value=False`` because the proposed sentence
            # contained an unsupported number; the deterministic fallback then
            # deliberately removes that number.  Carrying the old coverage map
            # into the realization check made the repaired sentence fail
            # forever, so one defective Claim could prevent an otherwise valid
            # section from ever completing.  Recompute semantic/value coverage
            # from the exact realized sentence while still enforcing immutable
            # fact IDs, evidence references, and paper identity below.
            planned_coverage = dict(claim_plan.get("coverage") or {})
            planned_failed_coverage_fields = list(
                claim_plan.get("failed_coverage_fields") or []
            )
            realization_coverage = claim_support_coverage(
                {
                    **claim_plan,
                    "coverage": {},
                    "proposition": sentence,
                    "paper_ids": cited,
                },
                evidence_texts=cited_evidence_texts,
                available_fact_ids=claim_plan.get("fact_ids") or [],
                evidence_paper_ids=[
                    evidence_by_key[str(ref["evidence_key"])].get("paper_id")
                    for ref in refs
                ],
                domain_terms=domain_terms or [],
            )
            hard_coverage_failures = [
                value
                for value in realization_coverage["failed_coverage_fields"]
                if value in {"paper_identity", "fact_ids", "value"}
            ]
            if hard_coverage_failures:
                anchor_failures.append(
                    f"Claim {claim_id} failed coverage fields: "
                    + ", ".join(hard_coverage_failures)
                    + "."
                )
            elif planned_failed_coverage_fields:
                narrowed_claims.append(
                    {
                        "claim_id": claim_id,
                        "planned_failed_coverage_fields": planned_failed_coverage_fields,
                        "realized_as_supported_boundary": sentence,
                    }
                )
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
                    "fact_ids": list(claim_plan.get("fact_ids") or []),
                    "claim_kind": claim_plan.get("claim_kind"),
                    "source_verification": claim_plan.get("source_verification"),
                    "result_context": claim_plan.get("result_context") or [],
                    "support_status": realization_coverage["support_status"],
                    "coverage": realization_coverage["coverage"],
                    "planned_coverage": planned_coverage,
                    "claim_revision": claim_plan.get("claim_revision", 1),
                    "argument_basis": claim_plan.get("argument_basis"),
                    "planned_failed_coverage_fields": planned_failed_coverage_fields,
                    "failed_coverage_fields": realization_coverage[
                        "failed_coverage_fields"
                    ],
                }
            )
            all_realized_claims.add(claim_id)
        paragraph_text = render_paragraph_citations(paragraph_parts(paragraph_plan,
            dict(zip(expected_claims, realized_parts))))
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
    if narrowed_claims:
        issues.append(
            {
                "type": "planned_claim_scope_narrowed",
                "severity": "warning",
                "reason": (
                    "One or more proposed Claims exceeded their cited evidence; "
                    "the realized prose was narrowed to a source-supported boundary."
                ),
                "claims": narrowed_claims,
            }
        )
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
    if not overview and writing_section.get("evidence_mode") != SOURCE_CONTRACT:
        raise RuntimeError(f"The section writer did not produce an overview for {section_id}.")
    overview_anchors = unsupported_realization_anchors(
        overview,
        [
            " ".join(
                str(value or "")
                for value in (
                    item.get("content") or item.get("evidence") or "",
                    item.get("normalized_fact_value") or "",
                )
            )
            for item in evidence_by_key.values()
        ],
        domain_terms=domain_terms or [],
    )
    if any(overview_anchors.values()):
        details = "; ".join(
            f"{key}={', '.join(values)}"
            for key, values in overview_anchors.items()
            if values
        )
        anchor_failures.append(
            f"Section overview introduced unsupported evidence anchors: {details}."
        )
    if anchor_failures:
        raise RuntimeError(" ".join(anchor_failures))
    return overview, paragraphs, validations, reviews




def recover_evidence_section(task, package, evidence, citation_map, reason, declared_claims=None):
    """A failed authoring attempt must not turn source quotations into a manuscript."""
    sid = task["section_id"]
    record = resolution_record(package, pending=True, reason=reason)
    output = {"section_id": sid, "heading": task.get("heading") or sid,
        "section_role": task.get("section_role") or "body",
        "generation_mode": record["status"], "evidence_resolution": record,
        "section_readiness": {"status": record["status"]}, "overview": "",
        "paragraphs": [], "draft_md": pending_markdown(sid, task.get("heading") or sid),
        "validations": [], "reviews": [],
        "primary_papers": task.get("primary_papers") or [],
        "supporting_papers": task.get("supporting_papers") or []}
    return {"heading": output["heading"], "output": output,
        "synthesis": {"section_id": sid, "components": [], "evidence_resolution": record},
        "writing": {"section_id": sid, "paragraphs": [], "claims": []}}






def main() -> int:
    parser = argparse.ArgumentParser(description="Generate review sections with an OpenAI-compatible writing model.")
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--wire-api", default="")
    parser.add_argument("--audit-mode", choices=("full", "selective"), default=None)
    parser.add_argument("--section-concurrency", "--max-new-sections", dest="section_concurrency", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.review_root).resolve()
    dotenv = load_dotenv(root)
    # Full review remains the deployment baseline until isolated quality acceptance.
    audit_mode = args.audit_mode or os.environ.get("REVIEW_SECTION_AUDIT_MODE") or dotenv.get("REVIEW_SECTION_AUDIT_MODE") or "full"
    if audit_mode not in {"full", "selective"}:
        raise SystemExit("REVIEW_SECTION_AUDIT_MODE must be full or selective.")
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
    matrix = read_json(project / "01_matrix_outline" / "literature_matrix.json")
    blueprint = read_json(project / "01_matrix_outline" / "section_blueprint.json")
    # Compatibility only: standalone conclusions are composed in Draft.
    tasks = [t for t in read_json(stage / "section_tasks.json")
             if str(t.get("section_role") or "").casefold() != "conclusion"]
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
    task_order = {section_id: index for index, section_id in enumerate(task_ids)}
    checkpoint_path = stage / "section_checkpoints.json"
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    selected_outline_path = project / "01_matrix_outline" / "selected_outline.md"
    selected_outline = selected_outline_path.read_text(encoding="utf-8", errors="ignore")[:12000] if selected_outline_path.exists() else ""
    rules = load_blueprint_rule_pack(root, blueprint)
    synthesis_rules = load_cross_study_synthesis_skill()
    section_signatures = section_input_fingerprints(tasks, evidence_sections, matrix, blueprint, {
        "model": model, "base_url": base_url, "wire_api": wire_api,
        "authoring_version": AUTHORING_VERSION, "audit_mode": audit_mode, "fact_routing_contract": FACT_ROUTING_CONTRACT,
        "rules": rules, "synthesis_rules": synthesis_rules, "outline": selected_outline})
    generation_fingerprint = hashlib.sha256(json.dumps({
        "contract": "section-authoring/3", "fact_routing_contract": FACT_ROUTING_CONTRACT,
        "rule_pack_prompt_version": RULE_PACK_PROMPT_VERSION,
        "source_writing_contract": SOURCE_CONTRACT,
        "authoring_version": AUTHORING_VERSION, "audit_mode": audit_mode,
        "tasks": tasks, "evidence": evidence_package,
        "matrix": matrix, "blueprint": blueprint, "model": model,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    checkpoint_entries = (
        checkpoint.get("entries")
        if isinstance(checkpoint, dict)
        and checkpoint.get("project_id") == args.project_id
        else {}
    )
    if not isinstance(checkpoint_entries, dict):
        checkpoint_entries = {}
    checkpoint_entries, input_rejections = matching_section_inputs(checkpoint_entries, section_signatures,
        legacy_validated=checkpoint.get("generation_fingerprint") == generation_fingerprint
            or checkpoint.get("resume_validated") is True)
    checkpoint_entries, rejected_checkpoints = reusable_section_entries(
        checkpoint_entries, tasks, evidence_sections
    )
    rejected_checkpoints = {**input_rejections, **rejected_checkpoints}
    for sid, entry in checkpoint_entries.items():
        entry["input_fingerprint"] = section_signatures[sid]
    authoring_states = {sid: value for sid, value in (checkpoint.get("authoring_states") or {}).items()
        if checkpoint.get("project_id") == args.project_id and isinstance(value, dict)
        and value.get("section_input") == section_signatures.get(sid)}
    failed_progress: list[dict[str, Any]] = [
        dict(row) for row in checkpoint.get("failed_sections") or []
        if isinstance(row, dict) and str(row.get("section_id") or "") in task_ids
    ]
    # An unbounded CLI invocation is a new explicit attempt. Bounded Worker
    # leases retain failures until the coordinator decides whether to retry.
    if not args.section_concurrency:
        failed_progress = []
    checkpoint_lock = RLock()
    def persist_checkpoint():
        with checkpoint_lock:
            write_section_checkpoint(stage, {"schema_version": 1, "project_id": args.project_id,
                "task_ids": task_ids, "generation_fingerprint": generation_fingerprint,
                "entries": checkpoint_entries, "rejected_entries": rejected_checkpoints,
                "authoring_states": authoring_states,
                "failed_sections": failed_progress,
            })
    def persist_authoring(sid, value):
        with checkpoint_lock:
            authoring_states[sid] = {"section_input": section_signatures[sid], "state": value}
            persist_checkpoint()
    # Persist partial drafting/review state in the same checkpoint, not another store.
    persist_checkpoint()
    completed_progress: list[dict[str, Any]] = [
        {
            "section_id": section_id,
            "heading": str((checkpoint_entries.get(section_id) or {}).get("heading") or section_id),
            "generation_mode": str(
                ((checkpoint_entries.get(section_id) or {}).get("output") or {}).get(
                    "generation_mode"
                )
                or "standard"
            ),
            "section_readiness": dict(
                ((checkpoint_entries.get(section_id) or {}).get("output") or {}).get(
                    "section_readiness"
                )
                or {}
            ),
        }
        for section_id in task_ids
        if isinstance(checkpoint_entries.get(section_id), dict)
    ]
    write_generation_progress(
        stage,
        current=len(completed_progress),
        total=progress_total,
        phase="preparing",
        completed_sections=completed_progress,
    )
    rows_list = matrix.get("rows") if isinstance(matrix, dict) else matrix
    rows = {str(row.get("paper_id")): row for row in rows_list or [] if isinstance(row, dict) and row.get("paper_id")}
    writing_scope_contract = derive_writing_scope_contract(
        blueprint.get("scope_contract")
    )
    supplied_writing_scope = blueprint.get("writing_scope_contract")
    if isinstance(supplied_writing_scope, dict):
        supplied_fingerprint = str(
            supplied_writing_scope.get("fingerprint") or ""
        )
        if (
            supplied_fingerprint
            and supplied_fingerprint != writing_scope_contract["fingerprint"]
        ):
            raise RuntimeError(
                "The executable Writing Scope does not match Blueprint.scope_contract."
            )
    # Taxonomy aliases classify papers and outline partitions; they are not a
    # claim-level named-entity registry.  Treating broad aliases such as
    # ``iodide`` or ``computational`` as hard evidence anchors creates false
    # rejections, so the integrity gate uses formula and quantitative anchors
    # derived from the realized sentence itself.
    domain_terms: list[str] = []
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

    responsibilities = chapter_responsibilities(tasks)

    def generate(task, emit):
        def failure(section_id, heading, error, *, evidence_failure=False):
            if evidence_failure:
                entry = recover_evidence_section(task, section_evidence, evidence, citation_map, error, [])
                entry["input_fingerprint"] = section_signatures[section_id]
                return entry
            gateway_details = error.details if isinstance(error, GatewayRequestError) else {}
            rate_limited = isinstance(error, GatewayRequestError) and (
                error.status_code == 429 or gateway_details.get("provider_status") == 429
            )
            return {"error": str(error)[:2000], "retryable_rate_limit": rate_limited}
        section_id = str(task.get("section_id"))
        role = str(task.get("section_role") or "body").strip().casefold()
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
        allowed = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("allowed_papers", [*assigned_primary, *supporting])
                if str(pid) in rows
            )
        )
        section_evidence = evidence_sections.get(section_id, {})
        scientific_claim_states = [
            dict(item)
            for item in section_evidence.get("scientific_claim_states") or []
            if isinstance(item, dict)
        ]
        primary = list(
            dict.fromkeys(
                str(pid)
                for pid in required_primary_papers(task, section_evidence)
                if str(pid) in assigned_primary
            )
        )
        unresolved_primary = [
            str(pid)
            for pid in section_evidence.get("unresolved_primary_papers") or []
            if str(pid) in assigned_primary
        ]
        retrieval_mode = effective_retrieval_mode(section_evidence)
        if retrieval_mode == "lexical":
            evidence = [
                item
                for item in section_evidence.get("hits") or []
                if isinstance(item, dict)
                and str(item.get("paper_id") or "") in allowed
                and str(item.get("chunk_id") or "")
            ]
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
                f"Unsupported section retrieval mode for {section_id}: {section_evidence.get('retrieval_mode')}."
                if retrieval_mode == "unsupported_retrieval_mode" else
                f"No usable indexed evidence for {section_id}."
                if retrieval_mode in {"lexical", "insufficient_evidence"}
                else f"No usable MinerU Markdown or matrix evidence for {section_id}."
            )
            return failure(
                section_id, str(task.get("heading") or section_id), message,
                evidence_failure=(retrieval_mode != "unsupported_retrieval_mode"
                                  and section_evidence.get("source_lookup_complete", False))
            )
        spec = section_specs.get(section_id, {})
        depth_contract = resolve_section_depth_contract({**spec, "depth_contract": task.get("depth_contract") or spec.get("depth_contract") or {}})
        # Keep the compact, source-bound fact bindings beside their original
        # passages. ``write_from_sources`` exposes only the safe fact fields;
        # the full Evidence Package remains the authoritative registry.
        plan_evidence, plan_evidence_budget = bounded_evidence_payload(
            evidence, char_budget=55_000
        )
        plan_recovery = {}
        fallback_reason = ""
        generation_mode = "standard"
        try:
            def source_call(prompt, schema, label):
                emit("drafting" if label == "section-source-writing" else "reviewing")
                return call_structured_llm(prompt, schema, api_key, base_url, model, wire_api,
                                           label=label, schema_name=label.replace("-", "_"))
            writing_section, generated_draft, source_review = write_from_sources(
                section_id=section_id, task=task, evidence=evidence, prompt_evidence=plan_evidence, domain_terms=domain_terms, responsibilities=responsibilities,
                audit_mode=audit_mode, resume_state=deepcopy(authoring_states.get(section_id, {}).get("state")),
                save_state=lambda value: persist_authoring(section_id, value),
                context=("Topic: " + str(blueprint.get("review_topic") or project.name) + "\n"
                    + writing_scope_prompt_block(writing_scope_contract, stage="drafting") + "\n"
                    + section_constraint_prompt_block(task) + "\nConfirmed outline:\n" + selected_outline
                    + "\nWriting rules:\n" + rules + "\n" + synthesis_rules
                    + "\nPaper contribution guides (navigation, not verified conclusions):\n"
                    + json.dumps({pid: contribution_context(rows.get(pid, {}).get("paper_analysis"), limit=300)
                                  for pid in task.get("allowed_papers") or []}, ensure_ascii=False)
                    + "\nCompleted dependencies (context, not additional source evidence):\n" + json.dumps(task.get("dependency_context", []), ensure_ascii=False)), call=source_call)
            if source_review.get("unresolved"):
                return failure(section_id, str(task.get("heading") or section_id),
                    "Source checking is incomplete. Draft and check state were preserved; retry this section after reviewing its evidence.")
            if not writing_section["paragraphs"]:
                malformed = any(row.get("reason") in {
                    "invalid_paragraph", "invalid_claim", "missing_or_invalid_source_span", "invalid_result_context"
                } for row in source_review.get("omitted") or [])
                return failure(section_id, str(task.get("heading") or section_id),
                    "Source response needs repair." if malformed else
                    "No source-supported prose remained after checking the actual claims.", evidence_failure=not malformed)
            overview, paragraphs, validations, reviews = validate_and_realize_section(
                section_id=section_id, generated=generated_draft, writing_section=writing_section,
                evidence=evidence, citation_map=citation_map, domain_terms=domain_terms)
            # Fingerprints bind full registered passages, not truncated prompt
            # copies. Bounded payloads keep full text on selected rows.
            synthesis_section = {"section_id": section_id, "evidence_mode": SOURCE_CONTRACT,
                "components": [], "source_review": source_review,
                "comparison_table": {"cells": [record for c in writing_section["claims"] for record in c.get("result_context") or []]},
                "prompt_evidence_budget": plan_evidence_budget}
            missing = missing_primary_papers(primary, paragraphs, require_evidence=retrieval_mode == "lexical", source_evidence=evidence)
            if source_review["omitted"] or source_review.get("unresolved") or missing or unresolved_primary:
                generation_mode = "limited_evidence"
                fallback_reason = "Unsupported statements were omitted; unanswered questions remain pending."
            elif source_review["narrowed"]:
                generation_mode = "evidence_repaired"
            validations.append({"rule_id": "section.used_claim_source_check", "status": "pass_with_warning"
                if generation_mode != "standard" else "pass", **source_review})
        except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            return failure(section_id, str(task.get("heading") or section_id),
                exc)
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
        actual_word_count = manuscript_word_count(
            " ".join(
                [overview, *(str(item.get("text") or "") for item in paragraphs)]
            )
        )
        minimum_word_count = int(
            depth_contract.get("target_word_min")
            or target_word_floor(spec.get("target_words"))
            or 0
        )
        depth_sufficient = bool(
            not minimum_word_count or actual_word_count >= minimum_word_count * 0.9
        )
        planning_repair = (
            dict(synthesis_section.get("planning_contract_repair") or {})
            if isinstance(synthesis_section, dict)
            else {}
        )
        structure_gaps = list(planning_repair.get("remaining_gaps") or [])
        narrative_diagnostics = derive_narrative_diagnostics(
            writing_section,
            depth_contract,
        )
        structure_gaps.extend(
            f"narrative_{value}"
            for value in narrative_diagnostics.get("missing_requirements") or []
        )
        structure_gaps = list(dict.fromkeys(structure_gaps))
        section_readiness = derive_section_readiness(
            generation_mode=generation_mode,
            required_claim_states=scientific_claim_states,
            structure_gaps=structure_gaps,
            depth_sufficient=depth_sufficient,
        )
        depth_diagnostics = {
            "actual_word_count": actual_word_count,
            "minimum_word_count": minimum_word_count,
            "sufficient": depth_sufficient,
            "derived_from": "section_text_and_blueprint_depth_contract",
            "target_word_max": int(depth_contract.get("target_word_max") or 0),
            "target_paragraph_count": int(
                depth_contract.get("target_paragraph_count") or 0
            ),
        }
        presentation = paper_presentation_outcomes(task, {"section_id": section_id,
            "section_role": role, "primary_papers": primary, "paragraphs": paragraphs}, rows, citation_map)
        output = {
                "section_id": section_id,
                "heading": task.get("heading"),
                "section_role": role,
                "writing_mode": task.get("writing_mode"),
                "paper_presentation": presentation,
                "generation_mode": generation_mode,
                "plan_recovery": plan_recovery,
                "section_readiness": section_readiness,
                "depth_diagnostics": depth_diagnostics,
                "narrative_diagnostics": narrative_diagnostics,
                "scientific_claim_states": scientific_claim_states,
                "fallback_reason": fallback_reason if generation_mode == "safe_evidence_fallback" else "",
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
        if generation_mode == "limited_evidence":
            record = resolution_record(section_evidence, pending=False, reason=fallback_reason)
            output["evidence_resolution"] = record
            output["section_readiness"] = {"status": "limited_evidence"}
            synthesis_section["evidence_resolution"] = record
        return {"input_fingerprint": section_signatures[section_id],
                "heading": str(task.get("heading") or section_id), "output": output,
                "synthesis": synthesis_section, "writing": writing_section}

    active = {}
    def observe(task, phase):
        sid = task["section_id"]
        active[sid] = {"section_id": sid, "heading": task.get("heading", sid), "phase": phase}
        progress()

    def progress():
        write_generation_progress(stage, current=len(completed_progress), total=progress_total,
            phase="generating" if active else "finalizing", active_sections=list(active.values()),
            completed_sections=completed_progress, failed_sections=failed_progress)

    def save(task, entry):
        sid = task["section_id"]
        active.pop(sid, None)
        if "error" in entry:
            failed_progress.append({"section_id": sid, "heading": task.get("heading", sid),
                                    "error": entry["error"],
                                    "retryable_rate_limit": bool(entry.get("retryable_rate_limit"))})
            persist_checkpoint()
        else:
            with checkpoint_lock:
                checkpoint_entries[sid] = entry
                authoring_states.pop(sid, None)
                persist_checkpoint()
            output_sections.append(entry["output"])
            synthesis_sections.append(entry["synthesis"])
            writing_sections.append(entry["writing"])
            (sections_dir / f"{sid}.md").write_text(entry["output"]["draft_md"], encoding="utf-8")
            completed_progress.append({"section_id": sid, "heading": entry["heading"],
                "generation_mode": entry["output"].get("generation_mode"),
                "section_readiness": entry["output"].get("section_readiness")})
        progress()

    run_sections(
        tasks, generate, observe, save, completed=checkpoint_entries,
        deferred={row["section_id"] for row in failed_progress},
        concurrency=args.section_concurrency or 2,
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
    # Resumed chapters finish after cached ones; completion order must never
    # replace Blueprint order in the manuscript or downstream evidence bundle.
    for sections in (output_sections, synthesis_sections, writing_sections, completed_progress):
        sections.sort(key=lambda row: task_order[row["section_id"]])
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
    narrative_by_section = {
        str(section.get("section_id") or ""): dict(
            section.get("narrative_diagnostics")
            or derive_narrative_diagnostics(
                section,
                section.get("depth_contract") if isinstance(section.get("depth_contract"), dict) else {},
            )
        )
        for section in writing_sections
        if str(section.get("section_id") or "")
    }
    synthesis_diagnostics = {
        "schema_version": 1,
        "comparison_paragraph_count": len(comparison_paragraphs),
        "planned_paragraph_count": len(comparable_paragraphs),
        "comparison_coverage": round(
            len(comparison_paragraphs) / max(1, len(comparable_paragraphs)), 4
        ),
        "narrative_complete_section_count": sum(
            1
            for diagnostic in narrative_by_section.values()
            if str(diagnostic.get("status") or "") == "complete"
        ),
        "narrative_shallow_section_ids": [
            section_id
            for section_id, diagnostic in narrative_by_section.items()
            if str(diagnostic.get("status") or "") != "complete"
        ],
        "section_narrative_diagnostics": narrative_by_section,
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
            "planning_mode": "evidence_first_source_writing",
            "evidence_mode": SOURCE_CONTRACT,
            "source_evidence_registry": "sections/evidence_package.json",
            "writing_scope_contract": writing_scope_contract,
            "writing_scope_contract_fingerprint": writing_scope_contract[
                "fingerprint"
            ],
            "provenance": {
                "writing_scope_contract_source": "blueprint.scope_contract",
                "writing_scope_contract_fingerprint": writing_scope_contract[
                    "fingerprint"
                ],
            },
            "synthesis_diagnostics": synthesis_diagnostics,
            "sections": synthesis_sections,
        },
    )
    write_json(
        stage / "writing_plan.json",
        {
            "schema_version": 1,
            "project_id": args.project_id,
            "planning_mode": "evidence_first_source_writing",
            "evidence_mode": SOURCE_CONTRACT,
            "source_evidence_registry": "sections/evidence_package.json",
            "writing_scope_contract": writing_scope_contract,
            "writing_scope_contract_fingerprint": writing_scope_contract[
                "fingerprint"
            ],
            "provenance": {
                "writing_scope_contract_source": "blueprint.scope_contract",
                "writing_scope_contract_fingerprint": writing_scope_contract[
                    "fingerprint"
                ],
            },
            "sections": writing_sections,
        },
    )
    write_json(stage / "section_drafts.json", {"project_id": args.project_id, "sections": output_sections})
    (stage / "section_drafts.md").write_text("\n\n".join(section["draft_md"] for section in output_sections), encoding="utf-8")
    generation_counts = {
        mode: sum(
            1 for section in output_sections
            if str(section.get("generation_mode") or "standard") == mode
        )
        for mode in ("standard", "evidence_repaired", "safe_evidence_fallback")
    }
    (stage / "section_drafting_report.md").write_text(
        "# Section Drafting Report\n\n"
        + f"Generated {len(output_sections)} source-grounded sections with evidence-first Synthesis, Paragraph, Claim/Citation, realization, and deterministic review contracts using model `{model}`.\n\n"
        + f"- Standard generation: {generation_counts['standard']}\n"
        + f"- Evidence-repaired generation: {generation_counts['evidence_repaired']}\n"
        + f"- Safe evidence fallback: {generation_counts['safe_evidence_fallback']}\n",
        encoding="utf-8",
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
