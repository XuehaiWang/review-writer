import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { DraftCompositionPanel, DraftSynthesisStatus } from "./DraftCompositionPanel";

it("shows persisted synthesis state and removes the progress bar when finished", () => {
  usePreferences.getState().setLanguage("en");
  const { rerender } = render(<DraftSynthesisStatus job={{ id: "j", status: "queued" }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Queued");
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("value");
  rerender(<DraftSynthesisStatus job={{ id: "j", status: "running" }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Generating");
  rerender(<DraftSynthesisStatus job={{ id: "j", status: "failed" }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Existing text is retained");
  expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  rerender(<DraftSynthesisStatus job={{ id: "j", status: "succeeded" }} />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("prevents duplicate generation while the server synthesis job is active", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 0, history: [] });
  render(<QueryClientProvider client={new QueryClient()}><DraftCompositionPanel projectId="p" markdown="# Title"
    refresh={vi.fn()} job={{ id: "j", status: "running" }} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Abstract" }));
  expect(screen.getByRole("button", { name: "Generate new version" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Conclusion" }));
  expect(screen.getByRole("button", { name: "Generate new version" })).toBeDisabled();
  expect(vi.mocked(apiRequest).mock.calls.every(([, opts]) => !opts?.method)).toBe(true);
});

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
it("submits optional reaction references without adopting the image", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 0, history: [] });
  render(<QueryClientProvider client={new QueryClient()}><DraftCompositionPanel projectId="p" markdown="# Title" refresh={vi.fn()} /></QueryClientProvider>);
  await waitFor(() => expect(apiRequest).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: "Overview" }));
  fireEvent.click(screen.getByText("Structure references (optional)"));
  fireEvent.click(screen.getByRole("button", { name: "Add structure reference" }));
  fireEvent.change(screen.getByLabelText("Type"), { target: { value: "reaction" } });
  fireEvent.change(screen.getByLabelText("Reaction SMILES (reactants>>products)"), { target: { value: "CCO>>CC=O" } });
  fireEvent.click(screen.getByRole("button", { name: "Generate new version" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/projects/p/draft/overview-jobs", expect.objectContaining({
    body: JSON.stringify({ instructions: "", structure_references: [{ kind: "reaction", label: "", name: "", smiles: "CCO>>CC=O", role: "reference", conditions: "" }] }),
  })));
  expect(vi.mocked(apiRequest).mock.calls.some(([url]) => url.endsWith("/overview/adopt"))).toBe(false);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});

it("refreshes manuscript composition when the selected overview becomes available", async () => {
  usePreferences.getState().setLanguage("en");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 2, overview_figure_exists: true,
    overview_figure_url: "/api/v1/artifacts/img/content", overview_text: { title: "Caption" } });
  render(<QueryClientProvider client={client}><DraftCompositionPanel projectId="p" markdown="# Title"
    refresh={vi.fn()} /></QueryClientProvider>);
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["draft", "p"] }));
});

it("opens a local workspace without starting generation or navigating to batch", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 0, overview_figure_exists: false });
  const refresh = vi.fn().mockResolvedValue(undefined);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <DraftCompositionPanel projectId="p" markdown="# Title\n\nBody" refresh={refresh}
      job={{ id: "j", status: "succeeded", result: { candidate_ids: ["c"] } }} />
  </QueryClientProvider>);
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/projects/p/draft/overview"));
  expect(screen.getAllByRole("button").map(b => b.textContent)).toEqual(["Abstract", "Conclusion", "Overview"]);
  expect(vi.mocked(apiRequest).mock.calls.every(([, opts]) => !opts?.method)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Abstract" }));
  expect(vi.mocked(apiRequest).mock.calls.every(([, opts]) => !opts?.method)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Generate new version" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/projects/p/draft/synthesis/abstract", expect.objectContaining({ method: "POST" })));
  expect(screen.queryByRole("link", { name: /view candidate/ })).not.toBeInTheDocument();
});

it("edits and accepts synthesis inside its window", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 2, history: [] });
  render(<QueryClientProvider client={new QueryClient()}><DraftCompositionPanel projectId="p" markdown="## Abstract\nOriginal"
    refresh={vi.fn()} candidates={[{ candidate_id: "c", paragraph_key: "synthesis:abstract", original_text: "Original", candidate_text: "New", status: "pending", base_text_sha256: "hash" }]} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Abstract" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Author edited" } });
  fireEvent.click(screen.getByRole("button", { name: "Save to manuscript" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/projects/p/draft/synthesis-candidates/c/accept", expect.objectContaining({ body: JSON.stringify({ text: "Author edited", base_text_sha256: "hash" }) })));
});

it("retains overview history and adopts only on explicit save", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 3, history: [
    { id: "img", url: "/image", title: "Caption", instructions: "Minimal labels", selected: false, source_changed: true, created_at: "",
      structure_references: [{ kind: "molecule", name: "Ethanol", smiles: "CCO", role: "substrate", label: "Example", conditions: "" }] }] });
  render(<QueryClientProvider client={new QueryClient()}><DraftCompositionPanel projectId="p" markdown="# Title" refresh={vi.fn()} /></QueryClientProvider>);
  await waitFor(() => expect(apiRequest).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: "Overview" }));
  await screen.findByAltText("Overview preview");
  fireEvent.click(screen.getByText(/Structure references \(optional\)/));
  expect(screen.getByLabelText("SMILES")).toHaveValue("CCO");
  expect(screen.getByLabelText("Structure role")).toHaveValue("substrate");
  expect(vi.mocked(apiRequest).mock.calls.every(([, opts]) => !opts?.method)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Save to manuscript" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/projects/p/draft/overview/adopt", expect.objectContaining({ body: JSON.stringify({ image_id: "img", title: "Caption", revision: 3 }) })));
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(vi.mocked(apiRequest).mock.calls.some(([url]) => url.endsWith("/cancel"))).toBe(false);
});
