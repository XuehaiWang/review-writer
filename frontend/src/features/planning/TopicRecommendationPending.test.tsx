import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { TopicRecommendationPending, type TopicRecommendationState } from "./TopicRecommendationPending";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
function mount(state: TopicRecommendationState) {
  usePreferences.getState().setLanguage("en");
  const refresh = vi.fn().mockResolvedValue(undefined);
  render(<QueryClientProvider client={new QueryClient()}><TopicRecommendationPending projectId="p" state={state} refresh={refresh} /></QueryClientProvider>);
  return refresh;
}
it("starts the missing recommendation once without saving an outline", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ id: "job" });
  const refresh = mount({ status: "not_started", input_fingerprint: "f" });
  await waitFor(() => expect(refresh).toHaveBeenCalled());
  expect(apiRequest).toHaveBeenCalledTimes(1);
  expect(apiRequest).toHaveBeenCalledWith(expect.stringContaining("/outline/topic/jobs?retry=false"), { method: "POST" });
});
it("keeps failures and previous recommendations visible without automatic paid retries", async () => {
  mount({ status: "failed", input_fingerprint: "f", job: { error_message: "Provider unavailable" }, previous_outline_md: "Previous questions" });
  expect(screen.getByText("Provider unavailable")).toBeVisible();
  expect(screen.getByText("Previous questions")).toBeInTheDocument();
  expect(apiRequest).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Generate / retry recommendation" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith(expect.stringContaining("retry=true"), { method: "POST" }));
});
it("restores a running task without submitting another", () => {
  mount({ status: "running", input_fingerprint: "f" });
  expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();
  expect(apiRequest).not.toHaveBeenCalled();
});

it("shows persisted model retries without resubmitting", () => {
  mount({ status: "running", input_fingerprint: "f", model_progress: { attempt: 2, status: "running", model: "provider-model" } });
  expect(screen.getByText(/Model request attempt 2/)).toBeVisible();
  expect(apiRequest).not.toHaveBeenCalled();
});

it("keeps polling an accepted job when the first refresh fails", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ id: "job", status: "queued" });
  const refresh = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  render(<QueryClientProvider client={new QueryClient()}><TopicRecommendationPending projectId="p" state={{ status: "not_started", input_fingerprint: "f" }} refresh={refresh} /></QueryClientProvider>);
  await waitFor(() => expect(screen.getByText(/Status refresh failed/)).toBeVisible());
  expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(2), { timeout: 4000 });
  expect(apiRequest).toHaveBeenCalledTimes(1);
});
