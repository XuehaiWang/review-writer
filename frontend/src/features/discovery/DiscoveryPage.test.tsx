import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { apiRequest } from "../../api/client";
import { usePreferences } from "../../state/preferences";
import { DiscoveryPage } from "./DiscoveryPage";
import type { DiscoveryPayload } from "./model/discoveryModel";

vi.mock("../../api/client", async original => ({ ...await original<object>(), apiRequest: vi.fn() }));
vi.mock("../../components/ProjectSelector", () => ({
  ProjectSelector: () => null,
  useSelectedProject: () => ({
    selected: { project_id: "project-1", topic: "Example topic", model_tier: "default" },
    projects: { data: { items: [{ project_id: "project-1" }] } },
  }),
}));
vi.mock("../../components/PreparationNotice", () => ({ PreparationNotice: () => null }));
vi.mock("./useFulltextChecks", () => ({ useFulltextChecks: () => ({}) }));

afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear(); });

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/discovery?project=project-1"]}><DiscoveryPage /></MemoryRouter></QueryClientProvider>);
  return client;
}

function mockRequests(downloadError?: Error) {
  usePreferences.getState().setLanguage("zh-CN");
  vi.mocked(apiRequest).mockImplementation(async path => {
    if (path === "/api/v1/me") return { user_id: "user-1" };
    if (path === "/api/v1/projects/project-1/discovery") return {
      project_id: "project-1", artifact_id: "discovery-1", revision: 3, topic: "Example topic", status: "review",
      results: [{ keyword: "example", keep: true, local_results: [
        { paper_id: "P001", title: "Local paper", role: "uncertain", selected_for_matrix: false },
      ], web_results: [
        { candidate_id: "crossref:10.1234/example", doi: "10.1234/example", title: "Online paper", access_status: "open_access_downloadable" },
      ] }],
    };
    if (path.endsWith("/discovery/jobs/current")) return { active_job: null, latest_job: null };
    if (path.startsWith("/api/v1/library")) return { items: [] };
    if (path.endsWith("/discovery/download-jobs")) {
      if (downloadError) throw downloadError;
      return { id: "download-1", status: "queued" };
    }
    if (path === "/api/v1/jobs/download-1") return { id: "download-1", status: "running" };
    throw new Error(`Unexpected request: ${path}`);
  });
}

it("recommends English queries and shows an English Topic example without excluding Chinese input", async () => {
  mockRequests();
  mount();
  expect(await screen.findByLabelText("标准 Topic 示例")).toHaveTextContent("推荐使用英文检索需求；中文输入仍然支持。");
  expect(screen.getByText(/Write a review of asymmetric syntheses of axially chiral allenes/)).toBeInTheDocument();
  expect(screen.getByText(/中文也可输入，系统会尝试转换为英文检索词/)).toBeInTheDocument();
});

it("shows the same English Topic example in the English interface", async () => {
  mockRequests();
  usePreferences.getState().setLanguage("en");
  mount();
  expect(await screen.findByLabelText("Example review topic")).toHaveTextContent("English search requests are recommended");
  expect(screen.getByText(/Write a review of asymmetric syntheses of axially chiral allenes/)).toBeInTheDocument();
});

it("submits an online candidate from Discovery and shows progress beside its button", async () => {
  mockRequests();
  const client = mount();
  fireEvent.click(await screen.findByRole("button", { name: "加入大纲候选" }));
  expect(screen.getByRole("button", { name: "已加入候选" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "尝试下载并解析" }));
  await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.some(([path]) => path.endsWith("/discovery/download-jobs"))).toBe(true));
  const request = vi.mocked(apiRequest).mock.calls.find(([path]) => path.endsWith("/discovery/download-jobs"))!;
  expect(request[0]).toBe("/api/v1/projects/project-1/discovery/download-jobs");
  expect(JSON.parse(request[1]!.body as string)).toEqual({ candidate_id: "crossref:10.1234/example", revision: 3 });
  expect(await screen.findByText(/已提交，正在获取全文/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
  const previous = client.getQueryData<DiscoveryPayload>(["discovery", "project-1"])!;
  act(() => client.setQueryData(["discovery", "project-1"], { ...previous }));
  expect(screen.getByRole("button", { name: "已加入候选" })).toBeInTheDocument();
  act(() => client.setQueryData(["discovery", "project-1"], {
    ...previous, artifact_id: "discovery-2", revision: 4,
    candidate_refresh: { source_artifact_id: "discovery-1", candidate_id: "crossref:10.1234/example", paper_id: "P002" },
    results: [{ keyword: "example", keep: true, local_results: [
      { paper_id: "P001", title: "Local paper", role: "uncertain", selected_for_matrix: false },
      { paper_id: "P002", title: "Downloaded paper", role: "uncertain", selected_for_matrix: false },
    ], web_results: [] }],
  }));
  expect(screen.getByRole("button", { name: "已加入候选" })).toBeInTheDocument();
});

it("shows a failed submission beside the candidate instead of silently doing nothing", async () => {
  mockRequests(new Error("Another paper is being downloaded. Wait for it to finish, then retry."));
  mount();
  const button = await screen.findByRole("button", { name: "尝试下载并解析" });
  fireEvent.click(button);
  const message = await screen.findByText("另一篇论文正在获取全文，请等待完成后重试。");
  expect(button.closest(".result-card")).toContainElement(message);
});

it("excludes a paper and restores it with one toggle instead of a role dropdown", async () => {
  mockRequests();
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "排除" }));
  expect(await screen.findByRole("button", { name: "已排除 · 点击恢复" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "已排除 · 点击恢复" }));
  expect(await screen.findByRole("button", { name: "排除" })).toBeInTheDocument();
});
