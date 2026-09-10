import { StrictMode } from "react";
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Job } from "../../api/types";
import { usePlanningCompletionSync } from "./usePlanningCompletionSync";

function job(status: Job["status"], id = "plan-1", updated_at = "1") {
  return { id, status, updated_at };
}

describe("planning completion synchronization", () => {
  it("fetches published content after running becomes succeeded, without a refresh loop", () => {
    const refetch = vi.fn();
    const { rerender } = renderHook(({ jobs }) => usePlanningCompletionSync("p1", jobs, refetch), {
      initialProps: { jobs: [job("running")] },
    });
    expect(refetch).not.toHaveBeenCalled();
    rerender({ jobs: [job("succeeded")] });
    expect(refetch).toHaveBeenCalledTimes(1);
    rerender({ jobs: [job("succeeded")] });
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("also synchronizes when the first snapshot already says completed", () => {
    const refetch = vi.fn();
    renderHook(() => usePlanningCompletionSync("p1", [job("succeeded")], refetch), {
      wrapper: StrictMode,
    });
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("fetches partial results after failure or cancellation, and handles retries", () => {
    const refetch = vi.fn();
    const { rerender } = renderHook(({ jobs }) => usePlanningCompletionSync("p1", jobs, refetch), {
      initialProps: { jobs: [job("failed"), job("cancelled", "matrix-1")] },
    });
    expect(refetch).toHaveBeenCalledTimes(1);
    rerender({ jobs: [job("running", "plan-1", "2")] });
    expect(refetch).toHaveBeenCalledTimes(1);
    rerender({ jobs: [job("succeeded", "plan-1", "3")] });
    expect(refetch).toHaveBeenCalledTimes(2);
  });

  it("isolates projects and does not fetch an empty project", () => {
    const refetch = vi.fn();
    const { rerender } = renderHook(({ project }) => usePlanningCompletionSync(project, [job("succeeded")], refetch), {
      initialProps: { project: "" },
    });
    expect(refetch).not.toHaveBeenCalled();
    rerender({ project: "p1" });
    rerender({ project: "p2" });
    expect(refetch).toHaveBeenCalledTimes(2);
  });
});
