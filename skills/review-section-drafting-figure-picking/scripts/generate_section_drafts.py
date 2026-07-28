#!/usr/bin/env python3
"""Generate source-grounded review sections from Blueprint tasks and MinerU Markdown."""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dotenv(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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


def call_llm(prompt: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["overview", "paragraphs"],
        "properties": {
            "overview": {"type": "string"},
            "paragraphs": {
                "type": "array", "minItems": 1,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["text", "paper_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "paper_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }
    payload = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_schema", "name": "review_section", "schema": schema, "strict": True}},
    }
    request = urllib.request.Request(
        openai_endpoint(base_url, "responses"),
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
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=300) as response:
        data = json.loads(response.read().decode("utf-8"))
    text = data.get("output_text") or ""
    if not text:
        text = "\n".join(
            content.get("text", "")
            for output in data.get("output", []) for content in output.get("content", [])
            if content.get("type") in {"output_text", "text"}
        )
    parsed = json.loads(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("paragraphs"), list):
        raise RuntimeError("Section-writing model returned an invalid response.")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate review sections with an OpenAI-compatible writing model.")
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
        raise SystemExit("Missing OPENAI_API_KEY. Configure it in the environment or local .env before generating sections.")
    model = args.model or os.environ.get("REVIEW_WRITING_MODEL", "gpt-5.4")
    project = root / "review-projects" / args.project_id
    stage = project / "02_section_drafting"
    tasks = read_json(stage / "section_tasks.json")
    matrix = read_json(project / "01_matrix_outline" / "literature_matrix.json")
    rows_list = matrix.get("rows") if isinstance(matrix, dict) else matrix
    rows = {str(row.get("paper_id")): row for row in rows_list or [] if isinstance(row, dict) and row.get("paper_id")}
    blueprint = read_json(project / "01_matrix_outline" / "section_blueprint.json")
    selected_outline_path = project / "01_matrix_outline" / "selected_outline.md"
    selected_outline = selected_outline_path.read_text(encoding="utf-8", errors="ignore")[:12000] if selected_outline_path.exists() else ""
    rule_path = root / "skills" / "review-section-blueprint" / "references" / "rule_packs" / "allenation" / "organic-review-style.md"
    rules = rule_path.read_text(encoding="utf-8", errors="ignore")[:14000]
    section_specs = {str(item.get("section_id")): item for item in blueprint.get("sections", []) if isinstance(item, dict)}
    paper_order = [str(pid) for task in tasks for pid in task.get("allowed_papers", []) if str(pid) in rows]
    citation_map = {paper_id: index for index, paper_id in enumerate(dict.fromkeys(paper_order), start=1)}
    sections_dir = stage / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    output_sections = []
    for task in tasks:
        section_id = str(task.get("section_id"))
        allowed = [str(pid) for pid in task.get("allowed_papers", []) if str(pid) in rows]
        evidence = [paper_evidence(root, rows, paper_id) for paper_id in allowed]
        if not evidence or not any(item["evidence"] for item in evidence):
            raise SystemExit(f"No usable MinerU Markdown or matrix evidence for {section_id}.")
        spec = section_specs.get(section_id, {})
        prompt = f"""Write one body section of a source-grounded organic-chemistry review.

Topic: {blueprint.get('review_topic') or project.name}
Selected review outline (preserve its ordering and heading intent):
{selected_outline}

Section title: {task.get('heading')}
Section thesis: {task.get('core_argument')}
Required claims: {json.dumps(task.get('must_cover_points') or [], ensure_ascii=False)}
Comparison axes and constraints: {json.dumps(spec.get('review_claims') or [], ensure_ascii=False)[:10000]}
Allowed paper IDs only: {', '.join(allowed)}

Return a 90-150 word `overview`, followed by exactly one complete review paragraph for every allowed paper, in the same order as the allowed-paper list. The overview introduces the comparison axis for this section and must contain no citations or paper IDs.

Each paper paragraph must explain why that paper matters, its main transformation, the relevant substrate/product/catalyst/selectivity or mechanism evidence, and a review-level limitation or judgment where supported. Do not group papers and do not use paper titles as prose. Set `paper_ids` to a one-item list containing only the paper discussed in that paragraph. Do not put citations or paper IDs in `text`; the workflow adds numeric citations after validation. Preserve limitations and use conditional language for proposed mechanisms. Do not invent conditions, yields, selectivities, structures, or mechanistic evidence.

Writing rules:\n{rules}

Source evidence (only use claims supported here):\n{json.dumps(evidence, ensure_ascii=False)}
"""
        try:
            generated = call_llm(prompt, api_key, base_url, model)
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
        by_paper: dict[str, str] = {}
        for item in generated["paragraphs"]:
            paper_ids = [str(pid) for pid in item.get("paper_ids", []) if str(pid) in citation_map]
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
            if len(paper_ids) != 1 or not text or paper_ids[0] in by_paper:
                continue
            by_paper[paper_ids[0]] = text
        missing_papers = [paper_id for paper_id in allowed if paper_id not in by_paper]
        if missing_papers:
            raise SystemExit(
                f"The writing model did not produce one usable paragraph for every paper in {section_id}: "
                + ", ".join(missing_papers)
            )
        paragraphs = []
        markdown = [f"## {task.get('heading')}", "", overview, ""]
        for index, paper_id in enumerate(allowed, start=1):
            paragraph_id = f"{section_id}-p{index}"
            callout = f"[{citation_map[paper_id]}]"
            text = by_paper[paper_id] + " " + callout
            paper_title = str(rows[paper_id].get("title") or paper_id).strip()
            markdown.extend([
                f"### {paper_id}. {paper_title}",
                "",
                text,
                "",
                f"<!-- paragraph_id: {paragraph_id} -->",
                "",
            ])
            paragraphs.append({"paragraph_id": paragraph_id, "paper_id": paper_id, "cited_paper_ids": [paper_id], "text": text})
        section_text = "\n".join(markdown).strip() + "\n"
        (sections_dir / f"{section_id}.md").write_text(section_text, encoding="utf-8")
        output_sections.append({"section_id": section_id, "heading": task.get("heading"), "overview": overview, "paragraphs": paragraphs, "draft_md": section_text})
    write_json(stage / "section_drafts.json", {"project_id": args.project_id, "sections": output_sections})
    (stage / "section_drafts.md").write_text("\n\n".join(section["draft_md"] for section in output_sections), encoding="utf-8")
    (stage / "section_drafting_report.md").write_text(
        f"# Section Drafting Report\n\nGenerated {len(output_sections)} source-grounded sections with model `{model}`.\n", encoding="utf-8"
    )
    print(f"Generated {len(output_sections)} sections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
