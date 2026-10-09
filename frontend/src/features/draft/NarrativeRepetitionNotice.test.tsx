import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { usePreferences } from "../../state/preferences";
import { NarrativeRepetitionNotice } from "./NarrativeRepetitionNotice";

afterEach(cleanup);

it("shows non-blocking wording clues and locates the later paragraph", () => {
  usePreferences.getState().setLanguage("en");
  const onSelect = vi.fn();
  render(<NarrativeRepetitionNotice onSelect={onSelect} report={{
    status: "complete", method: "surface_clues_v1", covered_paragraph_count: 2,
    total_paragraph_count: 2, hints: [{ kind: "wording_overlap", paragraph_id: "S2-p1",
      other_paragraph_id: "S1-p1", excerpt: "Repeated description of the same method.",
      basis: "surface_wording_only" }],
  }} />);
  expect(screen.getByText(/not a scientific-claim verdict/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "View later paragraph" }));
  expect(onSelect).toHaveBeenCalledWith("S2-p1");
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("keeps ordinary empty states quiet", () => {
  usePreferences.getState().setLanguage("zh-CN");
  const { container } = render(<NarrativeRepetitionNotice onSelect={vi.fn()} report={{
    status: "complete", method: "surface_clues_v1", covered_paragraph_count: 1,
    total_paragraph_count: 1, hints: [],
  }} />);
  expect(container).toBeEmptyDOMElement();
});
