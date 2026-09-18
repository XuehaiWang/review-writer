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
