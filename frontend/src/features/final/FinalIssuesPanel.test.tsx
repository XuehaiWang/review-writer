import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { FinalIssuesPanel } from "./FinalIssuesPanel";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
let client: QueryClient;
beforeEach(() => {
  usePreferences.getState().setLanguage("en");
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  vi.mocked(apiRequest).mockImplementation(async (_path, options) => options?.method === "PUT" ? { saved: true } : {
    metadata_artifact_id: "m1", metadata: { title: { value: "Source paper" }, doi: "10/example", year: 2024,
      authors: { value: [{ given: "Ada", family: "Smith" }] }, journal: "Journal", page: "1–9" },
  });
});
afterEach(() => { cleanup(); client?.clear(); vi.resetAllMocks(); localStorage.clear(); });
function mount(onSync = vi.fn().mockResolvedValue({})) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><FinalIssuesPanel projectId="p" busy={false}
    issues={[{ target_type: "reference", target_id: "P001", reference_number: 2, title: "Source paper", issues: ["authors"] }]}
    onSync={onSync} /></MemoryRouter></QueryClientProvider>);
  return onSync;
}
async function fill() {
  fireEvent.click(screen.getByRole("button", { name: "Correct bibliography" }));
  expect(await screen.findByLabelText("Authors (one per line) (optional)")).toHaveValue("Ada Smith");
  expect(screen.getByLabelText("Pages (optional)")).toHaveValue("1–9");
  fireEvent.change(screen.getByLabelText("Authors (one per line) (optional)"), { target: { value: "Ada Jones\nBo Chen" } });
  expect(screen.getByLabelText("Source location (optional, e.g. PDF page 1)")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Save and update references" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Save and update references" }));
}
it("explains the finding, hides technical details and saves only bibliography", async () => {
  const sync = mount();
  expect(screen.getByText(/Author details are missing/)).toBeVisible();
  expect(screen.getByText("P001: authors").closest("details")).not.toHaveAttribute("open");
  await fill();
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
  const puts = vi.mocked(apiRequest).mock.calls.filter(([, options]) => options?.method === "PUT");
  expect(puts).toHaveLength(1);
  const body = JSON.parse(puts[0][1]!.body as string);
  expect(body.source_location).toBe("");
  expect(body).toMatchObject({ metadata_artifact_id: "m1", fields: { authors: ["Ada Jones", "Bo Chen"], pages: "1–9" } });
  expect(body.fields).not.toHaveProperty("title");
  expect(body.fields).not.toHaveProperty("doi");
  expect(body.fields).not.toHaveProperty("year");
});
it("retries assembly without repeating the already saved correction", async () => {
  const sync = mount(vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({}));
  await fill();
  fireEvent.click(await screen.findByRole("button", { name: "Retry Final sync" }));
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiRequest).mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
});
