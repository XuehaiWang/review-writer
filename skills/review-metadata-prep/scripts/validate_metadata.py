#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BLOCKING_FIELDS = ["paper_id", "slug", "title", "authors", "year", "abstract", "source_paths", "structured_tags"]
WARNING_FIELDS = ["journal", "volume", "pages", "doi"]
STRUCTURED_TAG_KEYS = [
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True


def field_value(meta: dict[str, Any], key: str) -> Any:
    value = meta.get(key)
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def validate_one(path: Path) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    try:
        meta = read_json(path)
    except Exception as exc:
        return {
            "metadata_path": str(path),
            "paper_id": None,
            "blocking_issues": [f"invalid_json: {type(exc).__name__}: {exc}"],
            "warnings": [],
            "status": "failed",
        }
    if not isinstance(meta, dict):
        return {
            "metadata_path": str(path),
            "paper_id": None,
            "blocking_issues": ["metadata_root_not_object"],
            "warnings": [],
            "status": "failed",
        }
    for key in BLOCKING_FIELDS:
        if not has_value(field_value(meta, key)):
            issues.append(f"missing_{key}")
    title = meta.get("title")
    if not isinstance(title, dict) or not has_value(title.get("value")):
        issues.append("missing_title_value")
    structured = meta.get("structured_tags")
    structured_value = structured.get("value") if isinstance(structured, dict) else None
    if not isinstance(structured_value, dict):
        issues.append("missing_structured_tags_value")
    else:
        for key in STRUCTURED_TAG_KEYS:
            if not has_value(structured_value.get(key)):
                issues.append(f"missing_structured_tag_{key}")
            elif str(structured_value.get(key)).strip().lower() == "not specified":
                warnings.append(f"structured_tag_not_specified_{key}")
            # Open-vocabulary tags: any non-empty label is accepted. There is no
            # fixed allowed-value list, so tag values are not validated against
            # an enum — only presence and "not specified" are checked above.
    source_paths = meta.get("source_paths") or {}
    if not isinstance(source_paths, dict):
        issues.append("invalid_source_paths")
        source_paths = {}
    for key in ["pdf", "markdown", "content_list"]:
        value = source_paths.get(key)
        if not value:
            issues.append(f"missing_source_{key}")
        elif not Path(value).exists():
            issues.append(f"source_{key}_not_found")
    for key in WARNING_FIELDS:
        if not has_value(field_value(meta, key)):
            warnings.append(f"missing_or_empty_{key}")
    for key in ["title", "abstract"]:
        value = meta.get(key)
        if isinstance(value, dict) and float(value.get("confidence") or 0) < 0.75:
            warnings.append(f"low_confidence_{key}")
    human_review = meta.get("human_review") or {}
    if not isinstance(human_review, dict) or human_review.get("status") != "reviewed":
        warnings.append("not_human_reviewed")
    return {
        "metadata_path": str(path),
        "paper_id": meta.get("paper_id"),
        "title": field_value(meta, "title"),
        "blocking_issues": sorted(set(issues)),
        "warnings": sorted(set(warnings)),
        "status": "failed" if issues else "ok",
    }


def write_reports(review_root: Path, reports: list[dict[str, Any]]) -> None:
    out_dir = review_root / "review-library" / "metadata"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "total": len(reports),
        "ok": sum(1 for r in reports if r["status"] == "ok"),
        "failed": sum(1 for r in reports if r["status"] != "ok"),
        "warning_count": sum(len(r["warnings"]) for r in reports),
        "reports": reports,
    }
    (out_dir / "metadata_validation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Metadata Validation Report",
        "",
        f"- Total papers: {summary['total']}",
        f"- OK: {summary['ok']}",
        f"- Failed: {summary['failed']}",
        f"- Warning count: {summary['warning_count']}",
        "",
        "## Blocking Issues",
        "",
    ]
    blocking = [r for r in reports if r["blocking_issues"]]
    if not blocking:
        lines.append("No blocking issues.")
    for r in blocking:
        lines.append(f"- {r.get('paper_id') or 'UNKNOWN'}: {', '.join(r['blocking_issues'])}")
    lines += ["", "## Warnings", ""]
    warned = [r for r in reports if r["warnings"]]
    if not warned:
        lines.append("No warnings.")
    for r in warned:
        lines.append(f"- {r.get('paper_id') or 'UNKNOWN'}: {', '.join(r['warnings'])}")
    (out_dir / "metadata_validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    if not meta_dir.exists():
        print(f"ERROR: metadata directory not found: {meta_dir}", file=sys.stderr)
        return 2
    paths = sorted(meta_dir.glob("*.metadata.json"))
    reports = [validate_one(path) for path in paths]
    write_reports(review_root, reports)
    failed = sum(1 for r in reports if r["status"] != "ok")
    print(f"Validated {len(reports)} metadata files; failed={failed}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate review metadata JSON files.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
