import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SectionStageActions } from "./SectionStageActions";

const base = {
  current: true,
  active: false,
  resumable: false,
  progress: 4,
  total: 4,
  generating: false,
  regenerating: false,
  confirming: false,
  onGenerate: vi.fn(),
  onRegenerate: vi.fn(),
  onConfirm: vi.fn(),
};

describe("SectionStageActions", () => {
  it("explains omitted sections before the existing confirmation", () => {
    render(<SectionStageActions {...base} pendingHeadings={["Development history"]} />);
    expect(screen.getByRole("status")).toHaveTextContent("本次不包含：Development history");
    fireEvent.click(screen.getByRole("button", { name: "确认并进入图像处理" }));
    expect(base.onConfirm).toHaveBeenCalledOnce();
  });
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("offers a fresh regeneration next to confirmation when drafts are current", () => {
    render(<SectionStageActions {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "重新生成全部章节" }));
    expect(base.onRegenerate).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "重新生成全部章节？" })).toHaveTextContent("后续图像、初稿和终稿可能需要更新");
    fireEvent.click(screen.getByRole("button", { name: "保留当前内容" }));
    expect(base.onRegenerate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重新生成全部章节" }));
    fireEvent.click(screen.getByRole("button", { name: "确认重新生成" }));
    expect(base.onRegenerate).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "确认并进入图像处理" })).toBeEnabled();
  });

  it("prevents confirmation while a replacement generation is active", () => {
    render(<SectionStageActions {...base} active />);
    expect(screen.getByRole("button", { name: "正在重新生成…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认并进入图像处理" })).toBeDisabled();
    expect(screen.getByText("正在重新生成章节")).toBeInTheDocument();
  });

  it("prioritizes resuming retained checkpoints after a partial failure", () => {
    const onGenerate = vi.fn();
    render(<SectionStageActions {...base} resumable progress={4} total={7} onGenerate={onGenerate} />);
    fireEvent.click(screen.getByRole("button", { name: "继续生成失败章节" }));
    expect(onGenerate).toHaveBeenCalledOnce();
    expect(screen.getByText(/已保留 4\/7 个章节/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成全部章节" })).toBeEnabled();
  });
});
