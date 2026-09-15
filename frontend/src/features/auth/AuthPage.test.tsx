import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AuthPage } from "./AuthPage";
import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";

vi.mock("../../api/client", async (original) => ({ ...await original<typeof import("../../api/client")>(), apiRequest: vi.fn() }));
const config = { enabled: true, registration_enabled: true, password_reset_enabled: true, password_reset_expiry_minutes: 30, password_min_length: 10 };
function show() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
    <MemoryRouter><AuthPage config={config} /></MemoryRouter>
  </QueryClientProvider>);
}
beforeEach(() => { usePreferences.setState({ language: "zh-CN" }); vi.mocked(apiRequest).mockReset(); });
afterEach(cleanup);

it("requests an email code without creating an account, then submits the bound code", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ message: "验证码已发送" });
  show();
  fireEvent.click(screen.getByRole("button", { name: "注册" }));
  fireEvent.change(screen.getByLabelText("邮箱", { exact: true }), { target: { value: "new@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "发送邮箱验证码" }));
  await screen.findByText("验证码已发送");
  expect(apiRequest).toHaveBeenCalledTimes(1);
  expect(apiRequest).toHaveBeenCalledWith("/api/v1/auth/registration-code", expect.objectContaining({ body: JSON.stringify({ email: "new@example.com" }) }));
  expect(screen.getByRole("button", { name: /秒后可重发/ })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("邮箱验证码"), { target: { value: "123456" } });
  fireEvent.change(screen.getByLabelText("密码", { exact: true }), { target: { value: "safe-password-123" } });
  fireEvent.click(screen.getByRole("button", { name: "验证并注册" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/auth/register", expect.objectContaining({ body: JSON.stringify({ email: "new@example.com", password: "safe-password-123", display_name: "", verification_code: "123456" }) })));
});

it("reports delivery failure and permits a retry", async () => {
  vi.mocked(apiRequest).mockRejectedValue(new Error("验证码发送失败"));
  show();
  fireEvent.click(screen.getByRole("button", { name: "注册" }));
  fireEvent.change(screen.getByLabelText("邮箱", { exact: true }), { target: { value: "new@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "发送邮箱验证码" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("验证码发送失败");
  expect(screen.getByRole("button", { name: "发送邮箱验证码" })).toBeEnabled();
  expect(apiRequest).toHaveBeenCalledTimes(1);
});

it("does not require a hidden registration code when switching back to login", async () => {
  vi.mocked(apiRequest).mockResolvedValue({});
  show();
  fireEvent.click(screen.getByRole("button", { name: "注册" }));
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  fireEvent.change(screen.getByLabelText("邮箱", { exact: true }), { target: { value: "existing@example.com" } });
  fireEvent.change(screen.getByLabelText("密码", { exact: true }), { target: { value: "existing-password" } });
  fireEvent.click(screen.getByRole("button", { name: "登录并进入工作台" }));
  await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/auth/login", expect.anything()));
});
