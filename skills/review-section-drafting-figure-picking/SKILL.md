---
name: review-section-drafting-figure-picking
description: Draft each review section from section_blueprint.json, literature matrix, and writing rules; each section is a separate output file and should be written by a separate subagent when possible.
---

# Review Section Drafting Figure Picking

Goal: write each section as a separate file, with figures tied to paragraphs.

## Inputs

All paths are relative to `<review-root>` (the folder containing `skills/`, `review-projects/`, and `review-library/`).

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/02_section_blueprint/section_blueprint.json
review-projects/<project_id>/02_section_blueprint/section_writing_plan.md
skills/review-section-blueprint/references/rule_packs.json
```

## Paper Content Cache (cost control -- build before drafting)

Papers are commonly assigned to more than one section. Without a shared cache, each section subagent reopens the same paper's full Markdown/PDF from scratch, and that cost multiplies once per section a paper appears in. Build the cache once, before any section subagent starts:

```bash
python3 <skill-root>/scripts/build_paper_content_cache.py --project-id <project_id>
```

This projects each paper's already-compressed `literature_matrix.json` fields (`main_content`, `most_relevant_figure`, structured-tag fields) into `paper_content_cache.json`, one entry per paper_id.

Every section subagent should read `paper_content_cache.json` for its assigned papers **instead of** reopening each paper's metadata JSON / full Markdown / PDF by default. Only fall back to the paper's actual metadata/Markdown/PDF (via the cache entry's `paper_id`, looked up in `review-library/metadata/papers/<paper_id>.metadata.json` for the real source paths) when:

```text
the cached main_content genuinely lacks a technical detail the paragraph needs, or
a figure/scheme/table's exact caption or image needs verification before citing it.
```

## Writing Rules

```text
Write by section.
Each section outputs one independent Markdown file.
Use one subagent per section when parallel execution is available.
Each paragraph normally corresponds to one paper's work.
Each paragraph must have one figure/scheme/table tied to that paper.
If no useful figure exists, write an explicit no_figure_reason.
Use paper_content_cache.json as the starting evidence for every paragraph; only reopen a paper's full Markdown/PDF for the fallback cases described above, not as a default verification step for every paragraph.
Do not write short examples; write complete review prose.
```

Read `<review-root>/skills/template/综述模板写作方式与风格总结.md` for the reference writing style and paragraph structure before drafting.

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

Figure selection has three ordered steps. Do not run step 3 before step 2 exists — it reads `section_tasks.json` and will fail if that file is missing.

1. Build the figure inventory (before writing):

```bash
python3 <skill-root>/scripts/build_paper_figure_inventory.py --project-id <project_id>
```

This reads `00_discovery/selected_discovery_results.json` (produced by `labkag-review-skill`'s `export_discovery_format.py` bridge, per `review-literature-matrix-outline/SKILL.md` -- not by `review-online-paper-discovery` directly) and writes `paper_figure_inventory.json`.

2. Write `section_tasks.json` (LLM output, see Outputs below — one item per section with `section_id`, `heading`, `core_argument`, `allowed_papers`, `must_cover_points`, `avoid_points`, `figure_need`).

3. After `section_tasks.json` exists, select initial figure candidates:

```bash
python3 <skill-root>/scripts/select_initial_figure_candidates.py --project-id <project_id>
```

This reads both `paper_figure_inventory.json` and `section_tasks.json`, and writes `paper_figure_candidates.json` and `figure_candidates.json`. Use these candidates when drafting `sections/<section_id>.md`.

`--review-root` defaults to the current working directory. `<skill-root>` is the directory containing this `SKILL.md`.

Use real source figures/schemes/tables from MinerU/PDF. Do not invent figures.

## Outputs

Write under:

```text
review-projects/<project_id>/03_section_drafting/
```

Required files:

```text
paper_content_cache.json
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
