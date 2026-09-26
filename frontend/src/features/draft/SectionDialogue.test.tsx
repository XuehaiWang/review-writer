import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { SectionDialogue } from "./SectionDialogue";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });
const section = { section_id: "S1", title: "Scientific comparison", paragraphs: [
  { paragraph_id: "S1-p1", paragraph_key: "k1", text: "First original", text_sha256: "a".repeat(64) },
  { paragraph_id: "S1-p2", paragraph_key: "k2", text: "Second original", text_sha256: "b".repeat(64) },
] };
const decide = vi.fn();
it("receives SSE snapshots and replaces restored text without duplicating it", async () => {
  let stream!: EventTarget & { onopen?: () => void; close: ReturnType<typeof vi.fn> };
  const opened = vi.fn();
  class FakeStream extends EventTarget {
    close = vi.fn();
    onopen?: () => void;
    constructor(url: string) { super(); opened(url); stream = this; }
  }
  vi.stubGlobal("EventSource", FakeStream);
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "live", message: "Explain", status: "running", progress_current: 0, progress_total: 1 }] });
  const view = mount();
  await waitFor(() => expect(opened).toHaveBeenCalledWith(expect.stringContaining("/stream/live")));
  act(() => { stream.onopen?.(); stream.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify({ id: "live", status: "running", phase: "answer", streaming_reply: "First" }) })); });
  expect(await screen.findByText("First")).toBeVisible();
  act(() => stream.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify({ id: "live", status: "running", phase: "answer", streaming_reply: "First second" }) })));
  expect(await screen.findByText("First second")).toBeVisible();
  expect(screen.queryByText("FirstFirst second")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  view.unmount();
  expect(stream.close).toHaveBeenCalled();
});
function mount(candidates: Parameters<typeof SectionDialogue>[0]["candidates"] = []) {
  usePreferences.getState().setLanguage("en");
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <SectionDialogue section={section} projectId="project" userId="user" revision={1} blocked={false}
      candidates={candidates} decisionPending={false} decide={decide} refresh={vi.fn().mockResolvedValue(undefined)} />
  </QueryClientProvider>);
}
it("uses the open chapter without scope or advanced controls", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [] });
  mount();
  expect(screen.queryByText("First original")).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "More options" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Reduce repetition" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith(expect.stringContaining("section-dialogues/S1"), expect.objectContaining({ method: "POST" })));
  const post = vi.mocked(apiRequest).mock.calls.find(c => c[1]?.method === "POST")!;
  const body = JSON.parse(post[1]!.body as string);
  expect(body.action).toBe("discuss");
  expect(body).not.toHaveProperty("paragraph_keys");
  expect(body).not.toHaveProperty("use_saved");
});
it("shows streamed reply before the final candidate without a save button", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "live", message: "Explain", status: "running", streaming_reply: "Evidence suggests", progress_current: 0, progress_total: 1 }] });
  mount();
  expect(await screen.findByText("Evidence suggests")).toBeVisible();
  expect(screen.getByText("Generating; not validated or saved")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
});
it("retains the composer if submission fails", async () => {
  vi.mocked(apiRequest).mockImplementation((_url, options) => options?.method === "POST"
    ? Promise.reject(new Error("Connection failed")) : Promise.resolve({ turns: [] }));
  mount();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep this question" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Connection failed");
  expect(screen.getByRole("textbox")).toHaveValue("Keep this question");
});
it("does not erase a new message typed while the previous send is pending", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(apiRequest).mockImplementation((_url, options) => options?.method === "POST"
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ turns: [] }));
  mount();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "First" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Next message" } });
  finish({});
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).not.toBeDisabled());
  expect(screen.getByRole("textbox")).toHaveValue("Next message");
});
it("generates the entire chapter with an empty composer and no paragraph selection", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [] });
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Generate revision from discussion" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(1));
  const post = vi.mocked(apiRequest).mock.calls.find(c => c[1]?.method === "POST")!;
  expect(JSON.parse(post[1]!.body as string)).toMatchObject({ action: "revise" });
  expect(decide).not.toHaveBeenCalled();
});
it("previews changed and unchanged paragraphs as one complete chapter", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Revise chapter", action: "revise", status: "succeeded", progress_current: 2, progress_total: 2 }] });
  mount([
    { candidate_id: "c1", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job", original_text: "First original", candidate_text: "Revised first", reply: "Changed", status: "pending" },
    { candidate_id: "c2", paragraph_key: "k2", paragraph_id: "S1-p2", batch_job_id: "job", original_text: "Second original", candidate_text: "", reply: "Retained", status: "kept_original" },
  ]);
  const preview = (await screen.findByText("Complete chapter candidate preview")).closest("details")!;
  expect(preview).toHaveTextContent("Revised first");
  expect(preview).toHaveTextContent("Second original");
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  expect(decide).toHaveBeenCalledWith(["c1"], "accept");
});
it("restores persisted chapter turns and requires explicit candidate acceptance", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Clarify chapter", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "First original", candidate_text: "Clear candidate", reply: "Clarified", status: "pending" }]);
  await screen.findByText("Clarify chapter");
  expect(screen.getAllByText("Clear candidate").some(node => node.closest("details") === null)).toBe(true);
  expect(screen.getByText("These are proposals, not saved text. Click Save changes to update the draft.")).toBeVisible();
  expect(screen.getByText("View comparison").closest("details")).not.toHaveAttribute("open");
  expect(screen.getByText("Clarified", { selector: ".chat-reply-text p" })).toBeVisible();
  expect(decide).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  expect(decide).toHaveBeenCalledWith(["c"], "accept");
});
it("keeps continuous messages and offers discard without opening comparison controls", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [
    { id: "old", message: "Earlier question", status: "succeeded", progress_current: 1, progress_total: 1 },
    { id: "new", message: "Follow up", status: "succeeded", progress_current: 1, progress_total: 1 },
  ] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "new",
    original_text: "First original", candidate_text: "Proposal", reply: "My explanation", status: "pending" }]);
  expect(await screen.findByText("Earlier question")).toBeVisible();
  expect(screen.getByText("Follow up")).toBeVisible();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "Discard" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(decide).toHaveBeenCalledWith(["c"], "reject");
  expect(screen.getAllByText("Proposal").some(node => node.closest("details") === null)).toBe(true);
});
it("shows server-confirmed saved state without offering to save again", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Improve", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "First original", candidate_text: "Saved version", reply: "Proposal explanation", status: "accepted" }]);
  await screen.findByText("Improve");
  expect(screen.getAllByText("Saved").some(node => node.closest("details") === null)).toBe(true);
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Discard" })).not.toBeInTheDocument();
  expect(decide).not.toHaveBeenCalled();
});
it("keeps unsaved manual text and prevents submitting against it", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [] });
  mount();
  localStorage.setItem("rw-draft-scratch:user:project:k1:manual", JSON.stringify({ text: "Local unsaved edit" }));
  fireEvent.change(screen.getByLabelText("Tell the AI how to improve this chapter"), { target: { value: "Improve" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Save or cancel this chapter's manual edits first.");
  expect(vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Chapter versions" }));
  expect(screen.queryByRole("button", { name: "Edit this paragraph" })).toBeNull();
});

it("allows saving author-reviewed citation changes with a visible warning", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Merge sentences", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "First [4]. Second [4].", candidate_text: "Both [4].", reply: "Merged", status: "pending",
    validation_warnings: ["protected_callouts_changed"], evidence_review: "author_review_required",
    scientific_changes: [{ field: "callouts", before: ["[4]", "[4]"], after: ["[4]"] }] }]);
  await screen.findByText("Merge sentences");
  expect(screen.getAllByText(/Citations \(check source-to-claim attribution\)/).length).toBeGreaterThan(0);
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  expect(decide).toHaveBeenCalledWith(["c"], "accept");
});

