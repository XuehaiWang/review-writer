---
name: review-section-blueprint
description: Middle-layer writing-rule skill that converts the selected outline and literature matrix into section_blueprint.json for constrained section writing.
---

# Review Section Blueprint

Goal: create the writing blueprint used by section subagents.

## Inputs

All paths are relative to `<review-root>` (the folder containing `skills/`, `review-projects/`, and `review-library/`).

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/paper_reading_notes.json
skills/review-section-blueprint/references/rule_packs.json
```

Default rule pack: `skills/review-section-blueprint/references/rule_packs/general/` — subject-agnostic writing rules that apply to any review topic.

The rule pack selected is determined by matching topic keywords against `rule_packs.json` `topic_signals`. Additional subject-specific rule packs can be registered in `rule_packs.json` and will be preferred over `general` when their `topic_signals` match the topic. Use the rule pack as writing constraints only. Do not import facts from it.

## Required Blueprint

Run initializer if useful:

```bash
python3 <skill-root>/scripts/init_section_blueprint.py \
  --project-id <project_id>
```

`--review-root` defaults to the current working directory; the script reads inputs from `01_matrix_outline/` and writes outputs to `02_section_blueprint/`. Override with `--stage-dir` to point directly at a single folder used for both input and output instead.

Then edit/complete:

```text
review-projects/<project_id>/02_section_blueprint/section_blueprint.json
review-projects/<project_id>/02_section_blueprint/section_writing_plan.md
```

Each section in `section_blueprint.json` must contain these script-compatible fields:

```text
section_id
title
section_thesis
review_problem
target_paragraphs
target_words
dominant_logic
major_papers
review_claims
figure_or_table_needs
depth_requirements
section_transition
avoid_patterns
```

`review_claims` must map each major claim to supporting paper IDs and comparison axes. `figure_or_table_needs` must name the scheme/table purpose and candidate papers.

## Paper-to-Section Assignment

`init_section_blueprint.py` assigns papers to sections one of two ways:

1. **Explicit assignment (preferred)**: if `selected_outline.md` states `Papers: P001, P002, ...` under a section's heading (see `review-literature-matrix-outline`'s outline format), the script uses that list directly as `major_papers`, in the given order.
2. **Keyword-overlap fallback**: if a section has no explicit `Papers:` line, the script falls back to scoring every paper's title/keywords/main_content against the section title. This fallback is a coarse heuristic and degrades badly when many papers share tightly clustered vocabulary (e.g. a review scoped to one narrow reaction class) -- it can scatter papers across sections in ways that don't match the actual outline intent.

When drafting `selected_outline.md`, list explicit `Papers:` per section wherever you already know the assignment (e.g. from `outline_options.md`, which already groups papers per section) rather than leaving it to the fallback scorer. Reserve the fallback for sections that are genuinely cross-cutting (Introduction, Conclusion) where no fixed paper set is expected.

## Hard Rules

```text
No section may be only a title.
Every section must have major_papers.
Every section must have review_claims.
Every section must have figure_or_table_needs, or explicitly state no figure/table is useful.
The blueprint is a plan, not prose. Keep it compact and enforceable.
```

Stop after blueprint for human check if interactive.
