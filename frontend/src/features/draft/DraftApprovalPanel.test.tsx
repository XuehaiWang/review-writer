import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { usePreferences } from "../../state/preferences";
import { DraftApprovalPanel } from "./DraftApprovalPanel";

afterEach(cleanup);
const quality = { current: true, score: 60, blocking_issue_count: 1,
  hard_gate_failures: ["citation_integrity_failed"],
  approval_findings: [{ issue_id: "q1", paragraph_id: "S2-p3", diagnosis: "Source needs review." }] };

it("allows explicit approval with unresolved findings and shows their paragraph", () => {
  usePreferences.getState().setLanguage("zh-CN");
  const onApprove = vi.fn();
  const onReview = vi.fn();
  render(<DraftApprovalPanel quality={quality} approved={false} busy={false} onApprove={onApprove} onReview={onReview} onNext={vi.fn()} />);
  expect(screen.getByText("S2-p3")).toBeInTheDocument();
  expect(screen.getByText("Source needs review.")).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "确认并允许进入终稿" });
  expect(button).toBeEnabled();
  expect(onApprove).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(onApprove).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "查看对应段落" }));
  expect(onReview).toHaveBeenCalledWith("S2-p3");
});

it.each([{ current: false, busy: false }, { current: true, busy: true }])("only disables approval for an actual blocking operation: %j", ({ current, busy }) => {
  usePreferences.getState().setLanguage("en");
  render(<DraftApprovalPanel quality={{ ...quality, current }} approved={false} busy={busy} onApprove={vi.fn()} onReview={vi.fn()} onNext={vi.fn()} />);
  const button = screen.getByRole("button", { name: "Approve and allow final stage" });
  if (busy) expect(button).toBeDisabled();
  else expect(button).toBeEnabled();
});

it("keeps findings visible after approval and exposes Final navigation", () => {
  usePreferences.getState().setLanguage("en");
  const onNext = vi.fn();
  render(<DraftApprovalPanel quality={quality} approved busy={false} onApprove={vi.fn()} onReview={vi.fn()} onNext={onNext} />);
  expect(screen.getByText("Source needs review.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approved" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Enter final stage" }));
  expect(onNext).toHaveBeenCalledOnce();
});
