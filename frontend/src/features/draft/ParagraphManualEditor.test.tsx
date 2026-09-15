import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { ParagraphManualEditor } from "./ParagraphManualEditor";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); });
const paragraph = { paragraph_id: "S01-p1", text: "Original", text_sha256: "a".repeat(64) };

it("saves with the viewed paragraph token and refreshes the draft", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({ revision: 4 });
  const refresh = vi.fn().mockResolvedValue(undefined);
  render(<QueryClientProvider client={new QueryClient()}><ParagraphManualEditor
    projectId="p" revision={2} paragraph={paragraph} refresh={refresh} />
  </QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Edit this paragraph" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Changed" } });
  fireEvent.click(screen.getByRole("button", { name: "Save paragraph" }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string)).toEqual({
    revision: 2, text: "Changed", base_text_sha256: paragraph.text_sha256,
  });
});

it("keeps user input after a rejected save", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockRejectedValue(new Error("Paragraph changed"));
  render(<QueryClientProvider client={new QueryClient()}><ParagraphManualEditor
    projectId="p" revision={2} paragraph={paragraph} refresh={vi.fn()} />
  </QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Edit this paragraph" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Keep this input" } });
  fireEvent.click(screen.getByRole("button", { name: "Save paragraph" }));
  await screen.findByText("Paragraph changed");
  expect(screen.getByRole("textbox")).toHaveValue("Keep this input");
});

it("restores only the matching user-project-paragraph scratch after remount", () => {
  usePreferences.getState().setLanguage("en");
  const mount = (key: string) => render(<QueryClientProvider client={new QueryClient()}><ParagraphManualEditor projectId="p" revision={2} paragraph={paragraph} scratchKey={key} refresh={vi.fn()} /></QueryClientProvider>);
  const first = mount("user:p:S01-p1:manual");
  fireEvent.click(screen.getByRole("button", { name: "Edit this paragraph" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Unsent local edit" } });
  first.unmount();
  const other = mount("other-user:p:S01-p1:manual");
  expect(screen.queryByRole("textbox")).toBeNull();
  other.unmount();
  mount("user:p:S01-p1:manual");
  expect(screen.getByRole("textbox")).toHaveValue("Unsent local edit");
});
