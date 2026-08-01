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

Default to `source-faithful-bw` when exact source geometry is required. It creates a 4x-resolution, pure black-and-white PNG without regenerating or relocating raster text. Before thresholding, bright saturated coloured fills are made white so cyan/magenta/yellow ring interiors do not become solid black blocks; dark coloured strokes and labels become black ink.

Dense reaction-scope figures with many products, substituent grids, yields, or example panels must use `source-faithful-bw`, not a generative redraw. Reconstructing dozens of chemical structures from a raster scope image can alter bond order, ring topology, and substituents. The source-faithful path preserves those pixels and only normalizes resolution, contrast, and black-and-white presentation.

Complex multi-panel reaction-overview, strategy, background, comparison, or rearrangement figures must use `source-faithful-color`. This creates a 4x PNG while retaining every source pixel's geometry and scientific color encoding. Do not use a generative redraw for these figures: broad image-level ink checks can miss a chemically serious local mutation in one panel.

For the `source-faithful-outline-color` profile, whiten only the broad interior of a bright coloured filled shape. Never whiten, hollow, fade, blur, or break a coloured word, glyph, label, arrow, bond, or symbol: coloured typography must remain solid, continuous, crisp, and in its original colour.

Use this routing order:

1. A requested curved-arrow-only mechanism edit: `ai-edit` with `mechanism-arrow-straighten`.
2. A dense reaction/substrate scope: `source-faithful-bw`.
3. A complex multi-panel overview, strategy, comparison, background, rearrangement, catalytic-cycle, kinetic-investigation, or total-synthesis figure: `source-faithful-color`. Strategy figures with bright cyan/green/orange interior fills use `source-faithful-outline-color`: retain dark coloured outlines and all symbols, but whiten only bright saturated interiors.
4. A simple single-transformation scheme without the above indicators: gated `ai-edit`, with no OCR extraction, OCR prompt injection, OCR masking, OCR text restoration, or OCR comparison.

Low-resolution or thin-stroke schemes are forced to `source-faithful-bw`, even when their caption suggests a simple transformation. The black-and-white renderer detects saturated red/blue/other coloured source ink at native resolution before enlargement, converts it to continuous black strokes, and uses sharpened neutral-grey antialiasing only to smooth their black edges. It hollows only broad bright fills before conversion. A generative model must never infer missing thin bonds, labels, or stereochemical marks from a small raster.

Tall portrait multi-step figures are forced to `source-faithful-bw`. Their full source canvas and every stacked panel are preserved at 4x resolution; broad bright colour interiors are whitened and all remaining chemical strokes, labels, and symbols are rendered as black line art. Do not use a generative edit for these figures: it can retain the nominal aspect ratio while reflowing or clipping the lowest panel.

Generative edits must preserve the source aspect ratio even when the provider only returns a square image. Before upload, place every non-square source unchanged inside a centered square white wrapper; explicitly require the model to leave those technical padding bands blank. After generation, crop the recorded content rectangle back out and save the result at the exact source width and height. Never stretch a square provider response directly into a wide or tall chemistry canvas. The normalized PNG remains the single base image used by the online SVG editor, so SVG coordinates and saved raster dimensions continue to match.

Every provider result is saved directly to Stage 7 **Redrawn Output**. The chemistry-integrity gate remains diagnostic metadata: a warning is recorded beside a saved image but it does not require a separate confirmation action or suppress the output path.

## OCR Policy

Stage 7 redraw no longer invokes OCR. Standard `ai-edit` sends the original source pixels and the structure-locking prompt directly to the image-edit provider; its acceptance relies on source-pixel line-geometry checks. Source-faithful modes use only local image processing. This prevents OCR from masking, replacing, or misreading chemical text, bonds, and symbols.

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

Every redrawn figure requires human verification against the source. Use `source-faithful-bw` when geometry must remain identical; use `ai-edit` only for simple schemes and accept only outputs that pass the source-pixel geometry check.

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

`redrawn_figure_manifest.json` must keep `needs_human_check: true` for redrawn images. New AI outputs record `chemistry_integrity`, the exact Stage 6 `source_image`, and its SHA-256 identity. Stage 8 inserts completed Stage 7 rows with an existing `redrawn_image`, but an output with `chemistry_integrity: failed`, `needs_human_arrow_check`, or `output_disposition: saved_with_integrity_warning` remains preview-only until an explicit `human_approval` is stored. That approval must be bound to the current source-image and output-image hashes, and it must be invalidated whenever Stage 6 selects a different source. Do not filter otherwise-safe outputs by a hard-coded render-mode list, so future safe profiles remain coupled to the draft.

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
