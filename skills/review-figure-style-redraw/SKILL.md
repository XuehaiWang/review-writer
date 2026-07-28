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

Use one unified style preset: `organic-review-structure-locked-v2`. The presentation target is crisp medium-weight black line art, a pure white background, even contrast, clean antialiasing, consistent line caps and arrowheads, and no decorative gradients, shadows, textures, or grayscale fills.

Scientific content is immutable. Every atom, substituent, ring, bond order, allene, wedge/dash, charge, radical, stereochemical descriptor, reaction arrow, process branch, reagent, condition, yield, label, panel, and table value must remain scientifically and topologically identical to the source. Permitted presentation changes are limited to stroke appearance, background cleanup, contrast, and non-scientific borders within their existing bounds.

Default to `source-faithful-bw` when exact source geometry is required. It creates a 4x-resolution, pure black-and-white PNG without regenerating or relocating raster text.

Dense reaction-scope figures with many products, substituent grids, yields, or example panels must use `source-faithful-bw`, not a generative or OCR-hollow redraw. Reconstructing dozens of chemical structures from a raster scope image can alter bond order, ring topology, and substituents. The source-faithful path preserves those pixels and only normalizes resolution, contrast, and black-and-white presentation.

Complex multi-panel reaction-overview, strategy, background, comparison, or rearrangement figures must use `source-faithful-color`. This creates a 4x PNG while retaining every source pixel's geometry and scientific color encoding (for example, colored products, radical markers, or substituent circles). Do not use a generative or OCR-hollow redraw for these figures: broad image-level ink checks can miss a chemically serious local mutation in one panel.

Use this routing order:

1. A requested curved-arrow-only mechanism edit: `ai-edit` with `mechanism-arrow-straighten`.
2. A dense reaction/substrate scope: `source-faithful-bw`.
3. A complex multi-panel overview, strategy, comparison, background, or rearrangement figure: `source-faithful-color`.
4. A simple single-transformation scheme without the above indicators: gated `ocr-hollow-ai`, retaining a rejected preview only if the gate fails.

`ai-edit` and `ocr-hollow-ai` are accepted only after the chemistry-integrity gate passes. The gate requires source-content preservation, bidirectional line-geometry fidelity (no new/displaced lines and no missing source lines), and mode-appropriate OCR/text protection. A failed gate clears the usable output path so the image cannot enter a draft or final release.

## Experimental OCR Guard for AI Edit

For standard `--render-mode ai-edit`, the script can invoke local `tesseract` before and after image editing. Its source transcription is appended to the standard edit prompt only as an advisory preservation constraint. The output transcription is compared token-by-token with the source transcription; any missing token is recorded in `redrawn_figure_manifest.json` as `missing_ocr_tokens` and sets `ocr_check_status` to `needs_human_check`.

The project automatically detects a portable Tesseract installation at `.tmp/tesseract/runtime/tesseract.exe`; it otherwise uses `TESSERACT_CMD`, the system `PATH`, or an explicit `--tesseract-cmd`. Select its installed language with `--ocr-language` (default: `eng`). OCR uses page segmentation mode 3 with a 300-DPI assumption so low-resolution scheme labels are less likely to be missed. AI-edited outputs fail closed when required OCR is unavailable. OCR never corrects chemical structures automatically; protected modes restore detected source text regions from original pixels.

`mechanism-arrow-straighten` does not invoke OCR at all: chemical bonds and element-labelled fragments are frequently misclassified as text, and neither OCR extraction, text-box restoration, nor output token comparison is reliable for this strict local edit.

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

## Mechanism Arrow Straightening

For a supplied chemical reaction mechanism figure that must retain its exact source appearance, use the dedicated profile below. It is a strict local edit, not a redraw:

```bash
python /home/ps/review-writer/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root /home/ps/review-writer \
  --project-id <project_id> \
  --figure-id <figure_id> \
  --render-mode ai-edit \
  --edit-profile mechanism-arrow-straighten \
  --require-redrawn
```

This profile permits only one change: replace every curved/arc/free-form process arrow with a straight arrow or a horizontal-vertical right-angle arrow. It explicitly requires preservation of every arrow’s count, start, end, direction, color, thickness, and connection relationship. It retains blue, black, and magenta/purple arrow classes and locks every molecular structure, bond, label, charge, radical, oxidation state, typography, layout coordinate, canvas dimension, and white background.