it("retains a read-only preview of structurally blocked proposals", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Improve", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "Original", candidate_text: "", rejected_candidate_text: "Blocked proposal", reply: "Proposal", status: "validation_failed",
    validation_errors: ["multiple_prose_blocks"] }]);
  expect(await screen.findByText("Inspect blocked text (read-only)")).toBeVisible();
  expect(screen.getByText("Blocked proposal")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
});

it("explains missing proposals without inventing a save button", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Save it", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "First original", candidate_text: "", reply: "fallback", status: "kept_original", save_guidance: "missing" }]);
  expect(await screen.findByText("No pending proposal exists. Describe the edit you want, and I will generate a proposal for you to save.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  expect(decide).not.toHaveBeenCalled();
});


it("keeps thinking and streamed text until the completed reply loads; reload only reads", async () => {
  let stream!: EventTarget & { close: ReturnType<typeof vi.fn> };
  class FakeStream extends EventTarget {
    close = vi.fn();
    constructor() { super(); stream = this; }
  }
  vi.stubGlobal("EventSource", FakeStream);
  let status = "running";
  let rejectRead!: (reason: Error) => void;
  let resolveRead!: (value: unknown) => void;
  let reads = 0;
  vi.mocked(apiRequest).mockImplementation(url => {
    if (url.endsWith("/draft")) {
      reads++;
      return new Promise((resolve, reject) => { resolveRead = resolve; rejectRead = reject; });
    }
    return Promise.resolve({ turns: [{ id: "live", message: "Explain", status, progress_current: 1, progress_total: 1 }] });
  });
  mount();
  await waitFor(() => expect(stream).toBeDefined());
  act(() => stream.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify({ id: "live", status: "running", streaming_reply: "Partial answer" }) })));
  expect(await screen.findByText("Partial answer")).toBeVisible();
  status = "succeeded";
  act(() => stream.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify({ id: "live", status }) })));
  await waitFor(() => expect(reads).toBe(1));
  expect(screen.getByText("Thinking…")).toBeVisible();
  expect(screen.getByText("Partial answer")).toBeVisible();
  expect(screen.queryByText(/No reply was generated|This turn ended without/)).not.toBeInTheDocument();
  await act(async () => rejectRead(new Error("Network error")));
  expect(await screen.findByText("Could not load the reply. Please retry.")).toBeVisible();
  expect(screen.getByText("Partial answer")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Reload reply" }));
  await waitFor(() => expect(reads).toBe(2));
  await act(async () => resolveRead({ rewrite_candidates: [{ candidate_id: "c", batch_job_id: "live", paragraph_key: "k1",
    paragraph_id: "S1-p1", original_text: "First original", candidate_text: "", reply: "Final discussion answer", status: "kept_original" }] }));
  expect(await screen.findByText("Final discussion answer", { selector: ".chat-reply-text p" })).toBeVisible();
  expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  expect(screen.queryByText("Partial answer")).not.toBeInTheDocument();
  expect(screen.queryByText(/No reply was generated|This turn ended without/)).not.toBeInTheDocument();
  expect(vi.mocked(apiRequest).mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
});

