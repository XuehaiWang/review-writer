import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SectionContentNotice } from "./SectionContentNotice";
import { SectionStageActions } from "./SectionStageActions";

afterEach(cleanup);

it("does not equate a table preference with a generated comparison", () => {
  render(<SectionContentNotice section={{ paper_presentation: [
    { paper_id: "P1", requested: "table", actual: "cited_in_prose", table_fallback: true },
  ] }} />);
  expect(screen.getByText(/暂未形成可导出的比较表记录/)).toBeInTheDocument();
  expect(screen.getByText(/已有正文引用仍保留/)).toBeInTheDocument();
});

it("shows semantic organization issues without labelling them evidence failures", () => {
  render(<SectionContentNotice section={{ narrative_diagnostics: {
    review_status: "needs_revision", issues: ["Explain why the next example follows."] } }} />);
  expect(screen.getByText(/建议优化表达与论证组织/)).toBeInTheDocument();
  expect(screen.getByText("Explain why the next example follows.")).toBeInTheDocument();
  expect(screen.queryByText(/来源绑定失败/)).not.toBeInTheDocument();
});

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
  expect(screen.getByText(/历史记录未区分自动处理与证据缺口/)).toBeInTheDocument();
  expect(screen.queryByText(/来源绑定失败/)).not.toBeInTheDocument();
});

it("shows handled omissions as a neutral record instead of a section-wide warning", () => {
  render(<SectionContentNotice section={{ generation_mode: "limited_evidence",
    primary_papers: ["P1"], paragraphs: [{ cited_paper_ids: ["P1"] }],
    validations: [{ omitted: [{ reason: "missing_or_invalid_source_span" }] }],
  }} />);
  expect(screen.getByText(/已自动排除 1 条/)).toBeInTheDocument();
  expect(screen.getByRole("status")).not.toHaveClass("message-warning");
  expect(screen.queryByText(/部分内容存在限制/)).not.toBeInTheDocument();
});

it("does not hide missing primary papers behind handled omissions", () => {
  render(<SectionContentNotice section={{ generation_mode: "limited_evidence",
    primary_papers: ["P1", "P2"], paragraphs: [{ cited_paper_ids: ["P1"] }],
    validations: [{ omitted: [{ reason: "missing_or_invalid_source_span" }] }],
  }} />);
  expect(screen.getByText(/1 篇主要论文尚未/)).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveClass("message-warning");
});

it("retains warnings for unresolved support and evidence-pending sections", () => {
  render(<SectionContentNotice section={{ generation_mode: "pending_evidence",
    validations: [{ unresolved: [{}] }] }} />);
  expect(screen.getByText(/尚缺可用于成文的证据/)).toBeInTheDocument();
  expect(screen.getByText(/1 项来源支持仍未确认/)).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveClass("message-warning");
});
