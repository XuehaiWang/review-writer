"""Checks for the strict Images-to-Draft handoff gate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from serve_review_dashboard import (
    DashboardHandler,
    FigureToDraftBlocked,
    confirm_figures_and_build_draft,
    execute_dashboard_stage,
    figure_to_draft_readiness,
)


def figures_payload(
    *,
    selected: int,
    usable: int,
    stale: bool = False,
    source_stale: bool = False,
    batch_status: str = "idle",
    figure_status: str = "completed",
) -> dict[str, object]:
    states = {"P001-F01": {"status": figure_status}} if selected else {}
    return {
        "freshness": {
            "selected_count": selected,
            "usable_count": usable,
            "stale": stale,
            "source_stale": source_stale,
        },
        "batch_redraw": {"status": batch_status},
        "figure_redraw_states": states,
    }


class FigureToDraftGateChecks(unittest.TestCase):
    def test_readiness_accepts_only_complete_current_outputs(self) -> None:
        readiness = figure_to_draft_readiness(figures_payload(selected=2, usable=2))

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["code"], "ready")
        self.assertEqual(readiness["selected_count"], 2)
        self.assertEqual(readiness["usable_count"], 2)
        self.assertEqual(readiness["remaining_count"], 0)
        self.assertIn("All 2 selected manuscript figures are usable", readiness["message"])

    def test_readiness_explains_no_selection_incomplete_and_stale_outputs(self) -> None:
        no_selection = figure_to_draft_readiness(figures_payload(selected=0, usable=0))
        incomplete = figure_to_draft_readiness(
            figures_payload(selected=3, usable=1, stale=True)
        )
        stale = figure_to_draft_readiness(
            figures_payload(selected=2, usable=2, stale=True, source_stale=True)
        )

        self.assertEqual(no_selection["code"], "no_selection")
        self.assertFalse(no_selection["ready"])
        self.assertEqual(incomplete["code"], "incomplete")
        self.assertEqual(incomplete["remaining_count"], 2)
        self.assertIn("1/3", incomplete["message"])
        self.assertEqual(stale["code"], "out_of_date")

    def test_readiness_blocks_batch_and_single_figure_generation(self) -> None:
        batch = figure_to_draft_readiness(
            figures_payload(selected=1, usable=1, batch_status="running")
        )
        queued = figure_to_draft_readiness(
            figures_payload(selected=1, usable=1, figure_status="queued")
        )

        self.assertEqual(batch["code"], "generating")
        self.assertTrue(batch["generation_active"])
        self.assertEqual(queued["code"], "generating")
        self.assertTrue(queued["generation_active"])

    def test_confirm_builds_draft_without_generating_any_figure(self) -> None:
        ready = figures_payload(selected=2, usable=2)
        with (
            patch("serve_review_dashboard.project_figures_payload", return_value=ready),
            patch(
                "serve_review_dashboard.regenerate_first_draft",
                return_value={"first_draft": "first_draft.md"},
            ) as build_draft,
            patch("serve_review_dashboard.regenerate_figures") as generate_figures,
        ):
            result = confirm_figures_and_build_draft(Path("workspace"), "demo")

        build_draft.assert_called_once_with(Path("workspace"), "demo")
        generate_figures.assert_not_called()
        self.assertEqual(result["readiness"]["code"], "ready")
        self.assertEqual(result["draft"]["first_draft"], "first_draft.md")

    def test_blocked_confirm_does_not_touch_the_draft(self) -> None:
        incomplete = figures_payload(selected=2, usable=1, stale=True)
        with (
            patch("serve_review_dashboard.project_figures_payload", return_value=incomplete),
            patch("serve_review_dashboard.regenerate_first_draft") as build_draft,
        ):
            with self.assertRaises(FigureToDraftBlocked) as caught:
                confirm_figures_and_build_draft(Path("workspace"), "demo")

        build_draft.assert_not_called()
        self.assertEqual(caught.exception.readiness["selected_count"], 2)
        self.assertEqual(caught.exception.readiness["usable_count"], 1)
        self.assertEqual(caught.exception.readiness["remaining_count"], 1)

    def test_figures_stage_uses_confirmation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "review-projects" / "demo").mkdir(parents=True)
            with patch(
                "serve_review_dashboard.confirm_figures_and_build_draft",
                return_value={"draft": {"first_draft": "first_draft.md"}},
            ) as confirm:
                result = execute_dashboard_stage(root, "demo", "figures")

        confirm.assert_called_once_with(root, "demo")
        self.assertEqual(result["next_stage"], "draft")

    def test_stage_run_returns_structured_conflict_when_confirmation_is_blocked(self) -> None:
        class HandlerProbe:
            def __init__(self, review_root: Path) -> None:
                self.review_root = review_root
                self.response: tuple[dict[str, object], object] | None = None

            def send_json(self, payload: dict[str, object], status: object = None) -> None:
                self.response = (payload, status)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "review-projects" / "demo").mkdir(parents=True)
            readiness = figure_to_draft_readiness(
                figures_payload(selected=2, usable=1)
            )
            probe = HandlerProbe(root)
            with patch(
                "serve_review_dashboard.run_stage_with_prefect",
                side_effect=FigureToDraftBlocked(readiness),
            ):
                DashboardHandler.handle_project_stage_run(
                    probe,
                    "demo",
                    "figures",
                )

        self.assertIsNotNone(probe.response)
        payload, status = probe.response or ({}, None)
        self.assertEqual(int(status), 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["readiness"], readiness)


if __name__ == "__main__":
    unittest.main()
