"""CLI entry point for the private model gateway."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from review_writer_api.config import ApiSettings
from review_writer_api.gateway_app import create_gateway_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the internal model gateway.")
    parser.add_argument("--review-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8782)
    args = parser.parse_args()
    settings = ApiSettings.from_env(args.review_root)
    uvicorn.run(create_gateway_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
