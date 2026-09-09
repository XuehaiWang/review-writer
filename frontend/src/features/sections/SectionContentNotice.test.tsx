import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SectionContentNotice } from "./SectionContentNotice";
import { SectionStageActions } from "./SectionStageActions";

afterEach(cleanup);

it("distinguishes binding failures from other omissions and leaves actions enabled", () => {
  render(<>
    <SectionContentNotice section={{ generation_mode: "limited_evidence", depth_diagnostics: { sufficient: false },
      validations: [{ omitted: [{ reason: "missing_or_invalid_source_span" }, { reason: "support_not_established" }] }] }} />
    <SectionStageActions current active={false} resumable={false} progress={1} total={1}
      generating={false} regenerating={false} confirming={false}
      onGenerate={vi.fn()} onRegenerate={vi.fn()} onConfirm={vi.fn()} />
  </>);
  expect(screen.getByText(/1 条论述因来源绑定失败/)).toBeInTheDocument();
  expect(screen.getByText(/1 条论述因来源支持或内容检查问题/)).toBeInTheDocument();
  expect(screen.getByText(/篇幅或讨论结构/)).toBeInTheDocument();
  expect(screen.getByText("查看生成记录").closest("details")).not.toHaveAttribute("open");
  expect(screen.queryByText(/请检查本章/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认并进入图像处理" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "重新生成全部章节" })).toBeEnabled();
});

it("does not invent a warning for complete content", () => {
  render(<SectionContentNotice section={{ generation_mode: "standard", depth_diagnostics: { sufficient: true } }} />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("shows coverage guidance without calling it a binding failure", () => {
  render(<SectionContentNotice section={{ generation_mode: "limited_evidence" }} />);
  expect(screen.getByText(/文献覆盖仍待补充/)).toBeInTheDocument();
  expect(screen.queryByText(/来源绑定失败/)).not.toBeInTheDocument();
});
