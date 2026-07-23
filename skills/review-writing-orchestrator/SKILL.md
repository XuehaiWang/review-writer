---
name: review-writing-orchestrator
description: Orchestrate the concise review-writing workflow: discovery, fixed-field literature matrix, outline/blueprint, section-file drafting, figure redraw, merge, and final audit.
---

# Review Writing Orchestrator

The corpus is grown two ways: `review-online-paper-discovery` searches Crossref/SciAtlas and downloads confirmed candidates' PDFs into `review-library/paper_pdf/`, and/or `labkag-review-skill`'s ingest workflow processes PDFs already placed there. Neither requires a manually-located external paper directory.

If the user's topic is not in English, translate it to English first. Discovery search is English-keyword based; a non-English topic passed straight through will fail to match papers.

## Project folder naming

Derive `--project-id` from the English-translated topic, slugified (lowercase, spaces/punctuation replaced with `-`). This becomes the project's folder name under `review-projects/<project_id>/`.

Prefer a short, readable slug (a handful of the topic's key words) over slugifying the entire topic sentence verbatim — use your own judgment to pick the words that best identify the project at a glance.

Before creating the project, check whether `review-projects/<project_id>/` already exists:

```text
if review-projects/<project_id>/ does not exist:
    use <project_id> as-is
else:
    append _2, then _3, _4, ... until an unused folder name is found
    e.g. urban-heat-island-mitigation-strategies -> ..._2 -> ..._3
```

Never overwrite or reuse an existing project folder for a new topic. Confirm the resolved `project_id` with the user before running discovery if there is any ambiguity.

## Workflow

```text
1. review-online-paper-discovery
2. review-literature-matrix-outline
3. review-section-blueprint
4. review-section-drafting-figure-picking
5. review-figure-style-redraw
6. review-draft-merge-polish
7. review-conclusion-generator
8. review-final-audit-release
9. review-outline-summary-chart
10. review-export-docx
```

For each stage, invoke that stage's skill by name (e.g. call the `review-online-paper-discovery` skill, not just read its file). Do not read a sub-skill's `SKILL.md` yourself and improvise its steps inline — invoke the skill so it runs under its own instructions. The correct CLI invocation, required flags, required output files, and human-check gates for each stage live inside that sub-skill and are applied when it is invoked. The summary below is only a rough map of what each stage does, not a substitute for invoking it.

**A manual bridge step is required between stage 1 and stage 2, and it is not automatic.** `review-online-paper-discovery` only grows `review-library/paper_pdf/` with confirmed PDFs — it does not write `00_discovery/selected_discovery_results.json`, the file `review-literature-matrix-outline` (and later, figure-picking) hard-requires. Before invoking stage 2, run `labkag-review-skill`'s knowledge-base build (ingest + taxonomy) if not already done, then its `match-topic` workflow for this review's topic, then `labkag-review-skill/scripts/export_discovery_format.py` to produce `selected_discovery_results.json` in this project's `00_discovery/`. Stage 2 will fail outright, not degrade gracefully, if this step is skipped.

**Run exactly one stage per turn, then stop.** After invoking a stage's skill and its outputs are written, do not continue to the next stage in the same turn. Report what was produced, point to the human-check instructions for that stage, and explicitly ask the user to review and confirm before you proceed. Wait for the user's next message before invoking the following stage — even if the user's original request described the full end-to-end goal. The only exception is when the user explicitly says to run multiple stages back-to-back without stopping (e.g. "run all steps," "skip the checks," "continue through stage N").

## Core Contract (rough map only — always defer to the sub-skill's SKILL.md)

```text
Discovery: user topic -> expanded keywords -> search Crossref/SciAtlas -> human-confirmed candidates -> download PDFs into review-library/paper_pdf/.
Matrix: one row per paper with title, authors, keywords, abstract, ~1000-word main_content, most_relevant_figure.
Outline: use topic + matrix + writing-rule skill to create selected_outline.md.
Blueprint: convert outline into section_blueprint.json with section, paragraph, paper, and figure mapping.
Drafting: one section file per section; each paragraph normally maps to one paper and one figure/scheme/table.
Merge: combine section files into one polished first draft.
Conclusion: generate a grounded conclusion/challenges/insights section from the approved first draft.
Final audit: integrate the validated conclusion, then run content and format audits to produce final_draft.md.
Summary chart: generate a full-review and per-section Mermaid summary chart from the approved final draft.
```

## Status

```bash
python3 <skill-root>/scripts/project_status.py --project-id <project_id>
```

## Human Check Points

Pause after:

```text
00_discovery: confirm online-search candidates; confirming triggers the automatic PDF-download step into review-library/paper_pdf/.
01_matrix_outline: confirm literature matrix and selected_outline.md.
02_section_blueprint: confirm section/paragraph/paper/figure mapping.
03_section_drafting: confirm section files and figure candidates.
04_figure_redraw: confirm redrawn figures.
05_first_draft: confirm merged first draft.
06_conclusion_generation: no additional confirmation (agent-run stage).
07_final_audit: confirm final draft.
08_summary_chart: no additional confirmation (agent-run stage).
09_docx_export: download final_draft.docx and verify styling in Word.
```

Do not skip a human check unless the user explicitly says to continue. Each of these is a hard stop: finish the one stage, summarize its output, name the specific check the user should perform, and end your turn there.
