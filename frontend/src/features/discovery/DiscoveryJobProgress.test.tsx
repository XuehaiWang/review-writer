import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { usePreferences } from "../../state/preferences";
import { DiscoveryJobProgress } from "./DiscoveryJobProgress";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "discovery-job-1",
    project_id: "project-1",
    scope: "project",
    job_type: "discovery.search",
    status: "running",
    result: {},
    progress_current: 2,
    progress_total: 4,
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

describe("DiscoveryJobProgress", () => {
  afterEach(() => {
    cleanup();
    usePreferences.getState().setLanguage("zh-CN");
  });

  it("shows the live search milestone and determinate progress", () => {
    usePreferences.getState().setLanguage("zh-CN");
    render(<DiscoveryJobProgress job={job()} />);

    expect(screen.getByText("正在分析主题并检索论文")).toBeInTheDocument();
    expect(screen.getByText("步骤 2/4")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "文献检索进度" })).toHaveAttribute("aria-valuenow", "50");
  });

  it("uses an indeterminate bar while the request is being submitted", () => {
    const { container } = render(<DiscoveryJobProgress submitting />);

    expect(screen.getByText("正在提交检索任务")).toBeInTheDocument();
    expect(container.querySelector(".discovery-job-progress.indeterminate")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "文献检索进度" })).not.toHaveAttribute("aria-valuenow");
  });

  it("keeps the completed state visible", () => {
    render(<DiscoveryJobProgress job={job({ status: "succeeded", progress_current: 4 })} />);

    expect(screen.getByText("检索完成")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "文献检索进度" })).toHaveAttribute("aria-valuenow", "100");
  });

  it("states that discovery was not run when credit is insufficient", () => {
    render(<DiscoveryJobProgress job={job({
      status: "failed",
      error_code: "INSUFFICIENT_CREDIT",
      error_message: "余额不足，无法执行本次智能任务。",
      progress_current: 2,
      finished_at: "2026-08-14T00:00:03Z",
    })} />);

    expect(screen.getByText("检索未执行")).toBeInTheDocument();
    expect(screen.getByText(/没有生成新的候选结果/)).toBeInTheDocument();
    expect(screen.queryByText("检索完成")).not.toBeInTheDocument();
  });

  it("shows persisted per-source progress after polling", () => {
    render(<DiscoveryJobProgress job={job({ result: { source_progress: { sources: { crossref: { status: "completed", count: 12 }, openalex: { status: "running", count: 0 }, semantic_scholar: { status: "failed", count: 0 }, arxiv: { status: "completed", count: 5 } } } } })} />);

    expect(screen.getByLabelText("联网来源状态")).toBeInTheDocument();
    expect(screen.getByText("Crossref")).toBeInTheDocument();
    expect(screen.getByText("Semantic Scholar")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
  });

  it("labels online sources as disabled when online search was not requested", () => {
    render(<DiscoveryJobProgress job={job({ result: { source_progress: { sources: { crossref: { status: "disabled", count: 0 } } } } })} />);

    expect(screen.getByText("未启用")).toBeInTheDocument();
  });
});
