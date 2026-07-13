---
name: review-final-audit-release
description: Perform final content audit and format audit on a merged review draft, verify claims against available evidence, fix manuscript-level issues, and produce the final draft plus audit reports. Use after review-draft-merge-polish and human approval of the first draft.
---

# Review Final Audit Release

Use this skill after `review-draft-merge-polish` has produced `05_first_draft/first_draft.md` and the human has approved the draft structure.

This is the final quality gate. It should not create new arguments casually. Its job is to check, correct, and release a defensible final manuscript.

## Required Inputs

Read:

```text
review-projects/<project_id>/05_first_draft/first_draft.md
review-projects/<project_id>/05_first_draft/merge_report.md
review-projects/<project_id>/05_first_draft/remaining_issues.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/03_section_drafting/section_tasks.json
review-projects/<project_id>/03_section_drafting/section_drafts.json
review-projects/<project_id>/03_section_drafting/figure_candidates.json
```

If figures were redrawn, also read:

```text
review-projects/<project_id>/04_figure_redraw/redrawn_figure_manifest.json
review-projects/<project_id>/04_figure_redraw/figure_redraw_report.md
```

For high-risk claims, reopen the relevant local paper metadata and Markdown/PDF listed in the matrix or section outputs.

## Process

Follow this order:

```text
1. Run the deterministic format scan script.
2. Audit content and review quality per section (see "Audit Per Section" below), one section at a time.
3. Do one lightweight whole-document pass for cross-cutting format checks only (headings, citation numbering consistency, reference list formatting) -- this does not require re-reading paper content, only the merged draft's own text.
4. Revise the manuscript into a final draft.
5. Write content and format audit reports.
6. Write a release report with remaining risks.
```

Do not hide unresolved problems. If a claim cannot be verified from local evidence, either weaken it, remove it, or list it in `final_remaining_issues.md`.

## Audit Per Section (cost control)

Loading the entire merged manuscript plus every upstream evidence file into one continuous audit pass is the second-largest token cost in this pipeline (after section drafting). Bound it by auditing one section at a time instead:

```text
For each section in first_draft.md (identified by its ## heading), read only:
  - that section's own text (not the whole manuscript)
  - its section_tasks.json entry (core_argument, allowed_papers, must_cover_points, avoid_points)
  - its section_drafts.json entry
  - the literature_matrix.json rows for only the papers in that section's allowed_papers/major_papers -- not the full matrix
Run the Content Audit Rules and the review-quality checks (synthesis vs paper listing, comparison axes, figure/table integration) against that section alone, then move to the next section.
Only reopen a cited paper's full metadata/Markdown/PDF for a specific claim when the section's own evidence (matrix row, section draft) doesn't resolve it -- the same fallback rule review-section-drafting-figure-picking uses for paper_content_cache.json.
```

Aggregate each section's findings into one `content_audit_report.md` at the end. The cross-cutting format checks (citation numbering, reference list, headings) still need one pass over the whole merged document, but that pass only reads the document's own text — not upstream per-paper evidence — so it stays cheap regardless of paper count.

## Content Audit Rules

Check for:

```text
claim has support from the cited paper or matrix entry
bracket citation number (e.g. [3]) resolves to the correct paper via draft_bundle.json's citation_map
the paper's key technical details (method, subject, result, and any measured/qualitative outcome) are not distorted
non-comparable results or conditions are not directly ranked as if they were
speculative or proposed conclusions are described as tentative, not established
paragraphs synthesize patterns rather than listing one paper after another
each major section follows the approved outline purpose
figures or tables support the surrounding claims
```

Identify which specific technical details matter most for this review's subject area (e.g. for a synthesis-chemistry review: reaction type, catalyst, substrate scope, regio-/stereoselectivity, mechanism; for a different field: whatever the equivalent core technical claims are) and give those special attention — do not assume the review is about organic chemistry. Base this on the actual review topic and the terminology already used in the literature matrix and section drafts.

## Format Audit Rules

Check for:

```text
heading hierarchy
duplicate or empty headings
bracket citation numbering is sequential from [1], has no gaps, and every [N] used in the body has a matching ## References entry (and vice versa)
a bracket number in the body actually points at the review's own reference list, not a source paper's own citation number quoted verbatim inside a figure/table caption (a common leak — check any caption text copied from source material for stray [N]-style numbers that were never reworded during merge)
reference list entries follow the ACS journal-article format exactly: "Last, F.; Last, F." authors ending in a period, plain-text title ending in a period, *Journal* italic with no comma before the year, **Year** bold, comma, *Volume* italic, comma, Pages plain with an en dash (–) not a hyphen (see review-draft-merge-polish's Reference List Format)
figure/table numbering and callouts
caption completeness
source figure placeholders that still need redraw or permission review
undefined abbreviations
placeholder text such as TODO, verification needed, citation needed
broken Markdown links or image paths
front matter style inappropriate for the review's subject area
```

## Script

Run:

```bash
python3 <skill-root>/scripts/final_audit_scan.py --project-id <project_id>
```

`--review-root` defaults to the current working directory. `<skill-root>` is the directory containing this `SKILL.md`.

The script writes `format_scan.json` and `format_scan.md`. Codex must then perform the semantic content audit and final revision.

## Outputs

Write outputs under:

```text
review-projects/<project_id>/06_final_audit/
```

Create:

```text
format_scan.json
format_scan.md
content_audit_report.md
format_audit_report.md
final_draft.md
final_remaining_issues.md
release_report.md
```

## Output Requirements

`content_audit_report.md` must include:

```text
major content fixes made
claims weakened or removed
citation or paper_id mismatches found
sections still weak in evidence
subject-specific risks
```

`format_audit_report.md` must include:

```text
format scan summary
manual format fixes made
remaining formatting issues
```

`final_draft.md` must be the clean manuscript without inline TODOs, verification notes, or editor-only comments.

Do not treat source-paper image placeholders as final publication figures. If `figure_insertion_report.json` has `mode: source_candidates`, either replace them with redrawn figures or list this as a blocking remaining issue.

`final_remaining_issues.md` must be short and explicit. If there are no known remaining issues, say so.

`release_report.md` must state:

```text
source first draft
upstream evidence files used
final draft path
whether release is ready for human export
residual risks
```

## Human Check Point

Stop after this stage.

The human should check:

```text
whether the final manuscript is scientifically acceptable
whether any remaining risk requires returning to an earlier stage
whether figures and references are ready for export to the target format
```

Suggested completion message:

```text
终稿检查已完成，请人工最终确认 final_draft.md 和 release_report.md。
```
