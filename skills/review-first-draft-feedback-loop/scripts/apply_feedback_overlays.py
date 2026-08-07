#!/usr/bin/env python3
"""Replay safe paragraph feedback overlays after rebuilding a first draft."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from feedback_loop import apply_rewrite_overlays


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", default=".")
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    project = Path(args.review_root).resolve() / "review-projects" / args.project_id
    print(json.dumps(apply_rewrite_overlays(project), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
