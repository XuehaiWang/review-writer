import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePreferences } from "../../state/preferences";
import { PendingRewrite } from "./DraftPage";

describe("PendingRewrite", () => {
  afterEach(() => {
    cleanup();
    usePreferences.getState().setLanguage("zh-CN");
  });

  it("shows the candidate score before the user decides to save it", () => {
    const decide = vi.fn();
    render(<PendingRewrite
      candidate={{
        candidate_id: "candidate-1",
        paragraph_id: "p1",
        original_text: "Original paragraph.",
        candidate_text: "Improved paragraph.",
        status: "pending",
        source_paragraph_score: 61.25,
        candidate_paragraph_score: 91.5,
        evidence_repair_preview: {
          added_evidence_count: 2,
          downgraded_claim_count: 1,
        },
      }}
      decide={decide}
      disabled={false}
    />);

    expect(screen.getByText("61.3")).toBeInTheDocument();
    expect(screen.getByText("91.5")).toBeInTheDocument();
    expect(screen.getByText("候选已自动完成单段评分")).toBeInTheDocument();
    expect(screen.getByText(/不再调用模型复评/)).toBeInTheDocument();
    expect(screen.getByText(/新增 2 条本地全文证据/)).toBeInTheDocument();
    expect(screen.getByText(/记录 1 条证据不足声明/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "保存此候选" }));
    expect(decide).toHaveBeenCalledWith("candidate-1", "accept");
  });

  it("keeps manual source confirmation visible for a style-only candidate", () => {
    render(<PendingRewrite
      candidate={{
        candidate_id: "candidate-2",
        paragraph_id: "p2",
        original_text: "Original paragraph.",
        candidate_text: "Clearer paragraph.",
        status: "pending",
        source_paragraph_score: 55,
        candidate_paragraph_score: 64,
        route: "human_confirmation",
        rewrite_mode: "human_review_style_only",
        requires_manual_confirmation: true,
      }}
      decide={vi.fn()}
      disabled={false}
    />);

    expect(screen.getByText(/仍需人工核对/)).toBeInTheDocument();
    expect(screen.getByText(/不能视为已完成来源确认/)).toBeInTheDocument();
  });
});
