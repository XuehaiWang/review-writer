import { describe, expect, it } from "vitest";

import type { UploadJob } from "../../api/types";
import { uploadJobsNeedingLibraryRefresh } from "./uploadRefresh";

function job(id: string, status: UploadJob["status"]): UploadJob {
  return {
    id,
    project_id: null,
    scope: "library",
    job_type: "library.upload",
    status,
    result: {},
    progress_current: status === "succeeded" ? 3 : 1,
    progress_total: 3,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    started_at: "2026-08-18T00:00:00Z",
    finished_at: status === "succeeded" ? "2026-08-18T00:01:00Z" : null,
    available_actions: [],
    filename: `${id}.pdf`,
    batch_id: "batch-1",
  };
}

describe("uploadJobsNeedingLibraryRefresh", () => {
  it("refreshes when a polled upload changes from running to succeeded", () => {
    expect(uploadJobsNeedingLibraryRefresh(
      [job("upload-1", "succeeded")],
      new Map([["upload-1", "running"]]),
      new Set(),
      new Set(),
    )).toEqual(["upload-1"]);
  });

  it("refreshes a locally submitted job even when its first polled state is succeeded", () => {
    expect(uploadJobsNeedingLibraryRefresh(
      [job("upload-1", "succeeded")],
      new Map(),
      new Set(["upload-1"]),
      new Set(),
    )).toEqual(["upload-1"]);
  });

  it("does not refresh old, failed, or already refreshed jobs", () => {
    expect(uploadJobsNeedingLibraryRefresh(
      [job("old", "succeeded"), job("failed", "failed"), job("done", "succeeded")],
      new Map([["old", "succeeded"], ["failed", "running"], ["done", "running"]]),
      new Set(),
      new Set(["done"]),
    )).toEqual([]);
  });
});
