import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { AdminPage } from "./AdminPage";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn(), jsonBody: (body: unknown) => ({ body: JSON.stringify(body) }), newIdempotencyKey: () => crypto.randomUUID() }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (zh: string) => zh }) }));

const users = ["admin", "reader"].map(id => ({ user_id: id, email: `${id}@test.invalid`, display_name: id, role: id === "admin" ? "admin" : "user", status: "active", available_usd: "5", reserved_usd: "0", estimated_cost_usd: "1", project_count: 2 }));
const providers = ["text", "image", "embedding", "mineru"].map(provider_kind => ({ provider_kind, base_url: "https://provider.test/v1", model_name: "model", wire_api: "responses", enabled: true, source: "database", api_key_configured: true, api_key_hint: "***", updated_at: null }));
const catalog = { revision: 1, default_tier: "m1", items: [{ id: "m1", model: "model-one", label_zh: "测试模型", label_en: "Test", enabled: true, wire_api: "", input_usd_per_million: "1", output_usd_per_million: "2", cached_input_usd_per_million: "0" }] };
let client: QueryClient;
beforeEach(() => {
  vi.mocked(apiRequest).mockImplementation(async (url, options) => {
    if (options?.method === "PUT") return { ...providers[0], ...JSON.parse(String(options.body)) };
    if (options?.method === "POST") return {};
    if (url === "/api/v1/me") return { user_id: "admin" };
    if (url.includes("/admin/users")) return { items: users };
    if (url.includes("/admin/usage")) return { user_count: 2, active_user_count: 2, project_count: 4, total_tokens: 100, text_request_count: 2, estimated_cost_usd: "2", account_balance_total_usd: "10", reserved_total_usd: "0" };
    if (url.includes("/provider-settings")) return { items: providers };
    if (url.endsWith("/model-catalog")) return catalog;
    if (url === "/api/v1/admin/text-connections") return { items: [{ ...providers[0], id: "default", revision: 1, name: "默认连接" }] };
    if (url.includes("/admin/errors") || url.includes("/provider-audit")) return { items: [], has_more: false };
    throw new Error(`Unexpected request: ${url}`);
  });
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><AdminPage /></QueryClientProvider>);
});
afterEach(() => { cleanup(); client.clear(); vi.clearAllMocks(); });
const openServices = () => fireEvent.click(screen.getByRole("button", { name: /^模型与服务/ }));

it("groups services and logs separately and loads failures only when opened", async () => {
  await screen.findByText("reader@test.invalid");
  expect(screen.queryByRole("heading", { name: "文本生成服务" })).toBeNull();
  expect(vi.mocked(apiRequest).mock.calls.some(([url]) => url.includes("/admin/errors"))).toBe(false);
  openServices();
  expect(await screen.findByRole("heading", { name: "文本服务连接" })).toBeVisible();
  expect(await screen.findByRole("heading", { name: "文本模型目录" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "图像生成服务" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^图像生成/ }));
  expect(screen.getByRole("heading", { name: "图像生成服务" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "文本模型目录" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /^运行记录/ }));
  expect(await screen.findByText("此范围内没有故障记录。")).toBeVisible();
  expect(screen.queryByRole("heading", { name: "图像生成服务" })).toBeNull();
  expect(screen.getByRole("heading", { name: "服务与模型配置记录" })).toBeVisible();
});

it("preserves provider drafts across navigation and refresh, then saves the selected provider", async () => {
  openServices();
  await screen.findByText("默认连接", { selector: "strong" });
  const textPanel = document.getElementById("admin-service-text")!;
  fireEvent.click(within(textPanel).getByText("默认连接", { selector: "strong" }));
  const key = within(textPanel).getByLabelText("API Key");
  fireEvent.change(key, { target: { value: "test-only-key" } });
  fireEvent.click(screen.getByRole("button", { name: /^用户与余额/ }));
  openServices();
  fireEvent.click(screen.getByRole("button", { name: "刷新当前页面" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "刷新当前页面" })).not.toBeDisabled());
  expect(key).toHaveValue("test-only-key");
  fireEvent.click(within(textPanel).getByRole("button", { name: "保存连接" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url, opts]) => url === "/api/v1/admin/text-connections/default" && opts?.method === "PUT" && JSON.parse(String(opts.body)).api_key === "test-only-key")).toBe(true));
  await waitFor(() => expect(key).toHaveValue(""));
});

it("opens balance adjustment for an explicit user and preserves self-account protections", async () => {
  await screen.findByText("reader@test.invalid");
  expect(screen.queryByLabelText("调整金额（USD）")).toBeNull();
  expect(screen.getByLabelText("admin@test.invalid 的角色")).toBeDisabled();
  expect(screen.getByLabelText("admin@test.invalid 的状态")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "调整 reader@test.invalid 的余额" }));
  const form = screen.getByRole("region", { name: "调整用户余额" });
  fireEvent.change(within(form).getByLabelText("调整金额（USD）"), { target: { value: "0" } });
  fireEvent.change(within(form).getByLabelText("调整原因"), { target: { value: "测试额度" } });
  expect(within(form).getByRole("button", { name: "确认调整余额" })).toBeDisabled();
  fireEvent.change(within(form).getByLabelText("调整金额（USD）"), { target: { value: "2" } });
  fireEvent.click(within(form).getByRole("button", { name: "确认调整余额" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([url, opts]) => url === "/api/v1/admin/credits/adjustments" && JSON.parse(String(opts?.body)).target_user_id === "reader")).toBe(true));
  await waitFor(() => expect(screen.queryByRole("region", { name: "调整用户余额" })).toBeNull());
});
