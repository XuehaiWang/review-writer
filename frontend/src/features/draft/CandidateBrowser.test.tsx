import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CandidateBrowser } from "./CandidateBrowser";

it("shows one latest pending candidate and keeps historical candidates accessible", () => {
  const make = (id: string, key: string, status = "pending") => ({ candidate_id: id, paragraph_key: key,
    paragraph_id: key, original_text: "Original " + id, candidate_text: "Revised " + id, reply: "Reason " + id, status });
  const decide = vi.fn();
  render(<CandidateBrowser candidates={[make("old", "S01-p1"), make("new", "S01-p1"), make("next", "S02-p1"), make("saved", "S03-p1", "accepted")]} decide={decide} />);
  expect(screen.getByText("Revised new")).toBeInTheDocument();
  expect(screen.queryByText("Revised old")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /下一项|Next/ }));
  expect(screen.getByText("Revised next")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /保存候选|Save candidate/ }));
  expect(decide).toHaveBeenCalledWith("next", "accept");
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "accepted" } });
  expect(screen.getByText("Revised saved")).toBeInTheDocument();
});
