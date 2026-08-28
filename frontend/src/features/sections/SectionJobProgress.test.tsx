import { cleanup, render, screen } from "@testing-library/react";
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
            { section_id: "S01", heading: "Introduction", generation_mode: "standard" },
            { section_id: "S02", heading: "Methods", generation_mode: "evidence_repaired" },
            { section_id: "S03", heading: "Outlook", generation_mode: "safe_evidence_fallback" },
          ],
        },
      },
      progress_current: 3,
    })} />);

    expect(screen.getByText("标准生成 1 · 自动修复 1 · 安全保底 1")).toBeInTheDocument();
    expect(screen.getByText("安全保底")).toBeInTheDocument();
  });
});
