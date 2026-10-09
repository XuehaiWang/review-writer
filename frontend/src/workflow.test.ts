import { describe, expect, it } from "vitest";
import { workflowLinks, workflowStage, projectWorkflowPath } from "./workflow";

describe("public workflow", () => {
  it("has seven uniquely numbered stages", () => {
    expect(new Set(workflowLinks.map(x => x.id)).size).toBe(7);
    expect(workflowLinks.map(x => x.stage)).toEqual(["01", "02", "03", "04", "05", "06", "07"]);
  });
  it("retains explicit project and subview for existing and new projects", () => {
    expect(projectWorkflowPath("planning", "p & q")).toBe("/planning?view=reading&project=p%20%26%20q");
    expect(projectWorkflowPath("library", "p")).toBe("/library?project=p");
    expect(projectWorkflowPath("images", "p")).toBe("/images?tab=review&project=p");
  });
  it("maps cached checkpoint names without adding stages", () => {
    expect(workflowStage("blueprint")?.id).toBe("sections");
    expect(workflowStage("matrix")?.id).toBe("planning");
    expect(workflowStage("figure-review")?.id).toBe("images");
    expect(workflowStage("figures")?.id).toBe("images");
  });
});
