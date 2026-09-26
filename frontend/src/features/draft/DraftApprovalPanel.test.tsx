import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { usePreferences } from "../../state/preferences";
import { DraftApprovalPanel } from "./DraftApprovalPanel";

afterEach(cleanup);
const quality = { current: true, score: 60, blocking_issue_count: 1,
  hard_gate_failures: ["citation_integrity_failed"],
  approval_findings: [{ issue_id: "q1", paragraph_id: "S2-p3", diagnosis: "Source needs review." }] };

it("explains unresolved findings and links to the affected paragraph without duplicating approval controls", () => {
  usePreferences.getState().setLanguage("zh-CN");
  const onReview = vi.fn();
  render(<DraftApprovalPanel quality={quality} approved={false} onReview={onReview} />);
  expect(screen.getByText("S2-p3")).toBeInTheDocument();
  expect(screen.getByText("Source needs review.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "确认进入终稿" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看对应段落" }));
  expect(onReview).toHaveBeenCalledWith("S2-p3");
});

it.each([{ current: false }, { current: true }])("keeps the explanation available when quality current is $current", ({ current }) => {
  usePreferences.getState().setLanguage("en");
  render(<DraftApprovalPanel quality={{ ...quality, current }} approved={false} onReview={vi.fn()} />);
  expect(screen.getByText("Source needs review.")).toBeInTheDocument();
});

it("keeps findings visible after approval without adding a second Final button", () => {
  usePreferences.getState().setLanguage("en");
  render(<DraftApprovalPanel quality={quality} approved onReview={vi.fn()} />);
  expect(screen.getByText("Source needs review.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Enter final stage" })).not.toBeInTheDocument();
});
