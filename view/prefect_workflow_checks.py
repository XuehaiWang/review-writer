r"""Focused checks for the Prefect orchestration bridge.

Run with:
    .venv\Scripts\python.exe view\prefect_workflow_checks.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import prefect_runtime
    from .prefect_runtime import run_batch_redraw_with_prefect, run_stage_with_prefect
except ImportError:
    import prefect_runtime
    from prefect_runtime import run_batch_redraw_with_prefect, run_stage_with_prefect


class PrefectStartupRetryChecks(unittest.TestCase):
    def test_retries_one_local_transient_health_failure(self) -> None:
        calls: list[dict[str, object]] = []

        def flow(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError(
                    "Server error '502 Bad Gateway' for url "
                    "'http://127.0.0.1:8784/api/health'"
                )
            return {"ok": True}

        with patch.object(prefect_runtime, "_wait_for_local_health", return_value=True) as wait:
            result = prefect_runtime._run_flow_with_ephemeral_health_retry(flow, project_id="test")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        wait.assert_called_once_with("http://127.0.0.1:8784/api/health")

    def test_does_not_retry_business_or_remote_failures(self) -> None:
        failures = (
            RuntimeError("scientific stage failed"),
            RuntimeError(
                "Server error '502 Bad Gateway' for url "
                "'https://provider.example/v1/chat/completions'"
            ),
            RuntimeError(
                "Client error '401 Unauthorized' for url "
                "'http://127.0.0.1:8784/api/health'"
            ),
        )
        for failure in failures:
            calls = 0

            def flow() -> dict[str, object]:
                nonlocal calls
                calls += 1
                raise failure

            with self.subTest(failure=str(failure)):
                with patch.object(prefect_runtime, "_wait_for_local_health") as wait:
                    with self.assertRaises(RuntimeError):
                        prefect_runtime._run_flow_with_ephemeral_health_retry(flow)
                self.assertEqual(calls, 1)
                wait.assert_not_called()


class PrefectWorkflowChecks(unittest.TestCase):
    review_root = Path(__file__).resolve().parents[1]

    def test_stage_flow_returns_business_result_and_run_ids(self) -> None:
        started: list[str] = []
        result = run_stage_with_prefect(
            self.review_root,
            "prefect-check",
            "sections",
            lambda: {"result": {"section_count": 2}, "next_stage": "figure-review"},
            on_flow_started=started.append,
        )

        self.assertEqual(result["result"]["next_stage"], "figure-review")
        self.assertEqual(result["result"]["result"]["section_count"], 2)
        self.assertEqual(started, [result["prefect_flow_run_id"]])
        self.assertTrue(result["prefect_task_run_id"])

    def test_batch_flow_preserves_sequential_batch_callback_result(self) -> None:
        calls: list[str] = []

        def run_batch() -> dict[str, object]:
            calls.extend(["P001-F01", "P002-F01"])
            return {"status": "completed", "completed": len(calls)}

        result = run_batch_redraw_with_prefect(
            self.review_root,
            "prefect-check",
            2,
            run_batch,
        )

        self.assertEqual(calls, ["P001-F01", "P002-F01"])
        self.assertEqual(result["result"]["status"], "completed")
        self.assertEqual(result["result"]["completed"], 2)
        self.assertTrue(result["prefect_flow_run_id"])
        self.assertTrue(result["prefect_task_run_id"])


if __name__ == "__main__":
    unittest.main()
