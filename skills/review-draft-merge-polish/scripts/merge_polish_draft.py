#!/usr/bin/env python3
"""Create review-level framing and transitions for already source-grounded sections."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


_BOOTSTRAP_ROOT = next(
    (p for p in Path(__file__).resolve().parents if (p / "review_writer_core").is_dir()),
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


class ModelResponseError(RuntimeError):
    """A provider returned a successful transport response without usable model JSON."""


def openai_endpoint(base_url: str, endpoint: str) -> str:
    """Accept OpenAI-compatible base URLs with or without a trailing /v1."""
    return _shared_openai_endpoint(base_url, endpoint)


def resolve_api_key(cli_value: str, base_url: str, dotenv: dict[str, str] | None = None) -> str:
    del base_url
    return _shared_resolve_api_key(
        cli_value,
        env_names=("REVIEW_WRITING_API_KEY", "OPENAI_API_KEY"),
        dotenv=dotenv,
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dotenv(root: Path) -> dict[str, str]:
    path = root / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    return values


def response_json(response: Any, endpoint: str) -> dict[str, Any]:
    """Decode a provider response with actionable errors for blank relay bodies."""
    raw = response.read()
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ModelResponseError(f"{endpoint} returned an empty response body")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:160]
        raise ModelResponseError(f"{endpoint} returned non-JSON content: {preview!r}") from exc
    if not isinstance(payload, dict):
        raise ModelResponseError(f"{endpoint} returned JSON {type(payload).__name__}, not an object")
    return payload


def parse_framing(text: Any, endpoint: str) -> dict[str, Any]:
    """Accept JSON text and the common Markdown-fenced compatibility response."""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value).strip()
    if not value:
        raise ModelResponseError(f"{endpoint} returned no framing content")
    try:
        framing = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelResponseError(f"{endpoint} returned malformed framing JSON") from exc
    if not isinstance(framing, dict):
        raise ModelResponseError(f"{endpoint} returned framing JSON {type(framing).__name__}, not an object")
    return framing


def make_request(url: str, payload: dict[str, Any], api_key: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # The configured relay rejects Python's default user agent.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    )


def call_llm(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    wire_api: str = "responses",
) -> dict[str, Any]:
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["title", "introduction", "transitions"],
        "properties": {
            "title": {"type": "string"},
            "introduction": {"type": "string"},
            "transitions": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    wire = str(wire_api or "responses").strip().lower().replace("_", "-")
    response_error: Exception | None = None
    if wire not in {"chat", "chat-completion", "chat-completions"}:
        endpoint = openai_endpoint(base_url, "responses")
        payload = {"model": model, "input": [{"role": "user", "content": prompt}], "text": {"format": {"type": "json_schema", "name": "review_merge", "schema": schema, "strict": True}}}
        request = make_request(endpoint, payload, api_key)
        # A relay can occasionally return a transient gateway error or an empty body
        # for an otherwise valid Responses request. Retry both transport and payload
        # failures before using the broadly supported Chat Completions compatibility path.
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=300) as response:
                    data = response_json(response, endpoint)
                text = data.get("output_text") or "\n".join(content.get("text", "") for output in data.get("output", []) for content in output.get("content", []) if content.get("type") in {"output_text", "text"})
                return parse_framing(text, endpoint)
            except urllib.error.HTTPError as exc:
                response_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, ModelResponseError) as exc:
                response_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    chat_endpoint = openai_endpoint(base_url, "chat/completions")
    chat_prompt = f"{prompt}\n\nReturn only one JSON object with the keys title, introduction, and transitions."
    chat_payload = {
        "model": model,
        "messages": [{"role": "user", "content": chat_prompt}],
        "response_format": {"type": "json_object"},
    }
    try:
        with urllib.request.urlopen(
            make_request(chat_endpoint, chat_payload, api_key),
            context=ssl.create_default_context(),
            timeout=300,
        ) as response:
            chat_data = response_json(response, chat_endpoint)
        choices = chat_data.get("choices") if isinstance(chat_data.get("choices"), list) else []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        return parse_framing(content, chat_endpoint)
    except (urllib.error.HTTPError, urllib.error.URLError, ModelResponseError) as exc:
        detail = f"Responses failure: {response_error}; chat-completions failure: {exc}"
        raise ModelResponseError(detail) from exc


REFERENCE_LINE_RE = re.compile(r"^\[(\d+)\]\s+(.+?)\s+\[([^\[\]]+)\]\s*$")


def split_body_and_reference_entries(draft_md: str) -> tuple[str, list[dict[str, Any]]]:
    """Split one section's draft into its body and its parsed References entries.

    Every section written by generate_section_drafts.py ends with its own
    ``## References`` block using the shared global citation_map, so the same
    paper always gets the same [n] across every section. Splitting here (and
    consolidating the parsed entries once in main()) is what turns those
    per-section duplicates into the single final References section the
    Hard Output Requirements demand, instead of concatenating them verbatim.
    """
    marker = re.search(r"\n##\s+References\s*\n", draft_md)
    if marker is None:
        return draft_md.strip(), []
    body = draft_md[: marker.start()].strip()
    tail = draft_md[marker.end():]
    entries: list[dict[str, Any]] = []
    for line in tail.splitlines():
        match = REFERENCE_LINE_RE.match(line.strip())
        if not match:
            continue
        callout, reference_text, paper_id = match.groups()
        entries.append(
            {
                "callout": int(callout),
                "paper_id": paper_id,
                "cited_paper_ids": [paper_id],
                "reference": f"[{callout}] {reference_text} [{paper_id}]",
            }
        )
    return body, entries


def build_citation_entries(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate every section's parsed References entries into one ordered list.

    citations.json is the documented hard-gated contract that aggregates each
    paragraph's cited_paper_ids into a single ordered list per [n] slot; no
    other script in this pipeline produces it from the section drafts, so it
    is built here from the same References blocks split_body_and_reference_entries
    already parses out of each section.
    """
    by_callout: dict[int, dict[str, Any]] = {}
    for section in sections:
        _, entries = split_body_and_reference_entries(str(section.get("draft_md") or ""))
        for entry in entries:
            by_callout.setdefault(entry["callout"], entry)
    return [by_callout[callout] for callout in sorted(by_callout)]


