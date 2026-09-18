import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DraftReader } from "./DraftReader";
import { usePreferences } from "../../state/preferences";

afterEach(cleanup);
beforeEach(() => { HTMLElement.prototype.scrollTo = vi.fn(); });
const content = "# Long manuscript title\n\n## Introduction\n\nFirst section.\n\n## Methods\n\nSecond section.";

it("collapses the directory without remounting the manuscript or losing scroll", () => {
  usePreferences.getState().setLanguage("en");
  render(<DraftReader content={content} onChapter={vi.fn()} />);
  const article = screen.getByText("First section.").closest(".draft-long-document")!;
  article.scrollTop = 160;
  fireEvent.click(screen.getByRole("button", { name: "Hide contents" }));
  expect(screen.queryByRole("navigation", { name: "Manuscript contents" })).toBeNull();
  expect(screen.getByRole("button", { name: "Discuss current chapter" })).toBeVisible();
  expect(screen.getByText("First section.").closest(".draft-long-document")).toBe(article);
  expect(article.scrollTop).toBe(160);
  fireEvent.click(screen.getByRole("button", { name: "Show contents" }));
  expect(screen.getByRole("navigation", { name: "Manuscript contents" })).toBeVisible();
});

it("keeps one chapter-only directory and changes the discussion target from it", async () => {
  usePreferences.getState().setLanguage("en");
  const choose = vi.fn();
  render(<DraftReader content={content} onChapter={choose} discussOnSelect />);
  const nav = screen.getByRole("navigation", { name: "Manuscript contents" });
  await waitFor(() => expect(within(nav).getByRole("button", { name: "Methods" })).toBeTruthy());
  expect(within(nav).queryByRole("button", { name: "Long manuscript title" })).toBeNull();
  fireEvent.click(within(nav).getByRole("button", { name: "Methods" }));
  expect(choose).toHaveBeenCalledWith("Methods");
  expect(screen.getAllByRole("navigation")).toHaveLength(1);
});

it("reading navigation does not open a discussion unless requested", async () => {
  const choose = vi.fn();
  render(<DraftReader content={content} onChapter={choose} />);
  const nav = screen.getByRole("navigation", { name: "Manuscript contents" });
  fireEvent.click(await within(nav).findByRole("button", { name: "Methods" }));
  expect(choose).not.toHaveBeenCalled();
  fireEvent.click(within(nav).getByRole("button", { name: "Discuss current chapter" }));
  expect(within(nav).getByRole("button", { name: "Discuss current chapter" })).toHaveClass("button-primary", "draft-discuss-button");
  expect(choose).toHaveBeenCalledWith("Methods");
});
