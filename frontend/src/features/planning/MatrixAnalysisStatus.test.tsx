import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MatrixAnalysisStatus, MatrixEvidenceUse, analysisFailed, analysisFailureReason, analysisState } from "./MatrixAnalysisStatus";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

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
