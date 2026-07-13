#!/usr/bin/env python3
"""Backfill journal/volume/pages/doi on existing paper metadata by querying Crossref.

Local papers registered via rule-only extraction (or preprints with a noisy PDF
header) often end up with empty journal/volume/pages/doi even though the paper
is a real, indexed publication. This script looks each paper up on Crossref by
its extracted title and fills in whichever bibliographic fields are still
missing, without overwriting anything already present or human-checked.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
from pathlib import Path
from typing import Any

from prepare_metadata import crossref_lookup, has_value, read_json, scored, update_quality, write_json


def backfill_one(meta_path: Path, timeout: int, sleep_seconds: float, force: bool) -> dict[str, Any]:
    meta = read_json(meta_path)
    title = str((meta.get("title") or {}).get("value") or "")
    fields_needed = [
        key
        for key in ["journal", "volume", "pages", "doi", "year"]
        if force or not has_value((meta.get(key) or {}).get("value"))
    ]
    if not title.strip():
        return {"paper_id": meta.get("paper_id"), "status": "skipped", "reason": "no_title"}
    if not fields_needed:
        return {"paper_id": meta.get("paper_id"), "status": "skipped", "reason": "already_complete"}

    result = crossref_lookup(title, timeout)
    if sleep_seconds:
        time.sleep(sleep_seconds)
    if not result:
        return {"paper_id": meta.get("paper_id"), "status": "no_match", "title": title}

    updated_fields = []
    for key in ["journal", "volume", "pages", "doi", "year"]:
        if key not in fields_needed:
            continue
        value = result.get(key)
        if not has_value(value):
            continue
        current = meta.get(key) or {}
        if isinstance(current, dict) and current.get("human_checked"):
            continue
        meta[key] = scored(value, "crossref_backfill", min(0.55 + result["title_match_score"] * 0.2, 0.85))
        updated_fields.append(key)

    if not updated_fields:
        return {
            "paper_id": meta.get("paper_id"),
            "status": "no_new_fields",
            "title": title,
            "title_match_score": result["title_match_score"],
        }

    meta.setdefault("extraction", {}).setdefault("notes", []).append("crossref_backfill")
    update_quality(meta)
    write_json(meta_path, meta)
    return {
        "paper_id": meta.get("paper_id"),
        "status": "ok",
        "title": title,
        "title_match_score": result["title_match_score"],
        "updated_fields": updated_fields,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill journal/volume/pages/doi via Crossref for existing paper metadata.")
    parser.add_argument("--review-root", default=str(Path.cwd()))
    parser.add_argument("--paper-id", action="append", default=[], help="Backfill only selected paper_id. Repeatable.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--force", action="store_true", help="Re-check and overwrite non-human-checked fields even if already present.")
    return parser.parse_args()


def write_report(out_path: Path, reports: list[dict[str, Any]]) -> None:
    ok = [r for r in reports if r["status"] == "ok"]
    payload = {
        "total": len(reports),
        "updated": len(ok),
        "reports": reports,
    }
    write_json(out_path, payload)
    lines = ["# Crossref Backfill Report", "", f"- Total checked: {len(reports)}", f"- Updated: {len(ok)}", "", "## Updates", ""]
    if ok:
        for r in ok:
            lines.append(f"- {r['paper_id']}: {', '.join(r['updated_fields'])} (match score {r['title_match_score']})")
    else:
        lines.append("None.")
    lines += ["", "## No Match / Skipped", ""]
    others = [r for r in reports if r["status"] != "ok"]
    if others:
        for r in others:
            lines.append(f"- {r['paper_id']}: {r['status']}")
    else:
        lines.append("None.")
    out_path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    review_root = Path(args.review_root).resolve()
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    paths = sorted(meta_dir.glob("*.metadata.json"))
    if args.paper_id:
        wanted = set(args.paper_id)
        paths = [p for p in paths if p.stem.replace(".metadata", "") in wanted]
    if args.limit > 0:
        paths = paths[: args.limit]

    reports = []
    for path in paths:
        try:
            report = backfill_one(path, args.timeout, args.sleep_seconds, args.force)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            report = {"paper_id": path.stem.replace(".metadata", ""), "status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        print(f"{report['paper_id']}: {report['status']}")
        reports.append(report)

    out_dir = review_root / "review-library" / "metadata"
    write_report(out_dir / "crossref_backfill_report.json", reports)
    print(f"Wrote {out_dir / 'crossref_backfill_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
