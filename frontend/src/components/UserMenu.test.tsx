import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { UserMenu } from "./UserMenu";
import { AppShell } from "./AppShell";
import { usePreferences } from "../state/preferences";

const logout = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, error: null }));
vi.mock("../hooks/useSessionLogout", () => ({ useSessionLogout: () => logout }));
const identity = { user_id: "u", email: "researcher@example.com", display_name: "Researcher", roles: [], permissions: ["provider:manage"] };
const authConfig = { enabled: true, registration_enabled: true, password_reset_enabled: true, password_reset_expiry_minutes: 30, password_min_length: 10 };
afterEach(() => { cleanup(); vi.clearAllMocks(); usePreferences.setState({ language: "zh-CN" }); });

it("keeps account controls separate from workflow and preserves project context", () => {
  render(<MemoryRouter initialEntries={["/draft?project=p1"]}><AppShell identity={identity} authConfig={authConfig}>Content</AppShell></MemoryRouter>);
  const nav = screen.getByRole("navigation", { name: "综述工作流" });
  expect(within(nav).queryByRole("link", { name: /API/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "退出登录" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /用户菜单/ }));
  expect(screen.getByRole("link", { name: /API/ })).toHaveAttribute("href", "/settings?project=p1");
  expect(screen.getByRole("link", { name: /管理后台/ })).toHaveAttribute("href", "/admin");
  fireEvent.click(screen.getByRole("button", { name: "English" }));
  expect(usePreferences.getState().language).toBe("en");
  fireEvent.click(screen.getByRole("button", { name: "Log out" }));
  expect(logout.mutate).toHaveBeenCalledOnce();
});

it("closes on outside click and Escape with focus returned; hides admin and local logout", () => {
  render(<MemoryRouter><UserMenu identity={{ ...identity, permissions: [] }} authConfig={{ ...authConfig, enabled: false }} /></MemoryRouter>);
  const trigger = screen.getByRole("button", { name: /用户菜单/ });
  fireEvent.click(trigger);
  expect(screen.queryByRole("link", { name: /管理后台/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "退出登录" })).toBeNull();
  fireEvent.keyDown(screen.getByRole("button", { name: "English" }), { key: "Escape" });
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(trigger).toHaveFocus();
  fireEvent.click(trigger);
  fireEvent.pointerDown(document.body);
  expect(trigger).toHaveAttribute("aria-expanded", "false");
});
