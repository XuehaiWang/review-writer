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
from review_writer_api.job_queues import JOB_QUEUES


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Review Writer background worker.")
    parser.add_argument("--review-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--queues",
        default=os.environ.get(
            "REVIEW_WRITER_WORKER_QUEUES", ",".join(sorted(JOB_QUEUES))
        ),
        help="Comma-separated worker queues: " + ",".join(sorted(JOB_QUEUES)),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Maximum concurrent jobs in this worker process. Defaults to "
            "REVIEW_WRITER_JOB_WORKERS for backward compatibility."
        ),
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
    queues = {item.strip() for item in args.queues.split(",") if item.strip()}
    max_workers = args.workers if args.workers is not None else settings.job_worker_count
    if max_workers < 1:
        parser.error("--workers must be at least 1")
    logging.getLogger(__name__).info(
        "starting worker pool queues=%s max_workers=%s",
        ",".join(sorted(queues)),
        max_workers,
    )
    worker = WorkerService(
        application.state.workflow_repository,
        job_service.handlers,
        max_workers=max_workers,
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.worker_lease_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        queues=queues,
        maintenance=(lambda: maintain_storage(application)) if "ingest" in queues else None,
    )

    def request_stop(_signum, _frame) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    worker.run_forever()


def maintain_storage(application):
    from sqlalchemy import select
    from review_writer_api.database import database_session
    from review_writer_api.workflow_models import LibraryVectorStore
    from review_writer_api.system_errors import prune_failures
    sessions = application.state.session_factory
    prune_failures(sessions)
    with database_session(sessions) as session:
        users = list(session.scalars(select(LibraryVectorStore.user_id)))
    for user_id in users:
        try:
            application.state.library_index_service.vector_store.prune(user_id)
        except Exception as exc:
            logging.getLogger(__name__).warning("vector_prune_failed user_id=%s exception=%s",
                                              user_id, type(exc).__name__)


if __name__ == "__main__":
    main()
