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
it("defaults to chapter scope and can target one paragraph without separate chat requests", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [] });
  mount();
  expect(screen.getByText("First original")).not.toBeVisible();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "  Reduce repetition\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith(expect.stringContaining("section-dialogues/S1"), expect.objectContaining({ method: "POST" })));
  let posts = vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST");
  expect(JSON.parse(posts[0][1]!.body as string).paragraph_keys).toEqual([]);
  expect(JSON.parse(posts[0][1]!.body as string).action).toEqual("discuss");
  expect(JSON.parse(posts[0][1]!.body as string).message).toEqual("Reduce repetition");
  await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue(""));
  fireEvent.click(screen.getByRole("button", { name: "More options" }));
  fireEvent.change(screen.getByLabelText("Revision scope"), { target: { value: "k2" } });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Clarify the second paragraph" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(2));
  posts = vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST");
  expect(JSON.parse(posts[1][1]!.body as string).paragraph_keys).toEqual(["k2"]);
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
  expect(JSON.parse(post[1]!.body as string)).toMatchObject({ action: "revise", paragraph_keys: [] });
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
  fireEvent.click(screen.getByRole("button", { name: "Text and manual editing" }));
  fireEvent.click(screen.getAllByRole("button", { name: "Edit this paragraph" })[0]);
  fireEvent.change(screen.getByLabelText("Paragraph text"), { target: { value: "Local unsaved edit" } });
  fireEvent.change(screen.getByLabelText("Tell the AI how to improve this chapter"), { target: { value: "Improve" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("Save or cancel this chapter's manual edits first.");
  expect(vi.mocked(apiRequest).mock.calls.filter(c => c[1]?.method === "POST")).toHaveLength(0);
  expect(screen.getByLabelText("Paragraph text")).toHaveValue("Local unsaved edit");
});

it("explains missing proposals without inventing a save button", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ turns: [{ id: "job", message: "Save it", status: "succeeded", progress_current: 1, progress_total: 1 }] });
  mount([{ candidate_id: "c", paragraph_key: "k1", paragraph_id: "S1-p1", batch_job_id: "job",
    original_text: "First original", candidate_text: "", reply: "fallback", status: "kept_original", save_guidance: "missing" }]);
  expect(await screen.findByText("No pending proposal exists. Describe the edit you want, and I will generate a proposal for you to save.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  expect(decide).not.toHaveBeenCalled();
});
