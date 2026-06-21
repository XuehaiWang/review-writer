---
name: review-writing-orchestrator
description: Orchestrate the concise review-writing workflow: discovery, fixed-field literature matrix, outline/blueprint, section-file drafting, figure redraw, merge, and final audit.
---

# Review Writing Orchestrator

Use after the paper library metadata has been prepared.

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

## Core Contract

```text
Discovery: user topic -> extracted keywords -> search 8 LLM tag categories -> 20-30 papers.
Matrix: one row per paper with title, authors, keywords, abstract, ~1000-word main_content, most_relevant_figure.
Outline: use topic + matrix + writing-rule skill to create selected_outline.md.
Blueprint: convert outline into section_blueprint.json with section, paragraph, paper, and figure mapping.
Drafting: one section file per section; each paragraph normally maps to one paper and one figure/scheme/table.
Merge: combine section files into one polished first draft.
```

## Status

```bash
python /home/ps/review-writer/skills/review-writing-orchestrator/scripts/project_status.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id>
```

## Human Check Points

Pause after:

```text
00_discovery: confirm 20-30 papers.
01_matrix_outline: confirm literature matrix and selected_outline.md.
01_matrix_outline/section_blueprint: confirm section/paragraph/paper/figure mapping.
02_section_drafting: confirm section files and figure candidates.
03_figure_redraw: confirm redrawn figures.
04_first_draft: confirm merged first draft.
05_final_audit: confirm final draft.
05_final_audit (docx): download final_draft.docx and verify styling in Word.
```

Do not skip a human check unless the user explicitly says to continue.
