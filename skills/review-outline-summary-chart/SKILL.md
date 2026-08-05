---
name: review-outline-summary-chart
description: Use when an approved final review Markdown needs one current full-review summary chart before document export.
---

# Review Outline Summary Chart

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Generate the single full-review Mermaid summary only after the
final-audit checkpoint has approved the final draft.

## Orchestrated Contract

- Orchestrated use requires `05_final_audit/final_draft.md` and `--scope full`.
- Write HTML, JSON, and one full-review PNG next to the selected draft.
- JSON records the resolved source in `stats.draft_source` and its exact-byte
  SHA-256 in `stats.draft_sha256`, plus scope in `stats.generation_scope` and
  the exact HTML bytes in `stats.html_sha256`.
- Orchestrated completion requires scope `full`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- JSON `stats.image_manifest` records every PNG path and exact-byte SHA-256.
- A missing, wrong-source, stale, or hash-mismatched chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Inputs and Outputs

The orchestrated source is
`review-projects/<project_id>/05_final_audit/final_draft.md`. Optional
`04_first_draft/citations.json` maps numeric callouts to paper IDs, and the
blueprint and topic input can enrich labels.

The selected draft directory receives:

```text
review_summary_chart.html
review_summary_chart.json
review_summary_chart.png
```

`--scope full` is mandatory for orchestrated use because the publication flow
keeps one overall chart and does not generate per-section summary images.

## Run

```bash
python skills/review-outline-summary-chart/scripts/generate_review_summary_chart.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --scope full
```

For standalone inspection, the selector may fall back to
`04_first_draft/first_draft.md` and then
`02_section_drafting/section_drafts.md`. Those outputs remain next to the
selected draft and are not accepted as final-stage artifacts.

## Chart Contents

The HTML contains metadata, statistics, and a full-review Mermaid flowchart.
JSON contains the same outline, summaries, callouts,
mapped paper IDs, source path, draft digest, generation scope, and HTML digest. Citation callouts remain countable
when `citations.json` is absent, but paper leaves cannot then be resolved.