it("confirms an empty published result before showing no reply, sharing the read across history", async () => {
  let resolveRead!: (value: unknown) => void;
  let reads = 0;
  vi.mocked(apiRequest).mockImplementation(url => {
    if (url.endsWith("/draft")) {
      reads++;
      return new Promise(resolve => { resolveRead = resolve; });
    }
    return Promise.resolve({ turns: ["one", "two"].map(id => ({ id, message: id, status: "succeeded", progress_current: 1, progress_total: 1 })) });
  });
  mount();
  await waitFor(() => expect(reads).toBe(1));
  expect(screen.getAllByText("Thinking…")).toHaveLength(2);
  expect(screen.queryByText(/No reply was generated/)).not.toBeInTheDocument();
  await act(async () => resolveRead({ rewrite_candidates: [] }));
  await waitFor(() => expect(screen.getAllByText(/No reply was generated/)).toHaveLength(2));
  expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  expect(reads).toBe(1);
});


it("opens versions outside the chat and restarts without saving or generating until Send", async () => {
  const initial = { ...section, artifact_id: "initial-id", created_at: "2026-09-16", paragraphs: section.paragraphs.map(p => ({ ...p, text: "Original baseline" })) };
  vi.mocked(apiRequest).mockImplementation(url => Promise.resolve(url.endsWith("/versions")
    ? { current: { ...section, artifact_id: "current-id" }, initial, versions: [] }
    : { turns: [{ id: "old", status: "succeeded", message: "Abandoned instructions", progress_current: 1, progress_total: 1 }], rewrite_candidates: [] }));
  mount();
  await screen.findByText("Abandoned instructions");
  fireEvent.click(screen.getByRole("button", { name: "Chapter versions" }));
  const modal = screen.getByRole("dialog", { name: "Chapter text and versions" });
  expect(modal.closest(".chapter-chat")).toBeNull();
  fireEvent.change(screen.getByLabelText("View version"), { target: { value: "initial" } });
  const restart = await screen.findByRole("button", { name: "Revise from this version" });
  fireEvent.click(restart);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("Abandoned instructions")).not.toBeInTheDocument();
  expect(vi.mocked(apiRequest).mock.calls.every(([, options]) => !options?.method)).toBe(true);
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Start over" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([, options]) => options?.method === "POST")).toBe(true));
  const call = vi.mocked(apiRequest).mock.calls.find(([, options]) => options?.method === "POST")!;
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({ initial_artifact_id: "initial-id", branch_id: expect.any(String) });
  expect(JSON.parse(call[1]!.body as string).branch_id).not.toBe("");
});
