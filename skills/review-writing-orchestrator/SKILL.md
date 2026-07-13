---
name: review-writing-orchestrator
description: Orchestrate the concise review-writing workflow: discovery, fixed-field literature matrix, outline/blueprint, section-file drafting, figure redraw, merge, and final audit.
---

# Review Writing Orchestrator

Before starting, locate the server's local paper storage directory (a folder containing `.pdf` files). Pass it as `--paper-dir` to `review-topic-paper-discovery` so new papers are auto-registered. If the directory is unknown, ask the user.

If the user's topic is not in English, translate it to English first. Discovery scoring and web search are English-keyword based; a non-English topic passed straight through will fail to match papers.

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
1. review-topic-paper-discovery
2. review-literature-matrix-outline
3. review-section-blueprint
4. review-section-drafting-figure-picking
5. review-figure-style-redraw
6. review-draft-merge-polish
7. review-final-audit-release
8. review-export-docx
```

For each stage, invoke that stage's skill by name (e.g. call the `review-topic-paper-discovery` skill, not just read its file). Do not read a sub-skill's `SKILL.md` yourself and improvise its steps inline — invoke the skill so it runs under its own instructions. The correct CLI invocation, required flags, required output files, and human-check gates for each stage live inside that sub-skill and are applied when it is invoked. The summary below is only a rough map of what each stage does, not a substitute for invoking it.

**Run exactly one stage per turn, then stop.** After invoking a stage's skill and its outputs are written, do not continue to the next stage in the same turn. Report what was produced, point to the human-check instructions for that stage, and explicitly ask the user to review and confirm before you proceed. Wait for the user's next message before invoking the following stage — even if the user's original request described the full end-to-end goal. The only exception is when the user explicitly says to run multiple stages back-to-back without stopping (e.g. "run all steps," "skip the checks," "continue through stage N").

## Core Contract (rough map only — always defer to the sub-skill's SKILL.md)

```text
Discovery: user topic -> expanded keywords -> score local papers + optional web search -> 20-30 papers.
Matrix: one row per paper with title, authors, keywords, abstract, ~1000-word main_content, most_relevant_figure.
Outline: use topic + matrix + writing-rule skill to create selected_outline.md.
Blueprint: convert outline into section_blueprint.json with section, paragraph, paper, and figure mapping.
Drafting: one section file per section; each paragraph normally maps to one paper and one figure/scheme/table.
Merge: combine section files into one polished first draft.
```

## Status

```bash
python3 <skill-root>/scripts/project_status.py --project-id <project_id>
```

## Human Check Points

Pause after:

```text
00_discovery: confirm 20-30 papers.
01_matrix_outline: confirm literature matrix and selected_outline.md.
02_section_blueprint: confirm section/paragraph/paper/figure mapping.
03_section_drafting: confirm section files and figure candidates.
04_figure_redraw: confirm redrawn figures.
05_first_draft: confirm merged first draft.
06_final_audit: confirm final draft.
07_docx_export: download final_draft.docx and verify styling in Word.
```

Do not skip a human check unless the user explicitly says to continue. Each of these is a hard stop: finish the one stage, summarize its output, name the specific check the user should perform, and end your turn there.
