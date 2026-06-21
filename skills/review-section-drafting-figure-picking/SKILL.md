---
name: review-section-drafting-figure-picking
description: Draft each review section from section_blueprint.json, literature matrix, and writing rules; each section is a separate output file and should be written by a separate subagent when possible.
---

# Review Section Drafting Figure Picking

Goal: write each section as a separate file, with figures tied to paragraphs.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/section_blueprint.json
review-projects/<project_id>/01_matrix_outline/section_writing_plan.md
/home/ps/review-writer/skills/review-section-blueprint/references/rule_packs.json
/home/ps/review-writer/template/综述模板写作方式与风格总结.md
```

For every assigned paper, reopen:

```text
metadata JSON
linked Markdown
PDF when checking figures/schemes/tables
```

## Writing Rules

```text
Write by section.
Each section outputs one independent Markdown file.
Use one subagent per section when parallel execution is available.
Each paragraph normally corresponds to one paper's work.
Each paragraph must have one figure/scheme/table tied to that paper.
If no useful figure exists, write an explicit no_figure_reason.
Use the literature matrix main_content as the starting evidence, but verify against Markdown/PDF.
Do not write short examples; write complete review prose.
```

Follow the template review paragraph mode:

```text
1. introduce why this paper/method matters in the section
2. describe the paper's main transformation or principle
3. attach the corresponding scheme/figure/table
4. explain what the scheme shows: substrate, product, catalyst, selectivity, mechanism, or limitation
5. close with a review-level judgment or transition
```

Paragraph must include:

```text
paper_id
claim or topic sentence
main work of the paper
why it matters to the review topic
figure reference or no_figure_reason
```

## Figure Rules

Before writing, run:

```bash
python /home/ps/review-writer/skills/review-section-drafting-figure-picking/scripts/build_paper_figure_inventory.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id>
```

Use real source figures/schemes/tables from MinerU/PDF. Do not invent figures.

## Outputs

Write under:

```text
review-projects/<project_id>/02_section_drafting/
```

Required files:

```text
section_tasks.json
sections/<section_id>.md
section_drafts.json
section_drafts.md
paper_figure_inventory.json
paper_figure_candidates.json
figure_candidates.json
section_drafting_report.md
```

`section_tasks.json` must be a list. Each item must contain:

```text
section_id
heading
core_argument
allowed_papers
must_cover_points
avoid_points
figure_need
```

Use `section_blueprint.json.sections[].major_papers` as the source for `allowed_papers`.

`sections/<section_id>.md` is mandatory for every section. `section_drafts.md` concatenates the section files for preview only.

Stop after this stage for human check.
