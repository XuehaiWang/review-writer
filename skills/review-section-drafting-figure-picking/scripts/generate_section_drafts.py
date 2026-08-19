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


def parse_json_object(text: Any) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value).strip()
    parsed = json.loads(value)
    parsed = repair_model_unicode(parsed)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("paragraphs"), list):
        raise RuntimeError("Section-writing model returned an invalid response.")
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


def call_llm(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    wire_api: str = "responses",
) -> dict[str, Any]:
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["overview", "paragraphs"],
        "properties": {
            "overview": {"type": "string"},
            "paragraphs": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["text"],
                    "properties": {
                        "text": {"type": "string"},
                        "paper_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["paper_id", "chunk_ids", "claim"],
                                "properties": {
                                    "paper_id": {"type": "string"},
                                    "chunk_ids": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"type": "string"},
                                    },
                                    "claim": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    }
    schema_prompt = (
        f"{prompt}\n\nReturn only one JSON object matching this JSON Schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    if gateway_configured():
        return call_gateway_json(schema_prompt, label="section-drafting")
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
            "text": {"format": {"type": "json_schema", "name": "review_section", "schema": schema, "strict": True}},
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
    data = open_json_response(request, label="Section-writing model request")
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
    return parse_json_object(text)


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
    completed_progress: list[dict[str, str]] = []
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
    output_sections = []
    for task in tasks:
        section_id = str(task.get("section_id"))
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="generating",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
        )
        role = str(task.get("section_role") or "body").strip().casefold()
        primary = list(
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
                if str(pid) in rows and str(pid) not in primary
            )
        )
        allowed = list(
            dict.fromkeys(
                str(pid)
                for pid in task.get("allowed_papers", [*primary, *supporting])
                if str(pid) in rows
            )
        )
        section_evidence = evidence_sections.get(section_id, {})
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
        else:
            evidence = [paper_evidence(root, rows, paper_id) for paper_id in allowed]
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
            if retrieval_mode == "lexical":
                raise SystemExit(f"No usable indexed evidence for {section_id}.")
            raise SystemExit(f"No usable MinerU Markdown or matrix evidence for {section_id}.")
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
        if role == "introduction":
            paragraph_instruction = """Write 2-4 claim-centered framing paragraphs. Define the problem, scope, terminology, organizing logic, and evidence landscape. Use the supporting papers only as brief representative anchors. Do not give any paper a standalone summary, and do not repeat detailed methods, conditions, datasets, results, yields, or limitations that belong in a body section."""
        elif role == "conclusion":
            paragraph_instruction = """Write 2-4 claim-centered synthesis paragraphs. Compare conclusions across the body sections, identify shared limitations and defensible future directions, and cite prior evidence concisely. Do not replay the body as a paper-by-paper list and do not repeat full methods, conditions, datasets, or results."""
        elif primary:
            paragraph_instruction = f"""Write claim-centered review paragraphs, not one paragraph per paper. Every primary paper must support at least one paragraph, but related studies should be compared or synthesized together when they address the same claim. A paragraph may cite one or several allowed papers. Discuss detailed study evidence only here, in the paper's primary section. Supporting papers may be used briefly for comparison, without repeating their full descriptions. Cover all {len(primary)} primary papers."""
        else:
            paragraph_instruction = """Write 2-4 cross-cutting synthesis paragraphs using only the supporting evidence. Compare previously introduced findings from a new analytical angle, but do not repeat complete paper descriptions, methods, conditions, datasets, or results."""
        evidence_instruction = (
            "For every paragraph, return `evidence` entries with `paper_id`, one or more "
            "`chunk_ids` copied exactly from the indexed evidence below, and the specific "
            "claim those chunks support. Do not return a paper or chunk that is absent from "
            "the evidence package."
            if retrieval_mode == "lexical"
            else "For every paragraph, set `paper_ids` to all and only the allowed sources "
            "that support that paragraph."
        )
        prompt = f"""Write one body section of a source-grounded scientific review.

Topic: {blueprint.get('review_topic') or project.name}
Selected review outline (preserve its ordering and heading intent):
{selected_outline}

Section title: {task.get('heading')}
Section role: {role}
Section thesis: {task.get('core_argument')}
Required claims: {json.dumps(task.get('must_cover_points') or [], ensure_ascii=False)}
Comparison axes and constraints: {json.dumps(spec.get('review_claims') or [], ensure_ascii=False)[:10000]}
Primary paper IDs (detailed discussion belongs only in this section): {', '.join(primary) or 'none'}
Supporting paper IDs (brief comparison or synthesis only): {', '.join(supporting) or 'none'}
Allowed paper IDs only: {', '.join(allowed)}

Return a 90-150 word `overview`, followed by complete review paragraphs. The overview introduces the comparison axis for this section and must contain no citations or paper IDs.

{paragraph_instruction}

Evidence contract: {evidence_instruction}
Do not put citations, source IDs, chunk IDs, or paper titles as headings in `text`; the workflow adds numeric citations after validation. Preserve limitations and use conditional language for proposed mechanisms. Do not invent conditions, yields, selectivities, structures, or mechanistic evidence.

Writing rules:\n{rules}

Source evidence (only use claims supported here):\n{json.dumps(evidence, ensure_ascii=False)}
"""
        try:
            generated = call_llm(prompt, api_key, base_url, model, wire_api)
        except RuntimeError as exc:
            message = str(exc)
            if "transport failed" in message.casefold():
                raise SystemExit(
                    "Section-writing provider could not be reached after three retries. "
                    f"Configured endpoint: {base_url}. Open API Settings from this deployment, "
                    "confirm that the displayed active workspace is correct, save the text provider again, "
                    "and retry the stage. "
                    f"Details: {message}"
                ) from None
            raise SystemExit(message) from None
        except urllib.error.HTTPError as exc:
            raise SystemExit(
                f"Section-writing model request was rejected (HTTP {exc.code}). "
                "Check OPENAI_API_KEY, OPENAI_BASE_URL, and REVIEW_WRITING_MODEL."
            ) from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Section-writing model is unreachable: {exc.reason}") from exc
        overview = re.sub(r"\s+", " ", str(generated.get("overview") or "")).strip()
        if not overview:
            raise SystemExit(f"The writing model did not produce a section overview for {section_id}.")
        generated_paragraphs: list[dict[str, Any]] = []
        covered_primary: set[str] = set()
        valid_evidence_chunks = {
            (str(item.get("paper_id") or ""), str(item.get("chunk_id") or ""))
            for item in evidence
            if isinstance(item, dict)
            and item.get("paper_id")
            and item.get("chunk_id")
        }
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="validating",
            current_section_id=section_id,
            current_heading=str(task.get("heading") or section_id),
            completed_sections=completed_progress,
            evidence_hit_count=len(evidence),
            evidence_paper_count=evidence_paper_count,
        )
        for item in generated["paragraphs"]:
            paragraph_evidence: list[dict[str, Any]] = []
            if retrieval_mode == "lexical":
                for raw_evidence in item.get("evidence") or []:
                    if not isinstance(raw_evidence, dict):
                        continue
                    evidence_paper = str(raw_evidence.get("paper_id") or "")
                    chunk_ids = list(
                        dict.fromkeys(
                            str(chunk_id)
                            for chunk_id in raw_evidence.get("chunk_ids") or []
                            if (
                                evidence_paper,
                                str(chunk_id),
                            )
                            in valid_evidence_chunks
                        )
                    )
                    claim = re.sub(
                        r"\s+", " ", str(raw_evidence.get("claim") or "")
                    ).strip()
                    if (
                        evidence_paper in allowed
                        and evidence_paper in citation_map
                        and chunk_ids
                        and claim
                    ):
                        paragraph_evidence.append(
                            {
                                "paper_id": evidence_paper,
                                "chunk_ids": chunk_ids,
                                "claim": claim,
                            }
                        )
                paper_ids = list(
                    dict.fromkeys(item["paper_id"] for item in paragraph_evidence)
                )
            else:
                paper_ids = list(
                    dict.fromkeys(
                        str(pid)
                        for pid in item.get("paper_ids", [])
                        if str(pid) in allowed and str(pid) in citation_map
                    )
                )
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if not paper_ids or not text:
                continue
            generated_paragraphs.append(
                {
                    "paper_ids": paper_ids,
                    "text": text,
                    "evidence": paragraph_evidence,
                }
            )
            covered_primary.update(set(paper_ids) & set(primary))
        if not generated_paragraphs:
            raise SystemExit(
                f"The writing model did not produce any usable review paragraph for {section_id}."
            )
        missing_papers = [paper_id for paper_id in primary if paper_id not in covered_primary]
        if missing_papers:
            raise SystemExit(
                f"The writing model did not cover every primary paper in {section_id}: "
                + ", ".join(missing_papers)
            )
        paragraphs = []
        markdown = [f"## {task.get('heading')}", "", overview, ""]
        for index, item in enumerate(generated_paragraphs, start=1):
            paragraph_id = f"{section_id}-p{index}"
            paper_ids = item["paper_ids"]
            callout = f"[{', '.join(str(citation_map[paper_id]) for paper_id in paper_ids)}]"
            text = item["text"] + " " + callout
            markdown.extend([
                text,
                "",
                f"<!-- paragraph_id: {paragraph_id} -->",
                "",
            ])
            paragraphs.append(
                {
                    "paragraph_id": paragraph_id,
                    "paper_id": paper_ids[0],
                    "cited_paper_ids": paper_ids,
                    "text": text,
                    "evidence": item.get("evidence") or [],
                }
            )
        section_text = make_xml_compatible("\n".join(markdown).strip() + "\n")[0]
        (sections_dir / f"{section_id}.md").write_text(section_text, encoding="utf-8")
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
            }
        )
        completed_progress.append(
            {
                "section_id": section_id,
                "heading": str(task.get("heading") or section_id),
            }
        )
        write_generation_progress(
            stage,
            current=len(completed_progress),
            total=progress_total,
            phase="generating" if len(completed_progress) < progress_total else "finalizing",
            completed_sections=completed_progress,
        )
    write_json(stage / "section_drafts.json", {"project_id": args.project_id, "sections": output_sections})
    (stage / "section_drafts.md").write_text("\n\n".join(section["draft_md"] for section in output_sections), encoding="utf-8")
    (stage / "section_drafting_report.md").write_text(
        f"# Section Drafting Report\n\nGenerated {len(output_sections)} source-grounded sections with model `{model}`.\n", encoding="utf-8"
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
