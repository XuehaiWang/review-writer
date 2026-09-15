import { beforeEach, expect, it, vi } from "vitest";
import { ApiError, apiRequest } from "../../api/client";
import { approveCurrentDraft } from "./approveCurrentDraft";

vi.mock("../../api/client", async (importOriginal) => ({ ...await importOriginal<object>(), apiRequest: vi.fn() }));
beforeEach(() => vi.clearAllMocks());
const viewed = { revision: 2, draft_artifact_id: "draft", quality_artifact_id: "quality", quality: { current: true } };

it("uses the fresh revision for the same manuscript and evaluation", async () => {
  vi.mocked(apiRequest).mockResolvedValueOnce({ ...viewed, revision: 4 }).mockResolvedValueOnce({ approved: true });
  await approveCurrentDraft("p", viewed, "changed");
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[1][1]!.body as string)).toEqual({ revision: 4 });
});

it("analysis changes do not require approving unchanged prose again", async () => {
  vi.mocked(apiRequest).mockResolvedValueOnce({ ...viewed, quality_artifact_id: "new" });
  await approveCurrentDraft("p", viewed, "changed");
  expect(apiRequest).toHaveBeenCalledTimes(2);
});

it("recovers one revision race only after checking artifact identities again", async () => {
  vi.mocked(apiRequest).mockResolvedValueOnce(viewed).mockRejectedValueOnce(new ApiError("conflict", 409))
    .mockResolvedValueOnce({ ...viewed, revision: 5 }).mockResolvedValueOnce({ approved: true });
  await expect(approveCurrentDraft("p", viewed, "changed")).resolves.toEqual({ approved: true });
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[3][1]!.body as string).revision).toBe(5);
});

it("can approve saved prose while an unaccepted candidate is generating", async () => {
  vi.mocked(apiRequest).mockResolvedValueOnce({ ...viewed, active_feedback_job_id: "job" });
  await approveCurrentDraft("p", viewed, "changed");
  expect(apiRequest).toHaveBeenCalledTimes(2);
});

it("never silently approves changed prose", async () => {
  vi.mocked(apiRequest).mockResolvedValueOnce({ ...viewed, draft_artifact_id: "changed" });
  await expect(approveCurrentDraft("p", viewed, "changed")).rejects.toThrow("changed");
  expect(apiRequest).toHaveBeenCalledTimes(1);
});
