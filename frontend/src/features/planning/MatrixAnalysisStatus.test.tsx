import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MatrixAnalysisStatus, MatrixEvidenceUse, analysisFailed, analysisFailureReason, analysisState, unfinishedAnalysis } from "./MatrixAnalysisStatus";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("reaches the same state from a list digest as from the full fact records", () => {
  // The paper list omits every paper's fact records and carries only the
  // server-derived digest, so both paths must classify a paper identically.
  // Keep this table in step with fact_display_signals in review_writer_core.
  const enrichment = { status: "complete", review_readiness: "complete", processing: { reading: "completed", extraction: "completed" } };
  const cases = [
    { name: "no findings", facts: [], digest: { has_pending_revision: false, has_limited_fact: false } },
    { name: "verified record", facts: [{ verification: { status: "supported" } }], digest: { has_pending_revision: false, has_limited_fact: false } },
    { name: "context-only record", facts: [{ support_level: "context_only" }], digest: { has_pending_revision: false, has_limited_fact: true } },
    { name: "uncertain record", facts: [{ verification: { status: "uncertain" } }], digest: { has_pending_revision: false, has_limited_fact: true } },
    { name: "rejected record", facts: [{ verification: { status: "rejected" } }], digest: { has_pending_revision: false, has_limited_fact: true } },
    { name: "unverified revision", facts: [{ revision_of_fact_id: "old" }], digest: { has_pending_revision: true, has_limited_fact: false } },
    { name: "superseded revision", facts: [{ revision_of_fact_id: "old", superseded_by_fact_id: "new" }], digest: { has_pending_revision: false, has_limited_fact: false } },
  ];
  for (const item of cases) {
    const fromRecords = analysisState({ paper_id: item.name, scientific_facts: item.facts, fact_enrichment: enrichment });
    const fromDigest = analysisState({ paper_id: item.name, facts_digest: item.digest, fact_enrichment: enrichment });
    expect([item.name, fromDigest]).toEqual([item.name, fromRecords]);
  }
});

it("keeps evidence guidance compact and separate from task feedback", () => {
  const paper = { paper_id: "P1", fact_enrichment: { status: "complete", review_readiness: "complete" } };
  const client = new QueryClient();
  const { container } = render(<QueryClientProvider client={client}>
    <MatrixAnalysisStatus paper={paper} papers={[paper]} projectId="test" busy={false} refresh={async () => undefined} />
    <MatrixEvidenceUse paper={paper} />
  </QueryClientProvider>);
  expect(screen.getByText("证据用途").closest("details")).not.toHaveAttribute("open");
  expect(container.querySelector(".matrix-analysis-actions")).toBeNull();
  expect(screen.queryByText(/请求已提交/)).not.toBeInTheDocument();
  client.clear();
});

it("distinguishes processing failures from scientific coverage gaps", () => {
  expect(analysisState({ paper_id: "P1", fact_enrichment: { status: "partial", review_readiness: "source_not_established", processing: { extraction: "completed", verification: "completed" } } })).toBe("limited");
  expect(analysisState({ paper_id: "P1", scientific_facts: [{ support_level: "context_only", verification: { status: "rejected" } }], fact_enrichment: { status: "complete", review_readiness: "complete" } })).toBe("limited");
  expect(analysisFailed({ paper_id: "P1", fact_enrichment: { status: "partial" } })).toBe(false);
  expect(analysisFailed({ paper_id: "P1", fact_enrichment: { status: "complete", last_attempt: { status: "failed" } } })).toBe(true);
  expect(analysisFailureReason("provider timed out")).toBe("network");
  expect(analysisFailureReason("source extraction missing")).toBe("source");
  expect(analysisFailureReason("JSONDecodeError")).toBe("format");
  expect(analysisFailureReason("No source-addressable evidence candidate is available.")).toBe("retrieval");
  expect(analysisState({ paper_id: "P1", fact_enrichment: { status: "partial" } })).toBe("complete");
  expect(analysisState({ paper_id: "P1", fact_enrichment: { status: "partial", processing: { verification: "pending" } } })).toBe("pending");
  expect(analysisState({ paper_id: "P1", fact_enrichment: { status: "partial", fact_extraction_profile: { stop_reason: "retrieval_unavailable" } } })).toBe("failed");
  expect(analysisState({ paper_id: "P1", fact_enrichment: { status: "failed" } }, true)).toBe("running");
});

