"""Lazy bridge between the local dashboard and Prefect flows.

Prefect is imported only when a workflow is executed.  This keeps read-only
dashboard pages and lightweight unit tests importable even when the optional
workflow environment has not been installed yet.
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class _RegisteredAction:
    action: Callable[[], dict[str, Any]]
    on_flow_started: Callable[[str], None] | None = None


_ACTION_LOCK = threading.RLock()
_ACTIONS: dict[str, _RegisteredAction] = {}


def configure_prefect_environment(review_root: Path) -> None:
    """Keep Prefect runtime files inside the review-writer workspace."""
    root = Path(review_root).resolve()
    account = _effective_account_name()
    account_key = re.sub(r"[^a-z0-9_-]+", "-", account.casefold()).strip("-") or "local"
    # Prefect hardens its home-directory ACL on Windows. Codex tests and the
    # user-launched dashboard can run under different effective accounts, so
    # sharing one home makes the second account unable to open prefect.db.
    prefect_home = root / ".review-writer" / f"prefect-{account_key}"
    storage = prefect_home / "storage"
    # A Python 3.14/Windows child process can raise WinError 183 from
    # mkdir(exist_ok=True) for an already-existing Prefect storage directory.
    # Avoid invoking mkdir in that case instead of depending on the exception
    # suppression path in pathlib.
    for directory in (prefect_home, storage):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # In the affected Windows child-process case the directory is
            # usable even though pathlib's post-error is_dir() probe reports
            # false. Prefect will surface a clear error later if the path is
            # genuinely a non-directory file.
            pass
    os.environ["PREFECT_HOME"] = str(prefect_home)
    os.environ["PREFECT_RESULTS_LOCAL_STORAGE_PATH"] = str(storage)
    os.environ.setdefault("PREFECT_RESULTS_PERSIST_BY_DEFAULT", "true")
    os.environ.setdefault("PREFECT_SERVER_EPHEMERAL_ENABLED", "true")
    os.environ.setdefault("PREFECT_SERVER_EPHEMERAL_STARTUP_TIMEOUT_SECONDS", "60")
    os.environ["PREFECT_SERVER_MEMO_STORE_PATH"] = str(prefect_home / "memo_store.toml")
    os.environ.setdefault("PREFECT_CLOUD_ENABLE_ORCHESTRATION_TELEMETRY", "false")
    os.environ.setdefault("PREFECT_TELEMETRY_ENABLE_RESOURCE_METRICS", "false")


def _effective_account_name() -> str:
    """Return the Windows token account instead of the inherited USERNAME."""
    if os.name != "nt":
        return os.environ.get("USER") or "local"
    try:
        import ctypes

        size = ctypes.c_ulong(256)
        buffer = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            return buffer.value
    except Exception:
        pass
    return os.environ.get("USERNAME") or "local"


def ensure_prefect_available(review_root: Path) -> None:
    configure_prefect_environment(review_root)
    if importlib.util.find_spec("prefect") is None:
        raise RuntimeError(
            "Prefect is not installed in the active Python environment. "
            "Start the dashboard with .venv\\Scripts\\python.exe or install requirements-workflow.txt."
        )


def prefect_orchestration_enabled() -> bool:
    """Return whether this process was started as the Prefect-backed dashboard."""
    enabled = os.environ.get("REVIEW_WRITER_PREFECT_ENABLED", "").strip().lower()
    return enabled in {"1", "true", "yes", "on"} and importlib.util.find_spec("prefect") is not None


def _register_action(
    action: Callable[[], dict[str, Any]],
    on_flow_started: Callable[[str], None] | None,
) -> str:
    token = str(uuid.uuid4())
    with _ACTION_LOCK:
        _ACTIONS[token] = _RegisteredAction(action=action, on_flow_started=on_flow_started)
    return token


def execute_registered_action(token: str) -> dict[str, Any]:
    with _ACTION_LOCK:
        entry = _ACTIONS.get(token)
    if entry is None:
        raise RuntimeError("The dashboard action for this Prefect run is no longer available.")
    return entry.action()


def notify_flow_started(token: str, flow_run_id: str) -> None:
    with _ACTION_LOCK:
        entry = _ACTIONS.get(token)
    if entry and entry.on_flow_started:
        entry.on_flow_started(flow_run_id)


def _release_action(token: str) -> None:
    with _ACTION_LOCK:
        _ACTIONS.pop(token, None)


def run_stage_with_prefect(
    review_root: Path,
    project_id: str,
    stage_id: str,
    action: Callable[[], dict[str, Any]],
    *,
    on_flow_started: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    ensure_prefect_available(review_root)
    token = _register_action(action, on_flow_started)
    try:
        from prefect_flows import review_stage_flow

        return review_stage_flow(
            review_root=str(Path(review_root).resolve()),
            project_id=project_id,
            stage_id=stage_id,
            action_token=token,
        )
    finally:
        _release_action(token)


def run_batch_redraw_with_prefect(
    review_root: Path,
    project_id: str,
    figure_count: int,
    action: Callable[[], dict[str, Any]],
    *,
    on_flow_started: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    ensure_prefect_available(review_root)
    token = _register_action(action, on_flow_started)
    try:
        from prefect_flows import figure_redraw_batch_flow

        return figure_redraw_batch_flow(
            review_root=str(Path(review_root).resolve()),
            project_id=project_id,
            figure_count=figure_count,
            action_token=token,
        )
    finally:
        _release_action(token)


def run_literature_acquisition_with_prefect(
    review_root: Path,
    operation: str,
    item_count: int,
    action: Callable[[], dict[str, Any]],
    *,
    on_flow_started: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    ensure_prefect_available(review_root)
    token = _register_action(action, on_flow_started)
    try:
        from prefect_flows import literature_acquisition_flow

        return literature_acquisition_flow(
            review_root=str(Path(review_root).resolve()),
            operation=operation,
            item_count=max(int(item_count), 0),
            action_token=token,
        )
    finally:
        _release_action(token)
