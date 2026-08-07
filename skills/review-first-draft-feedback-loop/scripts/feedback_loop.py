#!/usr/bin/env python3
"""Score and iteratively improve a merged review draft without whole-draft regeneration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BOOTSTRAP_ROOT = next(
    (parent for parent in Path(__file__).resolve().parents if (parent / "review_writer_core").is_dir()),
    None,
)
if _BOOTSTRAP_ROOT is None:
    raise RuntimeError("Could not locate the Review Writer workspace")
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from review_writer_core.providers import (  # noqa: E402
    DEFAULT_OPENAI_BASE_URL,
    DEFAULT_TEXT_MODEL,
    DEFAULT_TEXT_WIRE_API,
    openai_endpoint,
)
from review_writer_core.text_safety import make_xml_compatible  # noqa: E402


PARAGRAPH_MARKER_RE = re.compile(r"<!--\s*paragraph_id:\s*([A-Za-z0-9_.:-]+)\s*-->")
REFERENCES_RE = re.compile(
    r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$",
    re.I | re.M,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
CALLOUT_RE = re.compile(r"\[(\d+(?:\s*[-,]\s*\d+)*)\]")
LABEL_SCAFFOLD_RE = re.compile(
    r"(?:^|(?<=[.!?])\s+)(?:reaction conditions?|substrate scope|selectivity|mechanism|"
    r"limitations?|evidence ceiling|method and activation mode)\s*:\s*",
    re.I,
)
SCAFFOLD_RE = re.compile(
    r"The paper reports the following|At the reaction level|The method description further emphasizes|"
    r"For operational context|The retained metric record|The evidence ceiling is equally important",
    re.I,
)
TRANSIENT_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
PROTECTED_NUMBER_RE = re.compile(
    r"(?<![A-Za-z])(?:\d+(?:\.\d+)?(?:\s*(?:%|mol%|°C|K|h|min|s|equiv|M|mM))?|"
    r"\d+\s*:\s*\d+)(?![A-Za-z])",
    re.I,
)
STEREO_RE = re.compile(
    r"\b(?:ee|er|dr|de|racemic|enantioselective|enantiospecific|diastereoselective|"
    r"R|S|E|Z|axial chirality|stereospecific)\b",
    re.I,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_body_references(markdown: str) -> tuple[str, str]:
    match = REFERENCES_RE.search(markdown or "")
    return (markdown[: match.start()], markdown[match.start() :]) if match else (markdown, "")


def parse_marked_paragraphs(markdown: str) -> list[dict[str, Any]]:
    """Read the current Stage-8 paragraph text; a marker terminates its paragraph."""
    body, _ = split_body_references(markdown)
    markers = list(PARAGRAPH_MARKER_RE.finditer(body))
    headings = list(HEADING_RE.finditer(body))
    paragraphs: list[dict[str, Any]] = []
    previous_end = 0
    for marker in markers:
        preceding = [heading for heading in headings if heading.end() <= marker.start()]
        boundary = max(preceding[-1].end() if preceding else previous_end, previous_end)
        segment = body[boundary : marker.start()]
        start = boundary + len(segment) - len(segment.lstrip())
        end = marker.start() - (len(segment) - len(segment.rstrip()))
        heading = preceding[-1].group(2).strip() if preceding else ""
        text = body[start:end]
        if text.strip() and not text.lstrip().startswith(("!", "|")):
            paragraphs.append(
                {
                    "paragraph_id": marker.group(1),
                    "heading": heading,
                    "text": text,
                    "start": start,
                    "end": end,
                    "marker_end": marker.end(),
                }
            )
        previous_end = marker.end()
    return paragraphs


def section_payload(project: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = project / "02_section_drafting" / "section_drafts.json"
    payload = read_json(path, {})
    sections = payload.get("sections") if isinstance(payload, dict) else payload
    if not isinstance(sections, list):
        raise RuntimeError(f"Invalid section draft envelope: {path}")
    return payload if isinstance(payload, dict) else {"sections": sections}, sections


def paragraph_metadata(project: Path) -> dict[str, dict[str, Any]]:
    _, sections = section_payload(project)
    result: dict[str, dict[str, Any]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if isinstance(paragraph, dict) and paragraph.get("paragraph_id"):
                result[str(paragraph["paragraph_id"])] = paragraph
    return result


def citation_entries(project: Path) -> list[dict[str, Any]]:
    payload = read_json(project / "04_first_draft" / "citations.json", {})
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    return entries if isinstance(entries, list) else []


def matrix_rows(project: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(project / "01_matrix_outline" / "literature_matrix.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return {
        str(row.get("paper_id")): row
        for row in rows or []
        if isinstance(row, dict) and row.get("paper_id")
    }


def metadata_record(review_root: Path, paper_id: str) -> dict[str, Any]:
    path = review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    return read_json(path, {}) if path.is_file() else {}


def source_evidence(
    review_root: Path,
    project: Path,
    paragraph: dict[str, Any],
    structured: dict[str, Any],
    rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    paper_ids = [
        str(value)
        for value in (structured.get("cited_paper_ids") or [structured.get("paper_id")])
        if value
    ]
    evidence: list[dict[str, Any]] = []
    local_source_available = True
    for paper_id in paper_ids:
        row = rows.get(paper_id, {})
        metadata = metadata_record(review_root, paper_id)
        source_paths = metadata.get("source_paths") if isinstance(metadata, dict) else {}
        paths = [Path(str(value)) for value in (source_paths or {}).values() if str(value or "").strip()]
        paths = [path if path.is_absolute() else review_root / path for path in paths]
        available = any(path.is_file() or path.is_dir() for path in paths)
        local_source_available = local_source_available and available
        evidence.append(
            {
                "paper_id": paper_id,
                "title": str(row.get("title") or metadata.get("title") or ""),
                "abstract": clean_text(row.get("abstract") or metadata.get("abstract"))[:1200],
                "main_content": clean_text(row.get("main_content"))[:1600],
                "local_source_available": available,
            }
        )
    return {
        "paragraph_id": paragraph["paragraph_id"],
        "heading": paragraph.get("heading", ""),
        "paper_ids": paper_ids,
        "local_source_available": local_source_available if paper_ids else False,
        "evidence": evidence,
    }


def expand_callouts(text: str) -> set[int]:
    values: set[int] = set()
    for match in CALLOUT_RE.finditer(text or ""):
        for part in re.split(r"\s*,\s*", match.group(1)):
            if "-" in part:
                left, right = [item.strip() for item in part.split("-", 1)]
                if left.isdigit() and right.isdigit():
                    values.update(range(int(left), int(right) + 1))
            elif part.strip().isdigit():
                values.add(int(part.strip()))
    return values


def deterministic_preflight(
    review_root: Path,
    project_id: str,
    *,
    min_words: int,
    max_words: int,
) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_path = project / "04_first_draft" / "first_draft.md"
    if not draft_path.is_file():
        raise FileNotFoundError(draft_path)
    markdown = make_xml_compatible(draft_path.read_text(encoding="utf-8", errors="replace"))[0]
    paragraphs = parse_marked_paragraphs(markdown)
    structured = paragraph_metadata(project)
    rows = matrix_rows(project)
    findings: list[dict[str, Any]] = []
    paragraph_checks: list[dict[str, Any]] = []
    for paragraph in paragraphs:
        paragraph_id = str(paragraph["paragraph_id"])
        text = clean_text(paragraph["text"])
        words = len(text.split())
        evidence = source_evidence(
            review_root,
            project,
            paragraph,
            structured.get(paragraph_id, {}),
            rows,
        )
        issues: list[str] = []
        if words < min_words or words > max_words:
            issues.append("P01")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P01",
                    "severity": "major",
                    "diagnosis": f"Paragraph has {words} words; configured range is {min_words}-{max_words}.",
                    "route": "section_rewrite",
                }
            )
        if LABEL_SCAFFOLD_RE.search(text) or SCAFFOLD_RE.search(text):
            issues.append("P08")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P08",
                    "severity": "major",
                    "diagnosis": "Label-style or extraction-field scaffolding remains in the prose.",
                    "route": "section_rewrite",
                }
            )
        sentences = [clean_text(value) for value in re.split(r"(?<=[.!?])\s+", text) if clean_text(value)]
        normalized = [re.sub(r"[^a-z0-9]", "", value.casefold()) for value in sentences]
        if any(value and value in normalized[:index] for index, value in enumerate(normalized)):
            issues.append("P03")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "P03",
                    "severity": "major",
                    "diagnosis": "An identical sentence is repeated inside the paragraph.",
                    "route": "section_rewrite",
                }
            )
        if not evidence["local_source_available"]:
            issues.append("C01")
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "rule": "C01",
                    "severity": "major",
                    "diagnosis": "No readable local source is registered for at least one cited paper.",
                    "route": "local_source_recheck",
                }
            )
        paragraph_checks.append(
            {
                "paragraph_id": paragraph_id,
                "word_count": words,
                "issues": issues,
                "paper_ids": evidence["paper_ids"],
                "local_source_available": evidence["local_source_available"],
            }
        )

    body, references = split_body_references(markdown)
    cited = expand_callouts(body)
    listed = {int(value) for value in re.findall(r"(?m)^\s*\[(\d+)\]\s*\.?\s*\S", references)}
    mapped = {
        int(entry.get("callout"))
        for entry in citation_entries(project)
        if str(entry.get("callout") or "").isdigit()
    }
    image_paths = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", markdown)
    broken_images = [
        raw
        for raw in image_paths
        if not ((project / "04_first_draft" / raw).resolve()).is_file()
        and not re.match(r"^[a-z]+://", raw, re.I)
    ]
    hard: list[str] = []
    if not references.strip():
        hard.append("missing_references_section")
    if cited != listed or not cited.issubset(mapped):
        hard.append("citation_reference_map_mismatch")
    if broken_images:
        hard.append("broken_image_paths")
    if any(item["severity"] in {"critical", "major"} for item in findings):
        hard.append("paragraph_readability_or_source_failures")
    report = {
        "project_id": project_id,
        "draft_path": str(draft_path.resolve()),
        "draft_sha256": sha256_file(draft_path),
        "case_word_range": [min_words, max_words],
        "checks": {
            "paragraph_count": len(paragraphs),
            "citation_callouts": sorted(cited),
            "listed_references": sorted(listed),
            "citation_records": sorted(mapped),
            "image_count": len(image_paths),
            "broken_images": broken_images,
        },
        "paragraph_checks": paragraph_checks,
        "paragraph_findings": findings,
        "hard_regressions": sorted(set(hard)),
        "hash_manifest_created": False,
    }
    write_json(project / "04_first_draft" / "first_draft_preflight.json", report)
    return report


def provider_config() -> dict[str, str]:
    return {
        "api_key": str(os.environ.get("REVIEW_WRITING_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip(),
        "base_url": str(os.environ.get("REVIEW_WRITING_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).strip().rstrip("/"),
        "model": str(os.environ.get("REVIEW_WRITING_MODEL") or DEFAULT_TEXT_MODEL).strip(),
        "wire_api": str(os.environ.get("REVIEW_WRITING_WIRE_API") or DEFAULT_TEXT_WIRE_API).strip().casefold().replace("_", "-"),
    }


def provider_endpoint(base_url: str, wire_api: str) -> str:
    wire = str(wire_api or "").casefold()
    route = "chat/completions" if wire in {"chat", "chat-completion", "chat-completions"} else "responses"
    return openai_endpoint(base_url, route)


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Feedback model returned no JSON object")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise RuntimeError("Feedback model JSON must be an object")
    return value


def call_json_model(prompt: str, *, label: str) -> dict[str, Any]:
    config = provider_config()
    if not config["api_key"]:
        raise RuntimeError("Feedback loop requires the text API key saved in API Settings.")
    wire = config["wire_api"]
    if wire in {"chat", "chat-completion", "chat-completions"}:
        endpoint = provider_endpoint(config["base_url"], wire)
        payload = {
            "model": config["model"],
            "messages": [{"role": "user", "content": prompt + "\nReturn only one valid JSON object."}],
        }
    else:
        endpoint = provider_endpoint(config["base_url"], wire)
        payload = {"model": config["model"], "input": [{"role": "user", "content": prompt}]}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=300) as response:
                raw = response.read()
            data = json.loads(raw.decode("utf-8"))
            if wire in {"chat", "chat-completion", "chat-completions"}:
                choices = data.get("choices") if isinstance(data, dict) else []
                message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
                text = message.get("content") if isinstance(message, dict) else ""
                if isinstance(text, list):
                    text = "\n".join(
                        str(item.get("text") or "") for item in text if isinstance(item, dict)
                    )
            else:
                text = data.get("output_text") if isinstance(data, dict) else ""
                if not text and isinstance(data, dict):
                    text = "\n".join(
                        str(content.get("text") or "")
                        for output in data.get("output") or []
                        if isinstance(output, dict)
                        for content in output.get("content") or []
                        if isinstance(content, dict)
                    )
            return extract_json_object(str(text or ""))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:600].replace("\n", " ")
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == 2:
                raise RuntimeError(f"{label} failed with HTTP {exc.code}: {body or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"{label} transport/JSON failure: {exc}") from exc
        time.sleep(2**attempt)
    raise RuntimeError(f"{label} failed after retries")


def rubric_dimensions(rubric: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = rubric.get("dimensions") or []
    if not isinstance(dimensions, list) or not dimensions:
        raise RuntimeError("Unified rubric has no dimensions")
    if abs(sum(float(item.get("weight", 0)) for item in dimensions) - 100.0) > 0.001:
        raise RuntimeError("Unified rubric weights must total 100")
    return dimensions


def evaluation_prompt(
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    paragraph_goal: float,
) -> str:
    compact_paragraphs = [
        {
            "paragraph_id": item["paragraph_id"],
            "heading": item.get("heading", ""),
            "text": clean_text(item["text"]),
            "source_evidence": evidence.get(str(item["paragraph_id"]), {}),
        }
        for item in paragraphs
    ]
    return (
        "Act as a detect-first scientific review evaluator. Do not rewrite text. "
        "Score the complete rubric at levels 0-4 and every marked paragraph on a 0-100 scale. "
        "Treat deterministic preflight findings as binding. Do not penalize a paragraph merely for passive voice. "
        "A protected-fact conflict must route to local_source_recheck or human_confirmation, never automatic invention. "
        "Return JSON with dimension_scores and paragraph_scores. dimension_scores must include every rubric id exactly once "
        "with id, level, evidence. paragraph_scores must include every paragraph exactly once with paragraph_id, score, "
        "failed_dimensions, severity (none|minor|major|critical), diagnosis, route "
        "(pass|section_rewrite|local_source_recheck|final_polish|human_confirmation).\n\n"
        f"Overall goal: {goal}; paragraph goal: {paragraph_goal}.\n"
        f"Rubric: {json.dumps(rubric, ensure_ascii=False)}\n"
        f"Deterministic preflight: {json.dumps(preflight, ensure_ascii=False)}\n"
        f"Paragraphs and evidence: {json.dumps(compact_paragraphs, ensure_ascii=False)}"
    )


def normalize_evaluation(
    raw: dict[str, Any],
    rubric: dict[str, Any],
    paragraphs: list[dict[str, Any]],
    preflight: dict[str, Any],
    goal: float,
    paragraph_goal: float,
) -> dict[str, Any]:
    dimensions = rubric_dimensions(rubric)
    expected_ids = [str(item["id"]) for item in dimensions]
    raw_dimensions = raw.get("dimension_scores") or []
    by_id = {
        str(item.get("id")): item
        for item in raw_dimensions
        if isinstance(item, dict) and item.get("id")
    }
    if set(by_id) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(by_id))
        extra = sorted(set(by_id) - set(expected_ids))
        raise RuntimeError(f"Feedback rubric response has invalid dimensions; missing={missing}, extra={extra}")
    normalized_dimensions: list[dict[str, Any]] = []
    total = 0.0
    for definition in dimensions:
        item = by_id[str(definition["id"])]
        level = max(0.0, min(4.0, float(item.get("level", 0))))
        weight = float(definition.get("weight", 0))
        weighted = weight * level / 4.0
        total += weighted
        normalized_dimensions.append(
            {
                "id": definition["id"],
                "weight": weight,
                "level": level,
                "weighted": round(weighted, 3),
                "evidence": clean_text(item.get("evidence")),
            }
        )
    paragraph_ids = [str(item["paragraph_id"]) for item in paragraphs]
    raw_scores = raw.get("paragraph_scores") or []
    score_by_id = {
        str(item.get("paragraph_id")): item
        for item in raw_scores
        if isinstance(item, dict) and item.get("paragraph_id")
    }
    missing_paragraphs = sorted(set(paragraph_ids) - set(score_by_id))
    if missing_paragraphs:
        raise RuntimeError(f"Feedback response omitted paragraph scores: {missing_paragraphs}")
    preflight_by_id: dict[str, list[dict[str, Any]]] = {}
    for finding in preflight.get("paragraph_findings") or []:
        preflight_by_id.setdefault(str(finding.get("paragraph_id") or ""), []).append(finding)
    paragraph_scores: list[dict[str, Any]] = []
    paragraph_failures: list[dict[str, Any]] = []
    for paragraph_id in paragraph_ids:
        item = score_by_id[paragraph_id]
        score = max(0.0, min(100.0, float(item.get("score", 0))))
        binding = preflight_by_id.get(paragraph_id, [])
        if binding:
            score = min(score, 79.0)
        severity = str(item.get("severity") or ("major" if binding else "none")).casefold()
        if severity not in {"none", "minor", "major", "critical"}:
            severity = "major" if binding or score < paragraph_goal else "none"
        if binding and severity in {"none", "minor"}:
            severity = "major"
        route = str(item.get("route") or (binding[0].get("route") if binding else "pass"))
        allowed_routes = {
            "pass",
            "section_rewrite",
            "local_source_recheck",
            "final_polish",
            "human_confirmation",
        }
        if route not in allowed_routes:
            route = "section_rewrite" if score < paragraph_goal else "pass"
        if score < paragraph_goal and route == "pass":
            route = "section_rewrite"
            if severity == "none":
                severity = "major"
        failed = [str(value) for value in item.get("failed_dimensions") or []]
        for finding in binding:
            for value in str(finding.get("rule") or "").split("/"):
                if value and value not in failed:
                    failed.append(value)
        record = {
            "paragraph_id": paragraph_id,
            "score": round(score, 2),
            "failed_dimensions": failed,
            "severity": severity,
            "diagnosis": clean_text(item.get("diagnosis") or "; ".join(str(f.get("diagnosis")) for f in binding)),
            "route": route,
        }
        paragraph_scores.append(record)
        if route != "pass" or severity in {"critical", "major"}:
            paragraph_failures.append(record)
    hard = sorted(set(preflight.get("hard_regressions") or []))
    decision = "PASS" if total >= goal and not hard and not paragraph_failures else "REGENERATE_SECTIONS"
    return {
        "rubric_model": str(rubric.get("name") or "readability_first_unified_review_rubric"),
        "pass_threshold": goal,
        "total_score": round(total, 2),
        "decision": decision,
        "dimension_scores": normalized_dimensions,
        "hard_gate_failures": hard,
        "paragraph_scores": paragraph_scores,
        "paragraph_failures": paragraph_failures,
    }


def protected_signature(text: str) -> dict[str, list[str]]:
    return {
        "callouts": sorted(match.group(0) for match in CALLOUT_RE.finditer(text or "")),
        "numbers": sorted(PROTECTED_NUMBER_RE.findall(text or ""), key=str.casefold),
        "stereo": sorted((match.group(0).casefold() for match in STEREO_RE.finditer(text or ""))),
    }


def rewrite_prompt(
    paragraph: dict[str, Any],
    score: dict[str, Any],
    evidence: dict[str, Any],
    min_words: int,
    max_words: int,
) -> str:
    return (
        "Rewrite exactly one scientific-review paragraph for readability and argument flow. Preserve every citation callout, "
        "number, condition, metric type, chemical identity, catalyst/reagent role, stereochemical descriptor, and evidence "
        "boundary. Do not add facts, citations, mechanisms, yields, selectivities, or compounds. Use only the original text "
        "and supplied local evidence. Return JSON {\"text\": \"...\"}.\n\n"
        f"Configured word range: {min_words}-{max_words}.\n"
        f"Paragraph id: {paragraph['paragraph_id']}\n"
        f"Diagnosis: {json.dumps(score, ensure_ascii=False)}\n"
        f"Local evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
        f"Original paragraph: {paragraph['text']}"
    )


def validate_rewrite(original: str, candidate: str, min_words: int, max_words: int) -> list[str]:
    errors: list[str] = []
    cleaned = clean_text(candidate)
    if not cleaned:
        return ["empty_rewrite"]
    words = len(cleaned.split())
    if words < min_words or words > max_words:
        errors.append(f"word_count_{words}_outside_{min_words}_{max_words}")
    before, after = protected_signature(original), protected_signature(candidate)
    for key in before:
        if before[key] != after[key]:
            errors.append(f"protected_{key}_changed")
    if LABEL_SCAFFOLD_RE.search(cleaned) or SCAFFOLD_RE.search(cleaned):
        errors.append("scaffolding_remains")
    return errors


def replace_paragraph_in_markdown(markdown: str, paragraph_id: str, replacement: str) -> str:
    body, references = split_body_references(markdown)
    paragraph = next(
        (item for item in parse_marked_paragraphs(markdown) if item["paragraph_id"] == paragraph_id),
        None,
    )
    if not paragraph:
        raise RuntimeError(f"Paragraph marker disappeared: {paragraph_id}")
    updated = body[: paragraph["start"]] + replacement.strip() + body[paragraph["end"] :]
    return updated + references


def record_rewrite_overlay(project: Path, paragraph_id: str, old_text: str, new_text: str) -> None:
    """Persist a replayable Stage-8 overlay without mutating Stage-5 source outputs."""
    path = project / "04_first_draft" / "feedback_loop_rewrites.json"
    payload = read_json(path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        entries = {}
    previous = entries.get(paragraph_id)
    original_source_hash = (
        str(previous.get("source_text_sha256") or "")
        if isinstance(previous, dict)
        else ""
    )
    entries[paragraph_id] = {
        "paragraph_id": paragraph_id,
        # Keep the hash of the original deterministic Stage-8 paragraph across
        # multiple loop iterations.  Otherwise a later rewrite would replace
        # it with the hash of an earlier rewrite and could no longer be safely
        # replayed after the draft is rebuilt.
        "source_text_sha256": original_source_hash
        or hashlib.sha256(clean_text(old_text).encode("utf-8")).hexdigest(),
        "rewritten_text": new_text.strip(),
        "updated_at": utc_now(),
    }
    write_json(
        path,
        {
            "schema_version": 1,
            "project_id": project.name,
            "policy": "Apply only when paragraph_id and source_text_sha256 still match.",
            "entries": entries,
        },
    )


def apply_rewrite_overlays(project: Path) -> dict[str, Any]:
    """Replay safe feedback rewrites after a future deterministic draft rebuild."""
    draft_path = project / "04_first_draft" / "first_draft.md"
    overlay_path = project / "04_first_draft" / "feedback_loop_rewrites.json"
    payload = read_json(overlay_path, {}) or {}
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not draft_path.is_file() or not isinstance(entries, dict) or not entries:
        return {"applied": [], "conflicts": []}
    markdown = draft_path.read_text(encoding="utf-8", errors="replace")
    applied: list[str] = []
    conflicts: list[str] = []
    for paragraph_id, entry in entries.items():
        current = next(
            (item for item in parse_marked_paragraphs(markdown) if item["paragraph_id"] == paragraph_id),
            None,
        )
        if not current or not isinstance(entry, dict):
            conflicts.append(str(paragraph_id))
            continue
        current_sha = hashlib.sha256(clean_text(current["text"]).encode("utf-8")).hexdigest()
        if current_sha != str(entry.get("source_text_sha256") or ""):
            conflicts.append(str(paragraph_id))
            continue
        rewritten = str(entry.get("rewritten_text") or "").strip()
        if not rewritten:
            conflicts.append(str(paragraph_id))
            continue
        markdown = replace_paragraph_in_markdown(markdown, str(paragraph_id), rewritten)
        applied.append(str(paragraph_id))
    if applied:
        temporary = draft_path.with_suffix(".md.feedback-replay.tmp")
        temporary.write_text(markdown, encoding="utf-8")
        temporary.replace(draft_path)
    report = {"applied": applied, "conflicts": conflicts, "applied_at": utc_now()}
    write_json(project / "04_first_draft" / "feedback_loop_replay.json", report)
    return report


def status_path(project: Path) -> Path:
    return project / "04_first_draft" / "feedback_loop_status.json"


def stop_path(project: Path) -> Path:
    return project / "04_first_draft" / "feedback_loop.stop"


def update_status(project: Path, **updates: Any) -> dict[str, Any]:
    path = status_path(project)
    current = read_json(path, {}) or {}
    current.update(updates)
    current["updated_at"] = utc_now()
    write_json(path, current)
    return current


def reviewer_findings(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"PAR-{index:03d}",
            "reviewer": "rubric_evaluator",
            "severity": item["severity"],
            "paragraph_id": item["paragraph_id"],
            "location": item["paragraph_id"],
            "fragment": "",
            "diagnosis": item["diagnosis"],
            "recommended_direction": "Rewrite only this marked paragraph while preserving protected facts.",
            "confidence": "high",
            "route": item["route"],
        }
        for index, item in enumerate(evaluation.get("paragraph_failures") or [], 1)
    ]


def queue_artifacts(project: Path, evaluation: dict[str, Any], preflight: dict[str, Any]) -> dict[str, Any]:
    first = project / "04_first_draft"
    rewrite = []
    polish = []
    for item in evaluation.get("paragraph_failures") or []:
        target = polish if item.get("route") == "final_polish" else rewrite
        target.append({"origin": "rubric", **item})
    score = float(evaluation.get("total_score", 0))
    goal = float(evaluation.get("pass_threshold", 90))
    hard = sorted(set(evaluation.get("hard_gate_failures") or []) | set(preflight.get("hard_regressions") or []))
    released = score >= goal and not hard and not rewrite
    decision = "GATE_RELEASE" if released else "GATE_HOLD_REWRITE_REQUIRED"
    write_json(first / "first_draft_rewrite_queue.json", {"project_id": project.name, "items": rewrite})
    write_json(first / "first_draft_final_polish_queue.json", {"project_id": project.name, "items": polish})
    gate = {
        "project_id": project.name,
        "status": "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH" if released else "REWRITE_REQUIRED",
        "gate_decision": decision,
        "unified_rubric_score": score,
        "hard_gate_failures": hard,
        "rewrite_queue_path": "04_first_draft/first_draft_rewrite_queue.json",
        "final_polish_queue_path": "04_first_draft/first_draft_final_polish_queue.json",
        "next_action": "Generate final outputs." if released else "Continue targeted paragraph improvement or review blocked facts.",
    }
    write_json(first / "first_draft_gate_status.json", gate)
    return gate


def run_feedback_loop(args: argparse.Namespace) -> dict[str, Any]:
    review_root = Path(args.review_root).resolve()
    project = review_root / "review-projects" / args.project_id
    first = project / "04_first_draft"
    draft_path = first / "first_draft.md"
    if not draft_path.is_file():
        raise FileNotFoundError(draft_path)
    rubric_path = Path(__file__).resolve().parents[1] / "references" / "unified_rubric.json"
    rubric = read_json(rubric_path, {})
    rubric_dimensions(rubric)
    stopper = stop_path(project)
    stopper.unlink(missing_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    runs_dir = first / "feedback_loop" / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(draft_path, run_dir / "first_draft_before.md")
    overlay_path = first / "feedback_loop_rewrites.json"
    overlay_before = overlay_path.read_bytes() if overlay_path.is_file() else None
    update_status(
        project,
        project_id=args.project_id,
        run_id=run_id,
        status="running",
        phase="preflight",
        iteration=0,
        max_iterations=args.max_iterations,
        goal=float(args.goal),
        paragraph_goal=float(args.paragraph_goal),
        started_at=utc_now(),
        source_draft_sha256=sha256_file(draft_path),
        current_paragraph_id="",
        error="",
    )
    best_score = -1.0
    plateau_count = 0
    final_evaluation: dict[str, Any] = {}
    final_preflight: dict[str, Any] = {}
    try:
        for iteration in range(1, int(args.max_iterations) + 1):
            if stopper.exists():
                update_status(
                    project,
                    status="stopped",
                    phase="stopped",
                    iteration=iteration - 1,
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "stopped", "iteration": iteration - 1}
            iteration_dir = run_dir / f"iteration_{iteration:03d}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            update_status(project, phase="preflight", iteration=iteration)
            preflight = deterministic_preflight(
                review_root,
                args.project_id,
                min_words=args.min_case_words,
                max_words=args.max_case_words,
            )
            write_json(iteration_dir / "first_draft_preflight.json", preflight)
            markdown = draft_path.read_text(encoding="utf-8", errors="replace")
            paragraphs = parse_marked_paragraphs(markdown)
            structured = paragraph_metadata(project)
            rows = matrix_rows(project)
            evidence = {
                str(paragraph["paragraph_id"]): source_evidence(
                    review_root,
                    project,
                    paragraph,
                    structured.get(str(paragraph["paragraph_id"]), {}),
                    rows,
                )
                for paragraph in paragraphs
            }
            update_status(
                project,
                phase="scoring",
                paragraph_total=len(paragraphs),
                paragraph_completed=0,
            )
            raw = call_json_model(
                evaluation_prompt(
                    rubric,
                    paragraphs,
                    evidence,
                    preflight,
                    float(args.goal),
                    float(args.paragraph_goal),
                ),
                label="First-draft rubric evaluation",
            )
            evaluation = normalize_evaluation(
                raw,
                rubric,
                paragraphs,
                preflight,
                float(args.goal),
                float(args.paragraph_goal),
            )
            write_json(first / "rubric_evaluation.json", evaluation)
            write_json(first / "reviewer_findings.json", reviewer_findings(evaluation))
            write_json(iteration_dir / "rubric_evaluation.json", evaluation)
            gate = queue_artifacts(project, evaluation, preflight)
            final_evaluation, final_preflight = evaluation, preflight
            paragraph_scores = evaluation.get("paragraph_scores") or []
            update_status(
                project,
                phase="evaluated",
                score=evaluation["total_score"],
                gate_decision=gate["gate_decision"],
                paragraph_scores=paragraph_scores,
                paragraph_completed=len(paragraphs),
            )
            all_paragraphs_pass = all(
                float(item.get("score", 0)) >= float(args.paragraph_goal)
                and item.get("route") in {"pass", "final_polish"}
                and item.get("severity") not in {"critical", "major"}
                for item in paragraph_scores
            )
            if (
                float(evaluation["total_score"]) >= float(args.goal)
                and not evaluation.get("hard_gate_failures")
                and all_paragraphs_pass
            ):
                gate["gate_decision"] = "GATE_RELEASE"
                gate["status"] = "RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH"
                write_json(first / "first_draft_gate_status.json", gate)
                shutil.copy2(draft_path, run_dir / "first_draft_after.md")
                update_status(
                    project,
                    status="completed",
                    phase="released",
                    gate_decision="GATE_RELEASE",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "released", "score": evaluation["total_score"], "iteration": iteration}
            if args.evaluate_only:
                update_status(
                    project,
                    status="completed",
                    phase="evaluated",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "evaluated", "score": evaluation["total_score"], "iteration": iteration}
            score_value = float(evaluation["total_score"])
            if best_score >= 0 and score_value - best_score < float(args.min_improvement):
                plateau_count += 1
            else:
                plateau_count = 0
            best_score = max(best_score, score_value)
            if plateau_count >= 2:
                update_status(
                    project,
                    status="needs_human_review",
                    phase="plateau",
                    error="The score stopped improving across two iterations.",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "needs_human_review", "reason": "plateau", "score": score_value}

            failures = [
                item
                for item in evaluation.get("paragraph_failures") or []
                if item.get("route") == "section_rewrite"
                and float(item.get("score", 0)) < float(args.paragraph_goal)
            ]
            accepted = 0
            for index, failure in enumerate(failures, 1):
                if stopper.exists():
                    update_status(
                        project,
                        status="stopped",
                        phase="stopped",
                        current_paragraph_id="",
                        finished_at=utc_now(),
                        output_draft_sha256=sha256_file(draft_path),
                    )
                    return {"status": "stopped", "iteration": iteration}
                paragraph_id = str(failure["paragraph_id"])
                current_markdown = make_xml_compatible(
                    draft_path.read_text(encoding="utf-8", errors="replace")
                )[0]
                current_paragraph = next(
                    (item for item in parse_marked_paragraphs(current_markdown) if item["paragraph_id"] == paragraph_id),
                    None,
                )
                if not current_paragraph:
                    continue
                update_status(
                    project,
                    phase="rewriting",
                    current_paragraph_id=paragraph_id,
                    rewrite_total=len(failures),
                    rewrite_completed=index - 1,
                )
                response = call_json_model(
                    rewrite_prompt(
                        current_paragraph,
                        failure,
                        evidence.get(paragraph_id, {}),
                        args.min_case_words,
                        args.max_case_words,
                    ),
                    label=f"Paragraph rewrite {paragraph_id}",
                )
                candidate = str(response.get("text") or "").strip()
                validation_errors = validate_rewrite(
                    str(current_paragraph["text"]),
                    candidate,
                    args.min_case_words,
                    args.max_case_words,
                )
                if validation_errors:
                    write_json(
                        iteration_dir / f"{paragraph_id}_rejected.json",
                        {"errors": validation_errors, "candidate": candidate},
                    )
                    continue
                snapshot = run_dir / f"before_{iteration:03d}_{paragraph_id}.md"
                shutil.copy2(draft_path, snapshot)
                updated = replace_paragraph_in_markdown(current_markdown, paragraph_id, candidate)
                temporary = draft_path.with_suffix(".md.feedback.tmp")
                temporary.write_text(make_xml_compatible(updated)[0], encoding="utf-8")
                temporary.replace(draft_path)
                try:
                    record_rewrite_overlay(
                        project,
                        paragraph_id,
                        str(current_paragraph["text"]),
                        candidate,
                    )
                except Exception:
                    shutil.copy2(snapshot, draft_path)
                    raise
                accepted += 1
                update_status(project, rewrite_completed=index, current_paragraph_id=paragraph_id)
            update_status(project, current_paragraph_id="", rewrite_accepted=accepted)
            if not accepted:
                update_status(
                    project,
                    status="needs_human_review",
                    phase="rewrite_blocked",
                    error="No proposed rewrite passed the protected-fact and citation checks.",
                    finished_at=utc_now(),
                    output_draft_sha256=sha256_file(draft_path),
                )
                return {"status": "needs_human_review", "reason": "no_safe_rewrite", "score": score_value}
        shutil.copy2(draft_path, run_dir / "first_draft_after.md")
        update_status(
            project,
            status="needs_human_review",
            phase="iteration_limit",
            error="The configured iteration limit was reached before the goal.",
            finished_at=utc_now(),
            output_draft_sha256=sha256_file(draft_path),
        )
        return {
            "status": "needs_human_review",
            "reason": "iteration_limit",
            "score": final_evaluation.get("total_score"),
            "hard_gate_failures": final_preflight.get("hard_regressions", []),
        }
    except Exception as exc:
        # A transport or schema failure must not leave a partially rewritten
        # manuscript outside the normal Draft handoff. Restore both the draft
        # and its replay overlay to the exact pre-run state.
        shutil.copy2(run_dir / "first_draft_before.md", draft_path)
        if overlay_before is None:
            overlay_path.unlink(missing_ok=True)
        else:
            overlay_tmp = overlay_path.with_suffix(overlay_path.suffix + ".restore.tmp")
            overlay_tmp.write_bytes(overlay_before)
            overlay_tmp.replace(overlay_path)
        update_status(
            project,
            status="failed",
            phase="failed",
            error=f"{type(exc).__name__}: {exc}",
            finished_at=utc_now(),
            output_draft_sha256=sha256_file(draft_path),
        )
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--goal", type=float, default=90.0)
    parser.add_argument("--paragraph-goal", type=float, default=85.0)
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--min-improvement", type=float, default=1.0)
    parser.add_argument("--min-case-words", type=int, default=140)
    parser.add_argument("--max-case-words", type=int, default=280)
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.goal <= 100 or not 0 <= args.paragraph_goal <= 100:
        parser.error("Goals must be between 0 and 100.")
    if not 1 <= args.max_iterations <= 10:
        parser.error("max-iterations must be between 1 and 10.")
    if args.min_case_words < 1 or args.max_case_words < args.min_case_words:
        parser.error("Invalid case word range.")
    return args


def main() -> int:
    result = run_feedback_loop(parse_args())
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
