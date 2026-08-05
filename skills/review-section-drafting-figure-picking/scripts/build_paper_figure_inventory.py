#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


FIGURE_TYPES = {"image", "chart", "table"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clean(text: Any) -> str:
    if isinstance(text, list):
        text = " ".join(str(x) for x in text if str(x).strip())
    return re.sub(r"\s+", " ", str(text or "")).strip()


def selected_paper_ids(project: Path) -> list[str]:
    path = project / "00_discovery" / "selected_discovery_results.json"
    data = read_json(path)
    rows = []
    if isinstance(data, dict):
        for key in ["local_papers", "selected_papers", "papers"]:
            value = data.get(key)
            if isinstance(value, list):
                rows.extend(value)
    elif isinstance(data, list):
        rows = data
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("keep") is False:
            continue
        paper_id = str(row.get("paper_id") or "").strip()
        if paper_id and paper_id not in seen:
            seen.add(paper_id)
            ids.append(paper_id)
    return ids


def metadata(review_root: Path, paper_id: str) -> dict[str, Any] | None:
    path = review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
    if not path.exists():
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None


def field_value(meta: dict[str, Any], key: str) -> Any:
    value = meta.get(key)
    if isinstance(value, dict):
        return value.get("value")
    return value


def resolve_recorded_path(review_root: Path, raw_value: Any) -> Path | None:
    """Resolve current and relocated workspace paths without treating an empty value as '.'."""
    value = str(raw_value or "").strip()
    if not value:
        return None
    recorded = Path(value)
    candidates = [recorded] if recorded.is_absolute() else [review_root / recorded]
    if recorded.is_absolute() and not recorded.exists():
        folded = [part.casefold() for part in recorded.parts]
        for marker in ("mineru-outputs", "review-library"):
            if marker in folded:
                marker_index = folded.index(marker)
                candidates.append(review_root.joinpath(*recorded.parts[marker_index:]))
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def block_caption(block: dict[str, Any]) -> str:
    parts = []
    for key in ["image_caption", "table_caption", "caption", "text"]:
        value = block.get(key)
        text = clean(value)
        if text:
            parts.append(text)
    return clean(" ".join(parts))


def infer_source_label(caption: str, index: int, source_type: str) -> str:
    match = re.search(r"\b(Scheme|Figure|Fig\.|Table)\s*[\w.-]+", caption, re.I)
    if match:
        return match.group(0).replace("Fig.", "Figure")
    prefix = "Table" if source_type == "table" else "Figure"
    return f"{prefix} candidate {index}"


def candidate_score(caption: str, source_type: str) -> int:
    low = caption.lower()
    score = 0
    for word, weight in [
        ("scheme", 8),
        ("mechanism", 8),
        ("catalytic cycle", 8),
        ("proposed", 5),
        ("reaction", 4),
        ("synthesis", 4),
        ("scope", 4),
        ("optimization", 2),
        ("crystal", -4),
        ("nmr", -5),
        ("hrms", -5),
        ("supporting", -3),
    ]:
        if word in low:
            score += weight
    if source_type == "table":
        score += 1
    return score


def build_inventory(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    ids = selected_paper_ids(project)
    papers = []
    for paper_id in ids:
        meta = metadata(review_root, paper_id)
        if not meta:
            papers.append({"paper_id": paper_id, "status": "missing_metadata", "candidates": []})
            continue
        source_paths = meta.get("source_paths") or {}
        content_path = resolve_recorded_path(review_root, source_paths.get("content_list"))
        extracted_dir = resolve_recorded_path(review_root, source_paths.get("extracted_dir"))
        candidates = []
        if content_path and content_path.is_file():
            blocks = read_json(content_path)
            if isinstance(blocks, list):
                for idx, block in enumerate(blocks, start=1):
                    if not isinstance(block, dict) or block.get("type") not in FIGURE_TYPES:
                        continue
                    img_rel = block.get("img_path") or block.get("image_path") or block.get("path")
                    source_image_path = (
                        str((extracted_dir / str(img_rel)).resolve())
                        if img_rel and extracted_dir and extracted_dir.is_dir()
                        else ""
                    )
                    caption = block_caption(block)
                    source_type = str(block.get("type") or "")
                    candidates.append(
                        {
                            "paper_id": paper_id,
                            "title": field_value(meta, "title"),
                            "source_label": infer_source_label(caption, len(candidates) + 1, source_type),
                            "source_type": source_type,
                            "source_pdf": source_paths.get("pdf"),
                            "source_content_list": str(content_path),
                            "source_image_path": source_image_path if source_image_path and Path(source_image_path).exists() else "",
                            "source_page_hint": f"page {int(block.get('page_idx', 0)) + 1}" if block.get("page_idx") is not None else "",
                            "source_caption_text": caption,
                            "inventory_score": candidate_score(caption, source_type),
                            "human_reading_hint": "Prefer if this is a reaction scheme, mechanism, catalytic cycle, or scope summary.",
                        }
                    )
        candidates.sort(key=lambda item: item.get("inventory_score", 0), reverse=True)
        papers.append(
            {
                "paper_id": paper_id,
                "status": (
                    "ready"
                    if content_path and content_path.is_file() and extracted_dir and extracted_dir.is_dir()
                    else "mineru_not_ready"
                ),
                "title": field_value(meta, "title"),
                "source_pdf": source_paths.get("pdf"),
                "markdown": source_paths.get("markdown"),
                "content_list": source_paths.get("content_list"),
                "candidate_count": len(candidates),
                "top_candidates": candidates[:12],
            }
        )
    return {"project_id": project_id, "paper_count": len(ids), "papers": papers}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a MinerU figure/table inventory for selected review papers.")
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--project-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_root = resolve_review_root(args.review_root, anchor=Path(__file__))
    project = review_root / "review-projects" / args.project_id
    if not project.exists():
        raise SystemExit(f"Project not found: {project}")
    out = project / "02_section_drafting" / "paper_figure_inventory.json"
    inventory = build_inventory(review_root, args.project_id)
    write_json(out, inventory)
    print(f"Wrote {out}")
    print(f"Papers: {inventory['paper_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
