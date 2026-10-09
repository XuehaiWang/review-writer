import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, apiRequest } from "../../api/client";
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
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 15000 }, mutations: { retry: false } } });
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

it("reopens with a fresh version even inside the cache freshness window", async () => {
  let version = "m1";
  let authors = ["Ada Smith"];
  vi.mocked(apiRequest).mockImplementation(async (_path, options) => {
    if (options?.method === "PUT") {
      const body = JSON.parse(options.body as string);
      expect(body.metadata_artifact_id).toBe(version);
      authors = body.fields.authors;
      version = "m2";
      return { saved: true };
    }
    return { metadata_artifact_id: version, metadata: { title: "Source paper", authors, pages: "1–9" } };
  });
  const sync = mount();
  await fill();
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Correct bibliography" }));
  await waitFor(() => expect(screen.getByLabelText("Authors (one per line) (optional)")).toHaveValue("Ada Jones\nBo Chen"));
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Save and update references" }));
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiRequest).mock.calls.filter(([, options]) => !options?.method)).toHaveLength(2);
});

it("retains edits on conflict and failed reload, merging only untouched fields from the latest version", async () => {
  let puts = 0;
  let gets = 0;
  vi.mocked(apiRequest).mockImplementation(async (_path, options) => {
    if (options?.method === "PUT") {
      puts++;
      if (puts === 1) throw new ApiError("Metadata changed", 409, "STATE_CONFLICT");
      expect(JSON.parse(options.body as string)).toMatchObject({ metadata_artifact_id: "m2", fields: {
        authors: ["Ada Jones", "Bo Chen"], journal: "New journal", pages: "1–9" } });
      return { saved: true };
    }
    gets++;
    if (gets === 2) throw new Error("offline");
    return { metadata_artifact_id: gets === 1 ? "m1" : "m2", metadata: {
      authors: gets === 1 ? ["Ada Smith"] : ["Server author"], journal: gets === 1 ? "Old journal" : "New journal", pages: "1–9" } };
  });
  const sync = mount();
  await fill();
  fireEvent.click(await screen.findByRole("button", { name: "Load latest and keep edits" }));
  expect(await screen.findByText(/Could not load the latest information/)).toBeVisible();
  expect(screen.getByLabelText("Authors (one per line) (optional)")).toHaveValue("Ada Jones\nBo Chen");
  fireEvent.click(screen.getByRole("button", { name: "Load latest and keep edits" }));
  expect(await screen.findByText(/Latest saved value: Server author/)).toBeVisible();
  expect(screen.getByLabelText("Journal (optional)")).toHaveValue("New journal");
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(sync).not.toHaveBeenCalled();
  expect(puts).toBe(1);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Save and update references" }));
  await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
});

it("background cache updates do not overwrite typed edits or silently advance their base version", async () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Correct bibliography" }));
  await screen.findByDisplayValue("Ada Smith");
  fireEvent.change(screen.getByLabelText("Authors (one per line) (optional)"), { target: { value: "Local edit" } });
  client.setQueryData(["final-reference", "p", "P001"], { metadata_artifact_id: "m2", metadata: { authors: ["Server edit"] } });
  expect(screen.getByLabelText("Authors (one per line) (optional)")).toHaveValue("Local edit");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "Save and update references" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([, options]) => options?.method === "PUT")).toBe(true));
  const put = vi.mocked(apiRequest).mock.calls.find(([, options]) => options?.method === "PUT")!;
  expect(JSON.parse(put[1]!.body as string).metadata_artifact_id).toBe("m1");
});
