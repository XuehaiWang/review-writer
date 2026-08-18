import { describe, expect, it } from "vitest";

import { hardGateDetails } from "./hardGateDetails";

describe("hardGateDetails", () => {
  it("lists the exact blocking preflight paragraphs for the compound paragraph gate", () => {
    expect(hardGateDetails({
      hard_gate_failures: ["paragraph_readability_or_source_failures"],
      preflight: {
        paragraph_findings: [
          { paragraph_id: "S01-p2", rule: "P01", severity: "major", diagnosis: "Paragraph has 104 words; configured range is 140-280.", route: "section_rewrite" },
          { paragraph_id: "S02-p1", rule: "P01", severity: "major", diagnosis: "Paragraph has 100 words; configured range is 140-280.", route: "section_rewrite" },
          { paragraph_id: "S03-p1", rule: "P08", severity: "minor", diagnosis: "Tighten wording.", route: "final_polish" },
        ],
      },
      paragraph_failures: [
        { paragraph_id: "S09-p1", severity: "major", diagnosis: "This is not the preflight trigger." },
      ],
    })).toEqual([{
      gate_id: "paragraph_readability_or_source_failures",
      findings: [
        { paragraph_id: "S01-p2", rule: "P01", severity: "major", diagnosis: "Paragraph has 104 words; configured range is 140-280.", route: "section_rewrite" },
        { paragraph_id: "S02-p1", rule: "P01", severity: "major", diagnosis: "Paragraph has 100 words; configured range is 140-280.", route: "section_rewrite" },
      ],
    }]);
  });

  it("falls back to legacy paragraph failures when preflight details are absent", () => {
    expect(hardGateDetails({
      hard_gate_failures: ["paragraph_readability_or_source_failures"],
      paragraph_failures: [
        { paragraph_id: "S04-p2", severity: "major", diagnosis: "Source confirmation required." },
      ],
    })[0].findings).toEqual([{
      paragraph_id: "S04-p2",
      severity: "major",
      diagnosis: "Source confirmation required.",
    }]);
  });

  it("keeps non-paragraph gates visible without inventing paragraph identities", () => {
    expect(hardGateDetails({ hard_gate_failures: ["citation_reference_map_mismatch"] })).toEqual([
      { gate_id: "citation_reference_map_mismatch", findings: [] },
    ]);
  });
});
