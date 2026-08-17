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
});
