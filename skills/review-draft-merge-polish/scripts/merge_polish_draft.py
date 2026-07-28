#!/usr/bin/env python3
"""Create review-level framing and transitions for already source-grounded sections."""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def openai_endpoint(base_url: str, endpoint: str) -> str:
    """Accept OpenAI-compatible base URLs with or without a trailing /v1."""
    base = str(base_url or "https://api.openai.com").rstrip("/")
    prefix = "" if base.lower().endswith("/v1") else "/v1"
    return f"{base}{prefix}/{endpoint.lstrip('/')}"


def resolve_api_key(cli_value: str, base_url: str) -> str:
    if cli_value:
        return cli_value
    if "api.xiaoleai.team" in str(base_url).lower():
        return os.environ.get("XIAOLEAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("XIAOLEAI_API_KEY", "")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dotenv(root: Path) -> None:
    path = root / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def call_llm(prompt: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["title", "introduction", "transitions"],
        "properties": {
            "title": {"type": "string"},
            "introduction": {"type": "string"},
            "transitions": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    payload = {"model": model, "input": [{"role": "user", "content": prompt}], "text": {"format": {"type": "json_schema", "name": "review_merge", "schema": schema, "strict": True}}}
    request = urllib.request.Request(
        openai_endpoint(base_url, "responses"), data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # The configured relay rejects Python's default user agent.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    )
    # A relay can occasionally return a transient gateway error for an otherwise
    # valid request. Retrying keeps this framing-only step from blocking a draft.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=300) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    text = data.get("output_text") or "\n".join(content.get("text", "") for output in data.get("output", []) for content in output.get("content", []) if content.get("type") in {"output_text", "text"})
    return json.loads(text)


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
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    root = Path(args.review_root).resolve()
    load_dotenv(root)
    base_url = args.base_url or os.environ.get("REVIEW_WRITING_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    api_key = resolve_api_key(args.api_key, base_url)
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY. Configure it before merging the draft.")
    model = args.model or os.environ.get("REVIEW_WRITING_MODEL", "gpt-5.4")
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
        framing = call_llm(prompt, api_key, base_url, model)
    except urllib.error.HTTPError as exc:
        fallback_reason = f"HTTP {exc.code}"
        framing = fallback_framing(topic, sections)
    except urllib.error.URLError as exc:
        fallback_reason = f"unreachable: {exc.reason}"
        framing = fallback_framing(topic, sections)
    body = [f"# {str(framing.get('title') or topic or args.project_id).strip()}", "", "## Introduction", "", str(framing.get("introduction") or "").strip(), ""]
    transitions = framing.get("transitions") if isinstance(framing.get("transitions"), dict) else {}
    for index, section in enumerate(sections):
        body.extend([str(section.get("draft_md") or "").strip(), ""])
        if index < len(sections) - 1:
            transition = str(transitions.get(str(section.get("section_id")), "")).strip()
            if transition:
                body.extend([transition, ""])
    out = project / "04_first_draft"
    out.mkdir(parents=True, exist_ok=True)
    (out / "first_draft.md").write_text("\n".join(body).strip() + "\n", encoding="utf-8")
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
