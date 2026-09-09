import { describe, expect, it } from "vitest";

import { buildMatrixRecommendation } from "./matrixRecommendation";

describe("buildMatrixRecommendation", () => {
  it("recommends core and supporting papers and deduplicates repeated hits", () => {
    const result = buildMatrixRecommendation([
      { local_results: [
        { paper_id: "P001", role: "core_candidate", score: 0.8 },
        { paper_id: "P002", role: "uncertain", score: 0.1 },
      ] },
      { local_results: [
        { paper_id: "P001", role: "supporting_candidate", score: 0.5 },
        { paper_id: "P003", role: "supporting_candidate", score: 0.4 },
      ] },
    ]);

    expect([...result.recommendedIds]).toEqual(["P001", "P003"]);
    expect([...result.reviewIds]).toEqual(["P002"]);
  });

  it("does not treat query-group coverage as a reason to recommend background papers", () => {
    const result = buildMatrixRecommendation([
      { local_results: [{ paper_id: "P001", role: "core_candidate", score: 0.8 }] },
      { local_results: [
        { paper_id: "P002", role: "background", score: 0.2 },
        { paper_id: "P003", role: "background", score: 0.16 },
      ] },
    ]);

    expect([...result.recommendedIds]).toEqual(["P001"]);
    expect([...result.reviewIds]).toEqual(["P002", "P003"]);
  });

  it("never recommends excluded, zero-score, uncertain, or inactive-group-only papers", () => {
    const result = buildMatrixRecommendation([
      { local_results: [
        { paper_id: "P001", role: "excluded", score: 0.9 },
        { paper_id: "P002", role: "core_candidate", score: 0 },
        { paper_id: "P003", role: "uncertain", score: 0.12 },
      ] },
      { keep: false, local_results: [{ paper_id: "P004", role: "core_candidate", score: 0.95 }] },
    ]);

    expect(result.recommendedIds.size).toBe(0);
    expect([...result.reviewIds]).toEqual(["P003"]);
    expect([...result.excludedIds]).toEqual(["P001", "P002"]);
  });
});
