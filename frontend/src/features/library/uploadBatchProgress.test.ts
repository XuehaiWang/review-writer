import { describe, expect, it } from "vitest";

import type { UploadBatchSummary } from "../../api/types";
import { buildUploadBatchCounts } from "./uploadBatchProgress";

function summary(overrides: Partial<UploadBatchSummary> = {}): UploadBatchSummary {
  return {
    batch_id: "batch-1",
    total: 205,
    queued: 178,
    running: 1,
    cancel_requested: 0,
    succeeded: 26,
    failed: 0,
    cancelled: 0,
    interrupted: 0,
    created_at: "2026-08-23T04:41:29Z",
    updated_at: "2026-08-23T04:42:00Z",
    ...overrides,
  };
}

describe("buildUploadBatchCounts", () => {
  it("keeps the complete batch denominator after old completed rows are hidden", () => {
    expect(buildUploadBatchCounts(summary(), [], 205)).toEqual({
      total: 205,
      done: 26,
      failed: 0,
      uploading: 1,
      queued: 178,
    });
  });

  it("includes files that are still being submitted by the browser", () => {
    expect(buildUploadBatchCounts(summary({ total: 5, queued: 3, running: 1, succeeded: 1 }), ["uploading", "queued", "queued"], 8)).toEqual({
      total: 8,
      done: 1,
      failed: 0,
      uploading: 2,
      queued: 5,
    });
  });
});
