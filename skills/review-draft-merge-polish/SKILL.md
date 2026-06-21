---
name: review-draft-merge-polish
description: Merge separately drafted section files into one coherent first review draft and polish transitions, terminology, and figure placement.
---

# Review Draft Merge Polish

Goal: merge section files into one complete review draft.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/02_section_drafting/sections/*.md
review-projects/<project_id>/02_section_drafting/figure_candidates.json
review-projects/<project_id>/02_section_drafting/section_drafting_report.md
```

If available, also use:

```text
review-projects/<project_id>/03_figure_redraw/redrawn_figure_manifest.json
```

## Merge Rules

```text
Keep the selected outline order.
Merge all section files.
Polish transitions and terminology.
Preserve paper-to-paragraph and figure-to-paragraph links.
Do not delete caveats or no_figure_reason notes silently.
Do not invent new papers, claims, or figures.
```

## Outputs

Write under:

```text
review-projects/<project_id>/04_first_draft/
```

Required files:

```text
first_draft.md
merge_report.md
remaining_issues.md
```

`first_draft.md` must be a continuous review manuscript, not a list of section notes.

Stop after this stage for human check.
