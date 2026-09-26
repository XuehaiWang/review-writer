import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { LibraryPaper } from "../../api/types";
import { PaperListItem } from "./LibraryPage";

afterEach(cleanup);

const paper: LibraryPaper = {
  id: "id", paper_id: "P001", title: "A paper", authors: ["A. Author"], keywords: [],
  tags: {}, original_filename: "paper.pdf", content_sha256: "hash", artifact_ids: {}, updated_at: "",
};

it("shows green for complete content even when human review is still pending", () => {
  render(<PaperListItem paper={{ ...paper, content_complete: true, content_missing_fields: [], needs_human_check: true }} displayLabel="P001" selected={false} onSelect={() => {}} />);
  const dot = screen.getByRole("img", { name: "论文内容齐全" });
  expect(dot).toHaveClass("status-dot", "ok");
});

it("shows yellow with the missing fields even after human review", () => {
  render(<PaperListItem paper={{ ...paper, content_complete: false, content_missing_fields: ["abstract", "fulltext"], human_review_status: "reviewed" }} displayLabel="P001" selected={false} onSelect={() => {}} />);
  const dot = screen.getByRole("img", { name: "内容待补全：摘要、解析正文" });
  expect(dot).toHaveClass("status-dot", "warning");
});
