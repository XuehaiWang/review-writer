import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { usePreferences } from "../../state/preferences";
import { MatrixFactRecords, MatrixPaperGuide, MatrixReadingStatus } from "./MatrixPaperReading";

afterEach(() => { cleanup(); usePreferences.setState({ language: "zh-CN" }); });

const read = { processing: { reading: "completed", verification: "pending" },
  fact_extraction_profile: { reading_strategy: "review-oriented/1", reading_status: "completed", reading_source_key: "source-1" } };

it("shows a source-linked guide even when first reading produced no standalone facts", () => {
  render(<MatrixPaperGuide paper={{ paper_id: "paper/1", scientific_facts: [], paper_analysis: {
    research_question: "How does the reaction proceed?", contribution: "A bounded mechanism study.",
    topic_relation: "Provides a control experiment.", evidence_keys: ["stable-source"], usage: "navigation_only",
  } }} />);
  expect(screen.getByRole("region", { name: "论文导读" })).toHaveTextContent("A bounded mechanism study.");
  expect(screen.getByRole("link", { name: "查看本篇原文" })).toHaveAttribute("href", "/api/v1/library/papers/paper%2F1/pdf");
  expect(screen.getByText(/不代表其中的科学关系已经核验/)).toBeInTheDocument();
  expect(screen.queryByText("已核验依据")).not.toBeInTheDocument();
});

it("separates verified evidence, new findings, and rejected records without changing source excerpts", () => {
  render(<MatrixFactRecords emptyMessage="Empty" paper={{ paper_id: "P1", fact_enrichment: read, scientific_facts: [
    { fact_id: "V", value: "Existing verified result", validation_contract: "fact-support/3", verification: { status: "supported" },
      support_level: "direct", support_excerpt: "Exact source passage", evidence_refs: [{ page_start: 4 }] },
    { fact_id: "C", value: "New candidate result", validation_contract: "fact-support/3", verification: { status: "pending" }, support_level: "context_only" },
    { fact_id: "M", value: "Candidate missing audit fields" },
    { fact_id: "R", value: "Rejected interpretation", validation_contract: "fact-support/3", verification: { status: "rejected" } },
  ] }} />);
  const verified = screen.getByRole("region", { name: "已核验依据" });
  expect(verified).toHaveTextContent("Existing verified result");
  expect(verified).not.toHaveTextContent("New candidate result");
  expect(within(verified).getByRole("link", { name: "打开原始 PDF", hidden: true })).toHaveAttribute("href", "/api/v1/library/papers/P1/pdf#page=4");
  expect(screen.getByText("Exact source passage")).toBeInTheDocument();
  const findings = screen.getByRole("region", { name: "初读发现" });
  expect(findings).toHaveTextContent("New candidate result");
  expect(findings).toHaveTextContent("Candidate missing audit fields");
  expect(screen.getByRole("region", { name: "受限记录" })).toHaveTextContent("Rejected interpretation");
});

it("keeps historical cards readable without inventing a verification result", () => {
  render(<MatrixFactRecords emptyMessage="Empty" paper={{ paper_id: "old", scientific_facts: [
    { value: "Historical finding", support_excerpt: "Historical source", evidence_refs: [{ chunk_id: "abstract" }], source_channel: "abstract" },
  ] }} />);
  expect(screen.getByRole("region", { name: "历史来源记录" })).toHaveTextContent("Historical finding");
  expect(screen.queryByRole("region", { name: "已核验依据" })).not.toBeInTheDocument();
  expect(screen.getByText(/仅摘要/)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "打开原始 PDF", hidden: true })).not.toBeInTheDocument();
});

it.each([
  { ref: { source_file_id: "P1/SI", paper_id: "P1" }, sourceId: "P1%2FSI" },
  { ref: { paper_id: "P1/SI" }, sourceId: "P1%2FSI" },
  { ref: {}, sourceId: "P1" },
])("opens the referenced source PDF with its own page number ($sourceId)", ({ ref, sourceId }) => {
  render(<MatrixFactRecords emptyMessage="Empty" paper={{ paper_id: "P1", fact_enrichment: read,
    scientific_facts: [{ fact_id: "SI", value: "Supporting information finding",
      verification: { status: "pending" }, evidence_refs: [{ ...ref, page_start: 12 }] }],
  }} />);
  expect(screen.getByRole("link", { name: "打开原始 PDF", hidden: true }))
    .toHaveAttribute("href", `/api/v1/library/papers/${sourceId}/pdf#page=12`);
  expect(screen.getByRole("region", { name: "初读发现" })).toHaveTextContent("Supporting information finding");
  expect(screen.queryByRole("region", { name: "已核验依据" })).not.toBeInTheDocument();
});

it("explains deferred unused candidates in English but keeps a manual revision pending", () => {
  usePreferences.setState({ language: "en" });
  const paper = { paper_id: "P1", fact_enrichment: { ...read, pending_fact_count: 1 } };
  const { rerender } = render(<MatrixReadingStatus paper={paper} />);
  expect(screen.getByText(/First reading is complete/)).toBeInTheDocument();
  rerender(<MatrixReadingStatus paper={{ ...paper, scientific_facts: [
    { fact_id: "R", revision_of_fact_id: "V", verification: { status: "pending" } },
  ] }} />);
  expect(screen.getByText(/Requested verification is unfinished/)).toBeInTheDocument();
  expect(screen.queryByText(/First reading is complete/)).not.toBeInTheDocument();
});
