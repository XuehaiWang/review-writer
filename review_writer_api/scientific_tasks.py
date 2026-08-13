"""File-producing adapters for existing repository scientific functions."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load scientific module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def literature_module():
    return _module(
        "review_writer_native_literature",
        ROOT
        / "skills"
        / "review-literature-acquisition"
        / "scripts"
        / "literature_acquisition.py",
    )


def search(args: argparse.Namespace) -> int:
    module = literature_module()
    module.load_dotenv_if_present(args.review_root)
    candidates = module.search_crossref(
        args.topic,
        year_from=args.year_from,
        year_to=args.year_to,
        limit=args.limit,
        mailto=args.mailto,
    )
    _write(args.output, {"candidates": candidates, "candidate_count": len(candidates)})
    return 0


def download(args: argparse.Namespace) -> int:
    module = literature_module()
    candidates = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(candidates, list):
        raise ValueError("Literature download input must be a candidate list.")
    results: list[dict[str, Any]] = []
    added = already = failed = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            result = module.acquire_candidate(args.review_root, candidate, email=args.email)
        except Exception as exc:
            result = {
                "candidate_id": candidate.get("candidate_id"),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)
        state = str(result.get("status") or "")
        if state == "downloaded":
            added += 1
        elif state in {"already_in_library", "duplicate_file"}:
            already += 1
        else:
            failed += 1
    _write(
        args.output,
        {
            "added_count": added,
            "already_present_count": already,
            "failed_count": failed,
            "results": results,
        },
    )
    return 0


def precise_ingest(args: argparse.Namespace) -> int:
    """Subprocess adapter for local precise parsing; API code never invokes the legacy handler."""
    module = _module(
        "review_writer_precise_ingest_adapter",
        ROOT / "view" / "local_pdf_ingestion.py",
    )
    result = module.ingest_local_pdf(args.review_root, args.filename, args.input)
    _write(args.output, result)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search_parser = commands.add_parser("literature-search")
    search_parser.add_argument("--review-root", type=Path, required=True)
    search_parser.add_argument("--output", type=Path, required=True)
    search_parser.add_argument("--topic", required=True)
    search_parser.add_argument("--year-from", type=int)
    search_parser.add_argument("--year-to", type=int)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--mailto", default="")
    download_parser = commands.add_parser("literature-download")
    download_parser.add_argument("--review-root", type=Path, required=True)
    download_parser.add_argument("--input", type=Path, required=True)
    download_parser.add_argument("--output", type=Path, required=True)
    download_parser.add_argument("--email", default="")
    ingest_parser = commands.add_parser("precise-ingest")
    ingest_parser.add_argument("--review-root", type=Path, required=True)
    ingest_parser.add_argument("--filename", required=True)
    ingest_parser.add_argument("--input", type=Path, required=True)
    ingest_parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "literature-search":
        return search(args)
    if args.command == "literature-download":
        return download(args)
    return precise_ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
