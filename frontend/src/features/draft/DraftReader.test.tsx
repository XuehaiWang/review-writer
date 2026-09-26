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

it("marks only chapters with unsaved paragraphs and clears the dot after saving", async () => {
  usePreferences.getState().setLanguage("en");
  const manuscript = <article><h2>Introduction</h2><section data-paragraph-key="intro-p1">Intro text</section>
    <h2>Methods</h2><h3>Catalysis</h3><section data-paragraph-key="method-p1">Method text</section></article>;
  const { rerender } = render(<DraftReader content={content} dirtyParagraphKeys={new Set(["method-p1"])}>{manuscript}</DraftReader>);
  const nav = screen.getByRole("navigation", { name: "Manuscript contents" });
  const methods = await within(nav).findByRole("button", { name: "Methods · Unsaved changes" });
  expect(methods).toHaveClass("draft-toc-pending");
  expect(within(nav).getByRole("button", { name: "Catalysis · Unsaved changes" })).toHaveClass("draft-toc-pending");
  expect(within(nav).getByRole("button", { name: "Introduction" })).not.toHaveClass("draft-toc-pending");
  rerender(<DraftReader content={content} dirtyParagraphKeys={new Set()}>{manuscript}</DraftReader>);
  await waitFor(() => expect(within(nav).getByRole("button", { name: "Methods" })).not.toHaveClass("draft-toc-pending"));
  expect(within(nav).getByRole("button", { name: "Catalysis" })).not.toHaveClass("draft-toc-pending");
});
