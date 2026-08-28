#!/usr/bin/env python3
"""Run the project-compatible deterministic first-draft preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from feedback_loop import (
    CASE_PARAGRAPH_MAX_WORDS,
    CASE_PARAGRAPH_MIN_WORDS,
    deterministic_preflight,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--min-case-words", type=int, default=CASE_PARAGRAPH_MIN_WORDS
    )
    parser.add_argument(
        "--max-case-words", type=int, default=CASE_PARAGRAPH_MAX_WORDS
    )
    args = parser.parse_args()
    report = deterministic_preflight(
        Path(args.review_root).resolve(),
        args.project_id,
        min_words=args.min_case_words,
        max_words=args.max_case_words,
    )
    print(
        json.dumps(
            {
                "hard_regressions": report["hard_regressions"],
                "paragraph_findings": len(report["paragraph_findings"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
