---
name: review-section-blueprint
description: Middle-layer writing-rule skill that converts the selected outline and literature matrix into section_blueprint.json for constrained section writing.
---

# Review Section Blueprint

Goal: create the writing blueprint used by section subagents.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/paper_reading_notes.json
/home/ps/review-writer/skills/review-section-blueprint/references/rule_packs.json
```

Default rule pack:

```text
references/rule_packs/allenation/
```

Use the rule pack as writing constraints only. Do not import facts from it.

## Required Blueprint

Run initializer if useful:

```bash
python /home/ps/review-writer/skills/review-section-blueprint/scripts/init_section_blueprint.py \
  --review-root /home/ps/review-writer \
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
