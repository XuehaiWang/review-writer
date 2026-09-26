import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyOutlinePaperRecommendations, buildOutlineTree, changeOutlineLevel, moveOutlineSubtree, OutlineBuilder, buildPaperDisplayLabels, parseOutlineMarkdown, serializeOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";
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
  it("numbers nested headings and moves a whole subtree with IDs and papers intact", () => {
    const sections = parseOutlineMarkdown("## A\n<!-- section_id: S-A -->\nAssigned papers: P001.\n### A child\n<!-- section_id: S-A1 -->\nAssigned papers: P002.\n#### A grandchild\n<!-- section_id: S-A11 -->\n## B\n<!-- section_id: S-B -->\n### B child\n<!-- section_id: S-B1 -->").sections;
    expect(buildOutlineTree(sections).valid).toBe(true);
    expect(buildOutlineTree(sections).roots[0].children[0].children[0].number).toBe("1.1.1");
    const moved = moveOutlineSubtree(sections, 0, 1);
    expect(moved.map(section => section.sectionId)).toEqual(["S-B", "S-B1", "S-A", "S-A1", "S-A11"]);
    const saved = serializeOutlineMarkdown({ preamble: "", sections: moved });
    expect(saved).toContain("### 2.1. A child");
    expect(saved).toContain("#### 2.1.1. A grandchild");
    expect(parseOutlineMarkdown(saved).sections[3].paperIds).toEqual(["P002"]);
  });

  it("changes a branch level only when unrelated siblings keep their parents", () => {
    const sections = parseOutlineMarkdown("## A\n## B\n### B child\n## C\n").sections;
    const nested = changeOutlineLevel(sections, 1, 3);
    expect(nested?.map(section => section.headingLevel)).toEqual([2, 3, 4, 2]);
    expect(changeOutlineLevel(sections, 0, 3)).toBeNull();
    const withSibling = parseOutlineMarkdown("## A\n### B\n### C\n").sections;
    expect(changeOutlineLevel(withSibling, 1, 2)).toBeNull();
  });

  it("warns before removing a parent and preserves deep headings in code mode", () => {
    function ControlledOutlineBuilder() {
      const [value, setValue] = useState("## A\n<!-- section_id: S-A -->\n### B\n<!-- section_id: S-B -->\n#### C\n<!-- section_id: S-C -->\n##### D\n<!-- section_id: S-D -->\nAssigned papers: P001.\n");
      return <><OutlineBuilder value={value} papers={[]} onChange={setValue} /><output data-testid="outline-source">{value}</output></>;
    }
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<ControlledOutlineBuilder />);
    const source = screen.getByTestId("outline-source").textContent;
    expect(screen.getByText("1.1.1.1 D")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(confirm).toHaveBeenCalledOnce();
    expect(screen.getByTestId("outline-source").textContent).toBe(source);
    fireEvent.click(screen.getByRole("button", { name: "代码填写" }));
    expect(screen.getByRole("textbox")).toHaveValue(source);
    fireEvent.click(screen.getByRole("button", { name: "模块填写" }));
    expect(screen.getByTestId("outline-source").textContent).toBe(source);
    confirm.mockRestore();
  });

  it("keeps unsupported code intact instead of silently rebuilding it as modules", () => {
    const source = "## Methods\n```md\n### Example heading inside a code block\n```\n";
    const onChange = vi.fn();
    render(<OutlineBuilder value={source} papers={[]} onChange={onChange} />);
    expect(screen.getByText(/模块无法可靠解析/)).toBeVisible();
    expect(screen.getByRole("button", { name: "添加主章节" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "代码填写" }));
    expect(screen.getByRole("textbox")).toHaveValue(source);
    expect(onChange).not.toHaveBeenCalled();
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

  it("adds a subsection inside its parent and exposes the organizing-only role", () => {
    function ControlledOutlineBuilder() {
      const [value, setValue] = useState("## Routes\n<!-- section_id: S-routes -->\nAssigned papers: P001.\n## Comparison\n<!-- section_id: S-comparison -->\n");
      return <><OutlineBuilder value={value} papers={[]} onChange={setValue} /><output data-testid="outline-source">{value}</output></>;
    }
    render(<ControlledOutlineBuilder />);
    fireEvent.click(screen.getAllByRole("button", { name: "添加下级" })[0]);
    const sections = parseOutlineMarkdown(screen.getByTestId("outline-source").textContent || "").sections;
    expect(sections.map(section => section.headingLevel)).toEqual([2, 3, 2]);
    expect(sections[0].sectionId).toBe("S-routes");
    expect(sections[0].paperIds).toEqual(["P001"]);
    expect(screen.getByText("组织标题，不单独写正文")).toBeInTheDocument();
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
