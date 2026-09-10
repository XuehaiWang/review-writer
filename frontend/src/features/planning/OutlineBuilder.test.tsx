import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { applyOutlinePaperRecommendations, OutlineBuilder, buildPaperDisplayLabels, parseOutlineMarkdown, serializeOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";
import { displayFigureLabel, replacePaperIdsForDisplay } from "../../utils/paperLabels";

afterEach(cleanup);

describe("visual outline format", () => {
  it("keeps IDs and nested headings when editing and reordering", () => {
    const draft = parseOutlineMarkdown("## Methods\n<!-- section_id: S-methods -->\n### Details\n<!-- section_id: S-details -->\n");
    draft.sections[1].title = "Updated details";
    const saved = parseOutlineMarkdown(serializeOutlineMarkdown(draft));
    expect(saved.sections[1]).toMatchObject({ sectionId: "S-details", headingLevel: 3, title: "Updated details" });
    const flat = parseOutlineMarkdown("## A\n## B\n");
    flat.sections.reverse();
    expect(parseOutlineMarkdown(serializeOutlineMarkdown(flat)).sections.map(s => s.sectionId)).toEqual(["S02", "S01"]);
  });
  it("round-trips beginner fields into Blueprint-compatible Markdown", () => {
    const markdown = serializeOutlineMarkdown({
      preamble: "# Selected Outline",
      sections: [{ title: "Catalyst families", purpose: "Compare catalyst systems.", paperIds: ["P001", "P002"], notes: "Figure plan: overview." }],
    });
    expect(markdown).toContain("## 1. Catalyst families");
    expect(markdown).toContain("Assigned papers: P001, P002.");
    expect(parseOutlineMarkdown(markdown).sections[0]).toMatchObject({ title: "Catalyst families", purpose: "Compare catalyst systems.", paperIds: ["P001", "P002"], contextPaperIds: [], notes: "Figure plan: overview." });
  });

  it("preserves format-only introduction roles without requiring uploaded content", () => {
    const draft = parseOutlineMarkdown("## Introduction\nSection role: introduction\nPurpose: frame the current Matrix.\n");
    expect(draft.sections[0].sectionRole).toBe("introduction");
    expect(validateVisualOutline(draft).ready).toBe(true);
    expect(serializeOutlineMarkdown(draft)).toContain("Section role: introduction");
  });

  it("explains incomplete beginner sections before saving", () => {
    const validation = validateVisualOutline({ preamble: "", sections: [{ title: "", purpose: "", paperIds: [], notes: "" }] });
    expect(validation.ready).toBe(false);
    expect(validation.missingTitles).toEqual([1]);
    expect(validateVisualOutline({ preamble: "", sections: [] }).ready).toBe(false);
  });

  it("allows initial-selection sections without papers in either editor mode", () => {
    const draft = parseOutlineMarkdown("## Historical development\nSection role: body\nPurpose: Explain the field's development.\n\n## Mechanistic comparison\n");
    expect(validateVisualOutline(draft).ready).toBe(true);
    expect(parseOutlineMarkdown(serializeOutlineMarkdown(draft)).sections.map((section) => section.paperIds)).toEqual([[], []]);
  });

  it("does not assign arbitrary papers when adding introduction and conclusion", () => {
    let updated = "";
    render(<OutlineBuilder value="" papers={[{ paper_id: "P001" }, { paper_id: "P002" }]} onChange={(value) => { updated = value; }} />);
    fireEvent.click(screen.getByRole("button", { name: /加入引言和结论|Add introduction and conclusion/ }));
    const draft = parseOutlineMarkdown(updated);
    expect(draft.sections.map((section) => section.sectionRole)).toEqual(["introduction", "conclusion"]);
    expect(draft.sections.every((section) => section.paperIds.length === 0)).toBe(true);
    expect(validateVisualOutline(draft).ready).toBe(true);
  });

  it("applies one whole-outline recommendation without changing section content", () => {
    const source = "# Selected Outline\n\n## Introduction\nSection role: introduction\nPurpose: Frame the review.\n\n## 1. Ketone ATA\nAssigned papers: P009.\nPurpose: Compare ketone reactions.\nKeep the mechanism note.\n";
    const updated = applyOutlinePaperRecommendations(source, [{ section_index: 1, paper_ids: ["P002", "P003"] }]);
    const draft = parseOutlineMarkdown(updated);
    expect(draft.sections[0].paperIds).toEqual([]);
    expect(draft.sections[1].paperIds).toEqual(["P002", "P003"]);
    expect(draft.sections[1].notes).toBe("Keep the mechanism note.");
    const noMatch = parseOutlineMarkdown(applyOutlinePaperRecommendations(updated, [{ section_index: 1, paper_ids: [] }]));
    expect(noMatch.sections[1].paperIds).toEqual([]);
    expect(validateVisualOutline(noMatch).ready).toBe(true);
  });

  it("keeps a new section title editable without replacing the input node", () => {
    function ControlledOutlineBuilder() {
      const [value, setValue] = useState("");
      return <OutlineBuilder value={value} papers={[]} onChange={setValue} />;
    }

    render(<ControlledOutlineBuilder />);
    fireEvent.click(screen.getByRole("button", { name: /添加第一个章节|Add first section/ }));

    const titleInput = screen.getByRole("textbox", { name: /章节标题|Section title/ });
    expect(titleInput).toHaveValue("");
    titleInput.focus();
    fireEvent.change(titleInput, { target: { value: "酮基" } });
    expect(screen.getByRole("textbox", { name: /章节标题|Section title/ })).toBe(titleInput);
    expect(titleInput).toHaveFocus();

    fireEvent.change(titleInput, { target: { value: "酮基 ATA" } });
    expect(titleInput).toHaveValue("酮基 ATA");
    fireEvent.change(titleInput, { target: { value: "" } });
    expect(titleInput).toHaveValue("");
    expect(titleInput).toHaveFocus();
  });

  it("uses compact display numbers without changing internal paper ids", () => {
    const labels = buildPaperDisplayLabels([
      { paper_id: "00e190dc-3db9-4232-8e01-e8aff7a6b6f6" },
      { paper_id: "P157" },
    ]);
    expect(labels.get("00e190dc-3db9-4232-8e01-e8aff7a6b6f6")).toBe("P001");
    expect(labels.get("P157")).toBe("P002");
    expect(displayFigureLabel(
      "00e190dc-3db9-4232-8e01-e8aff7a6b6f6-F01",
      "00e190dc-3db9-4232-8e01-e8aff7a6b6f6",
      Object.fromEntries(labels),
    )).toBe("P001-F01");
    expect(replacePaperIdsForDisplay(
      "### 00e190dc-3db9-4232-8e01-e8aff7a6b6f6. Scope",
      labels,
    )).toBe("### P001. Scope");
    expect(serializeOutlineMarkdown({
      preamble: "",
      sections: [{ title: "Scope", purpose: "Compare evidence.", paperIds: ["00e190dc-3db9-4232-8e01-e8aff7a6b6f6"], notes: "" }],
    })).toContain("Assigned papers: 00e190dc-3db9-4232-8e01-e8aff7a6b6f6.");
  });

  it("parses long internal ids from advanced Markdown", () => {
    const draft = parseOutlineMarkdown("## Scope\nAssigned papers: 00e190dc-3db9-4232-8e01-e8aff7a6b6f6, P157.\nPurpose: Compare evidence.\n");
    expect(draft.sections[0].paperIds).toEqual(["00e190dc-3db9-4232-8e01-e8aff7a6b6f6", "P157"]);
  });
});
