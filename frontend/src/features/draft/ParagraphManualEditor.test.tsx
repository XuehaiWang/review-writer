import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { ParagraphManualEditor } from "./ParagraphManualEditor";
import { manuscriptSegments } from "./EditableManuscript";
import { manuscriptText } from "./InlineManuscriptText";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); });
const paragraph = { paragraph_id: "S01-p1", paragraph_key: "stable-key", text: "Original", text_sha256: "a".repeat(64) };
function mount(key = "user:p:stable-key:manual", displayText?: string, raw = paragraph.text, onEditingChange?: (editing: boolean) => void) {
  usePreferences.getState().setLanguage("en");
  const refresh = vi.fn().mockResolvedValue(undefined);
  return { refresh, ...render(<QueryClientProvider client={new QueryClient()}><ParagraphManualEditor
    projectId="p" revision={2} paragraph={{ ...paragraph, text: raw }} refresh={refresh} scratchKey={key} displayText={displayText} onEditingChange={onEditingChange} />
  </QueryClientProvider>) };
}
function type(value: string) {
  const input = screen.getByRole("textbox");
  input.textContent = value; fireEvent.input(input); return input;
}

it("confirms deleting an empty paragraph and keeps it when cancelled", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ revision: 3 });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const { refresh } = mount();
  type("");
  const button = screen.getByRole("button", { name: "Delete paragraph" });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(apiRequest).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  fireEvent.click(button);
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string).text).toBe("");
  confirm.mockRestore();
});

it("is directly editable and saves using the paragraph token without losing the input node", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ revision: 4 });
  const { refresh } = mount();
  expect(screen.queryByRole("button", { name: "Edit this paragraph" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Save paragraph" })).toBeNull();
  const input = type("Changed");
  expect(screen.getByRole("textbox")).toBe(input);
  expect(input).toHaveTextContent("Changed");
  fireEvent.click(screen.getByRole("button", { name: "Save paragraph" }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string)).toEqual({
    revision: 2, text: "Changed", base_text_sha256: paragraph.text_sha256,
  });
});

it("keeps input after a rejected save and supports undo", async () => {
  vi.mocked(apiRequest).mockRejectedValue(new Error("Paragraph changed"));
  mount(); type("Keep this input");
  fireEvent.click(screen.getByRole("button", { name: "Save paragraph" }));
  await screen.findByText("Paragraph changed");
  expect(screen.getByRole("textbox")).toHaveTextContent("Keep this input");
  fireEvent.click(screen.getByRole("button", { name: "Undo changes" }));
  expect(screen.getByRole("textbox")).toHaveTextContent("Original");
});

it("isolates scratch by user and restores pending input", () => {
  const first = mount(); type("Unsent local edit"); first.unmount();
  const other = mount("other:p:stable-key:manual");
  expect(screen.getByRole("textbox")).toHaveTextContent("Original"); other.unmount();
  mount(); expect(screen.getByRole("textbox")).toHaveTextContent("Unsent local edit");
});

it("clears the chapter pending indicator when an edited paragraph unmounts", () => {
  const onEditingChange = vi.fn();
  const mounted = mount(undefined, undefined, paragraph.text, onEditingChange);
  type("Changed");
  expect(onEditingChange).toHaveBeenLastCalledWith(true);
  mounted.unmount();
  expect(onEditingChange).toHaveBeenLastCalledWith(false);
});

it("preserves math, citation identity and canonical figure numbers from composed preview", () => {
  mount(undefined, "Result **strong** $\\alpha$ [12] (Figure 2).", "Result **strong** $\\alpha$ [12] (Figure 1).");
  const input = screen.getByRole("textbox");
  expect(input).toHaveTextContent("Figure 2");
  expect(manuscriptText(input).trim()).toBe("Result **strong** $\\alpha$ [12] (Figure 1).");
  fireEvent.input(input);
  expect(manuscriptText(input).trim()).toBe("Result **strong** $\\alpha$ [12] (Figure 1).");
});

it("uses stable markers for repeated text and leaves images and unknown paragraphs read-only", () => {
  const other = { ...paragraph, paragraph_id: "S02-p1", paragraph_key: "other" };
  const md = "# Title\n\nOriginal\n\n<!-- paragraph_id: S01-p1 -->\n\n![Figure](/api/v1/artifacts/id/content)\n\nOriginal\n\n<!-- paragraph_id: S02-p1 -->\n\nUnknown\n\n<!-- paragraph_id: unknown -->";
  const parts = manuscriptSegments(md, [paragraph, other]);
  expect(parts.filter(p => p.paragraph).map(p => p.paragraph!.paragraph_key)).toEqual(["stable-key", "other"]);
  expect(parts.filter(p => !p.paragraph).map(p => p.text).join("")).toContain("![Figure]");
  expect(parts.filter(p => p.paragraph).map(p => p.text)).toEqual(["Original", "Original"]);
});

it("retains grouped figure references and pastes plain text without HTML", () => {
  mount(undefined, "Compare Figures 2 and 3.", "Compare Figures 1 and 2.");
  const input = screen.getByRole("textbox");
  expect(manuscriptText(input).trim()).toBe("Compare Figures 1 and 2.");
  const selection = window.getSelection()!;
  const range = document.createRange(); range.selectNodeContents(input); range.collapse(false);
  selection.removeAllRanges(); selection.addRange(range);
  fireEvent.paste(input, { clipboardData: { getData: (format: string) => format === "text/plain" ? " Added text" : "<img src=x onerror=alert(1)>" } });
  expect(input.querySelector("img")).toBeNull();
  expect(manuscriptText(input)).toContain("Added text");
});
