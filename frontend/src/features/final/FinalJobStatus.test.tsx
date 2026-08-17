import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Job } from "../../api/types";
import { usePreferences } from "../../state/preferences";
import { FinalJobStatus } from "./FinalJobStatus";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-final-1",
    project_id: "project-1",
    scope: "project",
    job_type: "final.overview",
    status: "running",
    result: {},
    progress_current: 2,
    progress_total: 4,
    cancellation_requested: false,
    error_code: "",
    error_message: "",
    retry_of_job_id: null,
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:00:01Z",
    started_at: "2026-08-16T00:00:01Z",
    finished_at: null,
    available_actions: ["cancel"],
    ...overrides,
  };
}

describe("FinalJobStatus", () => {
  afterEach(() => {
    cleanup();
    usePreferences.getState().setLanguage("zh-CN");
  });

  it("shows a determinate overview-generation progress window", () => {
    render(<FinalJobStatus job={job()} />);

    expect(screen.getByText("正在生成并校验总览图")).toBeInTheDocument();
    expect(screen.getByText("步骤 2/4")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "总览图生成进度" })).toHaveAttribute("aria-valuenow", "50");
  });

  it("shows an indeterminate window while a final-build job is being submitted", () => {
    const { container } = render(<FinalJobStatus startingAction="build" />);

    expect(screen.getByText("正在提交任务")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "最终稿生成进度" })).not.toHaveAttribute("aria-valuenow");
    expect(container.querySelector(".final-job-status.indeterminate")).toBeInTheDocument();
  });

  it("explains a completed Word export", () => {
    render(<FinalJobStatus job={job({ job_type: "final.export", status: "succeeded", progress_current: 3, progress_total: 3 })} />);

    expect(screen.getByText("任务已完成")).toBeInTheDocument();
    expect(screen.getByText(/Word 已生成/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Word 生成与下载进度" })).toHaveAttribute("aria-valuenow", "100");
  });

  it("shows a submission failure inside the progress window", () => {
    render(<FinalJobStatus startingAction="build" submissionError={new Error("HTTP 404: Not Found")} />);

    expect(screen.getByText("任务执行失败")).toBeInTheDocument();
    expect(screen.getByText("HTTP 404: Not Found")).toBeInTheDocument();
  });
});
