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

it("retries only failed papers, excludes missing sources and keeps details collapsed", async () => {
  const fetch = vi.fn(async (_input: unknown) => Response.json({ id: "job", status: "queued" }));
  vi.stubGlobal("fetch", fetch);
  const refresh = vi.fn(async () => undefined);
  const papers = [
    { paper_id: "P1", fact_enrichment: { status: "failed", error: "provider timed out" } },
    { paper_id: "P2", fact_enrichment: { status: "complete" } },
    { paper_id: "P3", fact_enrichment: { status: "failed", error: "source extraction missing" } },
  ];
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><MatrixAnalysisStatus paper={papers[0]} papers={papers} projectId="test" busy={false} refresh={refresh} /></QueryClientProvider>);
  expect(screen.getByText("技术详情").closest("details")).not.toHaveAttribute("open");
  fireEvent.click(screen.getByText("重试失败项（1）"));
  await waitFor(() => expect(refresh).toHaveBeenCalled());
  expect(String(fetch.mock.calls[0][0])).toContain("paper_ids=P1");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P2");
  expect(String(fetch.mock.calls[0][0])).not.toContain("P3");
  expect(String(fetch.mock.calls[0][0])).not.toContain("force=true");
  client.clear();
});

it("disables retries while a task is running", () => {
  const paper = { paper_id: "P1", fact_enrichment: { status: "failed" } };
  const client = new QueryClient();
  render(<QueryClientProvider client={client}><MatrixAnalysisStatus paper={paper} papers={[paper]} projectId="test" busy refresh={async () => undefined} /></QueryClientProvider>);
  expect(screen.getByText("继续本篇未完成分析")).toBeDisabled();
  client.clear();
});
