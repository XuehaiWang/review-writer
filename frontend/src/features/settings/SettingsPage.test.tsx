import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { queryKeys } from "../../api/queries";
import { SettingsPage } from "./SettingsPage";

vi.mock("../../api/client", async (importOriginal) => ({ ...await importOriginal<typeof import("../../api/client")>(), apiRequest: vi.fn(), jsonBody: (body: unknown) => ({ body: JSON.stringify(body) }) }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ language: "zh-CN", text: (zh: string) => zh }) }));
const model = (id: string, enabled = true) => ({ id, model: `Vendor/${id}`, label_zh: `${id} 模型`, label_en: id, enabled, input_usd_per_million: "1.5", cached_input_usd_per_million: "0.25", output_usd_per_million: "8" });
let items = [model("custom-a"), model("custom-b"), model("retired", false)];
let projects = [{ project_id: "p1", slug: "First", model_tier: "custom-a" }, { project_id: "p2", slug: "Second", model_tier: "custom-a" }];
let catalogError = false;
let client: QueryClient;
beforeEach(() => {
  items = [model("custom-a"), model("custom-b"), model("retired", false)];
  projects = [{ project_id: "p1", slug: "First", model_tier: "custom-a" }, { project_id: "p2", slug: "Second", model_tier: "custom-a" }];
  catalogError = false;
  vi.mocked(apiRequest).mockImplementation(async (url, options) => {
    if (url === "/api/v1/projects") return { items: projects, count: projects.length };
    if (url === "/api/v1/model-catalog") {
      if (catalogError) throw new Error("Catalog unavailable");
      return { revision: 2, default_tier: "custom-b", items };
    }
    if (options?.method === "PATCH") {
      const id = url.split("/").at(-2);
      const patch = JSON.parse(String(options.body));
      projects = projects.map(p => p.project_id === id ? { ...p, model_tier: patch.model_tier } : p);
      return projects.find(p => p.project_id === id);
    }
    if (url.startsWith("/api/v1/usage/summary")) return { request_count: 0, total_tokens: 0, cached_input_tokens: 0, image_count: 0, image_request_count: 0, mineru_billable_pages: 0, estimated_cost_usd: "0" };
    if (url === "/api/v1/balance") return { available_usd: "0", balance_usd: "0", reserved_usd: "0", lifetime_debited_usd: "0" };
    return { items: [] };
  });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
});
afterEach(() => { cleanup(); client.clear(); vi.clearAllMocks(); });
function mount() {
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/settings?project=p1"]}><SettingsPage /></MemoryRouter></QueryClientProvider>);
}

it("uses the shared dynamic options, exposes cached input price, and saves the exact model ID", async () => {
  mount();
  await screen.findByRole("option", { name: "custom-b 模型" });
  const select = screen.getByRole("combobox", { name: "文本模型" });
  expect(select).toHaveValue("custom-a");
  expect(screen.getByRole("option", { name: "retired 模型（已停用）" })).toBeDisabled();
  expect(screen.queryByRole("option", { name: /Terra|Sol|Luna/ })).toBeNull();
  fireEvent.change(select, { target: { value: "custom-b" } });
  const details = screen.getByRole("region", { name: "所选模型信息" });
  expect(within(details).getByText("$0.25")).toBeVisible();
  expect(within(details).getByText("后台默认")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "确认并保存" }));
  await screen.findByText("文本模型已保存。");
  expect(vi.mocked(apiRequest).mock.calls.some(([url, options]) => url === "/api/v1/projects/p1/model-tier" && JSON.parse(String(options?.body)).model_tier === "custom-b")).toBe(true);
});

it("preserves unsaved selection on background refresh but resets it when switching projects", async () => {
  mount();
  await screen.findByRole("option", { name: "custom-b 模型" });
  fireEvent.change(screen.getByRole("combobox", { name: "文本模型" }), { target: { value: "custom-b" } });
  await act(async () => { await client.invalidateQueries({ queryKey: queryKeys.projects }); });
  expect(screen.getByRole("combobox", { name: "文本模型" })).toHaveValue("custom-b");
  fireEvent.change(screen.getByRole("combobox", { name: "应用到" }), { target: { value: "p2" } });
  expect(screen.getByRole("combobox", { name: "文本模型" })).toHaveValue("custom-a");
  expect(screen.getByRole("button", { name: "确认并保存" })).toBeDisabled();
});

it("refreshes a cached catalog on entry and retains missing legacy selections until explicitly changed", async () => {
  projects[0].model_tier = "legacy-model";
  client.setQueryData(queryKeys.modelCatalog, { revision: 1, default_tier: "old-model", items: [model("old-model")] });
  mount();
  await screen.findByRole("option", { name: "custom-b 模型" });
  expect(screen.queryByRole("option", { name: "old-model 模型" })).toBeNull();
  expect(screen.getByRole("combobox", { name: "文本模型" })).toHaveValue("legacy-model");
  expect(screen.getByText(/当前选择的模型已停用或不在目录中/)).toBeVisible();
  expect(screen.getByRole("button", { name: "确认并保存" })).toBeDisabled();
  expect(vi.mocked(apiRequest).mock.calls.some(([, options]) => options?.method === "PATCH")).toBe(false);
});

it("shows catalog errors and blocks saving instead of silently showing old choices", async () => {
  catalogError = true;
  mount();
  await screen.findByText("无法加载模型目录");
  expect(screen.getByRole("combobox", { name: "文本模型" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认并保存" })).toBeDisabled();
  catalogError = false;
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.getByRole("combobox", { name: "文本模型" })).not.toBeDisabled());
});

it("explains an unavailable catalog without falling back to a hardcoded model", async () => {
  items = [];
  mount();
  await screen.findByText("当前没有可用的文本模型，请联系管理员启用模型。");
  expect(screen.getByRole("combobox", { name: "文本模型" })).toHaveValue("custom-a");
  expect(screen.getByRole("combobox", { name: "文本模型" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认并保存" })).toBeDisabled();
});
