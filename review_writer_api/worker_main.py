"""CLI entry point for independently deployed PostgreSQL workers."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from dataclasses import replace
from pathlib import Path

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.gateway_client import GatewayTaskEnvironmentClient
from review_writer_api.worker_service import WorkerService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Review Writer background worker.")
    parser.add_argument("--review-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--queues",
        default=os.environ.get(
            "REVIEW_WRITER_WORKER_QUEUES", "scientific,image,ingest,document"
        ),
        help="Comma-separated worker queues: scientific,image,ingest,document.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = ApiSettings.from_env(args.review_root)
    settings = replace(settings, job_execution_enabled=False, embedded_gateway_routes_enabled=False)
    gateway_client = GatewayTaskEnvironmentClient(
        settings.internal_gateway_url,
        settings.internal_worker_token,
    )
    # Build the same domain services and handler registry as the public API,
    # but do not start its HTTP lifespan or compatibility executor.
    application = create_app(settings, model_gateway_override=gateway_client)
    job_service = application.state.job_service
    worker = WorkerService(
        application.state.workflow_repository,
        job_service.handlers,
        max_workers=settings.job_worker_count,
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.worker_lease_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        queues={item.strip() for item in args.queues.split(",") if item.strip()},
    )

    def request_stop(_signum, _frame) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
