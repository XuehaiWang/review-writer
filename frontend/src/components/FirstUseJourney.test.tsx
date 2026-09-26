import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { PropsWithChildren } from "react";
import { ProjectSelector, useSelectedProject } from "./ProjectSelector";
import { PreparationNotice } from "./PreparationNotice";
import { useFormDraft } from "../hooks/useFormDraft";
import { ProjectsPage } from "../features/projects/ProjectsPage";

function fixture(path = "/discovery?project=missing", enabled = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(["me"], { user_id: "isolated", roles: ["user"] });
  client.setQueryData(["projects"], { items: [{ project_id: "existing", slug: "existing", current_stage: "draft", completed_stages: [], taxonomy_profile: "chemistry_general" }] });
  client.setQueryData(["model-catalog"], { default_tier: "test", items: [{ id: "test", enabled, label: "Test" }] });
  client.setQueryData(["balance"], { available_usd: enabled ? "10" : "0" });
  client.setQueryData(["provider-settings"], { items: [] });
  client.setQueryData(["taxonomy-profiles"], { default_profile: "chemistry_general", items: [
    { id: "general_academic", label_zh: "通用学术" }, { id: "chemistry_general", label_zh: "通用化学" },
  ] });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>{children}</MemoryRouter></QueryClientProvider>;
  return { client, wrapper };
}
afterEach(() => { cleanup(); localStorage.clear(); });

describe("first-use and returning-user safeguards", () => {
  it("does not replace an explicit missing project with the first project", () => {
    render(<ProjectSelector />, { wrapper: fixture().wrapper });
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(screen.getByText(/未自动切换项目/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除项目" })).toBeDisabled();
  });
  it("retains the convenient default when no project was specified", () => {
    const { result } = renderHook(() => useSelectedProject(), { wrapper: fixture("/discovery").wrapper });
    expect(result.current.selected?.project_id).toBe("existing");
  });
  it("shows model and credit problems together and preserves the project link", () => {
    render(<PreparationNotice kind="text" />, { wrapper: fixture().wrapper });
    expect(screen.getByText(/所选文本模型暂不可用/)).toBeInTheDocument();
    expect(screen.getByText(/当前没有可用额度/)).toBeInTheDocument();
    expect(screen.getByRole("link")).toHaveAttribute("href", "/settings?project=missing");
  });
  it("does not add steps for a configured user or require image settings", () => {
    render(<PreparationNotice kind="text" />, { wrapper: fixture("/discovery", true).wrapper });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
  it("treats a missing parser configuration as unready", () => {
    render(<PreparationNotice kind="mineru" />, { wrapper: fixture().wrapper });
    expect(screen.getByText(/PDF 解析服务尚未就绪/)).toBeInTheDocument();
  });
  it("preserves edits on refetch and remount, isolates scopes, and protects newer input from an older save", () => {
    const wrapper = fixture().wrapper;
    const first = renderHook(({ scope, initial }) => useFormDraft(scope, initial), { wrapper, initialProps: { scope: "a", initial: "server" } });
    act(() => first.result.current.set("edited"));
    const committed = first.result.current.checkpoint();
    act(() => first.result.current.set("newer"));
    act(() => committed());
    expect(first.result.current.value).toBe("newer");
    first.rerender({ scope: "a", initial: "refetched" });
    expect(first.result.current.value).toBe("newer");
    first.rerender({ scope: "b", initial: "other" });
    expect(first.result.current.value).toBe("other");
    first.unmount();
    const again = renderHook(() => useFormDraft("a", "server"), { wrapper });
    expect(again.result.current.value).toBe("newer");
    act(() => again.result.current.clear());
    expect(again.result.current.value).toBe("server");
  });
  it("binds the chemistry default and restores new-project input", () => {
    const wrapper = fixture("/workspace", true).wrapper;
    const view = render(<ProjectsPage />, { wrapper });
    expect(screen.getByRole("combobox", { name: /^分类配置/ })).toHaveValue("chemistry_general");
    fireEvent.change(screen.getByLabelText("研究主题"), { target: { value: "unsaved topic" } });
    view.unmount();
    render(<ProjectsPage />, { wrapper });
    expect(screen.getByLabelText("研究主题")).toHaveValue("unsaved topic");
    expect(screen.getByRole("link", { name: "继续项目" })).toHaveAttribute("href", "/draft?project=existing");
  });
});
