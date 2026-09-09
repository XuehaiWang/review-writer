import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { usePreferences } from "../../state/preferences";
import { SavedSourceChecks, type SourceCheck } from "./DraftRepairSummary";

afterEach(cleanup);

const entry = {paragraph_id: "S1-p1", source_check_status: "verified",
  targeted_source_recheck: {status: "supported_without_prose_change"},
  source_evidence_refs: ["P1:p2:b3"], papers: [{paper_id: "P1", passages: [{ref: "P1:p2:b3", text: "Matching source."}]}]};

it("shows saved evidence separately from prose acceptance, including its location", () => {
  usePreferences.getState().setLanguage("zh-CN");
  render(<SavedSourceChecks report={{entries: [entry]}} />);
  expect(screen.getByText("已保存补证关联（1 段，正文未改动）")).toBeInTheDocument();
  expect(screen.getByText("P1 · P1:p2:b3")).toBeInTheDocument();
  expect(screen.getByText("Matching source.")).toBeInTheDocument();
});

it("does not count deferred or unresolved entries as saved support", () => {
  const report: SourceCheck = {entries: [{...entry, unsupported_claims: ["unresolved"]},
    {...entry, targeted_source_recheck: {status: "provider_deferred"}}]};
  const {container} = render(<SavedSourceChecks report={report} />);
  expect(container).toBeEmptyDOMElement();
});

it("supports the English interface", () => {
  usePreferences.getState().setLanguage("en");
  render(<SavedSourceChecks report={{entries: [entry]}} />);
  expect(screen.getByText("Saved source links (1 paragraphs, prose unchanged)")).toBeInTheDocument();
});
