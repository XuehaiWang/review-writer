---
name: review-literature-matrix-outline
description: Read the 20-30 selected papers, build a concise fixed-field literature matrix, and draft review outline options using the writing-rule skill.
---

# Review Literature Matrix Outline

Goal: read selected papers and create the literature matrix plus outline options.

## Inputs

```text
review-projects/<project_id>/00_discovery/selected_discovery_results.json
review-projects/<project_id>/00_discovery/topic_input.md
/home/ps/review-writer/skills/review-section-blueprint/SKILL.md
/home/ps/review-writer/skills/review-section-blueprint/references/rule_packs.json
/home/ps/review-writer/template/综述模板写作方式与风格总结.md
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
