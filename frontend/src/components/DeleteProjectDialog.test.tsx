import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Project } from "../api/types";
import { usePreferences } from "../state/preferences";
import { DeleteProjectDialog } from "./DeleteProjectDialog";

const project: Project = {
  project_id: "project-1",
  slug: "copper-review",
  owner_user_id: "user-1",
  topic: "Copper-catalyzed reactions",
  taxonomy_profile: "chemistry_general",
  discovery_status: "complete",
  current_stage: "final",
  completed_stages: ["library"],
};

describe("DeleteProjectDialog", () => {
  beforeEach(() => usePreferences.setState({ language: "en" }));
  afterEach(() => cleanup());

  it("requires the exact project name before confirming deletion", () => {
    const confirm = vi.fn();
    render(<DeleteProjectDialog project={project} onCancel={vi.fn()} onConfirm={confirm} />);

    const deleteButton = screen.getByRole("button", { name: "Delete permanently" });
    const input = screen.getByLabelText("Type the project name to confirm");
    expect(deleteButton).toBeDisabled();

    fireEvent.change(input, { target: { value: " copper-review " } });
    expect(deleteButton).toBeDisabled();
    expect(screen.getByText("The project name does not match.")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "copper-review" } });
    expect(deleteButton).toBeEnabled();
    fireEvent.click(deleteButton);
    expect(confirm).toHaveBeenCalledWith(project);
  });

  it("closes with Escape without invoking deletion", () => {
    const cancel = vi.fn();
    const confirm = vi.fn();
    render(<DeleteProjectDialog project={project} onCancel={cancel} onConfirm={confirm} />);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(cancel).toHaveBeenCalledOnce();
    expect(confirm).not.toHaveBeenCalled();
  });
});
