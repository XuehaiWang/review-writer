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
from review_writer_core.academic_contracts import mechanism_evidence_types  # noqa: E402
from review_writer_core.evidence_integrity import (  # noqa: E402
    unsupported_realization_anchors,
)
from review_writer_core.writing_contracts import (  # noqa: E402
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    derive_writing_scope_contract,
)
from review_writer_core.model_gateway_client import (  # noqa: E402
    call_json_model as call_gateway_json,
    gateway_configured,
    parse_json_object_text as _parse_json_object_text,
)
from review_writer_core.review_fact_readiness import (  # noqa: E402
    negative_claim_eligibility,
)
from review_writer_core.claim_contracts import (  # noqa: E402
    claim_support_coverage,
    derive_section_readiness,
)
from review_writer_core.section_narrative_contracts import (  # noqa: E402
    CANONICAL_PARAGRAPH_ROLES,
    canonical_argument_role,
    derive_narrative_diagnostics,
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


def writing_scope_prompt_block(
    writing_scope_contract: dict[str, Any], *, stage: str
) -> str:
    """Render the same compact Scope as a binding plan/draft instruction."""

    if str(writing_scope_contract.get("status") or "") != "active":
        return (
            "Writing Scope contract: unavailable in this legacy Blueprint. "
            "Do not infer a broader review scope from missing fields."
        )
    stage_instruction = (
        "Give this section a distinct responsibility that advances the central question "
        "and review objective. Use the primary navigation axis for organization, use "
        "secondary axes only for explicit comparison, and plan reader takeaways for the "
        "declared audience."
        if stage == "planning"
        else
        "Realize only the approved section responsibility and Claims within this Scope. "
        "Do not broaden the time window, corpus coverage, inclusion rules, organizing "
        "axes, audience, or evidence ceiling while turning the plan into prose."
    )
    return (
        "Executable Writing Scope (binding for this call; missing values are boundaries, "
        "not permission to infer them):\n"
        + json.dumps(writing_scope_contract, ensure_ascii=False, sort_keys=True)
        + "\nScope application rule: "
        + stage_instruction
        + " Apply the inclusion/exclusion, time, coverage, and evidence-availability "
        "policies exactly; never imply exhaustive field coverage when the contract is "
        "locally bounded."
    )


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
    """Bound model input while retaining evidence and paper identities."""

    rows = [item for item in evidence if isinstance(item, dict)]
    per_content = max(320, min(1800, char_budget // max(1, len(rows)) - 360))
    compacted: list[dict[str, Any]] = []
    for row in rows:
        content = compact_text(row.get("content") or row.get("evidence"), limit=per_content)
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
                "fact_ids": list(row.get("fact_ids") or []),
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
    return compacted, {
        "input_hit_count": len(rows),
        "output_hit_count": len(compacted),
        "content_chars_per_hit": per_content,
        "content_characters": sum(
            len(str(item.get("content") or "")) for item in compacted
        ),
        "char_budget": char_budget,
        "compacted": any(
            len(str(row.get("content") or row.get("evidence") or "")) > per_content
            for row in rows
        ),
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

    mode = str(section_evidence.get("retrieval_mode") or "insufficient_evidence")
    if mode == "fixed_prefix_fallback" and not bool(
        section_evidence.get("legacy_fallback_authorized")
    ):
        return "insufficient_evidence"
    return mode


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
                    "fact_ids": list(
                        dict.fromkeys(
                            str(fact_id)
                            for fact_id in [
                                fact.get("fact_id"),
                                *(fact.get("fact_ids") or []),
                            ]
                            if str(fact_id)
                        )
                    ),
                    "assertion_ceiling": fact.get("assertion_ceiling"),
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
    gaps: list[str] = []
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


def ensure_evidence_bound_comparison_plan(
    writing_section: dict[str, Any],
    synthesis_section: dict[str, Any],
    comparison_table: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> bool:
    """Add one conservative comparison when the model omitted an available one.

    This fallback uses only source-addressable Matrix cells whose evidence keys
    are present in the current section evidence registry. It never fills a
    missing cell or turns a retrieval miss into a negative scientific claim.
    """

    existing = [
        claim
        for claim in writing_section.get("claims") or []
        if isinstance(claim, dict)
        and str(claim.get("claim_kind") or "")
        in {"cross_study_comparison", "review_synthesis"}
        and len(set(claim.get("citation_group") or [])) >= 2
    ]
    if existing:
        return False
    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict)
        and str(item.get("evidence_key") or "")
        and bool(item.get("claim_eligible", True))
    }
    chosen_field = ""
    chosen_cells: list[tuple[dict[str, Any], str]] = []
    for field_id in comparison_table.get("comparable_fields") or []:
        candidates: list[tuple[dict[str, Any], str]] = []
        seen_papers: set[str] = set()
        for cell in comparison_table.get("cells") or []:
            if not isinstance(cell, dict) or str(cell.get("field_id") or "") != str(field_id):
                continue
            paper_id = str(cell.get("paper_id") or "")
            if not paper_id or paper_id in seen_papers:
                continue
            key = next(
                (
                    str(ref.get("evidence_key") or "")
                    for ref in cell.get("evidence_refs") or []
                    if isinstance(ref, dict)
                    and str(ref.get("evidence_key") or "") in evidence_by_key
                ),
                "",
            )
            if key:
                candidates.append((cell, key))
                seen_papers.add(paper_id)
            if len(candidates) >= 2:
                break
        if len(candidates) >= 2:
            chosen_field = str(field_id)
            chosen_cells = candidates[:2]
            break
    if len(chosen_cells) < 2:
        return False

    section_id = str(writing_section.get("section_id") or "S00")
    paragraph_number = len(
        [row for row in writing_section.get("paragraphs") or [] if isinstance(row, dict)]
    ) + 1
    paragraph_id = f"{section_id}-p{paragraph_number}"
    claim_id = f"{paragraph_id}-C01"
    papers = [str(cell.get("paper_id") or "") for cell, _key in chosen_cells]
    keys = [key for _cell, key in chosen_cells]
    fact_ids = list(
        dict.fromkeys(
            str(fact_id)
            for cell, _key in chosen_cells
            for fact_id in cell.get("fact_ids") or []
            if str(fact_id)
        )
    )
    values = [compact_text(cell.get("value"), limit=800) for cell, _key in chosen_cells]
    ceiling_order = {
        "context_only": 0,
        "abstract_report_only": 1,
        "attributed_author_interpretation": 2,
        "direct_report_with_local_context": 3,
        "direct_source_report": 4,
    }
    ceilings = [
        str(evidence_by_key[key].get("assertion_ceiling") or "context_only")
        for key in keys
    ]
    assertion_ceiling = min(
        ceilings,
        key=lambda value: ceiling_order.get(value, 0),
        default="context_only",
    )
    writing_section.setdefault("claims", []).append(
        {
            "claim_id": claim_id,
            "paragraph_id": paragraph_id,
            "sequence": 1,
            "claim": (
                f"The source-reported {chosen_field.replace('_', ' ')} differs "
                "across the compared studies under their respective reported contexts."
            ),
            "claim_kind": "cross_study_comparison",
            "synthesis_subtype": "descriptive_source_bounded_comparison",
            "epistemic_status": "direct_source_report",
            "support_status": "supported",
            "citation_group": papers,
            "evidence_refs": [
                {
                    "evidence_id": evidence_by_key[key].get("evidence_id"),
                    "evidence_key": key,
                    "relationship": "supports",
                }
                for key in keys
            ],
            "fact_ids": fact_ids,
            "allowed_assertion": " | ".join(values),
            "assertion_ceiling": assertion_ceiling,
            "ceiling_explanation": (
                "Compare only the two source-reported values and retain their "
                "study-specific contexts; do not infer a universal ranking."
            ),
            "evidence_ceiling": "Descriptive cross-study comparison only.",
            "semantic_constraints": [
                "Do not generalize beyond the two cited source contexts.",
                "Do not fill missing comparison cells or infer causality.",
            ],
            **claim_support_coverage(
                {
                    "proposition": (
                        f"The source-reported {chosen_field.replace('_', ' ')} differs "
                        "across the compared studies under their respective reported contexts."
                    ),
                    "paper_ids": papers,
                    "fact_ids": fact_ids,
                    "evidence_refs": [
                        {"evidence_key": key, "paper_id": paper_id}
                        for key, paper_id in zip(keys, papers)
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
                evidence_paper_ids=papers,
            ),
        }
    )
    comparison_paragraph = {
            "paragraph_id": paragraph_id,
            "theme": f"Source-bounded comparison of {chosen_field.replace('_', ' ')}",
            "argument_role": "cross_study_comparison",
            "objective": "Contrast two directly source-addressable reports without ranking them universally.",
            "target_words": {
                "min": CASE_PARAGRAPH_MIN_WORDS,
                "max": CASE_PARAGRAPH_MAX_WORDS,
            },
            "primary_papers": papers,
            "supporting_papers": [],
            "paper_ids": papers,
            "opening_function": "Introduce the shared comparison field.",
            "closing_function": "State the study-specific evidence boundary.",
            "reader_takeaway": "The studies differ on a comparable reported field, but the contexts remain distinct.",
            "positive_synthesis": "A direct source-bounded comparison is available.",
            "caveat_policy": "diagnostic_only",
            "knowledge_component_refs": [],
            "claim_ids": [claim_id],
        }
    paragraph_rows = writing_section.setdefault("paragraphs", [])
    insert_at = (
        len(paragraph_rows) - 1
        if paragraph_rows
        and str(paragraph_rows[-1].get("argument_role") or "")
        == "section_synthesis_exit"
        else len(paragraph_rows)
    )
    paragraph_rows.insert(insert_at, comparison_paragraph)
    components = synthesis_section.setdefault("components", [])
    comparison_component = next(
        (
            row
            for row in components
            if isinstance(row, dict)
            and str(row.get("component_type") or "") == "comparison"
        ),
        None,
    )
    if comparison_component is None:
        comparison_component = {
            "component_id": f"{section_id}-comparison-{len(components) + 1:02d}",
            "component_type": "comparison",
            "necessity": "required",
            "purpose": "Compare source-addressable study fields.",
            "provenance": "deterministic_evidence_bound_fallback",
        }
        components.append(comparison_component)
    comparison_component.update(
        {
            "status": "supported",
            "summary": f"Compared {chosen_field.replace('_', ' ')} across two source reports.",
            "evidence_keys": keys,
            "provenance": "deterministic_evidence_bound_fallback",
        }
    )
    return True


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
    depth_contract: dict[str, Any] | None = None,
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
    assertion_ceiling_rank = {
        "context_only": 0,
        "abstract_report_only": 1,
        "attributed_author_interpretation": 2,
        "direct_report_with_local_context": 3,
        "direct_source_report": 4,
    }

    def claim_assertion_ceiling(keys: list[str]) -> str:
        ceilings = [
            str(evidence_by_key[key].get("assertion_ceiling") or "direct_source_report")
            for key in keys
        ]
        return min(
            ceilings,
            key=lambda value: assertion_ceiling_rank.get(value, 0),
            default="context_only",
        )
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
    raw_paragraphs = [
        item
        for item in (generated.get("paragraphs") or [])[:8]
        if isinstance(item, dict)
    ]
    for paragraph_index, raw_paragraph in enumerate(raw_paragraphs, start=1):
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
            program_ceiling = claim_assertion_ceiling(keys)
            ceiling_explanation = compact_text(
                raw_claim.get("evidence_ceiling")
                or " ".join(
                    str(evidence_by_key[key].get("evidence_ceiling") or "")
                    for key in keys
                )
                or "Do not generalize beyond the cited source evidence."
            )
            coverage_report = claim_support_coverage(
                {
                    "proposition": claim_text,
                    "paper_ids": citation_group,
                    "fact_ids": fact_ids,
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
                        raw_claim.get("synthesis_subtype"), limit=80
                    ),
                    "epistemic_status": epistemic_status,
                    "support_status": support_status,
                    "citation_group": citation_group,
                    "evidence_refs": evidence_refs,
                    "fact_ids": fact_ids,
                    "allowed_assertion": " ".join(normalized_fact_values)
                    or claim_text,
                    "assertion_ceiling": program_ceiling,
                    "ceiling_explanation": ceiling_explanation,
                    "evidence_ceiling": ceiling_explanation,
                    "semantic_constraints": [
                        "Do not introduce uncited quantitative, causal, or mechanistic detail.",
                        "Preserve source attribution and the declared evidence ceiling.",
                    ],
                    "coverage": coverage_report["coverage"],
                    "failed_coverage_fields": coverage_report[
                        "failed_coverage_fields"
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
    if not paragraph_plans:
        raise RuntimeError(f"The academic planner produced no supported paragraph for {section_id}.")
    # The first and final responsibilities are structural properties of the
    # plan, not scientific assertions. Normalize their labels deterministically
    # so later validation can distinguish framing from evidence synthesis.
    if role == "body":
        paragraph_plans[0]["argument_role"] = "section_frame"
    if (
        (depth_contract or {}).get("requires_section_synthesis_exit")
        and len(paragraph_plans) > 1
    ):
        paragraph_plans[-1]["argument_role"] = "section_synthesis_exit"
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
        "depth_contract": dict(depth_contract or {}),
    }
    writing_section["narrative_diagnostics"] = derive_narrative_diagnostics(
        writing_section,
        depth_contract,
    )
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
                    "fact_ids": list(claim.get("fact_ids") or []),
                    "allowed_assertion": compact_text(
                        claim.get("allowed_assertion")
                    ),
                    "assertion_ceiling": str(
                        claim.get("assertion_ceiling") or "context_only"
                    ),
                    "ceiling_explanation": compact_text(
                        claim.get("ceiling_explanation")
                    ),
                    "evidence_ceiling": compact_text(claim.get("evidence_ceiling")),
                    "coverage": dict(claim.get("coverage") or {}),
                    "failed_coverage_fields": list(
                        claim.get("failed_coverage_fields") or []
                    ),
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
                    "support_status": realization_coverage["support_status"],
                    "coverage": realization_coverage["coverage"],
                    "planned_coverage": planned_coverage,
                    "planned_failed_coverage_fields": planned_failed_coverage_fields,
                    "failed_coverage_fields": realization_coverage[
                        "failed_coverage_fields"
                    ],
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
    if not overview:
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


def build_safe_evidence_fallback(
    *,
    writing_section: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build conservative prose from an already validated Writing Plan.

    This path never invents a missing scientific value.  It first reuses
    source-normalized facts that pass the same deterministic anchor gate and
    otherwise emits an attributed, claim-kind-specific boundary sentence.
    It is intentionally available only after academic planning has produced
    resolvable Claims, evidence references, and citation groups.
    """

    evidence_by_key = {
        str(item.get("evidence_key") or ""): item
        for item in evidence
        if isinstance(item, dict) and str(item.get("evidence_key") or "")
    }
    claims = {
        str(item.get("claim_id") or ""): item
        for item in writing_section.get("claims") or []
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    }

    def source_texts(claim: dict[str, Any]) -> list[str]:
        rows: list[str] = []
        for ref in claim.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            item = evidence_by_key.get(str(ref.get("evidence_key") or ""))
            if not item:
                continue
            rows.append(
                " ".join(
                    str(value or "")
                    for value in (
                        item.get("content") or item.get("evidence") or "",
                        item.get("normalized_fact_value") or "",
                    )
                )
            )
        return rows

    def sentence(value: Any) -> str:
        text = compact_text(value, limit=1800).strip()
        if text and text[-1] not in ".!?":
            text += "."
        return text

    def safe_candidate(claim: dict[str, Any]) -> str:
        cited_texts = source_texts(claim)
        normalized_values = [
            compact_text(evidence_by_key[str(ref.get("evidence_key") or "")].get("normalized_fact_value"), limit=900)
            for ref in claim.get("evidence_refs") or []
            if isinstance(ref, dict)
            and str(ref.get("evidence_key") or "") in evidence_by_key
            and compact_text(
                evidence_by_key[str(ref.get("evidence_key") or "")].get("normalized_fact_value"),
                limit=900,
            )
        ]
        candidates = [
            " ".join(dict.fromkeys(normalized_values)),
            compact_text(claim.get("allowed_assertion"), limit=1800),
            compact_text(claim.get("claim"), limit=1800),
        ]
        for candidate in dict.fromkeys(value for value in candidates if value):
            unsupported = unsupported_realization_anchors(candidate, cited_texts)
            if not any(unsupported.values()):
                return sentence(candidate)

        plural = len(set(claim.get("citation_group") or [])) > 1
        source = "The cited sources" if plural else "The cited source"
        kind = str(claim.get("claim_kind") or "reported_finding")
        templates = {
            "cross_study_comparison": (
                f"{source} support a bounded comparison of the reported approaches, "
                "without establishing additional quantitative or mechanistic detail."
            ),
            "review_synthesis": (
                f"{source} support this section's synthesis within the selected evidence scope."
            ),
            "mechanism_interpretation": (
                f"{source} present a mechanistic interpretation for the reported transformation, "
                "while the available evidence does not justify further mechanistic specification."
            ),
            "limitation": (
                f"{source} identify a boundary on the reported transformation that limits broader comparison."
            ),
            "future_direction": (
                f"{source} support the stated evidence boundary and motivate further targeted investigation."
            ),
        }
        return templates.get(
            kind,
            f"{source} report the transformation and its stated outcome under the investigated conditions.",
        )

    paragraphs: list[dict[str, Any]] = []
    for paragraph in writing_section.get("paragraphs") or []:
        if not isinstance(paragraph, dict):
            continue
        paragraph_id = str(paragraph.get("paragraph_id") or "")
        realizations = []
        for claim_id in paragraph.get("claim_ids") or []:
            claim = claims.get(str(claim_id))
            if claim is None:
                raise RuntimeError(
                    f"Safe evidence fallback could not resolve Claim {claim_id}."
                )
            realizations.append(
                {"claim_id": str(claim_id), "text": safe_candidate(claim)}
            )
        paragraphs.append(
            {
                "paragraph_id": paragraph_id,
                "claim_realizations": realizations,
            }
        )

    all_evidence_texts = [
        " ".join(
            str(value or "")
            for value in (
                item.get("content") or item.get("evidence") or "",
                item.get("normalized_fact_value") or "",
            )
        )
        for item in evidence_by_key.values()
    ]
    overview_candidates = [
        compact_text(writing_section.get("overview_intent"), limit=1800),
        *[
            compact_text(item.get("positive_synthesis"), limit=1800)
            for item in writing_section.get("paragraphs") or []
            if isinstance(item, dict)
        ],
    ]
    overview = ""
    for candidate in dict.fromkeys(value for value in overview_candidates if value):
        unsupported = unsupported_realization_anchors(candidate, all_evidence_texts)
        if not any(unsupported.values()):
            overview = sentence(candidate)
            break
    if not overview:
        overview = (
            "This section synthesizes the cited evidence within the selected scope and "
            "distinguishes directly reported findings from broader interpretation."
        )
    return {"overview": overview, "paragraphs": paragraphs}


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
        current=0,
        total=progress_total,
        phase="preparing",
        completed_sections=completed_progress,
    )
    matrix = read_json(project / "01_matrix_outline" / "literature_matrix.json")
    rows_list = matrix.get("rows") if isinstance(matrix, dict) else matrix
    rows = {str(row.get("paper_id")): row for row in rows_list or [] if isinstance(row, dict) and row.get("paper_id")}
    blueprint = read_json(project / "01_matrix_outline" / "section_blueprint.json")
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
    selected_outline_path = project / "01_matrix_outline" / "selected_outline.md"
    selected_outline = selected_outline_path.read_text(encoding="utf-8", errors="ignore")[:12000] if selected_outline_path.exists() else ""
    rules = load_blueprint_rule_pack(root, blueprint)
    synthesis_rules = load_cross_study_synthesis_skill()
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
    failed_progress: list[dict[str, Any]] = []

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
        scientific_claim_states = [
            dict(item)
            for item in section_evidence.get("scientific_claim_states") or []
            if isinstance(item, dict)
        ]
        claim_state_by_id = {
            str(item.get("claim_id") or ""): item
            for item in scientific_claim_states
            if str(item.get("claim_id") or "")
        }
        declared_scientific_claims = [
            dict(item)
            for item in task.get("scientific_claims") or []
            if isinstance(item, dict)
        ]
        supported_scientific_claims = [
            claim
            for claim in declared_scientific_claims
            if not claim_state_by_id
            or str(
                (claim_state_by_id.get(str(claim.get("claim_id") or "")) or {}).get(
                    "status"
                )
                or ""
            )
            in {"evidence_supported", "partially_supported"}
        ]
        writing_requirements = [
            dict(item)
            for item in task.get("writing_requirements") or []
            if isinstance(item, dict)
        ]
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
        retrieval_mode = effective_retrieval_mode(section_evidence)
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
        depth_contract = dict(spec.get("depth_contract") or task.get("depth_contract") or {})
        comparison_table = build_matrix_comparison_table(
            section_id, assigned_primary, rows
        )
        mechanism_table = build_mechanism_evidence_table(section_id, evidence)
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
        plan_evidence, plan_evidence_budget = bounded_evidence_payload(evidence)
        serialized_plan_evidence = json.dumps(plan_evidence, ensure_ascii=False)
        planning_scope_instruction = writing_scope_prompt_block(
            writing_scope_contract, stage="planning"
        )
        plan_prompt = f"""Plan one section of a source-grounded scientific review before prose is written.

Topic: {blueprint.get('review_topic') or project.name}
{planning_scope_instruction}

Selected review outline (preserve its ordering and heading intent):
{selected_outline}

Section title: {task.get('heading')}
Section role: {role}
Section thesis: {task.get('core_argument')}
Source-testable scientific claims permitted by current evidence: {json.dumps(supported_scientific_claims, ensure_ascii=False)[:10000]}
Scientific claim evidence states (missing claims are boundaries, not prose obligations): {json.dumps(scientific_claim_states, ensure_ascii=False)[:10000]}
Writing requirements (authoring operations, never treat these as source propositions): {json.dumps(writing_requirements, ensure_ascii=False)[:10000]}
Assigned primary paper IDs: {', '.join(assigned_primary) or 'none'}
Writeable primary paper IDs (the only papers that must be covered): {', '.join(primary) or 'none'}
Context-only primary paper IDs (optional broad attribution only): {', '.join(context_only_primary) or 'none'}
Unresolved primary paper IDs (do not force into prose): {', '.join(unresolved_primary) or 'none'}
Supporting paper IDs (brief comparison or synthesis only): {', '.join(supporting) or 'none'}
Context paper IDs (framing only; never substitute for primary evidence): {', '.join(contextual) or 'none'}
Allowed paper IDs only: {', '.join(allowed)}
Required synthesis components: {json.dumps(spec.get('synthesis_requirements') or [], ensure_ascii=False)}
Narrative depth contract (diagnostic targets, not permission to invent filler): {json.dumps(depth_contract, ensure_ascii=False)}
Use only these paragraph responsibility labels: {', '.join(CANONICAL_PARAGRAPH_ROLES)}.
Source-addressable Matrix comparison table (empty cells are unknown, never negative findings):
{json.dumps(comparison_table, ensure_ascii=False)}
Mechanism-evidence inventory (describes evidence type, not mechanistic truth):
{json.dumps(mechanism_table, ensure_ascii=False)}

Return an evidence-bound Synthesis and Writing Plan, not manuscript prose. First state the
section's positive synthesis. Then plan claim-centered paragraphs with one distinct academic
responsibility and a reader_takeaway each. Avoid one paragraph per paper when the evidence
supports comparison. Every planned Claim must separately declare claim_kind,
epistemic_status, support_status, citation_group, evidence_keys, and an evidence ceiling.
The workflow derives `fact_ids`, `allowed_assertion`, and the program-side
`assertion_ceiling` from those evidence keys after your plan is returned; you cannot raise
that ceiling in prose.

{paragraph_instruction}

{cross_section_input}

Evidence contract: {evidence_instruction}
Never invent conditions, yields, selectivities, structures, causal relations, or mechanistic
evidence. A limitation must follow a positive supported takeaway instead of replacing it.
Do not return blocked Claims as publishable content.

Academic rules:\n{rules}

Cross-study synthesis policy:\n{synthesis_rules}

Source evidence (only use claims supported here):\n{serialized_plan_evidence}
"""
        generation_mode = "standard"
        fallback_reason = ""
        repair_count = 0
        try:
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
            except RuntimeError as exc:
                if not request_body_budget_error(exc):
                    raise
                compact_evidence, compact_budget = bounded_evidence_payload(
                    evidence, char_budget=32_000
                )
                plan_prompt = plan_prompt.replace(
                    serialized_plan_evidence,
                    json.dumps(compact_evidence, ensure_ascii=False),
                )
                plan_evidence_budget = {
                    **compact_budget,
                    "compact_retry": True,
                }
                proposed_plan = call_structured_llm(
                    plan_prompt,
                    PLAN_SCHEMA,
                    api_key,
                    base_url,
                    model,
                    wire_api,
                    label="section-academic-planning-compact-retry",
                    schema_name="review_section_plan_compact_retry",
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
                depth_contract=depth_contract,
            )
            synthesis_section["comparison_table"] = comparison_table
            synthesis_section["mechanism_evidence_table"] = mechanism_table
            synthesis_section["prompt_evidence_budget"] = plan_evidence_budget
            contract_gaps = synthesis_contract_gaps(
                writing_section,
                synthesis_section,
                list(spec.get("synthesis_requirements") or []),
                comparison_table,
                mechanism_table,
                depth_contract,
            )
            if contract_gaps:
                initial_synthesis = synthesis_section
                initial_writing = writing_section
                try:
                    repaired_plan = call_structured_llm(
                        plan_prompt
                        + "\n\nThe previous plan did not satisfy these evidence-backed synthesis contracts: "
                        + ", ".join(contract_gaps)
                        + ". Regenerate the complete plan. When comparable source-addressable fields exist, include at least one cross-study comparison Claim citing two or more supporting papers. When mechanism evidence exists, distinguish experiment, computation, catalyst-state evidence, stereochemical assignment, and author proposal rather than repeating a generic caveat.",
                        PLAN_SCHEMA,
                        api_key,
                        base_url,
                        model,
                        wire_api,
                        label="section-academic-planning-repair",
                        schema_name="review_section_plan_repair",
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
                        generated=repaired_plan,
                        synthesis_requirements=list(
                            spec.get("synthesis_requirements") or []
                        ),
                        depth_contract=depth_contract,
                    )
                    synthesis_section["comparison_table"] = comparison_table
                    synthesis_section["mechanism_evidence_table"] = mechanism_table
                    remaining_gaps = synthesis_contract_gaps(
                        writing_section,
                        synthesis_section,
                        list(spec.get("synthesis_requirements") or []),
                        comparison_table,
                        mechanism_table,
                        depth_contract,
                    )
                    synthesis_section["planning_contract_repair"] = {
                        "attempted": True,
                        "initial_gaps": contract_gaps,
                        "remaining_gaps": remaining_gaps,
                        "status": "repaired" if not remaining_gaps else "incomplete",
                    }
                except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as repair_error:
                    synthesis_section = initial_synthesis
                    writing_section = initial_writing
                    synthesis_section["planning_contract_repair"] = {
                        "attempted": True,
                        "initial_gaps": contract_gaps,
                        "remaining_gaps": contract_gaps,
                        "status": "repair_unavailable",
                        "error": compact_text(repair_error, limit=500),
                    }
            else:
                synthesis_section["planning_contract_repair"] = {
                    "attempted": False,
                    "initial_gaps": [],
                    "remaining_gaps": [],
                    "status": "not_needed",
                }
            remaining_gaps = synthesis_contract_gaps(
                writing_section,
                synthesis_section,
                list(spec.get("synthesis_requirements") or []),
                comparison_table,
                mechanism_table,
                depth_contract,
            )
            deterministic_comparison_added = False
            if any("comparison" in gap for gap in remaining_gaps):
                deterministic_comparison_added = ensure_evidence_bound_comparison_plan(
                    writing_section,
                    synthesis_section,
                    comparison_table,
                    evidence,
                )
                remaining_gaps = synthesis_contract_gaps(
                    writing_section,
                    synthesis_section,
                    list(spec.get("synthesis_requirements") or []),
                    comparison_table,
                    mechanism_table,
                    depth_contract,
                )
            repair_record = dict(
                synthesis_section.get("planning_contract_repair") or {}
            )
            repair_record["deterministic_comparison_added"] = (
                deterministic_comparison_added
            )
            repair_record["remaining_gaps"] = remaining_gaps
            if deterministic_comparison_added and not remaining_gaps:
                repair_record["status"] = "repaired_with_evidence_bound_fallback"
            synthesis_section["planning_contract_repair"] = repair_record
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
        bounded_writer_evidence, writer_evidence_budget = bounded_evidence_payload(
            writer_evidence,
            char_budget=55_000,
        )
        serialized_writer_evidence = json.dumps(
            bounded_writer_evidence, ensure_ascii=False
        )
        drafting_scope_instruction = writing_scope_prompt_block(
            writing_scope_contract, stage="drafting"
        )
        writer_prompt = f"""Realize a validated academic Writing Plan as fluent review prose.

Topic: {blueprint.get('review_topic') or project.name}
{drafting_scope_instruction}

Section title: {task.get('heading')}
Section role: {role}

The plan below is an immutable contract for this call. Return every paragraph_id and every
claim_id exactly once and in plan order. Write one concise realization for each Claim. Do not
add, remove, merge, split, or reorder Claims; do not add citations, source IDs, paper IDs, or
headings because the workflow inserts citations after identity validation. Respect each
program-side assertion_ceiling, allowed_assertion, and evidence_ceiling; use conditional
attribution for author interpretations or mechanisms.
Lead with supported positive synthesis, then state necessary boundaries. Avoid reading-note
style and avoid one-paper-at-a-time narration unless the plan explicitly requires it.

Validated Synthesis slice:
{json.dumps(synthesis_section, ensure_ascii=False)}

Validated Writing Plan:
{json.dumps(writing_section, ensure_ascii=False)}

Selected source evidence only:
{serialized_writer_evidence}

Writing rules:
{rules}

Cross-study synthesis policy:
{synthesis_rules}
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
            except RuntimeError as exc:
                if not request_body_budget_error(exc):
                    raise
                compact_writer_evidence, compact_writer_budget = (
                    bounded_evidence_payload(writer_evidence, char_budget=28_000)
                )
                writer_prompt = writer_prompt.replace(
                    serialized_writer_evidence,
                    json.dumps(compact_writer_evidence, ensure_ascii=False),
                )
                writer_evidence_budget = {
                    **compact_writer_budget,
                    "compact_retry": True,
                }
                generated_draft = call_structured_llm(
                    writer_prompt,
                    WRITER_SCHEMA,
                    api_key,
                    base_url,
                    model,
                    wire_api,
                    label="section-claim-realization-compact-retry",
                    schema_name="review_claim_realization_compact_retry",
                    required_list="paragraphs",
                )
            for repair_index in range(3):
                try:
                    overview, paragraphs, validations, reviews = validate_and_realize_section(
                        section_id=section_id,
                        generated=generated_draft,
                        writing_section=writing_section,
                        evidence=evidence,
                        citation_map=citation_map,
                        domain_terms=domain_terms,
                    )
                    break
                except RuntimeError as validation_error:
                    if (
                        "unsupported evidence anchors" not in str(validation_error)
                        or repair_index >= 2
                    ):
                        raise
                    repair_count += 1
                    generated_draft = call_structured_llm(
                        writer_prompt
                        + "\n\nThe previous realization failed deterministic evidence-anchor "
                        + "validation: "
                        + str(validation_error)
                        + " Regenerate the complete realization without introducing any "
                        + "number, measurement, formula, catalyst, reagent, substrate, or "
                        + "product identity that is absent from the cited evidence chunks. "
                        + "Claim IDs, order, evidence references, and citation groups remain "
                        + "immutable, but the wording of allowed_assertion is not mandatory. "
                        + "Treat every anchor listed in the validation error as forbidden: "
                        + "omit it or replace it with a more general statement that is "
                        + "directly supported by the cited chunk, even when the same wording "
                        + "appears in the plan. Return the full realization.",
                        WRITER_SCHEMA,
                        api_key,
                        base_url,
                        model,
                        wire_api,
                        label=f"section-claim-realization-evidence-repair-{repair_index + 1}",
                        schema_name=f"review_claim_realization_evidence_repair_{repair_index + 1}",
                        required_list="paragraphs",
                    )
            if repair_count:
                generation_mode = "evidence_repaired"
            validations.append(
                {
                    "rule_id": "section.prompt_evidence_budget",
                    "target_id": section_id,
                    "status": "pass",
                    "planning": plan_evidence_budget,
                    "writing": writer_evidence_budget,
                }
            )
        except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            fallback_reason = compact_text(exc, limit=1200)
            try:
                fallback_draft = build_safe_evidence_fallback(
                    writing_section=writing_section,
                    evidence=evidence,
                )
                overview, paragraphs, validations, reviews = validate_and_realize_section(
                    section_id=section_id,
                    generated=fallback_draft,
                    writing_section=writing_section,
                    evidence=evidence,
                    citation_map=citation_map,
                    domain_terms=[],
                )
                generation_mode = "safe_evidence_fallback"
                validations.append(
                    {
                        "rule_id": "section.safe_evidence_fallback",
                        "target_id": section_id,
                        "status": "pass_with_warning",
                        "reason": fallback_reason,
                        "source": "validated_writing_plan_and_evidence",
                    }
                )
                reviews.append(
                    {
                        "iteration": 1,
                        "decision": "PASS_WITH_WARNINGS",
                        "target_ids": [section_id],
                        "issues": [
                            {
                                "type": "safe_evidence_fallback_used",
                                "severity": "warning",
                                "reason": fallback_reason,
                            }
                        ],
                        "preserve": [
                            "validated Claim/Citation identities",
                            "source evidence boundaries",
                        ],
                        "repair_objective": "Optional prose enrichment after source review.",
                        "reviewer": "deterministic_safe_evidence_fallback_v1",
                    }
                )
            except RuntimeError as fallback_error:
                record_section_failure(
                    section_id,
                    str(task.get("heading") or section_id),
                    f"{fallback_reason} Safe evidence fallback also failed: {fallback_error}",
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
            not minimum_word_count or actual_word_count >= minimum_word_count
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
        synthesis_sections.append(synthesis_section)
        writing_sections.append(writing_section)
        output_sections.append(
            {
                "section_id": section_id,
                "heading": task.get("heading"),
                "section_role": role,
                "writing_mode": task.get("writing_mode"),
                "generation_mode": generation_mode,
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
                "generation_mode": generation_mode,
                "section_readiness": section_readiness,
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
            "planning_mode": "evidence_first_pre_draft",
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
            "planning_mode": "evidence_first_pre_draft",
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
