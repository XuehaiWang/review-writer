import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { StorageMaintenance } from "./StorageMaintenance";
vi.mock("../../api/client", () => ({ apiRequest: vi.fn() }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (_zh: string, en: string) => en }) }));
let client: QueryClient;
const report = { available: true, disk: { total_bytes: 100e9, used_bytes: 98e9, free_bytes: 2e9, low_space: true },
  usage: { total_bytes: 5e6, categories: { temporary: 5e6 }, partial: false, scanned_at: "2026-09-17T01:00:00Z" },
  last_cleanup: { at: "2026-09-17T01:00:00Z", removed_directories: 2, freed_bytes: 3e6, errors: 0, partial: false } };
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.mocked(apiRequest).mockResolvedValue(report);
});
afterEach(() => { cleanup(); client.clear(); vi.resetAllMocks(); });
function mount(active = true) {
  return render(<QueryClientProvider client={client}><StorageMaintenance active={active} /></QueryClientProvider>);
}
it("loads only when opened and never cleans during reads", async () => {
  const view = mount(false);
  expect(apiRequest).not.toHaveBeenCalled();
  view.rerender(<QueryClientProvider client={client}><StorageMaintenance active /></QueryClientProvider>);
  await screen.findByRole("alert");
  expect(vi.mocked(apiRequest).mock.calls.every(([, options]) => !options?.method)).toBe(true);
});
it("shows disk warning and submits explicit safe cleanup", async () => {
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("Low disk space");
  fireEvent.click(screen.getByRole("button", { name: "Clean safe cache" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/admin/storage/cleanup", { method: "POST" }));
  expect(await screen.findByText("Safe maintenance completed.")).toBeVisible();
});
it("marks partial statistics instead of presenting them as complete", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ ...report, usage: { ...report.usage, partial: true } });
  mount();
  expect(await screen.findByText(/This scan is partial/)).toBeVisible();
});
it("shows a maintenance lock rather than claiming a cleanup occurred", async () => {
  vi.mocked(apiRequest).mockImplementation(async (_path, opts) => opts?.method ? { busy: true } : report);
  mount();
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Clean safe cache" }));
  expect(await screen.findByText(/Maintenance is already running/)).toBeVisible();
});
it("separates filesystem capacity, workspace usage and cleanup policy", async () => {
  mount();
  const meter = await screen.findByRole("meter", { name: "Filesystem utilization" });
  expect(meter).toHaveAttribute("aria-valuenow", "98");
  expect(screen.getByRole("region", { name: "Workspace breakdown" })).toHaveTextContent("Staging files (not all removable)");
  expect(screen.getByRole("region", { name: "Cleanup history and policy" })).toHaveTextContent("Staging usage is not the amount that can be freed.");
});
it("keeps cleanup disabled when storage maintenance is unavailable", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ available: false });
  mount();
  expect(await screen.findByText("Storage maintenance is unavailable in this mode.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Clean safe cache" })).toBeDisabled();
  expect(screen.queryByRole("meter")).not.toBeInTheDocument();
});
it("handles zero capacity and missing first scan without invalid percentages", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ available: true, disk: { total_bytes: 0, used_bytes: 0, free_bytes: 0, low_space: false } });
  mount();
  expect(await screen.findByRole("meter")).toHaveAttribute("aria-valuenow", "0");
  expect(screen.getByText(/Waiting for the first maintenance scan/)).toBeVisible();
  expect(screen.getByText("No cleanup history yet")).toBeVisible();
});
