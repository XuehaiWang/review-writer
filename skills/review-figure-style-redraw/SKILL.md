---
name: review-figure-style-redraw
description: Redraw selected source figures into a unified review style while preserving all technical content, using approved figure candidates and a configurable OpenAI-compatible image edit API. Use after section drafting has produced figure_candidates.json and before manuscript merge.
---

# Review Figure Style Redraw

Use this skill after `figure_candidates.json` has been human-checked.

This stage uses a script because file resolution, API calls, and manifests must be stable.

In the normal full review workflow, do not silently skip this stage. A no-image manuscript is allowed only when the user explicitly says to skip figures or when the section drafting report gives a defensible no-figure reason.

## Inputs

Read:

```text
review-projects/<project_id>/03_section_drafting/figure_candidates.json
review-projects/<project_id>/03_section_drafting/section_drafting_report.md
```

Each useful candidate should include:

```text
paper_id
source_label
source_type
source_pdf
source_content_list
source_image_path
source_caption_text
recommended_action
```

If `source_image_path` is missing, the script attempts to resolve it from metadata and `content_list.json`.

## Redraw Rule

Change visual style only.

Preserve:

```text
every structural/diagrammatic element and connector or relationship line
labels and symbols
quantitative values (parameters, conditions, units, results)
directional arrows and panel order
table values and figure labels
```

What counts as a "structural element" depends on the subject matter of the figure (e.g. reaction schemes and molecular structures for a chemistry review, block/flow diagrams for a systems review, plots or charts for a data-heavy review). Identify what the source figure actually shows before redrawing it, and preserve that content faithfully — do not assume any specific subject matter.

Every redrawn figure requires human verification against the source.

## API

Default recommendation for this project:

```text
base_url: https://naiccc.com
wire_api: images
model: gpt-image-2
endpoint: /v1/images/edits
```

Use `wire_api: images` for real source-image editing. Do not use `responses` for content-preserving redraw unless the relay demonstrably supports image input and image editing through `/v1/responses`; otherwise it can generate a new figure without faithfully editing the source.

## Run

```bash
python3 <skill-root>/scripts/redraw_figures.py \
  --project-id <project_id> \
  --base-url https://naiccc.com \
  --wire-api images \
  --api-key <key> \
  --require-redrawn
```

`--review-root` defaults to the current working directory. `<skill-root>` is the directory containing this `SKILL.md`.

Useful options:

```text
--figures-file
--model
--quality
--background
--output-format
--style-name
--limit
--dry-run
--require-redrawn
```

If `--api-key` is omitted, the script uses `OPENAI_API_KEY`.

Validate source resolution first when needed:

```bash
python3 <skill-root>/scripts/redraw_figures.py --project-id <project_id> --dry-run
```

## Outputs

Write under:

```text
review-projects/<project_id>/04_figure_redraw/
```

Create:

```text
style_config.json
source_figure_manifest.json
redrawn_figure_manifest.json
figure_redraw_report.md
source/
redrawn/
```

`redrawn_figure_manifest.json` must keep `needs_human_check: true` for redrawn images.

If no figure is redrawn successfully, return to `review-section-drafting-figure-picking` and fix `source_image_path`, `source_caption_text`, or the selected candidate list instead of moving to draft merge.

## Human Check

The human must compare every redrawn image with the original source and verify:

```text
all structures/diagrams, labels, values, panels, and table entries are unchanged
no technical or factual meaning changed
```

Suggested continuation message:

```text
已确认统一重绘图片无内容错误，进入全文合并与统一润色阶段。
```
