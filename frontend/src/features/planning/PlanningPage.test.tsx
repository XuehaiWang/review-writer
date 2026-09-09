import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Job } from "../../api/types";
import { usePreferences } from "../../state/preferences";
import { PlanningPage } from "./PlanningPage";

vi.mock("../../components/ProjectSelector", () => ({
  ProjectSelector: () => null,
  useSelectedProject: () => ({ selected: { project_id: "project-1" } }),
}));

const planningPath = "/api/v1/projects/project-1/planning";
const clients: QueryClient[] = [];
let payload: Record<string, unknown>;
let requests: Array<{ path: string; init: RequestInit }>;
let applyOutline: (body: { revision: number; outline_style: string }) => Promise<Response>;

function basicBlueprint() {
  return {
    blueprint_revision: 6,
    blueprint_artifact_id: "basic-blueprint",
    blueprint_current: true,
    section_blueprint: {
      academic_planning: { status: "completed", incomplete_sections: [] },
      sections: [{
        section_id: "S01", title: "Evidence theme", section_role: "body",
        primary_papers: ["P001"], required_fact_roles: ["finding"],
        paper_roles: [{ paper_id: "P001", role: "foundation", reason: "Introduces the reference method.", claim_ids: ["S01-SC001"] }],
        coverage_by_use: [{ use_id: "S01-SC001", purpose: "Which result is established?", status: "supported", missing_requirements: [] }],
        depth_contract: { target_paragraph_count: 4, target_word_min: 600, target_word_max: 900, minimum_comparison_paragraphs: 0 },
        generation_eligible: true, executable_claim_count: 1, pending_claim_count: 1,
        automatic_resolution: { action: "targeted_fact_repair_before_writing", requires_user_action: false },
        scientific_claims: [
          {
            claim_id: "S01-SC001", proposition: "The study reports a bounded result.",
            allowed_assertion: "The study reports a bounded result.", support_status: "supported",
            primary_papers: ["P001"], fact_ids: ["MF-1"], evidence_refs: [{ evidence_key: "sha256:a" }],
          },
          {
            claim_id: "S01-SC002", proposition: "Pending mechanism evidence.",
            required_for_section: false,
            support_status: "missing", primary_papers: ["P001"], required_fact_roles: ["mechanism"],
          },
        ],
      }],
    },
  };
}

function enhancementJob(status: Job["status"]): Job {
  return {
    id: "enhancement-1", project_id: "project-1", scope: "project", job_type: "planning.blueprint",
    status, result: {}, progress_current: 0, progress_total: 3, cancellation_requested: false,
    error_code: "", error_message: "", retry_of_job_id: null, created_at: "", updated_at: "",
    started_at: null, finished_at: null, available_actions: [],
  };
}

function renderPlanning(tab = "matrix") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={[`/planning?project=project-1&tab=${tab}`]}>
      <Routes>
        <Route path="/planning" element={<PlanningPage />} />
        <Route path="/sections" element={<h1>Section writing</h1>} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>);
}

