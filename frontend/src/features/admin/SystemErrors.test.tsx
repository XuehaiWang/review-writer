import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { apiRequest } from "../../api/client";
import { SystemErrors } from "./SystemErrors";

vi.mock("../../api/client", () => ({ apiRequest: vi.fn() }));
vi.mock("../../i18n/useUiText", () => ({ useUiText: () => ({ text: (zh: string) => zh }) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("does not query failures while its section is inactive", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SystemErrors active={false} /></QueryClientProvider>);
  expect(vi.mocked(apiRequest)).not.toHaveBeenCalled();
  client.clear();
});

it("shows failures and submits filters without a request per keystroke", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ items: [{ id: "job-1", source: "job", email: "user@test.invalid",
    project_id: "p1", request_id: "", error_code: "PROVIDER_FAILED", message: "Unavailable",
    operation: "sections.generate", status_code: 0, created_at: "2026-09-10T00:00:00Z" }], has_more: true });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SystemErrors /></QueryClientProvider>);
  expect(await screen.findByText("PROVIDER_FAILED")).toBeTruthy();
  const count = vi.mocked(apiRequest).mock.calls.length;
  fireEvent.change(screen.getByPlaceholderText("用户邮箱、项目 ID、任务 ID、错误码或请求 ID"), { target: { value: "PROVIDER_FAILED" } });
  expect(vi.mocked(apiRequest).mock.calls.length).toBe(count);
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.lastCall?.[0]).toContain("q=PROVIDER_FAILED"));
  await waitFor(() => expect(screen.getByRole("button", { name: "下一页" })).not.toBeDisabled());
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.lastCall?.[0]).toContain("offset=25"));
  fireEvent.click(screen.getByRole("button", { name: "重置筛选" }));
  await waitFor(() => expect(screen.getByText("第 1 页")).toBeVisible());
  expect(screen.getByRole("searchbox", { name: "关键词" })).toHaveValue("");
  expect(screen.getByLabelText("来源")).toHaveValue("");
  expect(screen.getByLabelText("时间")).toHaveValue("30");
  client.clear();
});

it("keeps diagnostic codes in expandable details and does not invent job codes for API failures", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ items: [{ id: "event-1", source: "api", email: null,
    project_id: "project-123", request_id: "request-456", error_code: "", message: "<script>alert('unsafe')</script>\nProvider unavailable",
    operation: "POST /api/v1/example", status_code: 503, created_at: "2026-09-10T00:00:00Z" }], has_more: false });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><SystemErrors /></QueryClientProvider>);
  const operation = await screen.findByText("POST /api/v1/example");
  expect(screen.getByText("request-456")).not.toBeVisible();
  expect(screen.queryByText("JOB_EXECUTION_FAILED")).toBeNull();
  fireEvent.click(operation.closest("summary")!);
  expect(screen.getByText("request-456")).toBeVisible();
  expect(screen.getByText("503")).toBeVisible();
  expect(screen.getByText("未记录")).toBeVisible();
  expect(document.querySelector(".system-error-message pre")?.textContent).toContain("<script>");
  expect(document.querySelector(".system-error-message script")).toBeNull();
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  client.clear();
});
