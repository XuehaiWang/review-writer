#!/usr/bin/env python3
"""Dump the paper library's actual vocabulary for vocabulary-aware keyword expansion.

Before expanding a review topic into search keywords (agent_keywords.json), the
LLM should read this dump and include the library's own phrasings as additional
keyword entries wherever they are semantically on-topic. Topic-side and
paper-side vocabularies are otherwise generated blind to each other, and
phrasing drift between them is the main cause of on-topic papers being missed
by keyword scoring.

Output: a JSON object with
  structured_tag_values: {category: [distinct values across the library]}
  titles: [{paper_id, title}]
  generated_at, paper_count
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

STRUCTURED_TAG_KEYS = [
    "output",
    "input",
    "method",
    "co_input",
    "modifier",
    "process_type",
    "document_scope",
]


def field_value(field, default=None):
    if isinstance(field, dict) and "value" in field:
        return field.get("value", default)
    return field if field is not None else default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument(
        "--output",
        default="",
        help="Output path. Defaults to <review-root>/review-library/metadata/library_vocabulary.json",
    )
    args = parser.parse_args()

    root = Path(args.review_root).resolve()
    meta_dir = root / "review-library" / "metadata" / "papers"
    if not meta_dir.is_dir():
        raise SystemExit(f"metadata dir not found: {meta_dir} (run review-metadata-prep first)")

    tag_values: dict[str, list[str]] = {k: [] for k in STRUCTURED_TAG_KEYS}
    seen: dict[str, set[str]] = {k: set() for k in STRUCTURED_TAG_KEYS}
    titles: list[dict[str, str]] = []
    for path in sorted(meta_dir.glob("*.metadata.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = str(field_value(meta.get("title"), "") or "").strip()
        if title:
            titles.append({"paper_id": meta.get("paper_id", path.stem), "title": re.sub(r"\s+", " ", title)})
        tags = field_value(meta.get("structured_tags"), {}) or {}
        for key in STRUCTURED_TAG_KEYS:
            value = str(tags.get(key) or "").strip()
            if not value or value.lower() == "not specified":
                continue
            if value.lower() not in seen[key]:
                seen[key].add(value.lower())
                tag_values[key].append(value)

    out = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "paper_count": len(titles),
        "structured_tag_values": tag_values,
        "titles": titles,
    }
    out_path = Path(args.output).resolve() if args.output else root / "review-library" / "metadata" / "library_vocabulary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(titles)} papers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
