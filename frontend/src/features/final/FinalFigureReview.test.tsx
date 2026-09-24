import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { FinalFigureReview } from "./FinalFigureReview";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
let client: QueryClient;
let record: Record<string, unknown>;
beforeEach(() => {
  usePreferences.getState().setLanguage("en");
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  record = { published_label: "Figure 4", caption: "Current caption", fingerprint: "version1", output_artifact_id: "out",
    source_artifact_id: "source", paper_id: "P001", paper_title: "Source paper", source_label: "Scheme 2",
    source_caption: "Original caption", context_md: "The reaction is shown in Figure 4.", blockers: [] };
  vi.mocked(apiRequest).mockImplementation(async (_path, options) => options?.method === "PUT" ? { saved: true } : record);
});
afterEach(() => { cleanup(); client.clear(); vi.resetAllMocks(); localStorage.clear(); });
function mount(sync = vi.fn().mockResolvedValue({})) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><FinalFigureReview projectId="p" figureId="F1" busy={false}
    close={vi.fn()} onSync={sync} /></MemoryRouter></QueryClientProvider>);
  return sync;
}
it("shows source and manuscript, then saves only the confirmed caption", async () => {
  const sync = mount();
  expect(await screen.findByAltText("Source figure")).toHaveAttribute("src", "/api/v1/artifacts/source/content");
  expect(screen.getByText("Original caption")).toBeVisible();
  expect(screen.getByText("The reaction is shown in Figure 4.")).toBeVisible();
  const button = screen.getByRole("button", { name: "Confirm and update Final" });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Final caption (required, without Figure number)"), { target: { value: "Correct caption" } });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(button);
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
  const put = vi.mocked(apiRequest).mock.calls.find(([, o]) => o?.method === "PUT");
  expect(JSON.parse(put![1]!.body as string)).toEqual({ caption: "Correct caption", fingerprint: "version1", confirmed: true, source_location: "" });
});
it("allows saving without a source location even when the original caption is unavailable", async () => {
  record.source_caption = "";
  const sync = mount();
  await screen.findByRole("checkbox");
  fireEvent.click(screen.getByRole("checkbox"));
  const button = screen.getByRole("button", { name: "Confirm and update Final" });
  expect(screen.getByLabelText(/Source page or figure number \(optional\)/)).toHaveValue("");
  expect(button).toBeEnabled();
  fireEvent.click(button);
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
});
it("does not permit confirming missing source records", async () => {
  record.blockers = ["来源论文不可用，请先到文献库核对。"];
  mount();
  expect(await screen.findByText("The source paper is unavailable. Check it in Library first.")).toBeVisible();
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Confirm and update Final" })).toBeDisabled();
  act(() => usePreferences.getState().setLanguage("zh-CN"));
  expect(screen.getByText("来源论文不可用，请先到文献库核对。")).toBeVisible();
});
it("retries sync without repeating a saved correction", async () => {
  const sync = mount(vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({}));
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Confirm and update Final" }));
  fireEvent.click(await screen.findByRole("button", { name: "Retry Final sync" }));
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiRequest).mock.calls.filter(([, o]) => o?.method === "PUT")).toHaveLength(1);
});
