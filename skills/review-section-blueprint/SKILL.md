---
name: review-section-blueprint
description: Middle-layer writing-rule skill that converts the selected outline and literature matrix into section_blueprint.json for constrained section writing.
---

# Review Section Blueprint

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Goal: create the writing blueprint used by section subagents.

Boundary: this is a pure rule/plan skill. It consumes the outline and
literature matrix and emits paragraph-level/claim-level constraints; it
does not re-derive section structure or paper assignments.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/paper_reading_notes.json
this skill folder/references/rule_packs.json
```

Default rule pack:

```text
references/rule_packs/allenation/
```

Use the rule pack as writing constraints only. Do not import facts from it.

## Required Blueprint

Run initializer if useful:

```bash
python scripts/init_section_blueprint.py \
  --review-root <review-root> \
  --project-id <project_id>
```

Then edit/complete:

```text
review-projects/<project_id>/01_matrix_outline/section_blueprint.json
review-projects/<project_id>/01_matrix_outline/section_writing_plan.md
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

## Hard Rules

```text
No section may be only a title.
Every section must have major_papers.
Every section must have review_claims.
Every section must have figure_or_table_needs, or explicitly state no figure/table is useful.
The blueprint is a plan, not prose. Keep it compact and enforceable.
```

Stop after blueprint for human check if interactive.