it("resumes pending and failed papers while excluding completed and blocked papers", async () => {
  const fetch = vi.fn(async (_input: unknown) => Response.json({ id: "job", status: "queued" }));
  vi.stubGlobal("fetch", fetch);
  const refresh = vi.fn(async () => undefined);
  const papers = [
    { paper_id: "P1", fact_enrichment: { status: "failed", error: "provider timed out" } },
    { paper_id: "P2", fact_enrichment: { status: "complete" } },
    { paper_id: "P4" },
    { paper_id: "P5", fact_enrichment: { status: "limited", review_readiness: "limited" } },
    { paper_id: "P6", fact_enrichment: { status: "failed", error: "Model x is not available for this group" } },
    { paper_id: "P3", fact_enrichment: { status: "failed", error: "source extraction missing" } },
  ];
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><MatrixAnalysisStatus papers={papers} projectId="test" busy={false} refresh={refresh} /></QueryClientProvider>);
  expect(screen.getByText("查看原因").closest("details")).not.toHaveAttribute("open");
  fireEvent.click(screen.getByText("继续未完成分析（2）"));
  await waitFor(() => expect(refresh).toHaveBeenCalled());
  expect(String(fetch.mock.calls[0][0])).toContain("paper_ids=P1");
  expect(String(fetch.mock.calls[0][0])).toContain("paper_ids=P4");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P5");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P6");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P2");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P3");
  expect(String(fetch.mock.calls[0][0])).not.toContain("force=true");
  client.clear();
});

it("disables retries while a task is running", () => {
  const paper = { paper_id: "P1", fact_enrichment: { status: "failed" } };
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><MatrixAnalysisStatus papers={[paper]} projectId="test" busy refresh={async () => undefined} /></QueryClientProvider>);
  expect(screen.getByText("分析进行中…")).toBeDisabled();
  client.clear();
});


it("does not restart exhausted evidence searches but resumes unfinished verification", () => {
  const fact = { status: "partial", fact_extraction_profile: { stop_reason: "no_new_evidence" }, processing: { extraction: "completed", verification: "completed", source_recovery: "pending" } };
  expect(analysisState({ paper_id: "p", fact_enrichment: fact })).toBe("limited");
  expect(analysisState({ paper_id: "p", fact_enrichment: { ...fact, processing: { ...fact.processing, verification: "pending" } } })).toBe("pending");
});

it("restores unpublished checkpoint before submitting new extraction", async () => {
  const fetch = vi.fn(async (_input: unknown) => Response.json({ id: "job", status: "queued" }));
  vi.stubGlobal("fetch", fetch);
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><MatrixAnalysisStatus papers={[{ paper_id: "p" }]} projectId="test" busy={false} recoveryJobId="previous-job" refresh={async () => undefined} /></QueryClientProvider>);
  fireEvent.click(screen.getByText("继续未完成分析（1）"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(fetch.mock.calls[0][0]).toBe("/api/v1/jobs/previous-job/retry");
  client.clear();
});

it("treats unused first-reading candidates as complete without concealing requested revisions or failures", () => {
  const profile = { reading_strategy: "review-oriented/1", reading_status: "completed", reading_source_key: "source-1" };
  const read = { paper_id: "read", scientific_facts: [{ validation_contract: "fact-support/3", verification: { status: "pending" } }],
    fact_enrichment: { status: "partial", review_readiness: "source_not_established", fact_extraction_profile: profile,
      processing: { reading: "completed", extraction: "completed", verification: "pending", source_recovery: "not_required" } } };
  expect(analysisState(read)).toBe("complete");
  expect(analysisState({ ...read, scientific_facts: [] })).toBe("complete");
  expect(analysisState({ ...read, fact_enrichment: { ...read.fact_enrichment, processing: undefined } })).toBe("complete");
  const revision = { ...read, paper_id: "revision", scientific_facts: [{ fact_id: "new", revision_of_fact_id: "old", verification: { status: "pending" } }] };
  expect(analysisState(revision)).toBe("pending");
  expect(analysisState({ ...read, fact_enrichment: { ...read.fact_enrichment, pending_revision_fact_ids: ["new"] } })).toBe("pending");
  const failed = { ...read, paper_id: "failed", fact_enrichment: { ...read.fact_enrichment, last_attempt: { status: "failed", error: "provider timed out" } } };
  expect(analysisState(failed)).toBe("failed");
  expect(analysisState({ ...read, fact_enrichment: { ...read.fact_enrichment,
    fact_extraction_profile: { ...profile, stop_reason: "provider_or_budget_unavailable" } } })).toBe("failed");
  expect(analysisState({ ...read, fact_enrichment: { ...read.fact_enrichment,
    processing: { ...read.fact_enrichment.processing, verification: "failed" } } })).toBe("failed");
  expect(unfinishedAnalysis([read, revision, failed]).retryable.map(paper => paper.paper_id)).toEqual(["revision", "failed"]);
});

it("does not offer a new paid request for a completed reading with pending candidates", () => {
  const paper = { paper_id: "read", fact_enrichment: { status: "partial", review_readiness: "source_not_established",
    processing: { reading: "completed", verification: "pending" } } };
  const client = new QueryClient();
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(<QueryClientProvider client={client}>
    <MatrixAnalysisStatus papers={[paper]} projectId="project-read" busy={false} refresh={async () => undefined} />
    <MatrixEvidenceUse paper={paper} />
  </QueryClientProvider>);
  expect(screen.getByRole("button", { name: "继续未完成分析（0）" })).toBeDisabled();
  expect(screen.getByText("初读已完成，具体判断按写作需要核验")).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
  client.clear();
});
