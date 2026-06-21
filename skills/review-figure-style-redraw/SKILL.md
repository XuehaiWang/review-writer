---
name: review-figure-style-redraw
description: Redraw selected source figures or schemes into a unified organic review style while preserving chemistry and content, using approved figure candidates and a configurable OpenAI-compatible image edit API. Use after section drafting has produced figure_candidates.json and before manuscript merge.
---

# Review Figure Style Redraw

Use this skill after `figure_candidates.json` has been human-checked.

This stage uses a script because file resolution, API calls, and manifests must be stable.

In the normal full review workflow, do not silently skip this stage. A no-image manuscript is allowed only when the user explicitly says to skip figures or when the section drafting report gives a defensible no-figure reason.

## Inputs

Read:

```text
review-projects/<project_id>/02_section_drafting/figure_candidates.json
review-projects/<project_id>/02_section_drafting/section_drafting_report.md
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
chemical structures
bond connectivity
stereochemistry
atom and substituent labels
reagents, catalysts, solvents, temperatures, times, yields
reaction arrows and panel order
table values and figure labels
```

Every redrawn figure requires human verification against the source.

## API

Default recommendation for this project:

```text
base_url: https://naiccc.com
wire_api: images
model: gpt-image-2
endpoint: /v1/images/edits
```

Use `wire_api: images` for real source-image editing. Do not use `responses` for chemistry-preserving redraw unless the relay demonstrably supports image input and image editing through `/v1/responses`; otherwise it can generate a new figure without faithfully editing the source.

## Run

```bash
python /home/ps/review-writer/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id> \
  --base-url https://naiccc.com \
  --wire-api images \
  --api-key <key> \
  --require-redrawn
```

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
python /home/ps/review-writer/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id> \
  --dry-run
```

## Outputs

Write under:

```text
review-projects/<project_id>/03_figure_redraw/
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
all structures, labels, conditions, panels, and table values are unchanged
no chemistry meaning changed
```

Suggested continuation message:

```text
已确认统一重绘图片无内容错误，进入全文合并与统一润色阶段。
```
