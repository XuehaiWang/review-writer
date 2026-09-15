import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { TextConnectionsEditor } from "./TextConnectionsEditor";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn(), jsonBody: (body: unknown) => ({ body: JSON.stringify(body) }) }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (zh: string) => zh }) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("creates a connection with its own key and removes the plaintext draft after saving", async () => {
  vi.mocked(apiRequest).mockImplementation(async (_url, options) => options?.method === "POST" ? { id: "new", revision: 1 } : { items: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><TextConnectionsEditor /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "添加连接" }));
  expect(screen.getByRole("button", { name: "保存连接" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("连接名称 / 分组备注"), { target: { value: "分组 A" } });
  fireEvent.change(screen.getByLabelText("Base URL"), { target: { value: "https://provider.test/v1" } });
  fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "fixture-key-only" } });
  fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url, opts]) => {
    if (url !== "/api/v1/admin/text-connections" || opts?.method !== "POST") return false;
    const data = JSON.parse(String(opts.body));
    return data.name === "分组 A" && data.revision === 0 && data.api_key === "fixture-key-only";
  })).toBe(true));
  await waitFor(() => expect(screen.queryByLabelText("API Key")).toBeNull());
  client.clear();
});
