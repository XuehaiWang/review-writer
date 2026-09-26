import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { DraftManuscriptFields } from "./DraftManuscriptFields";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear(); });
it("edits fields in place and permits saving after an unrelated paragraph revision", async () => {
  usePreferences.getState().setLanguage("en");
  vi.mocked(apiRequest).mockResolvedValue({});
  const client = new QueryClient();
  const view = (revision: number, title = "Saved title") => <QueryClientProvider client={client}>
    <DraftManuscriptFields userId="u" projectId="p" revision={revision} fields={{ title, keywords: ["chemistry"] }} refresh={vi.fn()} />
  </QueryClientProvider>;
  const mounted = render(view(1));
  const input = screen.getByRole("textbox", { name: "Title" });
  input.textContent = "New title"; fireEvent.input(input);
  mounted.rerender(view(2));
  expect(input).toHaveTextContent("New title");
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledOnce());
  expect(JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string)).toEqual({ revision: 2, title: "New title", keywords: ["chemistry"] });
});

it("does not overwrite a concurrent title change and can undo local input", () => {
  usePreferences.getState().setLanguage("en");
  const client = new QueryClient();
  const view = (revision: number, title: string) => <QueryClientProvider client={client}>
    <DraftManuscriptFields userId="u" projectId="p" revision={revision} fields={{ title, keywords: [] }} refresh={vi.fn()} />
  </QueryClientProvider>;
  const mounted = render(view(1, "Original"));
  const input = screen.getByRole("textbox", { name: "Title" });
  input.textContent = "My title"; fireEvent.input(input);
  mounted.rerender(view(2, "Other author's title"));
  expect(input).toHaveTextContent("My title");
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Undo changes" }));
  expect(screen.getByRole("textbox", { name: "Title" })).toHaveTextContent("Other author's title");
});