def section_brief(section: dict[str, Any]) -> dict[str, Any]:
    """Send only the evidence required for framing; preserve full prose locally."""
    paragraphs = section.get("paragraphs") if isinstance(section.get("paragraphs"), list) else []
    evidence = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, dict):
            continue
        paper_id = str(paragraph.get("paper_id") or "").strip()
        excerpt = " ".join(str(paragraph.get("text") or "").split())[:240]
        if paper_id and excerpt:
            evidence.append({"paper_id": paper_id, "evidence": excerpt})
    return {
        "section_id": section.get("section_id"),
        "heading": section.get("heading"),
        "overview": " ".join(str(section.get("overview") or "").split())[:900],
        "paper_evidence": evidence,
    }


def fallback_framing(topic: str, sections: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep the manuscript pipeline usable during a temporary model outage."""
    quoted_topic = re.search(r'"([^"\n]+)"', topic)
    title = quoted_topic.group(1).strip() if quoted_topic else topic.strip()
    title = title or "Scientific Review"
    headings = [str(section.get("heading") or section.get("section_id") or "").strip() for section in sections]
    headings = [heading for heading in headings if heading]
    scope = "; ".join(headings)
    introduction = (
        f"This review examines {title.lower()} through the substrate classes represented in the selected literature. "
        f"The discussion is organized around {scope}, so that reaction design, selectivity, scope, and limitations can be compared within each precursor family.\n\n"
        "Each paper-level subsection preserves the source-grounded analysis prepared in the Sections stage. "
        "The transitions below are editorial signposts only and do not add literature claims while the writing model is unavailable."
    )
    transitions = {
        str(section.get("section_id")): (
            f"The next section considers {headings[index + 1]}, where the precursor class and comparison constraints change."
        )
        for index, section in enumerate(sections[:-1])
        if index + 1 < len(headings)
    }
    return {"title": title, "introduction": introduction, "transitions": transitions}


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge and polish source-grounded review sections.")
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--wire-api", default="")
    args = parser.parse_args()
    root = resolve_review_root(args.review_root, anchor=Path(__file__))
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
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY. Configure it before merging the draft.")
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
    sections_data = read_json(project / "02_section_drafting" / "section_drafts.json")
    sections = sections_data.get("sections") if isinstance(sections_data, dict) else []
    if not isinstance(sections, list) or not sections:
        raise SystemExit("No source-grounded section drafts were found.")
    topic = ""
    topic_input = project / "00_discovery" / "topic_input.md"
    if topic_input.exists():
        topic = next((line[2:].strip() for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines() if line.startswith("# ")), "")
    summaries = [section_brief(item) for item in sections if isinstance(item, dict)]
    prompt = f"""You are editing a scientific review on {topic or args.project_id}.
Write a concise review title, a 2-3 paragraph Introduction, and one bridging transition for every section ID except the final section. The introduction and transitions must synthesize the supplied section evidence; do not invent literature facts, use paper IDs, or add numeric citations. Preserve the supplied section prose unchanged.
Section evidence summaries:\n{json.dumps(summaries, ensure_ascii=False)}"""
    fallback_reason = ""
    try:
        framing = call_llm(prompt, api_key, base_url, model, wire_api)
    except urllib.error.HTTPError as exc:
        fallback_reason = f"HTTP {exc.code}"
        framing = fallback_framing(topic, sections)
    except urllib.error.URLError as exc:
        fallback_reason = f"unreachable: {exc.reason}"
        framing = fallback_framing(topic, sections)
    except ModelResponseError as exc:
        fallback_reason = str(exc)
        framing = fallback_framing(topic, sections)
    # If the outline's own first section is already titled "Introduction" (a
    # common, reasonable outline choice), its section_body below will start
    # with its own "## Introduction" heading. Emitting the fixed heading here
    # too would duplicate it; keep the framing's synthesized intro prose as a
    # lead-in and let that section's own heading be the only one.
    first_section_body, _ = (
        split_body_and_reference_entries(str(sections[0].get("draft_md") or "")) if sections else ("", [])
    )
    first_heading_is_introduction = bool(
        re.match(r"^##\s+introduction\s*$", first_section_body.splitlines()[0].strip(), re.I)
    ) if first_section_body else False
    framing_intro = str(framing.get("introduction") or "").strip()
    if first_heading_is_introduction:
        body = [f"# {str(framing.get('title') or topic or args.project_id).strip()}", ""]
        if framing_intro:
            body.extend([framing_intro, ""])
    else:
        body = [f"# {str(framing.get('title') or topic or args.project_id).strip()}", "", "## Introduction", "", framing_intro, ""]
    transitions = framing.get("transitions") if isinstance(framing.get("transitions"), dict) else {}
    citation_entries = build_citation_entries(sections)
    for index, section in enumerate(sections):
        section_body, _ = split_body_and_reference_entries(str(section.get("draft_md") or ""))
        body.extend([section_body, ""])
        if index < len(sections) - 1:
            transition = str(transitions.get(str(section.get("section_id")), "")).strip()
            if transition:
                body.extend([transition, ""])
    body.extend(["", "## References", ""])
    body.extend(entry["reference"] for entry in citation_entries)
    out = project / "04_first_draft"
    out.mkdir(parents=True, exist_ok=True)
    merged_text, _ = make_xml_compatible("\n".join(body).strip() + "\n")
    (out / "first_draft.md").write_text(merged_text, encoding="utf-8")
    (out / "citations.json").write_text(
        json.dumps({"entries": citation_entries}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    status = (
        f"Generated review framing and section transitions with model `{model}`."
        if not fallback_reason
        else f"Writing model was unavailable ({fallback_reason}); generated deterministic framing and transitions instead."
    )
    (out / "merge_report.md").write_text(
        f"# Merge Report\n\n{status} Section prose remained source-grounded and unchanged.\n",
        encoding="utf-8",
    )
    print("Merged and polished draft framing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
