#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prepare_metadata import (
    STRUCTURED_TAG_KEYS,
    apply_structured_tags_to_compat_fields,
    read_json,
    scored,
    update_quality,
    write_json,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill empty eight-category structured_tags into existing metadata files.")
    parser.add_argument("--review-root", default=None)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing structured_tags.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    review_root = resolve_review_root(args.review_root, anchor=Path(__file__))
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    count = 0
    for path in sorted(meta_dir.glob("*.metadata.json")):
        meta = read_json(path)
        if meta.get("structured_tags") and not args.overwrite:
            continue
        meta["structured_tags"] = scored(
            {key: "not specified" for key in STRUCTURED_TAG_KEYS},
            "backfill_empty_structured_tags",
            0.0,
        )
        apply_structured_tags_to_compat_fields(meta)
        meta.setdefault("extraction", {}).setdefault("notes", [])
        meta["extraction"]["notes"].append("structured_tags_backfilled_empty")
        update_quality(meta)
        write_json(path, meta)
        count += 1
    print(f"Backfilled {count} metadata files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