describe("default argument planning and candidate confirmation", () => {
  beforeEach(() => {
    usePreferences.setState({ language: "zh-CN" });
    payload = {
      matrix_revision: 14, blueprint_revision: 5, outline_current: true,
      selected_outline_md: "## Evidence theme", literature_matrix: { rows: [] },
    };
    requests = [];
    applyOutline = async (body) => {
      payload = { ...payload, matrix_revision: body.revision + 1, outline_current: true,
        outline_selection: { outline_style: body.outline_style }, selected_outline_md: "## Applied section" };
      return Response.json(payload);
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init = {}) => {
      const path = String(input);
      if (["POST", "PUT"].includes(init.method || "")) requests.push({ path, init });
      if (path === `${planningPath}/outline` && init.method === "PUT") return applyOutline(JSON.parse(String(init.body)));
      if (path === `${planningPath}/blueprint`) payload = { ...payload, ...basicBlueprint() };
      if (path === `${planningPath}/blueprint/jobs`) {
        const job = enhancementJob("queued");
        payload = { ...payload, blueprint_jobs: [job] };
        return Response.json(job, { status: 202 });
      }
      if (path === "/api/v1/jobs/enhancement-1/cancel") {
        payload = { ...payload, blueprint_jobs: [enhancementJob("cancelled")] };
        return Response.json(enhancementJob("cancelled"));
      }
      if ([planningPath, `${planningPath}/blueprint`, `${planningPath}/blueprint/confirm`].includes(path)) {
        return Response.json(payload);
      }
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  afterEach(() => {
    cleanup();
    clients.splice(0).forEach((client) => client.clear());
    vi.restoreAllMocks();
  });

  it("queues default planning and confirms the completed candidate artifact", async () => {
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "生成章节规划" }));
    await screen.findByRole("button", { name: "停止生成" });
    expect(requests.map(({ path }) => path)).toEqual([`${planningPath}/blueprint/jobs`]);
    expect(JSON.parse(String(requests[0].init.body))).toEqual({ revision: 5 });
    expect(new Headers(requests[0].init.headers).get("Idempotency-Key")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "增强章节论证（可选）" })).not.toBeInTheDocument();
    payload = { ...payload, ...basicBlueprint(), blueprint_candidate_pending: true, blueprint_jobs: [enhancementJob("succeeded")] };
    await act(async () => { await clients[0].invalidateQueries(); });
    const confirm = await screen.findByRole("button", { name: "确认并进入章节" });
    await waitFor(() => expect(confirm).toBeEnabled());
    fireEvent.click(confirm);
    expect(await screen.findByRole("heading", { name: "Section writing" })).toBeInTheDocument();
    expect(JSON.parse(String(requests[1].init.body))).toEqual({ revision: 6, artifact_id: "basic-blueprint" });
  });

  it("shows paper roles and evidence coverage without requiring a fixed comparison count", async () => {
    payload = { ...payload, ...basicBlueprint() };
    renderPlanning("blueprint");
    expect(await screen.findByRole("heading", { name: "论文在本章中的作用" })).toBeInTheDocument();
    expect(screen.getByText(/Introduces the reference method/, { selector: "li" })).toBeInTheDocument();
    expect(screen.getByText(/所需证据已覆盖/)).toBeInTheDocument();
    expect(screen.getByText(/支撑材料 · 待正文核实/)).toBeInTheDocument();
    expect(screen.queryByText(/至少.*个比较段落/)).not.toBeInTheDocument();
  });

  it("combines completed status and actions, with unused papers collapsed", async () => {
    const basic = basicBlueprint();
    payload = { ...payload, ...basic, blueprint_candidate_pending: true,
      blueprint_jobs: [enhancementJob("succeeded")],
      section_blueprint: { ...basic.section_blueprint,
        sections: [{ ...basic.section_blueprint.sections[0], single_paper_policy: { mode: "source_bounded_case_analysis" } }],
        unused_papers: [{ paper_id: "P002", reason_code: "outside_scope", reason: "Outside the selected review scope." }],
      },
    };
    renderPlanning("blueprint");
    expect(await screen.findByText("章节规划已完成 · 1 个章节")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "重新生成" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "确认并进入章节" })).toBeEnabled();
    const disclosure = screen.getByText("1 篇论文未纳入本次综述 · 查看原因").closest("details")!;
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(disclosure.querySelector("summary")!);
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByText(/Outside the selected review scope/)).toBeVisible();
    expect(screen.queryByText(/单篇证据章节已自动处理|章节论证规划|succeeded/)).not.toBeInTheDocument();
    expect(screen.getByText(/1 篇主要论文/)).toBeInTheDocument();
  });

  it("opens an existing plan from Matrix without generating another job", async () => {
    payload = { ...payload, ...basicBlueprint() };
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "查看章节规划" }));
    expect(await screen.findByRole("button", { name: "确认并进入章节" })).toBeEnabled();
    expect(requests).toEqual([]);
  });

  it("uses one continuation action with current inputs and shows the failed reason", async () => {
    const basic = basicBlueprint();
    payload = { ...payload, ...basic, blueprint_jobs: [{ ...enhancementJob("failed"), available_actions: ["retry"], error_message: "Source lookup temporarily unavailable." }],
      section_blueprint: { ...basic.section_blueprint, academic_planning: { status: "incomplete", incomplete_sections: ["S01"] } },
    };
    renderPlanning("blueprint");
    const resume = await screen.findByRole("button", { name: "继续生成" });
    expect(screen.getAllByRole("button", { name: "继续生成" })).toHaveLength(1);
    expect(screen.getByText("Source lookup temporarily unavailable.")).toBeVisible();
    fireEvent.click(resume);
    await screen.findByRole("button", { name: "停止生成" });
    expect(requests.map(({ path }) => path)).toEqual([`${planningPath}/blueprint/jobs`]);
    expect(JSON.parse(String(requests[0].init.body))).toEqual({ revision: 6 });
  });

  it("shows outline application progress and loads the saved sections", async () => {
    const apply = applyOutline;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    applyOutline = async (body) => { await pending; return apply(body); };
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "大纲选择与上传" }));
    fireEvent.click(screen.getAllByRole("button", { name: "使用此结构" })[0]);
    expect(await screen.findByRole("button", { name: "正在应用…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存大纲" })).toBeDisabled();
    await act(async () => { release(); });
    expect(await screen.findByText("大纲已应用，章节已载入下方编辑器。")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "章节标题" })).toHaveValue("Applied section");
    expect(screen.getByRole("button", { name: "当前选择" })).toBeDisabled();
    expect(JSON.parse(String(requests[0].init.body))).toEqual({ revision: 14, outline_style: "substrate" });
  });

  it("shows a conflict, refreshes the revision, and retries only on another click", async () => {
    const apply = applyOutline;
    applyOutline = async (body) => {
      if (body.revision !== 14) return apply(body);
      payload = { ...payload, matrix_revision: 15 };
      return Response.json({ error: { code: "STATE_CONFLICT", message: "Workflow stage changed since it was loaded." } }, { status: 409 });
    };
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "大纲选择与上传" }));
    fireEvent.click(screen.getAllByRole("button", { name: "使用此结构" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Workflow stage changed since it was loaded.");
    expect(screen.getByRole("alert")).toHaveTextContent("请重新点击需要的大纲");
    expect(requests).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "章节标题" })).toHaveValue("Evidence theme");
    fireEvent.click(screen.getAllByRole("button", { name: "使用此结构" })[0]);
    await screen.findByText("大纲已应用，章节已载入下方编辑器。");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(JSON.parse(String(requests[1].init.body)).revision).toBe(15);
  });

  it("shows non-conflict failures instead of silently leaving the outline unchanged", async () => {
    applyOutline = async () => Response.json({ detail: "The outline could not be loaded." }, { status: 422 });
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "大纲选择与上传" }));
    fireEvent.click(screen.getAllByRole("button", { name: "使用此结构" })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("The outline could not be loaded.");
    expect(screen.getByRole("textbox", { name: "章节标题" })).toHaveValue("Evidence theme");
    expect(screen.getAllByRole("button", { name: "使用此结构" })[0]).toBeEnabled();
  });

  it("allows reapplying the same recommended outline after Matrix changes", async () => {
    payload = { ...payload, outline_current: false, outline_selection: { outline_style: "topic-guided" },
      outline_candidates: [{ source: "topic", outline_style: "topic-guided", title: "Recommended" }] };
    renderPlanning();
    fireEvent.click(await screen.findByRole("button", { name: "大纲选择与上传" }));
    const choose = screen.getByRole("button", { name: "使用推荐大纲" });
    expect(choose).toBeEnabled();
    fireEvent.click(choose);
    await screen.findByText("大纲已应用，章节已载入下方编辑器。");
    expect(screen.getByRole("button", { name: "当前推荐大纲" })).toBeDisabled();
  });

  it("prevents duplicate generation while the default planner is running", async () => {
    payload.blueprint_jobs = [enhancementJob("running")];
    renderPlanning();
    expect(await screen.findByRole("button", { name: "停止生成" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "生成章节规划" })).not.toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "章节规划进度" })).toBeInTheDocument();
    expect(requests).toEqual([]);
  });

  it("allows cancellation and retains the completed candidate", async () => {
    payload = { ...payload, ...basicBlueprint() };
    renderPlanning("blueprint");
    fireEvent.click(await screen.findByRole("button", { name: "重新生成" }));
    const cancel = await screen.findByRole("button", { name: "停止生成" });
    expect(screen.getByRole("button", { name: "确认并进入章节" })).toBeDisabled();
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "确认并进入章节" })).toBeEnabled();
    expect(requests[1].path).toBe("/api/v1/jobs/enhancement-1/cancel");
  });

  it.each(["failed", "cancelled", "succeeded"] as const)("keeps an incomplete argument candidate unconfirmed after %s", async (status) => {
    const basic = basicBlueprint();
    payload = { ...payload, ...basic, blueprint_jobs: [enhancementJob(status)], section_blueprint: {
      ...basic.section_blueprint, academic_planning: { status: "incomplete", incomplete_sections: ["S01"] },
    } };
    renderPlanning("blueprint");
    expect(await screen.findByRole("button", { name: "确认并进入章节" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "继续生成" })).toBeEnabled();
    expect(requests).toEqual([]);
  });

  it.each([
    { name: "outdated outline", patch: { outline_current: false } },
  ])("preserves the generation gate for $name", async ({ patch }) => {
    payload = { ...payload, ...patch };
    renderPlanning();
    const generate = await screen.findByRole("button", { name: "生成章节规划" });
    expect(generate).toBeDisabled();
    fireEvent.click(generate);
    expect(requests).toEqual([]);
  });

  it("allows provisional planning and confirmation despite evidence and taxonomy warnings", async () => {
    const basic = basicBlueprint();
    payload = { ...payload, ...basic, matrix_enrichment: { planning_blocked: true },
      taxonomy_diagnostics: { can_confirm: false }, scope_diagnostics: { can_confirm: false },
      section_blueprint: { ...basic.section_blueprint, sections: [{ section_id: "S01", title: "Provisional theme",
        planning_status: "planned", thesis_status: "provisional", scientific_thesis: { text: "Explore the theme" }, scientific_claims: [] }] } };
    renderPlanning("blueprint");
    expect(await screen.findByRole("button", { name: "确认并进入章节" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled();
    expect(screen.getByText("写作目标")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认并进入章节" }));
    await screen.findByRole("heading", { name: "Section writing" });
  });

  it("can plan while a recoverable historical fact job runs", async () => {
    payload = { ...payload, matrix_enrichment: { jobs: [{ ...enhancementJob("running"), job_type: "matrix.enrich" }] } };
    renderPlanning();
    expect(await screen.findByRole("button", { name: "生成章节规划" })).toBeEnabled();
    expect(requests).toEqual([]);
  });

  it("shows chapter questions and retrieval directions", async () => {
    const basic = basicBlueprint();
    payload = { ...payload, ...basic, section_blueprint: { ...basic.section_blueprint, sections: [{
      section_id: "S01", title: "Conditions", thesis_status: "provisional", writing_objective: "Compare conditions.",
      questions_to_answer: ["Which temperatures were tested?"], retrieval_directions: ["Temperature and reaction time"],
    }] } };
    renderPlanning("blueprint");
    expect((await screen.findAllByText("Compare conditions.")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Which temperatures were tested?").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Temperature and reaction time").length).toBeGreaterThan(0);
  });

  it.each([
    { phase: "structure", active_sections: [], message: "正在组织全篇结构" },
    { phase: "sections", active_sections: ["S02", "S03"], message: "正在规划章节：S02、S03" },
  ])("shows $phase planning progress", async ({ phase, active_sections, message }) => {
    payload = { ...payload, blueprint_jobs: [{ ...enhancementJob("running"),
      result: { blueprint_progress: { phase, active_sections } } }] };
    renderPlanning("blueprint");
    expect(await screen.findByText(message)).toBeInTheDocument();
  });

  it("shows integrated fact analysis as the first chapter-planning phase", async () => {
    payload = { ...payload, blueprint_jobs: [{ ...enhancementJob("running"),
      progress_current: 2, progress_total: 5,
      result: { planning_pipeline: { phase: "fact_enrichment" } } }] };
    renderPlanning("blueprint");
    expect(await screen.findByText("正在核验当前主题需要的事实依据")).toBeInTheDocument();
    expect(screen.getByText("2/5 篇论文已分析")).toBeInTheDocument();
  });
});
