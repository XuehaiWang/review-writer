r"""Focused checks for the Prefect orchestration bridge.

Run with:
    .venv\Scripts\python.exe view\prefect_workflow_checks.py
"""

from __future__ import annotations

import unittest
from pathlib import Path

from prefect_runtime import run_batch_redraw_with_prefect, run_stage_with_prefect


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
