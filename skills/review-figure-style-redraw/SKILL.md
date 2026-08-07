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

Keep the three `source-faithful-*` modes as explicit maintenance tools, but do not select them automatically from the Stage 7 AI redraw button. Stage 7 now sends dense scopes, complex multi-panel figures, low-resolution schemes, coloured chemistry figures, tables, and scientific plots through type-specific, structure-locked AI edit prompts. Preserve the provider result only as a reviewable Stage 7 artifact until the relevant integrity checks and required human chemistry approval are complete.

For the generated full-review overview figure, negotiate image size from the configured provider capabilities instead of a hostname. `IMAGE_SUPPORTED_SIZES` may list sizes in retry order; unsupported landscape requests must fall back to `1024x1024`. On a square-only route, keep the template's complete landscape reading order within balanced white margins and never crop or omit a panel.

Overview layout references are versioned assets owned by this skill under `assets/overview-templates/`. Resolve the catalog and its images relative to the skill directory, independently of runtime Library contents.

For the `source-faithful-outline-color` profile, whiten only the broad interior of a bright coloured filled shape. Never whiten, hollow, fade, blur, or break a coloured word, glyph, label, arrow, bond, or symbol: coloured typography must remain solid, continuous, crisp, and in its original colour.

Use the shared image-aware router in `review_writer_core.figure_redraw_routing` so the dashboard and worker always agree. Apply this routing order:

1. Explicit reviewer type, when supplied.
2. Mechanism/catalytic-cycle source caption or explicit profile.
3. Visual closed-cycle layout detection for under-described figures such as P026.
4. Data table, reaction/substrate scope, scientific plot, or complex multi-panel evidence.
5. Significant scientific colour before low-resolution evidence, so a small coloured chemistry scheme uses the fill-removal profile instead of the generic low-resolution profile.
6. Simple reaction scheme, otherwise general scientific figure.

Use `ai-edit` for every automatically selected type. Add the corresponding type-specific prompt: scope prompts lock every example and yield; multi-panel prompts lock boundaries and reading order; low-resolution prompts prohibit inference; colour prompts remove only non-semantic fills and prohibit black blocks; table and plot prompts lock every value and data coordinate. High-risk types must record `requires_human_chemistry_approval: true`; they cannot enter Stage 8 until approval is bound to the exact source and output hashes.

Derive `edit_profile` again from the final shared classification inside the worker; never trust a stale dashboard-supplied mechanism profile for a non-mechanism figure. For `colored-chemistry`, require broad cyan/teal/other decorative ring fills to become white while preserving every perimeter and inner bond. Measure chromatic pixels inside the content bounds after generation, retry once when most fill remains, and retain a still-failing output only as an integrity-warning preview for human review.

Treat an HTTP-successful Chat Completions response with no image reference as a retryable provider failure. Retry the complete image request up to three times, use streaming first and a final non-streaming request for relays that attach images only to final JSON, accept common `content`, `images`, `data`, `result`, and `output` shapes, and include the provider's safe text excerpt plus `finish_reason` in the final error so channel refusals and text-only responses remain diagnosable.

Treat an explicit provider moderation response such as `内容被安全审核拦截`, `疑似成人内容`, `content_filter`, or `moderation` as a possible false positive only when editing the supplied scientific figure. Retry exactly once on the same source image and route with a concise, neutral prompt that identifies the upload as a peer-reviewed academic chemistry schematic with no people or photographic subject matter. Retain the same structure, label, arrow, layout, and type-specific constraints; for mechanisms, keep the arrow-only edit rule. Do not retry a generic HTTP 400, authentication failure, malformed request, or unsupported image. Record the trigger and retry outcome in `safety_moderation_retries` in the redraw manifest.

The former generic “AI comparison” override is replaced by a reviewer figure-type selector. Pass the selected value with `--figure-type`; a reviewer-selected mechanism type must still use `mechanism-arrow-straighten`, while every other selected type uses its structure-locked AI prompt.

Standard `ai-edit` redraws retain the image provider's complete output canvas and native aspect ratio. A square-only result must not be cropped or stretched back to the source ratio, because doing so can delete generated structures and labels at the canvas edges. Require safe white margins and complete source content in the prompt, then use the returned image dimensions as the SVG editor base. Pixel-local workflows normally crop the technical wrapper back to the source rectangle, but content completeness overrides exact source dimensions: detect meaningful generated ink beyond the wrapper rectangle, ignore only a narrow antialiasing halo, and forbid the fixed crop when labels or structures enter the padding. Expand the crop around all generated ink with a safe margin, or preserve the complete provider canvas when the ink reaches its boundary. Record the detection and chosen crop in `aspect_ratio_normalization`, allow the saved dimensions to become the SVG editor base, and keep the output subject to the existing chemistry gate and human approval. When the online SVG editor saves an edit based on this protected AI canvas, preserve the normalization record and treat the manual output as derived from the same allowed canvas; keep the human-approval control available even though `render_mode` becomes `manual-arrow-edit`. Do not extend this exception to a manual edit based directly on the source image or to an output without the recorded padding-content provenance.

Every provider result is saved directly to Stage 7 **Redrawn Output**. The chemistry-integrity gate remains diagnostic metadata: failed outputs stay available for viewing and download, but require explicit human approval before manuscript insertion.

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
python <review-root>/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --figure-id <figure_id> \
  --render-mode ai-edit \
  --edit-profile mechanism-arrow-straighten \
  --require-redrawn
