---
name: review-draft-merge-polish
description: Merge separately drafted section files into one coherent first review draft and polish transitions, terminology, and figure placement.
---

# Review Draft Merge Polish

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

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

## Run

Invoke the bundled scripts in this order:

```bash
python skills/review-draft-merge-polish/scripts/init_first_draft.py \
  --review-root <review-root> --project-id <project-id>

python skills/review-draft-merge-polish/scripts/merge_polish_draft.py \
  --review-root <review-root> --project-id <project-id>

python skills/review-draft-merge-polish/scripts/insert_figures_into_draft.py \
  --review-root <review-root> --project-id <project-id>

python skills/review-draft-merge-polish/scripts/renumber_figures_in_draft.py \
  --review-root <review-root> --project-id <project-id>
```

`merge_polish_draft.py` also strips each section's own `## References` block
(every section from Section Drafting carries one, numbered from the same
shared global citation map) before concatenating section bodies, and appends
a single consolidated `## References` section built from the union of those
per-section entries. It writes that consolidated mapping to `citations.json`
in the same run — no separate script produces `citations.json`.

`paragraph_editor.py` and `paragraph_manifest_builder.py` in this skill's
`scripts/` folder are library modules for the paragraph-edit dashboard flow
(see `review-paragraph-edit`), not part of this automated Run order.

## Hard Output Requirements

`first_draft.md` must satisfy all of:

```text
at least one ![](...) figure or scheme image,
  resolved against 04_first_draft/ (use redrawn images when available,
  or source-figure placeholders during early development; never zero figures
  unless 03_figure_redraw/skip_reason.md exists);
inline citation callouts using the `[n]` style for every claim that
  references a paper;
a final References section. Heading must be one of
  References / Reference List / Bibliography / Cited Literature / 参考文献.
  Items numbered 1., 2., ... or [1], [2], ... and the numbering must align
  with the inline `[n]` callouts.
```

The orchestrator status script will mark this stage incomplete with
`draft_has_no_figures`, `draft_has_no_citation_callouts`, or
`missing_references_section` whenever any of these are violated.

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
citations.json
```

`citations.json` aggregates every paragraph's `cited_paper_ids` into a single
ordered list per `[n]` slot; `merge_polish_draft.py` writes it directly from
the consolidated References entries described in Run above. It is consumed by
the final audit to cross-check inline `[n]` callouts and the References
section against `literature_matrix.json`.

Figure insertion is paragraph-anchored: read `target_paragraph_id` from
`02_section_drafting/figure_candidates.json` and insert each figure right after
its anchor paragraph. Do not fall back to heading-only matching when
`target_paragraph_id` exists.

## Figure Numbering and Paragraph References

After inserting visual assets, number them from the order in which they occur
in `first_draft.md`, never from redraw-manifest order, source-paper numbering,
or the temporary copied-file name. Use conventional independent sequences:

```text
Scheme 1, Scheme 2, ...
Figure 1, Figure 2, ...
Table 1, Table 2, ...
```

The insertion script records a stable, invisible figure-to-paragraph anchor and
rewrites the Markdown alt label and publication caption to that sequence. If
the anchor paragraph refers to that selected source visual (for example,
`Scheme 1` or `Fig. 1`), update the reference to the new published label in
that paragraph too. Do **not** apply a document-wide `Scheme 1` replacement:
each cited paper may have its own source `Scheme 1`, so global replacement can
corrupt unrelated references. Write the resulting mapping and reference-update
counts to `04_first_draft/figure_numbering_report.json` and include it in
`figure_insertion_report.json`. Before Final Audit, rerun the same pass against
`05_final_audit/final_draft.md` so the published manuscript is the definitive
numbered version.

`first_draft.md` must be a continuous review manuscript, not a list of section notes.

Stop after this stage for human check.
