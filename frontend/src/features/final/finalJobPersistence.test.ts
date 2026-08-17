import { afterEach, describe, expect, it } from "vitest";

import { readFinalJobId, writeFinalJobId } from "./finalJobPersistence";

describe("final job persistence", () => {
  afterEach(() => window.localStorage.clear());

  it("keeps the current final-stage job per project across refreshes", () => {
    writeFinalJobId("project-a", "job-a");
    writeFinalJobId("project-b", "job-b");

    expect(readFinalJobId("project-a")).toBe("job-a");
    expect(readFinalJobId("project-b")).toBe("job-b");
  });

  it("ignores incomplete identifiers", () => {
    writeFinalJobId("", "job-a");
    writeFinalJobId("project-a", "");

    expect(readFinalJobId("")).toBe("");
    expect(readFinalJobId("project-a")).toBe("");
  });
});
