import { describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { draftPublicationIsPending } from "./draftPublicationSync";

function job(status: Job["status"], result: Record<string, unknown>): Job {
  return {
    id: "job-1",
    project_id: "project-1",
    scope: "project",
    job_type: "draft.optimize",
    status,
    result,
    progress_current: 0,
    progress_total: 0,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "",
    updated_at: "",
    started_at: null,
    finished_at: null,
    available_actions: [],
  };
}

describe("draftPublicationIsPending", () => {
  it("waits for the revision published by a successful job", () => {
    expect(draftPublicationIsPending(job("succeeded", { revision: 44 }), { revision: 43 })).toBe(true);
    expect(draftPublicationIsPending(job("succeeded", { revision: 44 }), { revision: 44 })).toBe(false);
  });

  it("does not replace a newer current revision with an older job result", () => {
    expect(draftPublicationIsPending(job("succeeded", { revision: 44, draft_artifact_id: "old" }), {
      revision: 45,
      draft_artifact_id: "newer",
    })).toBe(false);
  });

  it("falls back to artifact ids for legacy results without a revision", () => {
    const completed = job("succeeded", { draft_artifact_id: "draft-new", quality_artifact_id: "quality-new" });
    expect(draftPublicationIsPending(completed, {
      draft_artifact_id: "draft-old",
      quality_artifact_id: "quality-old",
    })).toBe(true);
    expect(draftPublicationIsPending(completed, {
      draft_artifact_id: "draft-new",
      quality_artifact_id: "quality-new",
    })).toBe(false);
  });

  it("does not poll publication while a job is still active", () => {
    expect(draftPublicationIsPending(job("running", { revision: 44 }), { revision: 43 })).toBe(false);
  });
});
