import { describe, expect, it } from "vitest";

import { preferredDraftJobId, restorableDraftJobId, serverJobToRemember } from "./draftJobSelection";

describe("draft job selection", () => {
  it("shows a newly submitted local job instead of a stale completed server job", () => {
    expect(preferredDraftJobId({
      localJobId: "new-rewrite-job",
      latestServerJobId: "previous-completed-job",
      storedJobId: "previous-completed-job",
    })).toBe("new-rewrite-job");
  });

  it("adopts the active server job after the draft payload refreshes", () => {
    expect(preferredDraftJobId({
      activeServerJobId: "active-server-job",
      localJobId: "submitted-job",
      latestServerJobId: "previous-completed-job",
    })).toBe("active-server-job");
  });

  it("does not overwrite a new local job with an older latest server job", () => {
    expect(serverJobToRemember({
      locallySelectedJobId: "new-rewrite-job",
      latestServerJobId: "previous-completed-job",
    })).toBe("new-rewrite-job");
  });

  it("restores the latest server job on an initial page load", () => {
    expect(serverJobToRemember({ latestServerJobId: "latest-job" })).toBe("latest-job");
  });

  it("does not revive a historical terminal failure after a page refresh", () => {
    expect(restorableDraftJobId("failed-job", "failed")).toBe("");
    expect(restorableDraftJobId("cancelled-job", "cancelled")).toBe("");
    expect(restorableDraftJobId("interrupted-job", "interrupted")).toBe("");
  });

  it("restores active and successful jobs", () => {
    expect(restorableDraftJobId("running-job", "running")).toBe("running-job");
    expect(restorableDraftJobId("successful-job", "succeeded")).toBe("successful-job");
  });
});
