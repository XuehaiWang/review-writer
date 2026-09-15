import { describe, expect, it } from "vitest";
import { buildPaperDisplayLabels } from "./paperLabels";

describe("canonical library labels", () => {
  it("preserves server labels through filtering and reordering", () => {
    const papers = [{ paper_id: "fixed-b", display_label: "P009" }, { paper_id: "fixed-a", display_label: "P002" }];
    expect(buildPaperDisplayLabels(papers).get("fixed-b")).toBe("P009");
    expect(buildPaperDisplayLabels(papers.slice(1)).get("fixed-a")).toBe("P002");
  });
});