For this deployment, do not generate or send an edit mask for mechanism figures. The provider receives the original image, allowing short or connected curved arrows to be considered even when automatic component detection cannot isolate a safe corridor. The strict local-edit prompt and post-generation source-pixel geometry gate are mandatory; any output that changes chemistry or non-arrow content remains rejected.

After each mechanism edit, run the mechanism source-fidelity gate. It permits localized arrow displacement but rejects the image when more than 28% of source ink is unmatched, more than 14% new/displaced ink appears, or total output ink falls below 75% of the source. No OCR transcription, OCR text-box restoration, or OCR output comparison is used by this profile. Geometry checks run inside each attempt; if the second attempt fails, clear the usable output path and report a fidelity/integrity failure. Preserve the final provider result as `rejected_preview_image` for Stage 7 inspection, but label it as rejected and never allow it into manuscript insertion.

Do not use `ocr-hollow-ai` or `source-faithful-bw` with this profile: the former masks text and changes graphics, while the latter performs no arrow edit. The result remains subject to mandatory human comparison against the source, including an arrow-by-arrow count and routing check.

### Manual Arrow-Path Fallback (Stage 7)

If the image-edit provider fails the mechanism integrity gate, do not approve, reuse, or repeatedly retry its partial output. In the Stage 7 **Figures** page, select the source figure and choose **手动编辑机理箭头**. This opens the exact source pixels on a local canvas: erase only the curved-arrow stroke with the eraser, then click the start point, bends, and endpoint of each replacement and choose **完成当前箭头**. Select the original arrow colour and line width for every path. The saved PNG is the original image with only these local pixel edits; no OCR, generated chemistry, or text reconstruction is involved.

Manual arrow edits are stored as `manual-arrow-edit` records, together with a JSON audit trail of paths, colours, widths, and timestamp in `03_figure_redraw/manual_arrow_edits/`. They are never eligible for automatic manuscript insertion and require a human arrow-by-arrow check of count, endpoints, direction, routing, colour, and non-arrow content before use.

The Stage 7 manual editor can use either the original source image or the current AI-redrawn/preview image as its base. It saves a PNG for dashboard display and a full-image SVG beside the audit record. The SVG is generated by tracing the complete selected image into grouped SVG paths (including structures, labels, colours, and all linework), with no embedded `<image>` raster element and no OCR or molecular reconstruction. Erasures and replacement arrows remain a dedicated editable vector-overlay group.

Stage 7 also provides an **online SVG editor**. It first creates that complete SVG trace of the selected base image, then opens the full vector figure in the workspace. Every traced connected component can be selected, moved, deleted, restored with Undo, downloaded, and saved; replacement arrows can additionally be removed, erased, or redrawn as vector paths. Existing manual-arrow audit paths are reloaded into the SVG workspace. Because source components may correspond to molecular structures, labels, or bonds, warn the user before all-figure edits and preserve the complete edited vector SVG alongside the dashboard PNG.

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
--edit-profile
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

`redrawn_figure_manifest.json` must keep `needs_human_check: true` for redrawn images. New AI outputs record `chemistry_integrity`. `source-faithful-bw` rows are eligible for insertion; `ocr-hollow-ai` rows are eligible only when content fidelity, bidirectional structural fidelity, and chemistry integrity pass. Mechanism-arrow edits remain blocked from automatic manuscript insertion until their arrow topology is checked by a human.

If no figure is redrawn successfully, return to `review-section-drafting-figure-picking` and fix `source_image_path`, `source_caption_text`, or the selected candidate list instead of moving to draft merge. To intentionally produce a no-figure manuscript (only when the user explicitly approves), create `03_figure_redraw/skip_reason.md` with a one-line justification. The orchestrator and final audit treat this file as the only valid opt-out; without it, drafts with zero figures fail the hard gate.

## Human Check

The human must compare every redrawn image with the original source and verify:

```text
all structures, labels, conditions, panels, and table values are unchanged
all single/double/triple/aromatic bonds, rings, allenes, wedges, dashes, charges, radicals, and stereochemical marks are unchanged
all reaction/process arrows retain their count, endpoints, direction, branching, and sequence
no chemistry meaning changed
```

Suggested continuation message:

```text
已确认统一重绘图片无内容错误，进入全文合并与统一润色阶段。
```
