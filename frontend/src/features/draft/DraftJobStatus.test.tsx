import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { usePreferences } from "../../state/preferences";
import { DraftJobStatus } from "./DraftJobStatus";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    project_id: "project-1",
    scope: "project",
    job_type: "draft.evaluate",
    status: "running",
    result: {},
    progress_current: 1,
    progress_total: 3,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:01Z",
    started_at: "2026-08-14T00:00:01Z",
    finished_at: null,
    available_actions: ["cancel"],
    ...overrides,
  };
}

describe("DraftJobStatus", () => {
  afterEach(() => {
    cleanup();
    usePreferences.getState().setLanguage("zh-CN");
  });

  it("shows the live evaluation milestone and an accessible progress bar", () => {
    usePreferences.getState().setLanguage("zh-CN");
    render(<DraftJobStatus job={job()} />);

    expect(screen.getByText("正在评估当前初稿")).toBeInTheDocument();
    expect(screen.getByText("步骤 1/3")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "初稿评估进度" })).toHaveAttribute("aria-valuenow", "33");
  });

  it("uses an indeterminate status while the evaluation request is starting", () => {
    const { container } = render(<DraftJobStatus startingType="draft.evaluate" />);

    expect(screen.getByText("正在启动评估")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "初稿评估进度" })).not.toHaveAttribute("aria-valuenow");
    expect(container.querySelector(".draft-job-status.indeterminate")).toBeInTheDocument();
  });

  it("shows completion and fills the progress bar", () => {
    render(<DraftJobStatus job={job({ status: "succeeded", progress_current: 3 })} />);

    expect(screen.getByText("初稿评估完成")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "初稿评估进度" })).toHaveAttribute("aria-valuenow", "100");
  });

  it("shows a synchronization state until the published revision is visible", () => {
    render(<DraftJobStatus
      job={job({ status: "succeeded", job_type: "draft.optimize", progress_current: 3 })}
      publicationPending
    />);

    expect(screen.getByText("正在同步最新结果")).toBeInTheDocument();
    expect(screen.getByText("任务已完成，正在重新读取已发布的初稿与评估。")).toBeInTheDocument();
  });

  it("explains when optimization restores the best draft without a net text change", () => {
    render(<DraftJobStatus job={job({
      status: "succeeded",
      job_type: "draft.optimize",
      progress_current: 5,
      progress_total: 5,
      result: {
        draft_changed: false,
        rewrite_accepted: 21,
        rewrite_rejected: 4,
        score: 76.59,
        feedback_status: {
          phase: "plateau",
          best_score: 76.59,
          best_score_restored: true,
        },
      },
    })} />);

    expect(screen.getByText("优化完成，正文未改变")).toBeInTheDocument();
    expect(screen.getByText(/21 次候选通过单次安全校验、4 次被拒绝/)).toBeInTheDocument();
    expect(screen.getByText(/最佳分数 76.59/)).toBeInTheDocument();
  });

  it("shows the current paragraph during batch safe optimization", () => {
    render(<DraftJobStatus job={job({
      job_type: "draft.optimize",
      progress_current: 3,
      progress_total: 5,
      result: { feedback_status: { phase: "rewriting", iteration: 2, max_iterations: 3, rewrite_completed: 1, rewrite_total: 4, current_paragraph_id: "sec2-p3" } },
    })} />);

    expect(screen.getByText("正在批量安全优化")).toBeInTheDocument();
    expect(screen.getByText(/第 2\/3 轮：安全重写 1\/4，当前 sec2-p3/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "批量优化进度" })).toHaveAttribute("aria-valuenow", "60");
  });

  it("reports deferred paragraphs without presenting the whole batch as failed", () => {
    render(<DraftJobStatus job={job({
      status: "succeeded",
      job_type: "draft.optimize",
      progress_current: 5,
      progress_total: 5,
      result: {
        draft_changed: false,
        rewrite_deferred: 2,
        feedback_status: {
          phase: "provider_deferred",
          rewrite_deferred: 2,
          deferred_paragraph_ids: ["S02-p5", "S03-p1"],
        },
      },
    })} />);

    expect(screen.getByText("优化完成，正文未改变")).toBeInTheDocument();
    expect(screen.getByText(/2 个段落因服务商暂时不可用而待重试/)).toBeInTheDocument();
    expect(screen.getByText(/S02-p5、S03-p1/)).toBeInTheDocument();
  });

  it("shows live scoring batch and paragraph progress", () => {
    usePreferences.getState().setLanguage("en");
    render(<DraftJobStatus job={job({
      result: {
        feedback_status: {
          phase: "scoring",
          scoring_batch_completed: 1,
          scoring_batch_total: 4,
          paragraph_completed: 8,
          paragraph_total: 30,
        },
      },
    })} />);

    expect(screen.getByText("Scoring batch 2/4; 8/30 paragraphs completed.")).toBeInTheDocument();
  });

  it("shows that accepting a candidate saves its precomputed score", () => {
    render(<DraftJobStatus job={job({
      job_type: "draft.accept-rewrite",
      progress_current: 1,
      progress_total: 2,
    })} />);

    expect(screen.getByText("正在保存已评分候选")).toBeInTheDocument();
    expect(screen.getByText(/不再调用模型/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "候选保存进度" })).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows that candidate scoring happens before human review", () => {
    render(<DraftJobStatus job={job({
      job_type: "draft.rewrite",
      progress_current: 2,
      progress_total: 4,
    })} />);

    expect(screen.getByText("正在生成并评分候选")).toBeInTheDocument();
    expect(screen.getByText(/随后只评分该候选段落/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "候选生成与评分进度" })).toHaveAttribute("aria-valuenow", "50");
  });
});
