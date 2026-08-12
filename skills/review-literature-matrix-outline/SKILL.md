---
name: review-literature-matrix-outline
description: Read every paper explicitly selected by the human reviewer, build a concise fixed-field literature matrix, and draft review outline options using the writing-rule skill.
---

# Review Literature Matrix Outline

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Goal: read selected papers and create the literature matrix plus outline options.

Boundary: this skill produces high-level structure (sections, purposes,
assigned papers, expected figures). It does NOT emit per-paragraph or
per-claim constraints; that is `review-section-blueprint`'s job.

## Inputs

```text
review-projects/<project_id>/00_discovery/selected_discovery_results.json
review-projects/<project_id>/00_discovery/topic_input.md
<review-root>/skills/review-section-blueprint/SKILL.md
<review-root>/skills/review-section-blueprint/references/rule_packs.json
<review-root>/examples/reference-reviews/template_summary.md (optional reference-review example)
```

For each paper, open:

```text
review-library/metadata/papers/<paper_id>.metadata.json
linked Markdown
linked PDF when choosing figures or checking chemistry
```

## Matrix Rules

For every selected paper, every matrix row must contain all fields:

```text
paper_id
title
authors
keywords
abstract
main_content
most_relevant_figure
```

Field requirements:

```text
keywords: use the 8 structured tag values from metadata.
abstract: use metadata abstract if reliable; if missing or poor, write "abstract unavailable or unreliable" and continue.
main_content: around 1000 English words; summarize the paper's actual work, not just the abstract.
most_relevant_figure: the figure/scheme/table that best reflects the principle or main work of the paper; include source label, caption, page hint, image path if available, and why it is relevant.
```

Do not omit any field. Do not exclude a paper only because its abstract is poor.

External `web_papers` (SciAtlas/Crossref) from discovery are reference-only:
they may be cited in the manuscript with a reference list entry, but they do
not get a `paper_id` and do not become matrix rows.

## Outline Rules

After the matrix is complete, use:

```text
review topic
literature matrix
review-section-blueprint writing rules / rule pack
template review organization summary
```

Create `2-3` outline options. Each option must include section titles, purpose, assigned papers, and expected figures.

The outline must imitate the template reviews' organization mode. Choose and name one primary structure:

```text
problem-progressive
category-coverage
entry-classified
reaction-type-classified
application-oriented
```

Each major section must have a clear review question, assigned papers, and scheme/figure plan. Do not make a plain title list.

## Outputs

Write under:

```text
review-projects/<project_id>/01_matrix_outline/
```

Required files:

```text
paper_reading_notes.json
literature_matrix.json
literature_matrix.csv
outline_options.md
matrix_outline_report.md
```

Stop after this stage for human outline selection. The preferred human artifact is:

```text
selected_outline.md
```

The dashboard may create this artifact from a built-in structure, a reference
review, or a custom outline. A newly selected custom outline starts completely
blank and is not ready for Blueprint until the reviewer writes and saves it.
The dashboard's default editor is a visual section-card builder. Reviewers can
edit section titles and purposes, assign papers with checkboxes, request a
metadata-based paper recommendation, and reorder sections without knowing the
Markdown syntax. Every major section must have a non-empty title and at least
one assigned paper before the outline can be saved.

Advanced reviewers may switch to Markdown editing. Keep major sections as
level-2 headings (`## Section title` or `## 1. Section title`) and use
`Assigned papers: P001, P002.` for every major section. The visual editor and
Markdown editor must round-trip the same selected outline. Saved manual edits
are authoritative for Blueprint and all later stages.
