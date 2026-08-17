import { afterEach, describe, expect, it } from "vitest";

import { readDraftJobId, writeDraftJobId } from "./draftJobPersistence";

describe("draft job persistence", () => {
  afterEach(() => window.localStorage.clear());

  it("keeps each project's current draft job across a page reload", () => {
    writeDraftJobId("project-a", "job-a");
    writeDraftJobId("project-b", "job-b");

    expect(readDraftJobId("project-a")).toBe("job-a");
    expect(readDraftJobId("project-b")).toBe("job-b");
  });

  it("ignores incomplete identifiers", () => {
    writeDraftJobId("", "job-a");
    writeDraftJobId("project-a", "");

    expect(readDraftJobId("")).toBe("");
    expect(readDraftJobId("project-a")).toBe("");
  });
});
