import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CandidateComparison, type DialogueCandidate } from "./ParagraphDialogue";
afterEach(cleanup);
const candidate: DialogueCandidate = { candidate_id: "c1", paragraph_key: "k1", paragraph_id: "S1-p1", original_text: "Original text", candidate_text: "Revised text", reply: "Explanation", status: "pending" };
it("unmatched source markers show readable advice without blocking author edits", () => {
  const decide = vi.fn();
  render(<CandidateComparison candidate={{ ...candidate, evidence_review: "references_unverified", validation_warnings: ["unmatched_source_references"], scientific_changes: [{ field: "numbers", before: ["1"], after: ["2"] }] }} decide={decide} />);
  expect(screen.getByText("Explanation")).toBeVisible();
  fireEvent.click(screen.getByText(/查看依据与检查详情|Evidence and check details/));
  expect(screen.getByText(/回复已保留|The reply is retained/)).toBeVisible();
  expect(screen.getByText(/1 → 2/)).toBeVisible();
  expect(screen.queryByText("unmatched_source_references")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /保存候选|Save candidate/ }));
  expect(decide).toHaveBeenCalledWith("c1", "accept");
});
it("shows comparison without scores and requires explicit acceptance", () => {
  const decide = vi.fn(); render(<CandidateComparison candidate={candidate} decide={decide} />);
  expect(screen.getByText("Original text")).toBeTruthy();
  expect(screen.getByText("Revised text")).toBeTruthy();
  expect(screen.queryByText(/分数|Score/)).toBeNull();
  expect(decide).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /保存候选|Save candidate/ }));
  expect(decide).toHaveBeenCalledWith("c1", "accept");
});
it("historical candidates cannot be accepted again", () => {
  render(<CandidateComparison candidate={{ ...candidate, status: "stale" }} decide={vi.fn()} />);
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.getByText("Revised text")).toBeTruthy();
});
it("scientific differences without supporting passages remain saveable", () => {
  const decide = vi.fn();
  render(<CandidateComparison candidate={{ ...candidate, evidence_review: "author_review_required", scientific_changes: [{ field: "chemical_identities", before: ["cubr"], after: ["cubr2"] }] }} decide={decide} />);
  expect(screen.getByText(/cubr → cubr2/)).toBeTruthy();
  expect(screen.getByText(/未提供对应支持证据|No supporting passage/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /保存候选|Save candidate/ }));
  expect(decide).toHaveBeenCalledWith("c1", "accept");
});
it("technical failures do not display the model's misleading success claim", () => {
  render(<CandidateComparison candidate={{ ...candidate, candidate_text: "", status: "validation_failed", reply: "Successfully updated!", validation_errors: ["protected_images_changed"] }} decide={vi.fn()} />);
  expect(screen.queryByText("Successfully updated!")).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("protected_images_changed");
  expect(screen.queryByRole("button", { name: /保存候选|Save candidate/ })).toBeNull();
});
