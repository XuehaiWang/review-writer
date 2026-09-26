import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePreferences } from "../../state/preferences";
import { SectionsPage } from "./SectionsPage";

vi.mock("../../components/ProjectSelector", () => ({
  ProjectSelector: () => null,
  useSelectedProject: () => ({ selected: { project_id: "project-1" } }),
}));

const planningUrl = "/api/v1/projects/project-1/planning";
const sectionsUrl = "/api/v1/projects/project-1/sections";
let planning: Record<string, unknown>;
let calls: Array<{ path: string; method: string }>;
let client: QueryClient;

function renderStageFour() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/sections?project=project-1"]}>
      <Routes>
        <Route path="/sections" element={<SectionsPage />} />
        <Route path="/planning" element={<h1>Choose outline</h1>} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>);
}

describe("stage 04 chapter-planning handoff", () => {
  beforeEach(() => {
    usePreferences.setState({ language: "zh-CN" });
    planning = {
      matrix_revision: 2, blueprint_revision: 0,
      outline_current: true, outline_ready_for_chapter_planning: true,
      selected_outline_md: "## Introduction", outline_selection: { outline_complete: true },
      blueprint_approved: false, blueprint_current: false, blueprint_jobs: [],
      literature_matrix: { rows: [] },
    };
    calls = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      calls.push({ path, method });
      if (path === planningUrl) return Response.json(planning);
      if (path === `${planningUrl}/blueprint/confirm` && method === "POST") {
        planning = { ...planning, blueprint_approved: true, blueprint_candidate_pending: false };
        return Response.json({ status: "approved" });
      }
      if (path === `${planningUrl}/blueprint/jobs` && method === "POST") return Response.json({ id: "new-plan-job", status: "queued" }, { status: 202 });
      if (path === sectionsUrl && method === "GET") return Response.json({
        project_id: "project-1", section_tasks: [], section_files: [],
        section_drafts_md: "", section_drafting_report_md: "", papers: [], revision: 0,
        handoff: { drafts_stale: false, has_existing_drafts: false, current: false },
        report: { current_task_count: 0, current_output_count: 0, jobs: [] },
      });
      throw new Error(`Unexpected request: ${method} ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    client?.clear();
    vi.restoreAllMocks();
  });

  it("does not request section artifacts before a plan exists", async () => {
    renderStageFour();
    expect(await screen.findByRole("button", { name: "生成章节规划" })).toBeEnabled();
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(false);
  });

  it("shows a stage-03 recovery link when paper analysis has not produced an artifact", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
      calls.push({ path: String(input), method: "GET" });
      return Response.json({ error: { code: "WORKFLOW_NOT_FOUND", message: "Planning artifact not found." } }, { status: 404 });
    });
    renderStageFour();
    expect(await screen.findByRole("link", { name: "返回文献分析" })).toHaveAttribute("href", "/planning?view=reading&project=project-1");
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(false);
  });

  it("offers stage 03 recovery when the saved outline is missing", async () => {
    planning = { ...planning, outline_ready_for_chapter_planning: false, outline_current: false };
    renderStageFour();
    expect(await screen.findByRole("link", { name: "前往选择大纲" })).toHaveAttribute("href", "/planning?view=outline&project=project-1");
    expect(screen.getByRole("button", { name: "生成章节规划" })).toBeDisabled();
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(false);
  });

  it("confirms a candidate without automatically submitting the paid drafting job", async () => {
    planning = {
      ...planning, blueprint_revision: 3, blueprint_artifact_id: "candidate-1",
      blueprint_current: true, blueprint_candidate_pending: true,
      section_blueprint: {
        academic_planning: { status: "completed" },
        sections: [{ section_id: "S01", title: "Introduction", primary_papers: [] }],
      },
    };
    renderStageFour();
    fireEvent.click(await screen.findByRole("button", { name: "确认章节规划" }));
    expect(await screen.findByRole("button", { name: "生成章节正文" })).toBeEnabled();
    await waitFor(() => expect(calls.some(({ path }) => path === sectionsUrl)).toBe(true));
    expect(calls.some(({ path, method }) => path === `${sectionsUrl}/jobs` && method === "POST")).toBe(false);
  });

  it("opens approved chapter drafts and still lets the user review the plan", async () => {
    planning = {
      ...planning, blueprint_revision: 3, blueprint_artifact_id: "approved-1",
      blueprint_current: true, blueprint_approved: true, active_blueprint_approved: true,
      section_blueprint: {
        academic_planning: { status: "completed" },
        sections: [{ section_id: "S01", title: "Introduction", primary_papers: [] }],
      },
    };
    renderStageFour();
    expect(await screen.findByRole("button", { name: "生成章节正文" })).toBeEnabled();
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "查看／调整章节规划" }));
    expect(await screen.findByRole("button", { name: "重新生成" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "确认章节规划" })).not.toBeInTheDocument();
  });

  it("does not submit a replacement chapter plan until the warning is confirmed", async () => {
    planning = {
      ...planning, blueprint_revision: 3, blueprint_artifact_id: "approved-1",
      blueprint_current: true, blueprint_approved: true, active_blueprint_approved: true,
      section_blueprint: {
        academic_planning: { status: "completed" },
        sections: [{ section_id: "S01", title: "Introduction", primary_papers: [] }],
      },
    };
    renderStageFour();
    fireEvent.click(await screen.findByRole("button", { name: "查看／调整章节规划" }));
    fireEvent.click(await screen.findByRole("button", { name: "重新生成" }));
    expect(screen.getByRole("dialog", { name: "重新规划章节？" })).toHaveTextContent("现有规划不会立即删除");
    expect(calls.some(({ path, method }) => path === `${planningUrl}/blueprint/jobs` && method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "保留当前内容" }));
    expect(calls.some(({ path, method }) => path === `${planningUrl}/blueprint/jobs` && method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    fireEvent.click(screen.getByRole("button", { name: "确认重新规划" }));
    await waitFor(() => expect(calls.filter(({ path, method }) => path === `${planningUrl}/blueprint/jobs` && method === "POST")).toHaveLength(1));
  });

  it("keeps approved drafts accessible while a new plan candidate awaits confirmation", async () => {
    planning = {
      ...planning, blueprint_revision: 3, blueprint_artifact_id: "candidate-2",
      blueprint_current: true, blueprint_approved: false, active_blueprint_approved: true,
      blueprint_candidate_pending: true,
      section_blueprint: {
        academic_planning: { status: "completed" },
        sections: [{ section_id: "S01", title: "Introduction", primary_papers: [] }],
      },
    };
    renderStageFour();
    expect(await screen.findByRole("button", { name: "确认章节规划" })).toBeEnabled();
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "查看已确认正文" }));
    expect(await screen.findByRole("button", { name: "生成章节正文" })).toBeEnabled();
    expect(calls.some(({ path }) => path === sectionsUrl)).toBe(true);
    expect(screen.queryByRole("button", { name: "确认章节规划" })).not.toBeInTheDocument();
  });

  it("recovers confirmation when the response is lost without posting a second time", async () => {
    planning = {
      ...planning, blueprint_revision: 3, blueprint_artifact_id: "candidate-1",
      blueprint_current: true, blueprint_candidate_pending: true,
      section_blueprint: {
        academic_planning: { status: "completed" },
        sections: [{ section_id: "S01", title: "Introduction", primary_papers: [] }],
      },
    };
    vi.mocked(globalThis.fetch).mockImplementation(async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      calls.push({ path, method });
      if (path === `${planningUrl}/blueprint/confirm` && method === "POST") {
        planning = { ...planning, blueprint_approved: true, blueprint_candidate_pending: false };
        throw new TypeError("Connection closed after server committed");
      }
      if (path === planningUrl) return Response.json(planning);
      if (path === sectionsUrl) return Response.json({
        project_id: "project-1", section_tasks: [], section_files: [],
        section_drafts_md: "", section_drafting_report_md: "", papers: [], revision: 0,
        handoff: { drafts_stale: false, has_existing_drafts: false, current: false },
        report: { current_task_count: 0, current_output_count: 0, jobs: [] },
      });
      throw new Error(`Unexpected request: ${method} ${path}`);
    });
    renderStageFour();
    fireEvent.click(await screen.findByRole("button", { name: "确认章节规划" }));
    expect(await screen.findByRole("button", { name: "生成章节正文" })).toBeEnabled();
    expect(calls.filter(({ path, method }) => path === `${planningUrl}/blueprint/confirm` && method === "POST")).toHaveLength(1);
  });
});
