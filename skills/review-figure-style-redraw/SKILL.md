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

Default to `source-faithful-bw`. It creates a 4x-resolution, pure black-and-white PNG from the approved source image without regenerating or relocating raster text. This is the required mode whenever font size, font appearance, baseline, and panel geometry must remain identical to the source.

`ai-edit` and `ocr-hollow-ai` redraw chemical line art. They can alter chemical bonds, arrow geometry, and ring structures, so an AI result is accepted only when both content-fidelity and bidirectional line-geometry checks pass; otherwise it must not enter a draft or final release.

## Experimental OCR Guard for AI Edit

When `--render-mode ai-edit` is selected, the script tries to invoke a local `tesseract` executable before and after image editing. The source transcription is appended to the edit prompt as an advisory preservation constraint. The output transcription is compared token-by-token with the source transcription; any missing token is recorded in `redrawn_figure_manifest.json` as `missing_ocr_tokens` and sets `ocr_check_status` to `needs_human_check`.

The project automatically detects a portable Tesseract installation at `.tmp/tesseract/runtime/tesseract.exe`; it otherwise uses `TESSERACT_CMD`, the system `PATH`, or an explicit `--tesseract-cmd`. Select its installed language with `--ocr-language` (default: `eng`). OCR is optional: if the executable is absent or fails, the redraw still completes and the manifest records `ocr_check_status: not_available`. OCR never modifies the source image or corrects chemical structures automatically.

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

Every redrawn figure requires human verification against the source. Use `source-faithful-bw` when geometry must remain identical; use `ocr-hollow-ai` only when an AI redraw is explicitly requested and accept only outputs that pass both automated fidelity checks.

When a reviewer changes a candidate in Figure Review, the next redraw run reads `human_figure_review.json` and uses that selected candidate as the source automatically.

## API

Default recommendation for this project:

```text
base_url: https://api.xiaoleai.team/v1
wire_api: images
model: gpt-image-2
endpoint: /v1/images/edits
multipart image field: image
```

Use `wire_api: images` only for explicitly approved experimental image editing. Do not use `responses` for chemistry-preserving redraw unless the relay demonstrably supports image input and image editing through `/v1/responses`; otherwise it can generate a new figure without faithfully editing the source.

## Run

```bash
python /home/ps/review-writer/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id> \
  --render-mode source-faithful-bw \
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
--ocr-language
--tesseract-cmd
--limit
--dry-run
--require-redrawn
```

If `--api-key` is omitted, the script uses `XIAOLEAI_API_KEY` for the xiaoleai endpoint, otherwise `OPENAI_API_KEY`.

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

`redrawn_figure_manifest.json` must keep `needs_human_check: true` for redrawn images. `source-faithful-bw` rows are eligible for insertion; `ocr-hollow-ai` rows are eligible only when both `content_fidelity.status` and `structural_fidelity.status` are `pass`.

If no figure is redrawn successfully, return to `review-section-drafting-figure-picking` and fix `source_image_path`, `source_caption_text`, or the selected candidate list instead of moving to draft merge. To intentionally produce a no-figure manuscript (only when the user explicitly approves), create `03_figure_redraw/skip_reason.md` with a one-line justification. The orchestrator and final audit treat this file as the only valid opt-out; without it, drafts with zero figures fail the hard gate.

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
