import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OptimizationProposalReview } from "./DraftPage";

afterEach(cleanup);

describe("OptimizationProposalReview", () => {
  it("selects related paragraphs together and displays argument changes", () => {
    const decide = vi.fn();
    render(<OptimizationProposalReview disabled={false} decide={decide} proposal={{
      proposal_id: "joint", source_score: 80, candidate_score: 87, status: "pending", created_at: "now",
      changes: ["a-p1", "a-p2"].map(paragraph_id => ({ paragraph_id, original_text: "Old " + paragraph_id,
        candidate_text: "New " + paragraph_id, group_id: "linked",
        argument_revisions: [{ claim_id: "C", original_proposition: "Broad", proposition: "Supported scope", reason: "Local evidence" }],
      })),
    }} />);
    expect(screen.getAllByText(/Supported scope/)).toHaveLength(2);
    fireEvent.click(screen.getByRole("checkbox", { name: /a-p1/ }));
    expect(screen.getByRole("checkbox", { name: /a-p2/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: /a-p2/ }));
    expect(screen.getByRole("checkbox", { name: /a-p1/ })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "保存全部优化" }));
    expect(decide).toHaveBeenCalledWith("joint", "accept", ["a-p1", "a-p2"]);
  });
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
    expect(screen.getByText("71.5 → 89.3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存全部优化" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "accept", ["sec1-p1", "sec2-p3"]);
    fireEvent.click(screen.getByRole("checkbox", { name: /sec2-p3/ }));
    expect(screen.getByText("71.5 → 80.3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存选中的 1 段" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "accept", ["sec1-p1"]);
    fireEvent.click(screen.getByRole("button", { name: "放弃本批" }));
    expect(decide).toHaveBeenCalledWith("proposal-1", "reject");
  });
});
