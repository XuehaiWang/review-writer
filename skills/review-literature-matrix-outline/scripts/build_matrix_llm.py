#!/usr/bin/env python3
"""Build literature_matrix.json rows with isolated, resumable LLM API calls.

Reading selected papers inside a long agent conversation re-carries the whole
accumulated context on every turn; for 20-30 papers this is the single largest
token cost in the pipeline. This script replaces that with one bounded,
isolated API call per paper (the batch_llm_retag_metadata.py architecture):

  input per call:  the paper's metadata (title/abstract/structured tags) plus
                   the first --max-chars characters of its MinerU Markdown
  output per call: one matrix row JSON written to rows/<paper_id>.row.json

Rows already on disk are skipped (resumable; use --force to rebuild), and
literature_matrix.json / literature_matrix.csv are assembled from the row
files at the end of every run. The agent then only reads the finished matrix,
never the papers themselves, except for targeted spot-checks.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MATRIX_SYSTEM_PROMPT = """You are building one row of a literature matrix for an academic review, from one paper's metadata and the beginning of its full text (MinerU Markdown, which may contain OCR/LaTeX artifacts — read through them).

Return ONLY a JSON object with exactly these fields:

{
  "paper_id": "<copy from input>",
  "title": "<clean title; fix obvious OCR/LaTeX garbling>",
  "authors": ["Last, F.", ...],
  "keywords": {"output": "...", "input": "...", "method": "...", "co_input": "...", "modifier": "...", "process_type": "...", "document_scope": "..."},
  "abstract": "<metadata abstract if reliable, else a faithful 3-5 sentence abstract from the text, else 'abstract unavailable or unreliable'>",
  "main_content": "<300-450 English words summarizing the paper's actual work: the problem it addresses, its core method/conditions with REAL numbers (yields, loadings, temperatures, scales) taken from the text, its scope, its key selectivity or comparison findings, its stated limitations, and how it relates to prior work it cites. Do not pad; do not invent numbers.>",
  "most_relevant_figure": {"source_label": "<e.g. Scheme 1 / Figure 2 / Table 1>", "caption": "<its caption text>", "page_hint": "<rough position>", "image_path": "<the ![](...) path from the Markdown if one clearly belongs to this figure, else omit this key>", "why_relevant": "<one sentence>"},
  "output": "<main product/result, short phrase>",
  "input": "<main starting material/subject, short phrase>",
  "method": "<core method/technique, short phrase>",
  "process_type": "<process/study type, short phrase>",
  "limitation": "<the paper's most important limitation, one clause>",
  "selectivity": "<the paper's key selectivity/discrimination finding, one clause; omit this key if not applicable>"
}

