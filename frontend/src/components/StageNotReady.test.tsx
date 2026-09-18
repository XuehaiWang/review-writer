import { cleanup, render, screen, act } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ErrorState } from "./ErrorState";
import { ApiError } from "../api/client";
import { usePreferences } from "../state/preferences";

afterEach(() => { cleanup(); vi.useRealTimers(); });
it("explains the prerequisite and keeps project context", () => {
  usePreferences.getState().setLanguage("en");
  render(<ErrorState error={new ApiError("not ready", 404, "WORKFLOW_STAGE_NOT_READY", "", {
    next_stage: "sections", project_id: "project-one", active_job: { status: "running", current: 4, total: 7 },
  })} />);
  expect(screen.getByRole("link", { name: "Go to Sections" })).toHaveAttribute("href", "/sections?project=project-one");
  expect(screen.getByText(/4\/7/)).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
it("does not disguise missing files as an unstarted stage", () => {
  render(<ErrorState error={new ApiError("File missing", 404, "ARTIFACT_FILE_MISSING")} />);
  expect(screen.getByRole("alert")).toHaveTextContent("File missing");
});
it("refreshes read-only status and stops polling on unmount", async () => {
  vi.useFakeTimers();
  const refresh = vi.fn();
  const view = render(<ErrorState error={new ApiError("not ready", 404, "WORKFLOW_STAGE_NOT_READY", "", { next_stage: "planning" })} onRetry={refresh} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
  expect(refresh).toHaveBeenCalledTimes(1);
  view.unmount();
  await vi.advanceTimersByTimeAsync(15000);
  expect(refresh).toHaveBeenCalledTimes(1);
});
