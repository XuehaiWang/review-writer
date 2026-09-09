import { describe, expect, it } from "vitest";

import {
  authorsFromInput,
  markMetadataReviewed,
  metadataFieldValue,
  metadataForSave,
  metadataTextForEditing,
  metadataValidationError,
  setStructuredTagsVerified,
  structuredTagValue,
  updateBibliographicField,
  updateStructuredTag,
} from "./metadataEditorModel";

const source = {
  paper_id: "P001",
  title: { value: "Original title", source: "mineru", confidence: 0.72, human_checked: false },
  authors: { value: ["A. Author"], source: "mineru", confidence: 0.6, human_checked: false },
  structured_tags: {
    value: { product: "not specified", reaction_type: "isomerization" },
    source: "project_neutral_unverified",
    confidence: 0,
    human_checked: false,
  },
  source_paths: { pdf: "preserved.pdf" },
};

describe("metadata editor model", () => {
  it("edits a visible field without losing hidden metadata", () => {
    const edited = updateBibliographicField(source, "title", "Reviewed title");

    expect(metadataFieldValue(edited, "title")).toBe("Reviewed title");
    expect(edited.title).toMatchObject({ source: "human_review", confidence: 1, human_checked: true });
    expect(edited.source_paths).toEqual({ pdf: "preserved.pdf" });
    expect(source.title.value).toBe("Original title");
  });

  it("keeps content tags unverified until the user explicitly confirms them", () => {
    const edited = updateStructuredTag(source, "product", "chiral allenoate");
    expect(structuredTagValue(edited, "product")).toBe("chiral allenoate");
    expect((edited.structured_tags as { human_checked: boolean }).human_checked).toBe(false);

    const verified = setStructuredTagsVerified(edited, true);
    expect(verified.structured_tags).toMatchObject({ source: "human_review", confidence: 1, human_checked: true });
  });

  it("marks the overall record reviewed while preserving review notes", () => {
    const reviewed = markMetadataReviewed({ ...source, human_review: { status: "pending", notes: ["keep"] } }, "2026-09-01T00:00:00.000Z");
    expect(reviewed.human_review).toEqual({
      status: "reviewed",
      notes: ["keep"],
      reviewed_at: "2026-09-01T00:00:00.000Z",
      reviewer: "human",
    });
  });

  it("normalizes multiline authors and validates title and year", () => {
    expect(authorsFromInput(" Ada Lovelace \n\nGrace Hopper ")).toEqual(["Ada Lovelace", "Grace Hopper"]);
    expect(metadataValidationError(updateBibliographicField(source, "title", ""), 2026)).toBe("title");
    expect(metadataValidationError(updateBibliographicField(source, "year", 3026), 2026)).toBe("year");
    expect(metadataValidationError(updateBibliographicField(source, "year", 2025), 2026)).toBeNull();
  });

  it("keeps multiline editing natural and normalizes fields only for saving", () => {
    const editing = updateBibliographicField(source, "authors", "Ada Lovelace\nGrace Hopper\n");
    const tagEditing = updateStructuredTag(editing, "product", "chiral allenoate ");
    expect(metadataFieldValue(tagEditing, "authors")).toBe("Ada Lovelace\nGrace Hopper\n");
    expect(structuredTagValue(tagEditing, "product")).toBe("chiral allenoate ");

    const saved = metadataForSave(tagEditing);
    expect(metadataFieldValue(saved, "authors")).toEqual(["Ada Lovelace", "Grace Hopper"]);
    expect(structuredTagValue(saved, "product")).toBe("chiral allenoate");
  });

  it("presents extracted inline markup as readable text", () => {
    expect(metadataTextForEditing("Catalyst<sup>1</sup><br/>A &amp; B")).toBe("Catalyst1\nA & B");
  });
});
