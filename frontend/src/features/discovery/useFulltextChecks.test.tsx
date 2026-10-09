import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { apiRequest } from "../../api/client";
import { useFulltextChecks } from "./useFulltextChecks";
import type { DiscoveryPayload } from "./model/discoveryModel";

vi.mock("../../api/client", () => ({
  apiRequest: vi.fn(),
  jsonBody: (body: unknown) => ({ body: JSON.stringify(body) }),
}));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("checks unique external candidates in one batched request without submitting a download", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ checks: { C1: { state: "available", pdf_url: "https://example.org/a.pdf" } } });
  const payload = { project_id: "p", artifact_id: "a", revision: 1, results: [
    { keyword: "a", local_results: [{ paper_id: "local" }], web_results: [{ candidate_id: "C1" }] },
    { keyword: "b", web_results: [{ candidate_id: "C1" }] },
  ] } as DiscoveryPayload;
  const { result, rerender } = renderHook(() => useFulltextChecks("p", payload));
  await waitFor(() => expect(result.current.C1?.state).toBe("available"));
  // Probing may reach the network, so one POST covers the whole batch.
  expect(apiRequest).toHaveBeenCalledTimes(1);
  expect(vi.mocked(apiRequest).mock.calls[0][0]).toBe("/api/v1/projects/p/discovery/fulltext-check");
  expect(vi.mocked(apiRequest).mock.calls[0][1]?.method).toBe("POST");
  rerender();
  expect(apiRequest).toHaveBeenCalledTimes(1);
});

it("does not check another project's stale results", () => {
  renderHook(() => useFulltextChecks("new-project", { project_id: "old-project", revision: 1, artifact_id: "a" } as DiscoveryPayload));
  expect(apiRequest).not.toHaveBeenCalled();
});

it("reports failed checks as unknown, not unavailable", async () => {
  vi.mocked(apiRequest).mockRejectedValue(new Error("timeout"));
  const { result } = renderHook(() => useFulltextChecks("p", { project_id: "p", artifact_id: "a", revision: 1,
    results: [{ keyword: "x", web_results: [{ candidate_id: "C1" }] }] } as DiscoveryPayload));
  await waitFor(() => expect(result.current.C1?.state).toBe("unknown"));
});
