"""Command-line entry point for the versioned API."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ApiSettings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Review Writer versioned API.")
    parser.add_argument("--review-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = ApiSettings.from_env(args.review_root)
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