```

This profile permits only one change: replace every curved/arc/free-form process arrow with a straight arrow or a horizontal-vertical right-angle arrow. It explicitly requires preservation of every arrow’s count, start, end, direction, color, thickness, and connection relationship. It retains blue, black, and magenta/purple arrow classes and locks every molecular structure, bond, label, charge, radical, oxidation state, typography, layout coordinate, canvas dimension, and white background.

Keep rare chemistry symbols in executable prompt source as Unicode escape sequences when practical. At runtime, automatically repair known mojibake such as `h谓`, `鈥?`, `锟`, or the Unicode replacement character, restore the exact characters `hν` and `SN2′ oxidative addition`, and continue the provider request. Do not fail or stop a redraw solely because the prompt text can be repaired deterministically.

For this deployment, do not generate or send an edit mask for mechanism figures. The provider receives the original image, allowing short or connected curved arrows to be considered even when automatic component detection cannot isolate a safe corridor. The strict local-edit prompt and post-generation source-pixel geometry gate are mandatory; any output that changes chemistry or non-arrow content remains rejected.

After each mechanism edit, run the mechanism source-fidelity gate. It permits localized arrow displacement but rejects the image when more than 28% of source ink is unmatched, more than 14% new/displaced ink appears, total output ink falls below 75% of the source, or no detectable arrow-geometry change occurred. No OCR transcription, OCR text-box restoration, or OCR output comparison is used by this profile. Geometry checks run inside each attempt; if the second attempt fails, clear the usable output path and report a fidelity/integrity failure. Preserve the final provider result as `rejected_preview_image` for Stage 7 inspection, but label it as rejected and never allow it into manuscript insertion.

Do not use `ocr-hollow-ai` or `source-faithful-bw` with this profile: the former masks text and changes graphics, while the latter performs no arrow edit. The result remains subject to mandatory human comparison against the source, including an arrow-by-arrow count and routing check.

### Manual Arrow-Path Fallback (Stage 7)

If the image-edit provider fails the mechanism integrity gate, do not approve, reuse, or repeatedly retry its partial output. In the Stage 7 **Figures** page, select the source figure and choose **手动编辑机理箭头**. This opens the exact source pixels on a local canvas: erase only the curved-arrow stroke with the eraser, then click the start point, bends, and endpoint of each replacement and choose **完成当前箭头**. Select the original arrow colour and line width for every path. The saved PNG is the original image with only these local pixel edits; no OCR, generated chemistry, or text reconstruction is involved.

Manual arrow edits are stored as `manual-arrow-edit` records, together with a JSON audit trail of paths, colours, widths, and timestamp in `03_figure_redraw/manual_arrow_edits/`. They are never eligible for automatic manuscript insertion and require a human arrow-by-arrow check of count, endpoints, direction, routing, colour, and non-arrow content before use.

The Stage 7 manual editor can use either the original source image or the current AI-redrawn/preview image as its base. It saves a PNG for dashboard display and a full-image SVG beside the audit record. The SVG is generated by tracing the complete selected image into grouped SVG paths (including structures, labels, colours, and all linework), with no embedded `<image>` raster element and no OCR or molecular reconstruction. Erasures and replacement arrows remain a dedicated editable vector-overlay group.

Stage 7 also provides an **online SVG editor**. It first creates that complete SVG trace of the selected base image, then opens the full vector figure in the workspace. Every traced connected component can be selected, moved, deleted, restored with Undo, downloaded, and saved; replacement arrows can additionally be removed, erased, or redrawn as vector paths. Existing manual-arrow audit paths are reloaded into the SVG workspace. Because source components may correspond to molecular structures, labels, or bonds, warn the user before all-figure edits and preserve the complete edited vector SVG alongside the dashboard PNG.

## API

Default recommendation for this project:

```text
base_url: https://your-image-provider.example/v1
wire_api: chat-completions
model: gpt-image-2
endpoint: /v1/chat/completions
credential: IMAGE_OPENAI_API_KEY (vip_2_image group)
```

Embed the exact current Stage 6 image in every AI request, accept either SSE or JSON where supported, and validate the returned image before saving it. The mechanism-arrow profile may use the configured `images` or `chat-completions` transport because this deployment does not send an edit mask; both routes must receive the exact source image and strict arrow-only prompt. Do not use `responses` for chemistry-preserving redraw unless the relay demonstrably supports image input and image editing through `/v1/responses`; otherwise it can generate a new figure without faithfully editing the source.

An explicitly configured secondary provider may expose image editing through `/v1/chat/completions`:

```text
IMAGE_FALLBACK_BASE_URL=https://your-fallback-image-provider.example/v1
IMAGE_FALLBACK_WIRE_API=chat-completions
IMAGE_FALLBACK_MODEL=gpt-image-2
IMAGE_FALLBACK_API_KEY=<optional; otherwise reuse OPENAI_API_KEY>
```

This optional availability fallback is for the standard AI comparison path only. Activate it after a primary `images/edits` provider returns `ALL_CHANNELS_FAILED` or HTTP 502/503/504. Do not fail over on 400/401 errors, and do not route the strict mechanism-arrow profile through Chat Completions. Record any provider switch in `redrawn_figure_manifest.json`.

## Run

```bash
python <review-root>/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root <review-root> \
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
--figure-type
--force-standard-ai-edit
--limit
--dry-run
--require-redrawn
```

If `--api-key` is omitted, the script first uses `IMAGE_OPENAI_API_KEY`; text-model credentials are only compatibility fallbacks.

Validate source resolution first when needed:

```bash
python <review-root>/skills/review-figure-style-redraw/scripts/redraw_figures.py \
  --review-root <review-root> \
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
