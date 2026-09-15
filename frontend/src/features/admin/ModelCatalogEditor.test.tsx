import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { ModelCatalogEditor } from "./ModelCatalogEditor";
import { ModelOptions } from "../../components/ModelOptions";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn(), jsonBody: (body: unknown) => ({ body: JSON.stringify(body) }) }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (zh: string) => zh }) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const model = { id: "vendor-x", model: "Vendor/Model-X", label_zh: "自定义模型", label_en: "Custom model", description_zh: "", description_en: "", input_usd_per_million: "1", output_usd_per_million: "2", cached_input_usd_per_million: "0", enabled: true, wire_api: "" };
const catalog = { revision: 3, default_tier: model.id, items: [model] };
function mount(child: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>{child}</QueryClientProvider>);
  return client;
}

it("renders actual model names, preserving disabled selections", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ ...catalog, items: [{ ...model, enabled: false }] });
  const client = mount(<select defaultValue={model.id}><ModelOptions selected={model.id} /></select>);
  expect(await screen.findByRole("option", { name: "自定义模型（已停用）" })).toBeDisabled();
  expect(screen.queryByText("Sol")).toBeNull();
  client.clear();
});

it("saves catalog revision and tests the explicit saved model", async () => {
  vi.mocked(apiRequest).mockImplementation(async (url, options) => {
    if (url.includes("/test?")) return { ok: true };
    if (options?.method === "PUT") return { ...catalog, revision: 4 };
    return catalog;
  });
  const client = mount(<ModelCatalogEditor />);
  const name = await screen.findByLabelText("平台模型名称（精确匹配）");
  fireEvent.click(name.closest("details")!.querySelector("summary")!);
  fireEvent.change(name, { target: { value: "Vendor/New-Model" } });
  expect(screen.getByRole("button", { name: "测试已保存模型与 JSON 输出" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存模型目录" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url, opts]) => url === "/api/v1/admin/model-catalog" && String(opts?.body).includes('"revision":3') && String(opts?.body).includes("Vendor/New-Model"))).toBe(true));
  await waitFor(() => expect(screen.getByRole("button", { name: "测试已保存模型与 JSON 输出" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "测试已保存模型与 JSON 输出" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url]) => url.endsWith("?model_id=vendor-x"))).toBe(true));
  client.clear();
});

it("groups model details and prices while keeping price edits bound to the correct fields", async () => {
  vi.mocked(apiRequest).mockImplementation(async (_url, options) => options?.method === "PUT" ? { ...JSON.parse(String(options.body)), revision: 4 } : catalog);
  const client = mount(<ModelCatalogEditor />);
  const name = await screen.findByLabelText("平台模型名称（精确匹配）");
  expect(screen.getByText("默认")).toBeVisible();
  expect(screen.getByText("已启用")).toBeVisible();
  expect(screen.getByText("1 个模型 · 1 个启用")).toBeVisible();
  fireEvent.click(name.closest("details")!.querySelector("summary")!);
  expect(screen.getByRole("heading", { name: "基本信息" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "调用价格" })).toBeVisible();
  fireEvent.change(screen.getByLabelText("缓存输入价格"), { target: { value: "0.25" } });
  fireEvent.click(screen.getByRole("button", { name: "保存模型目录" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([, opts]) => {
    if (opts?.method !== "PUT") return false;
    const saved = JSON.parse(String(opts.body));
    return saved.default_tier === model.id && saved.items[0].cached_input_usd_per_million === "0.25" && saved.items[0].input_usd_per_million === "1" && saved.items[0].output_usd_per_million === "2";
  })).toBe(true));
  await waitFor(() => expect(screen.getByRole("button", { name: "添加模型" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
  expect(screen.getByText("新模型").closest("details")).toHaveAttribute("open");
  client.clear();
});

it("binds a model to the explicitly selected connection without exposing groups in user options", async () => {
  vi.mocked(apiRequest).mockImplementation(async (url, options) => {
    if (url.endsWith("/text-connections")) return { items: [
      { id: "default", name: "默认连接", enabled: true, api_key_configured: true },
      { id: "group-b", name: "分组 B", enabled: true, api_key_configured: true },
    ] };
    return options?.method === "PUT" ? { ...JSON.parse(String(options.body)), revision: 4 } : catalog;
  });
  const client = mount(<ModelCatalogEditor />);
  const select = await screen.findByLabelText("服务连接");
  fireEvent.click(select.closest("details")!.querySelector("summary")!);
  fireEvent.change(select, { target: { value: "group-b" } });
  fireEvent.click(screen.getByRole("button", { name: "保存模型目录" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url, opts]) => url === "/api/v1/admin/model-catalog" && opts?.method === "PUT" && JSON.parse(String(opts.body)).items[0].connection_id === "group-b")).toBe(true));
  client.clear();
});
