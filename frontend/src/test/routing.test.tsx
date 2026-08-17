import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../app/App";
import { usePreferences } from "../state/preferences";

const principal = {
  user_id: "user-1",
  email: "researcher@example.com",
  display_name: "Researcher",
  roles: ["member"],
  permissions: [],
};

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestPath(input: RequestInfo | URL): string {
  const value = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  return new URL(value, "http://localhost").pathname;
}

function renderApp(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("public and authenticated routing", () => {
  beforeEach(() => {
    localStorage.clear();
    usePreferences.setState({ language: "zh-CN" });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the product introduction at the public home while preserving an active identity", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      if (path === "/api/v1/auth/config") return jsonResponse({ enabled: true, registration_enabled: true, password_min_length: 10 });
      if (path === "/api/v1/me") return jsonResponse(principal);
      throw new Error(`Unexpected request: ${path}`);
    });

    renderApp("/");

    expect(await screen.findByRole("heading", { name: "从文献证据到可交付终稿，始终保持可追溯。" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "进入工作台" })[0]).toHaveAttribute("href", "/workspace");
  });

  it("redirects a signed-out user from a protected stage to the login page", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = requestPath(input);
      if (path === "/api/v1/auth/config") return jsonResponse({ enabled: true, registration_enabled: true, password_min_length: 10 });
      if (path === "/api/v1/me") return jsonResponse({ detail: "Not authenticated" }, 401);
      throw new Error(`Unexpected request: ${path}`);
    });

    renderApp("/final?project=project-1");

    expect(await screen.findByRole("heading", { name: "登录 Review Writer" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "首页" })).toHaveAttribute("href", "/");
  });

  it("opens the workspace after a successful login", async () => {
    let signedIn = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = requestPath(input);
      if (path === "/api/v1/auth/config") return jsonResponse({ enabled: true, registration_enabled: true, password_min_length: 10 });
      if (path === "/api/v1/me") return signedIn ? jsonResponse(principal) : jsonResponse({ detail: "Not authenticated" }, 401);
      if (path === "/api/v1/auth/login" && init?.method === "POST") {
        signedIn = true;
        return jsonResponse(principal);
      }
      if (path === "/api/v1/projects") return jsonResponse({ items: [], count: 0 });
      throw new Error(`Unexpected request: ${path}`);
    });

    renderApp("/login");
    fireEvent.change(await screen.findByLabelText("邮箱"), { target: { value: "researcher@example.com" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录并进入工作台" }));

    expect(await screen.findByRole("heading", { name: "科学综述项目" })).toBeInTheDocument();
  });

  it("clears the session view and returns to login after logout", async () => {
    let signedIn = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = requestPath(input);
      if (path === "/api/v1/auth/config") return jsonResponse({ enabled: true, registration_enabled: true, password_min_length: 10 });
      if (path === "/api/v1/me") return signedIn ? jsonResponse(principal) : jsonResponse({ detail: "Not authenticated" }, 401);
      if (path === "/api/v1/projects") return jsonResponse({ items: [], count: 0 });
      if (path === "/api/v1/auth/logout" && init?.method === "POST") {
        signedIn = false;
        return new Response(null, { status: 204 });
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    renderApp("/workspace");
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "登录 Review Writer" })).toBeInTheDocument());
  });

  it("opens a newly created project instead of retaining the previous project context", async () => {
    const createdProject = {
      project_id: "new-project-id",
      slug: "new-review",
      owner_user_id: principal.user_id,
      topic: "A new review topic",
      taxonomy_profile: "chemistry_general",
      discovery_status: "pending",
      current_stage: "library",
      completed_stages: [],
    };
    let created = false;
    const acquisitionRequests: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = requestPath(input);
      const requestUrl = String(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      if (path === "/api/v1/auth/config") return jsonResponse({ enabled: true, registration_enabled: true, password_min_length: 10 });
      if (path === "/api/v1/me") return jsonResponse(principal);
      if (path === "/api/v1/projects" && init?.method === "POST") {
        created = true;
        return jsonResponse(createdProject, 201);
      }
      if (path === "/api/v1/projects") return jsonResponse({ items: created ? [createdProject] : [], count: created ? 1 : 0 });
      if (path === "/api/v1/library/papers") return jsonResponse({ items: [], count: 0, query: "" });
      if (path === "/api/v1/library/search-jobs/current" || path === "/api/v1/library/download-jobs/current") {
        acquisitionRequests.push(requestUrl);
        return jsonResponse({ job: null });
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    renderApp("/workspace");
    fireEvent.change(await screen.findByRole("textbox", { name: /项目ID/ }), { target: { value: "new-review" } });
    fireEvent.change(screen.getByRole("textbox", { name: /研究主题/ }), { target: { value: "A new review topic" } });
    fireEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(await screen.findByRole("heading", { name: "文献库" })).toBeInTheDocument();
    expect(screen.getByLabelText("当前项目")).toHaveValue("new-project-id");
    await waitFor(() => expect(acquisitionRequests).toHaveLength(2));
    expect(acquisitionRequests.every((url) => url.includes("project_id=new-project-id"))).toBe(true);
  });
});