Rules:
- Ground every claim in the provided text or metadata. If the excerpt lacks something, say so ("not stated in excerpt") rather than inventing it.
- If the paper's structured tags are provided and sensible, reuse them for "keywords" and the short-phrase fields instead of re-deriving.
- For fields that do not apply, OMIT the key entirely (do not write "not applicable" or "none").
- most_relevant_figure should be the figure/scheme/table that best captures the paper's principle or main result.
- Output raw JSON only — no markdown fences, no commentary."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def field_value(field: Any, default: Any = None) -> Any:
    if isinstance(field, dict) and "value" in field:
        return field.get("value", default)
    return field if field is not None else default


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def call_responses(payload: dict[str, Any], api_key: str, base_url: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "review-writer-matrix-builder/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data.get("output_text")
    if not text:
        parts = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and content.get("text"):
                    parts.append(content["text"])
        text = "\n".join(parts)
    if not text:
        raise RuntimeError("response missing output_text")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def build_payload(meta: dict[str, Any], markdown_text: str, model: str, reasoning_effort: str) -> dict[str, Any]:
    user_input = {
        "paper_id": meta.get("paper_id"),
        "metadata": {
            "title": field_value(meta.get("title"), ""),
            "authors": field_value(meta.get("authors"), []),
            "year": field_value(meta.get("year")),
            "journal": field_value(meta.get("journal")),
            "abstract": field_value(meta.get("abstract"), ""),
            "structured_tags": field_value(meta.get("structured_tags"), {}),
        },
        "markdown_head": markdown_text,
    }
    payload: dict[str, Any] = {
        "model": model,
        "instructions": MATRIX_SYSTEM_PROMPT,
        "input": json.dumps(user_input, ensure_ascii=False),
    }
    if reasoning_effort and reasoning_effort != "none":
        payload["reasoning"] = {"effort": reasoning_effort}
    return payload


REQUIRED_ROW_FIELDS = ["paper_id", "title", "authors", "keywords", "abstract", "main_content", "most_relevant_figure"]


def validate_row(row: dict[str, Any], expected_pid: str) -> None:
    missing = [f for f in REQUIRED_ROW_FIELDS if f not in row]
    if missing:
        raise RuntimeError(f"row missing required fields: {missing}")
    if row.get("paper_id") != expected_pid:
        raise RuntimeError(f"row paper_id mismatch: got {row.get('paper_id')}, expected {expected_pid}")


def selected_paper_ids(discovery_file: Path) -> list[str]:
    sel = read_json(discovery_file)
    ids = [str(p["paper_id"]) for p in sel.get("local_papers") or [] if p.get("paper_id")]
    if not ids:
        raise SystemExit(f"no local_papers with paper_id in {discovery_file}")
    return ids


def assemble(out_dir: Path, rows_dir: Path, project_id: str, topic: str) -> int:
    rows = []
    for path in sorted(rows_dir.glob("*.row.json")):
        try:
            rows.append(read_json(path))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("paper_id") or "")
    write_json(out_dir / "literature_matrix.json", {"project_id": project_id, "topic": topic, "papers": rows})
    with (out_dir / "literature_matrix.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["paper_id", "title", "year", "output", "input", "method", "process_type", "most_relevant_figure", "selectivity", "limitation"])
        for r in rows:
            fig = r.get("most_relevant_figure") or {}
            w.writerow([
                r.get("paper_id"), r.get("title"), r.get("year", ""),
                r.get("output", ""), r.get("input", ""), r.get("method", ""), r.get("process_type", ""),
                f"{fig.get('source_label', '')}: {fig.get('caption', '')}"[:200],
                r.get("selectivity", ""), r.get("limitation", ""),
            ])
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build literature matrix rows with isolated resumable LLM calls.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--reasoning-effort", default="", choices=["", "none", "low", "medium", "high"])
    parser.add_argument("--max-chars", type=int, default=9000, help="Markdown head budget per paper (default 9000, matching the skill's reading cap).")
    parser.add_argument("--paper-id", action="append", default=[], help="Only build these paper_ids. Repeatable.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Rebuild rows that already exist.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=15.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true", help="Resolve inputs and report what would be sent, without calling the API.")
    args = parser.parse_args()

    review_root = Path(args.review_root).resolve()
    load_dotenv(review_root / ".env")
    proj = review_root / "review-projects" / args.project_id
    discovery_file = proj / "00_discovery" / "selected_discovery_results.json"
    if not discovery_file.exists():
        raise SystemExit(f"discovery results not found: {discovery_file}")
    out_dir = proj / "01_matrix_outline"
    rows_dir = out_dir / "rows"
    rows_dir.mkdir(parents=True, exist_ok=True)

    topic = ""
    topic_file = proj / "00_discovery" / "topic_input.md"
    if topic_file.exists():
        first = topic_file.read_text(encoding="utf-8").splitlines()
        topic = first[0].lstrip("# ").strip() if first else ""

    ids = selected_paper_ids(discovery_file)
    if args.paper_id:
        wanted = set(args.paper_id)
        ids = [i for i in ids if i in wanted]
    if args.limit > 0:
        ids = ids[: args.limit]

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    model = args.model or os.environ.get("REVIEW_MATRIX_MODEL", os.environ.get("REVIEW_METADATA_MODEL", "gpt-5.4"))
    reasoning_effort = args.reasoning_effort or os.environ.get("REVIEW_MATRIX_REASONING_EFFORT", "medium")
    if not api_key and not args.dry_run:
        raise SystemExit("Missing API key. Pass --api-key, set OPENAI_API_KEY, or write it to <review-root>/.env.")

    meta_dir = review_root / "review-library" / "metadata" / "papers"
    reports: list[dict[str, Any]] = []
    for pid in ids:
        row_path = rows_dir / f"{pid}.row.json"
        if row_path.exists() and not args.force:
            reports.append({"paper_id": pid, "status": "skipped_existing"})
            continue
        meta_path = meta_dir / f"{pid}.metadata.json"
        if not meta_path.exists():
            reports.append({"paper_id": pid, "status": "failed", "error": "metadata file missing"})
            print(f"{pid} failed: metadata missing")
            continue
        meta = read_json(meta_path)
        md_path = Path(str((meta.get("source_paths") or {}).get("markdown") or ""))
        md_text = md_path.read_text(encoding="utf-8", errors="ignore")[: args.max_chars] if md_path.is_file() else ""
        if args.dry_run:
            reports.append({"paper_id": pid, "status": "dry_run", "markdown_chars": len(md_text), "has_tags": bool(field_value(meta.get("structured_tags")))})
            print(f"{pid} dry-run: {len(md_text)} md chars")
            continue
        payload = build_payload(meta, md_text, model, reasoning_effort)
        last_error = ""
        for attempt in range(1, args.max_attempts + 1):
            try:
                row = call_responses(payload, api_key, base_url, args.timeout)
                validate_row(row, pid)
                row["built_at"] = utc_now()
                row["builder_model"] = model
                write_json(row_path, row)
                reports.append({"paper_id": pid, "status": "ok", "attempts": attempt})
                print(f"{pid} ok ({len(row.get('main_content', '').split())} words main_content)")
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < args.max_attempts:
                    time.sleep(args.retry_delay)
        else:
            reports.append({"paper_id": pid, "status": "failed", "error": last_error})
            print(f"{pid} failed after {args.max_attempts} attempts: {last_error}")
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    if args.dry_run:
        print(f"Dry run: {len(reports)} paper(s) resolved; no API calls made, matrix files untouched.")
        return 0
    if not any(rows_dir.glob("*.row.json")) and (out_dir / "literature_matrix.json").exists():
        # No script-built rows exist but a matrix does (e.g. agent-written earlier):
        # never clobber it with an empty assembly.
        print("No row files present and literature_matrix.json already exists; leaving it untouched.")
        return 1 if any(r["status"] == "failed" for r in reports) else 0
    count = assemble(out_dir, rows_dir, args.project_id, topic)
    write_json(out_dir / "matrix_build_report.json", {
        "project_id": args.project_id,
        "finished_at": utc_now(),
        "rows_assembled": count,
        "reports": reports,
    })
    failed = [r for r in reports if r["status"] == "failed"]
    print(f"Assembled {count} matrix rows -> {out_dir / 'literature_matrix.json'}")
    if failed:
        print(f"{len(failed)} paper(s) failed; re-run the same command to retry only those.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
