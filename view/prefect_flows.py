"""Prefect flow definitions for review-writer stage and batch execution."""

from __future__ import annotations

from typing import Any

from prefect import flow, task
from prefect.logging import get_run_logger
from prefect.runtime import flow_run, task_run

from prefect_runtime import execute_registered_action, notify_flow_started


TRANSIENT_ERROR_MARKERS = (
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "service unavailable",
    "all_channels_failed",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
)
NON_RETRYABLE_ERROR_MARKERS = (
    "http 400",
    "http 401",
    "http 403",
    "http 404",
    "unauthorized",
    "invalid payload",
    "validation",
    "not found",
)


def retry_transient_stage_failure(_task, _task_run, state) -> bool:
    """Retry provider/network failures, but not user input or authorization failures."""
    try:
        state.result()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}".lower()
        if any(marker in message for marker in NON_RETRYABLE_ERROR_MARKERS):
            return False
        return any(marker in message for marker in TRANSIENT_ERROR_MARKERS)
    return False


@task(
    name="execute-review-stage",
    retries=1,
    retry_delay_seconds=[10],
    retry_jitter_factor=0.25,
    retry_condition_fn=retry_transient_stage_failure,
    timeout_seconds=7200,
    persist_result=True,
    cache_policy=None,
)
def execute_review_stage_task(action_token: str, stage_id: str) -> dict[str, Any]:
    logger = get_run_logger()
    logger.info("Executing review stage %s", stage_id)
    result = execute_registered_action(action_token)
    return {
        "result": result,
        "prefect_task_run_id": str(task_run.id),
    }


@flow(
    name="review-writer-stage",
    flow_run_name="{project_id}-{stage_id}",
    timeout_seconds=7500,
    log_prints=True,
    persist_result=True,
)
def review_stage_flow(
    review_root: str,
    project_id: str,
    stage_id: str,
    action_token: str,
) -> dict[str, Any]:
    del review_root
    current_flow_run_id = str(flow_run.id)
    notify_flow_started(action_token, current_flow_run_id)
    task_result = execute_review_stage_task(action_token, stage_id)
    return {
        "result": task_result["result"],
        "prefect_flow_run_id": current_flow_run_id,
        "prefect_task_run_id": task_result["prefect_task_run_id"],
    }


@task(
    name="execute-sequential-figure-redraw-batch",
    retries=0,
    timeout_seconds=86400,
    persist_result=True,
    cache_policy=None,
)
def execute_figure_redraw_batch_task(action_token: str, figure_count: int) -> dict[str, Any]:
    logger = get_run_logger()
    logger.info("Executing sequential redraw batch for %s figures", figure_count)
    result = execute_registered_action(action_token)
    return {
        "result": result,
        "prefect_task_run_id": str(task_run.id),
    }


@flow(
    name="review-writer-figure-redraw-batch",
    flow_run_name="{project_id}-figures-{figure_count}",
    timeout_seconds=87000,
    log_prints=True,
    persist_result=True,
)
def figure_redraw_batch_flow(
    review_root: str,
    project_id: str,
    figure_count: int,
    action_token: str,
) -> dict[str, Any]:
    del review_root
    current_flow_run_id = str(flow_run.id)
    notify_flow_started(action_token, current_flow_run_id)
    task_result = execute_figure_redraw_batch_task(action_token, figure_count)
    return {
        "result": task_result["result"],
        "prefect_flow_run_id": current_flow_run_id,
        "prefect_task_run_id": task_result["prefect_task_run_id"],
    }


@task(
    name="execute-literature-acquisition",
    retries=1,
    retry_delay_seconds=[8],
    retry_jitter_factor=0.25,
    retry_condition_fn=retry_transient_stage_failure,
    timeout_seconds=3600,
    persist_result=True,
    cache_policy=None,
)
def execute_literature_acquisition_task(
    action_token: str,
    operation: str,
    item_count: int,
) -> dict[str, Any]:
    logger = get_run_logger()
    logger.info("Executing literature %s for %s item(s)", operation, item_count)
    result = execute_registered_action(action_token)
    return {
        "result": result,
        "prefect_task_run_id": str(task_run.id),
    }


@flow(
    name="review-writer-literature-acquisition",
    flow_run_name="library-{operation}-{item_count}",
    timeout_seconds=3900,
    log_prints=True,
    persist_result=True,
)
def literature_acquisition_flow(
    review_root: str,
    operation: str,
    item_count: int,
    action_token: str,
) -> dict[str, Any]:
    del review_root
    current_flow_run_id = str(flow_run.id)
    notify_flow_started(action_token, current_flow_run_id)
    task_result = execute_literature_acquisition_task(action_token, operation, item_count)
    return {
        "result": task_result["result"],
        "prefect_flow_run_id": current_flow_run_id,
        "prefect_task_run_id": task_result["prefect_task_run_id"],
    }
