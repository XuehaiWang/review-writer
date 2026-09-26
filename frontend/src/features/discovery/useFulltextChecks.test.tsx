import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { apiRequest } from "../../api/client";
import { useFulltextChecks } from "./useFulltextChecks";
import type { DiscoveryPayload } from "./model/discoveryModel";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("automatically checks unique external candidates without submitting a download", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ state: "available", pdf_url: "https://example.org/a.pdf" });
  const payload = { project_id: "p", artifact_id: "a", revision: 1, results: [
    { keyword: "a", local_results: [{ paper_id: "local" }], web_results: [{ candidate_id: "C1" }] },
    { keyword: "b", web_results: [{ candidate_id: "C1" }] },
  ] } as DiscoveryPayload;
  const { result, rerender } = renderHook(() => useFulltextChecks("p", payload));
  await waitFor(() => expect(result.current.C1?.state).toBe("available"));
  expect(apiRequest).toHaveBeenCalledTimes(1);
  expect(vi.mocked(apiRequest).mock.calls[0][0]).toContain("/fulltext-check?candidate_id=C1");
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
