import { describe, expect, it } from "vitest";

import { buildPaperDisplayLabels, parseOutlineMarkdown, serializeOutlineMarkdown, validateVisualOutline } from "./OutlineBuilder";
import { displayFigureLabel, replacePaperIdsForDisplay } from "../../utils/paperLabels";

describe("visual outline format", () => {
  it("round-trips beginner fields into Blueprint-compatible Markdown", () => {
    const markdown = serializeOutlineMarkdown({
      preamble: "# Selected Outline",
      sections: [{ title: "Catalyst families", purpose: "Compare catalyst systems.", paperIds: ["P001", "P002"], notes: "Figure plan: overview." }],
    });
    expect(markdown).toContain("## 1. Catalyst families");
    expect(markdown).toContain("Assigned papers: P001, P002.");
    expect(parseOutlineMarkdown(markdown).sections[0]).toEqual({ title: "Catalyst families", purpose: "Compare catalyst systems.", paperIds: ["P001", "P002"], contextPaperIds: [], notes: "Figure plan: overview." });
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
    expect(validation.missingPapers).toEqual([1]);
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
