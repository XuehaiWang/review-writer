import { describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { findResumableSectionJob } from "./sectionJobResume";

function failedJob(checkpoint: Record<string, unknown>, updatedAt = "2026-08-20T08:00:00Z"): Job {
  return {
    id: "failed-job",
    project_id: "project",
    scope: "project",
    job_type: "sections.generate",
    status: "failed",
    result: { section_checkpoint: checkpoint },
    progress_current: 1,
    progress_total: 2,
    cancellation_requested: false,
    error_code: "SCIENTIFIC_RUN_FAILED",
    error_message: "failed",
    retry_of_job_id: null,
    created_at: updatedAt,
    updated_at: updatedAt,
    started_at: updatedAt,
    finished_at: updatedAt,
    available_actions: ["retry"],
  };
}

describe("findResumableSectionJob", () => {
  const tasks = [
    { section_id: "S01", heading: "Introduction" },
    { section_id: "S02", heading: "Methods" },
  ];

  it("restores a failed job whose checkpoint matches the current blueprint", () => {
    const job = failedJob({
      task_ids: ["S01", "S02"],
      entries: { S01: { heading: "Introduction" } },
    });

    expect(findResumableSectionJob([job], tasks)?.id).toBe("failed-job");
  });

  it("supports historical report jobs that omitted available_actions", () => {
    const job = failedJob({
      task_ids: ["S01", "S02"],
      entries: { S01: { heading: "Introduction" } },
    });
    const historical = { ...job, available_actions: undefined } as unknown as Job;

    expect(findResumableSectionJob([historical], tasks)?.id).toBe("failed-job");
  });

  it("does not restore a checkpoint from a different blueprint", () => {
    const job = failedJob({
      task_ids: ["S01", "S02"],
      entries: { S01: { heading: "Old introduction" } },
    });

    expect(findResumableSectionJob([job], tasks)).toBeUndefined();
  });
});
