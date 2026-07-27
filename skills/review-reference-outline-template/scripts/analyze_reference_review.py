#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import statistics
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF analysis requires the pypdf package.") from exc
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def read_reference(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix == ".docx":
        return read_docx(path)
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    raise ValueError("Supported reference files are PDF, DOCX, Markdown, and text.")


def clean_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" -.:;")


def heading_rows(text: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for line in text.splitlines():
        value = line.strip()
        if not value or len(value) > 140:
            continue
        markdown = re.match(r"^(#{1,6})\s+(.+?)\s*$", value)
        numbered = re.match(r"^(\d+(?:\.\d+){0,3})[.)]?\s+(.+?)\s*$", value)
        roman = re.match(r"^([IVXLC]+)[.)]\s+(.+?)\s*$", value, flags=re.I)
        if markdown:
            level, title = len(markdown.group(1)), clean_heading(markdown.group(2))
        elif numbered:
            level, title = numbered.group(1).count(".") + 1, clean_heading(numbered.group(2))
        elif roman:
            level, title = 1, clean_heading(roman.group(2))
        elif value.isupper() and 3 <= len(value) <= 90 and len(value.split()) <= 12:
            level, title = 1, clean_heading(value.title())
        else:
            continue
        if len(title) < 3 or re.fullmatch(r"\d+(?:\.\d+)*", title):
            continue
        if not headings or headings[-1]["title"].casefold() != title.casefold():
            headings.append({"level": level, "title": title})
    return headings[:80]


def style_signals(text: str, headings: list[dict[str, Any]]) -> dict[str, Any]:
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text) if len(p.split()) >= 10]
    word_counts = [len(p.split()) for p in paragraphs]
    citations = {
        "numeric_square": len(re.findall(r"\[\d+(?:\s*[,\-]\s*\d+)*\]", text)),
        "numeric_superscript": len(re.findall(r"\^\{?\d+", text)),
        "author_year": len(re.findall(r"\b[A-Z][A-Za-z-]+\s*(?:et al\.)?\s*\(\d{4}\)", text)),
    }
    return {
        "heading_depth": max((row["level"] for row in headings), default=0),
        "heading_count": len(headings),
        "median_paragraph_words": int(statistics.median(word_counts)) if word_counts else 0,
        "citation_pattern": max(citations, key=citations.get) if any(citations.values()) else "not_detected",
        "citation_counts": citations,
        "uses_numbered_sections": any(re.match(r"^\d+", row["title"]) for row in headings),
    }


def is_non_body_heading(title: str) -> bool:
    normalized = re.sub(r"[^a-z]", "", title.casefold())
    return normalized in {
        "abstract", "keywords", "keyword", "references", "referencelist", "bibliography",
        "acknowledgments", "acknowledgements", "supportinginformation", "contents",
    }


def is_conclusion_heading(title: str) -> bool:
    return bool(re.search(r"conclusion|outlook|future|perspective|challenge", title, flags=re.I))


def body_headings(headings: list[dict[str, Any]]) -> list[str]:
    rows = [row["title"] for row in headings if row["level"] <= 2 and not is_non_body_heading(row["title"])]
    rows = [title for title in rows if not re.search(r"^introduction$", title, flags=re.I) and not is_conclusion_heading(title)]
    return rows[:12] or ["Foundations and scope", "Methods and applications"]


def matrix_paper_ids(matrix_path: Path) -> list[str]:
    data = read_json(matrix_path)
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("literature_matrix.json must contain a rows list.")
    return [str(row["paper_id"]) for row in rows if isinstance(row, dict) and row.get("paper_id")]


def split_assignments(paper_ids: list[str], section_count: int) -> list[list[str]]:
    groups = [[] for _ in range(max(1, section_count))]
    for index, paper_id in enumerate(paper_ids):
        groups[index % len(groups)].append(paper_id)
    return groups


def candidate_markdown(source_name: str, headings: list[dict[str, Any]], paper_ids: list[str]) -> str:
    body = body_headings(headings)
    assignments = split_assignments(paper_ids, len(body))
    introduction = next((row["title"] for row in headings if re.fullmatch(r"introduction", row["title"], flags=re.I)), "Introduction")
    conclusion = next((row["title"] for row in headings if is_conclusion_heading(row["title"])), "Conclusion and outlook")
    lines = [
        "# Selected Outline",
        "",
        f"Primary structure: reference-derived from {source_name}.",
        f"Assigned {len(paper_ids)} confirmed Discovery papers while preserving the reference review's body structure.",
        "",
        f"## {introduction}",
        "Purpose: introduce the scope, terminology, and organizing question used by this review.",
        "",
    ]
    for index, (title, assigned) in enumerate(zip(body, assignments), start=1):
        lines.extend([
            f"## {index}. {title}",
            f"Assigned papers: {', '.join(assigned)}.",
            "Purpose: synthesize the assigned papers using this reference-derived section logic.",
            "",
        ])
    lines.extend([
        f"## {conclusion}",
        "Purpose: compare the body sections, state limitations, and identify defensible future directions.",
        "",
        "## References",
        "Purpose: list all cited papers with the citation convention used in the final manuscript.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a reference review structure and create an outline candidate.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    args = parser.parse_args()
    source = Path(args.input).resolve()
    matrix_path = Path(args.matrix).resolve()
    if not source.exists() or not matrix_path.exists():
        raise SystemExit("Reference input or literature matrix does not exist.")
    text = read_reference(source)
    headings = heading_rows(text)
    if not headings:
        raise SystemExit("No usable headings were detected in the reference review.")
    paper_ids = matrix_paper_ids(matrix_path)
    if not paper_ids:
        raise SystemExit("The literature matrix contains no papers.")
    result = {
        "candidate_id": args.candidate_id,
        "project_id": args.project_id,
        "source_file": str(source),
        "source_name": source.name,
        "created_at": utc_now(),
        "heading_hierarchy": headings,
        "writing_style": style_signals(text, headings),
        "assigned_paper_count": len(paper_ids),
        "outline_md": candidate_markdown(source.name, headings, paper_ids),
    }
    write_json(Path(args.output), result)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
