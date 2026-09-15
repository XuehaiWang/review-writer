import { fireEvent, render, screen, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { EvidenceLinks } from "./EvidenceLinks";
import { usePreferences } from "../../state/preferences";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it("opens a saved passage and uses the authenticated PDF route with a known page", () => {
  usePreferences.getState().setLanguage("en");
  HTMLDialogElement.prototype.showModal = function() { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function() { this.removeAttribute("open"); this.dispatchEvent(new Event("close")); };
  const source = { ref: "a", paper_id: "P1", title: "Study", page: 4, text: "Yield was 44%." };
  render(<EvidenceLinks sources={[source, source]} />);
  expect(screen.getAllByRole("button", { name: /Source 1/ })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /Source 1/ }));
  expect(screen.getByRole("dialog")).toBeVisible();
  expect(screen.getByText("Yield was 44%.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open original PDF" })).toHaveAttribute("href", "/api/v1/library/papers/P1/pdf#page=4");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("shows no source UI for ordinary replies and never fabricates links for legacy refs", () => {
  usePreferences.getState().setLanguage("en");
  const view = render(<EvidenceLinks />);
  expect(view.container).toBeEmptyDOMElement();
  view.rerender(<EvidenceLinks legacyRefs={["P1:unknown"]} />);
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
  expect(screen.getByText("Historical source references")).toBeInTheDocument();
});
