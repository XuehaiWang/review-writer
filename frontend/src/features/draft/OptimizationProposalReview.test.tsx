import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OptimizationProposalReview } from "./DraftPage";

describe("OptimizationProposalReview", () => {
  it("shows every paragraph comparison and defers publication to the user", () => {
    const decide = vi.fn();
    render(<OptimizationProposalReview
      proposal={{
        proposal_id: "proposal-1",
        source_score: 71.5,
        candidate_score: 89.25,
        status: "pending",
        created_at: "2026-08-15T00:00:00Z",
        changes: [
          { paragraph_id: "sec1-p1", original_text: "Original one.", candidate_text: "Improved one.", source_paragraph_score: 70, candidate_paragraph_score: 88, overall_score_delta: 8.75 },
          { paragraph_id: "sec2-p3", original_text: "Original two.", candidate_text: "Improved two.", source_paragraph_score: 72, candidate_paragraph_score: 90, overall_score_delta: 9 },
        ],
      }}
      decide={decide}
      disabled={false}
    />);

    expect(screen.getByText("sec1-p1")).toBeInTheDocument();
    expect(screen.getByText("Original one.")).toBeInTheDocument();
    expect(screen.getByText("Improved two.")).toBeInTheDocument();
    expect(screen.getByText("70.0 → 88.0")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存全部优化" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "accept", ["sec1-p1", "sec2-p3"]);
    fireEvent.click(screen.getByRole("checkbox", { name: /sec2-p3/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存选中的 1 段" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "accept", ["sec1-p1"]);
    fireEvent.click(screen.getByRole("button", { name: "放弃本批" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "reject");
  });
});
