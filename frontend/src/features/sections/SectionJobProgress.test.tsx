import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { usePreferences } from "../../state/preferences";
import { SectionJobProgress } from "./SectionJobProgress";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "section-job-1",
    project_id: "project-1",
    scope: "project",
    job_type: "sections.generate",
    status: "running",
    result: {
      section_progress: {
        phase: "generating",
        current_heading: "Catalyst classes",
        completed_sections: [{ section_id: "S01", heading: "Introduction" }],
      },
    },
    progress_current: 1,
    progress_total: 10,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:01Z",
    started_at: "2026-08-18T00:00:01Z",
    finished_at: null,
    available_actions: ["cancel"],
    ...overrides,
  };
}

describe("SectionJobProgress", () => {
  it("shows drafted chapters while none has finished processing", () => {
    render(<SectionJobProgress job={job({ status: "queued", queue_reason: "model_waiting",
      progress_current: 0, progress_total: 7, result: { section_progress: {
        drafted_section_ids: ["S01", "S02", "S03", "S04"], active_sections: [
          { section_id: "S01", heading: "Introduction", phase: "reviewing" },
        ],
      } } })} />);
    expect(screen.getByText(/正文已生成 4\/7 · 全流程已处理 0\/7/)).toBeInTheDocument();
    expect(screen.getByText(/Introduction · 正文后处理/)).toBeInTheDocument();
  });
  it("explains that a delegated model call frees the writing worker", () => {
    render(<SectionJobProgress job={job({ status: "queued", queue_reason: "model_waiting" })} />);
    expect(screen.getByText("正在等待模型结果")).toBeInTheDocument();
    expect(screen.getByText(/章节写作资源已释放/)).toBeInTheDocument();
  });
  it("explains a delayed provider retry without saying the section failed", () => {
    render(<SectionJobProgress job={job({
      status: "queued", queue_reason: "provider_rate_limit",
      next_run_at: "2026-09-23T12:00:00Z",
    })} />);
    expect(screen.getByText("模型限流，稍后自动继续")).toBeInTheDocument();
    expect(screen.getByText(/已完成的章节会保留/)).toBeInTheDocument();
  });
  it("shows each concurrent chapter's actual model phase", () => {
    render(<SectionJobProgress job={job({ result: { section_progress: { active_sections: [
      { section_id: "S01", heading: "Introduction", phase: "drafting" },
      { section_id: "S02", heading: "Methods", phase: "reviewing" },
    ] } } })} />);
    expect(screen.getByText("正在处理 2 个章节")).toBeInTheDocument();
    expect(screen.getByText(/Introduction · 正在生成正文；Methods · 正在核对来源/)).toBeInTheDocument();
  });
  it("previews retained prose after a sibling failed without treating it as published", () => {
    const failed = job({ status: "failed", result: {
      section_progress: { completed_sections: [{ section_id: "S01", heading: "Introduction" }],
        failed_sections: [{ section_id: "S02", heading: "Methods", error: "Source mismatch" }] },
      section_checkpoint: { entries: {
        S01: { output: { draft_md: "## Introduction\n\nRetained supported prose." } },
        S02: { output: { draft_md: "Rejected prose must stay hidden." } },
      } },
    } });
    const { rerender } = render(<SectionJobProgress job={failed} />);
    expect(screen.getByText("部分结果已保留，任务待继续")).toBeInTheDocument();
    expect(screen.queryByText("Retained supported prose.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Introduction" }));
    expect(screen.getByText("Retained supported prose.")).toBeInTheDocument();
    expect(screen.queryByText("Rejected prose must stay hidden.")).not.toBeInTheDocument();
    expect(screen.getByText(/未发布预览/)).toBeInTheDocument();
    rerender(<SectionJobProgress job={{ ...failed, id: "different-job" }} />);
    expect(screen.queryByText("Retained supported prose.")).not.toBeInTheDocument();
  });

  it("does not call a word-count recommendation an evidence failure", () => {
    usePreferences.getState().setLanguage("en");
    render(<SectionJobProgress job={job({ result: { section_progress: { completed_sections: [
      { section_id: "S01", section_readiness: { status: "evidence_safe_but_shallow" } },
    ] } } })} />);
    expect(screen.getByText("Standard · Usable prose; below target length")).toBeInTheDocument();
  });

  afterEach(() => {
    cleanup();
    usePreferences.getState().setLanguage("zh-CN");
  });

  it("shows every completed chapter immediately", () => {
    usePreferences.getState().setLanguage("zh-CN");
    render(<SectionJobProgress job={job()} />);

    expect(screen.getByText("1/10")).toBeInTheDocument();
    expect(screen.getByText("正在生成：Catalyst classes")).toBeInTheDocument();
    expect(screen.getByText("Introduction")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "章节生成进度" })).toHaveAttribute("aria-valuenow", "10");
  });

  it("shows finalization while the job remains active at the chapter total", () => {
    render(<SectionJobProgress job={job({ progress_current: 10 })} />);
    expect(screen.getByText("章节正文已全部生成")).toBeInTheDocument();
    expect(screen.getByText("正在整理章节报告和图像候选。")).toBeInTheDocument();
  });

  it("distinguishes standard, repaired, and safe fallback sections", () => {
    render(<SectionJobProgress job={job({
      result: {
        section_progress: {
          phase: "generating",
          completed_sections: [
            { section_id: "S01", heading: "Introduction", generation_mode: "standard", section_readiness: { status: "scientific_complete" } },
            { section_id: "S02", heading: "Methods", generation_mode: "evidence_repaired", section_readiness: { status: "needs_evidence_repair" } },
            { section_id: "S03", heading: "Outlook", generation_mode: "safe_evidence_fallback", section_readiness: { status: "provider_fallback" } },
          ],
        },
      },
      progress_current: 3,
    })} />);

    expect(screen.getByText("标准生成 1 · 自动修复 1 · 安全保底 1")).toBeInTheDocument();
    expect(screen.getByText("标准生成 · 科学就绪")).toBeInTheDocument();
    expect(screen.getByText("自动修复 · 需补证据")).toBeInTheDocument();
    expect(screen.getByText("安全保底 · 服务降级保底")).toBeInTheDocument();
  });

  it("shows the specific incomplete section instead of claiming all prose is complete", () => {
    render(<SectionJobProgress job={job({ progress_current: 10, result: { section_progress: {
      phase: "continuing_after_failure", current_heading: "Methods",
      completed_sections: [{ section_id: "S01", heading: "Introduction" }],
      failed_sections: [{ section_id: "S02", heading: "Methods", error: "S02: missing validated evidence for paper-b" }],
    } } })} />);
    expect(screen.queryByText("章节正文已全部生成")).not.toBeInTheDocument();
    expect(screen.getByText("Methods")).toBeInTheDocument();
    expect(screen.queryByText("S02: missing validated evidence for paper-b")).not.toBeInTheDocument();
    expect(screen.getByText("本次操作未完成，请重试；若仍失败，请联系管理员。")).toBeInTheDocument();
    expect(screen.getByText("已保留 1 章的检查点；整批发布前不会替换当前正式版本。")).toBeInTheDocument();
  });

  it("explains an incomplete source check without presenting the section as completed", () => {
    render(<SectionJobProgress job={job({ status: "failed", result: { section_progress: {
      completed_sections: [{ section_id: "S01", heading: "Introduction" }],
      failed_sections: [{ section_id: "S02", heading: "Methods", error: "Source checking is incomplete. Draft and check state were preserved." }],
    } } })} />);
    expect(screen.getByText("该章部分论断尚未通过原文核对；已保存中间结果，可检查证据后继续生成。")).toBeInTheDocument();
    expect(screen.getByText("Methods")).toBeInTheDocument();
    expect(screen.queryByText("章节正文已全部生成")).not.toBeInTheDocument();
  });
});
