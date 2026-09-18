import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { FinalPage } from "./FinalPage";
import { StrictMode } from "react";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
vi.mock("../../components/ProjectSelector", () => ({ ProjectSelector: () => null,
  useSelectedProject: () => ({ selected: { project_id: "p" } }) }));
let payload: Record<string, unknown>;
let failSubmission = false;
const clients: QueryClient[] = [];
beforeEach(() => {
  usePreferences.getState().setLanguage("en");
  failSubmission = false;
  payload = { revision: 3, draft_approval_current: true, draft_approval: {}, front_matter: {},
    final_artifact_id: "", final_current: false, final_draft_md: "", freshness: {}, release: {},
    evidence_boundary: {}, latest_final_job_id: "", active_final_job_id: "" };
  vi.mocked(apiRequest).mockImplementation(async (path, options) => {
    if (options?.method === "POST") {
      if (failSubmission) throw new Error("Submission failed");
      return { id: "j", job_type: "final.build", status: "running", error_message: "", available_actions: [] };
    }
    if (path.endsWith("/final")) return payload;
    if (path.includes("/jobs/")) return { id: "j", job_type: "final.build", status: "running", error_message: "", available_actions: [] };
    throw new Error(path);
  });
});
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.resetAllMocks(); localStorage.clear(); });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(<StrictMode><QueryClientProvider client={client}><MemoryRouter><FinalPage /></MemoryRouter></QueryClientProvider></StrictMode>);
}
const posts = () => vi.mocked(apiRequest).mock.calls.filter(([, options]) => options?.method === "POST");
it("assembles the first approved draft once with an idempotent request", async () => {
  mount();
  await waitFor(() => expect(posts()).toHaveLength(1));
  expect(posts()[0][0]).toContain("/build-jobs");
  expect(posts()[0][1]?.headers).toMatchObject({ "Idempotency-Key": "final-initial:p:3" });
  expect(screen.queryByRole("button", { name: "Generate final draft" })).toBeNull();
});
it.each([
  { draft_approval_current: false },
  { active_final_job_id: "j" },
  { latest_final_job_type: "final.build", latest_final_job_status: "failed" },
  { final_artifact_id: "f", final_current: true },
])("does not auto-submit when blocked or already assembled: %j", async state => {
  Object.assign(payload, state);
  mount();
  await screen.findByText("Final outputs");
  expect(posts()).toHaveLength(0);
});
it("requires explicit sync for stale finals and blocks export", async () => {
  Object.assign(payload, { final_artifact_id: "f" });
  mount();
  const sync = await screen.findByRole("button", { name: "Sync latest Draft" });
  expect(posts()).toHaveLength(0);
  expect(screen.getByRole("button", { name: "Download Word" })).toBeDisabled();
  fireEvent.click(sync);
  await waitFor(() => expect(posts()).toHaveLength(1));
});
it("reuses current downloads and only regenerates PDF when the language changes", async () => {
  Object.assign(payload, { final_artifact_id: "f", final_current: true, release_current: true,
    final_draft_docx_exists: true, docx_url: "/word", final_pdf_exists: true, pdf_url: "/pdf", pdf_language_profile: "en" });
  mount();
  expect(await screen.findByRole("link", { name: "Download Word" })).toHaveAttribute("href", "/word");
  expect(screen.getByRole("link", { name: "Download PDF" })).toHaveAttribute("href", "/pdf");
  expect(screen.queryByRole("button", { name: "Cancel current task" })).toBeNull();
  fireEvent.change(screen.getByLabelText("PDF language"), { target: { value: "zh-CN" } });
  expect(screen.getByRole("button", { name: "Download PDF" })).toBeEnabled();
  expect(posts()).toHaveLength(0);
});
it("does not loop after an automatic submission failure and permits manual retry", async () => {
  failSubmission = true;
  mount();
  await screen.findByText("Submission failed");
  expect(posts()).toHaveLength(1);
  failSubmission = false;
  fireEvent.click(screen.getByRole("button", { name: "Retry final assembly" }));
  await waitFor(() => expect(posts()).toHaveLength(2));
});
it("does not offer stale files as current downloads", async () => {
  Object.assign(payload, { final_artifact_id: "f", final_current: true, release_current: true,
    final_draft_docx_exists: true, final_draft_docx_stale: true, docx_url: "/old-word",
    final_pdf_exists: true, final_pdf_stale: true, pdf_url: "/old-pdf", pdf_language_profile: "en" });
  mount();
  expect(await screen.findByRole("button", { name: "Download Word" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Download PDF" })).toBeEnabled();
  expect(screen.queryByRole("link", { name: "Download Word" })).toBeNull();
  expect(screen.queryByRole("link", { name: "Download PDF" })).toBeNull();
});
