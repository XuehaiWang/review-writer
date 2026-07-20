---
name: review-outline-summary-chart
description: Use when an approved final review Markdown needs a full-review and per-section summary chart before document export.
---

# Review Outline Summary Chart

Generate the full-review and per-section Mermaid summaries only after the
final-audit checkpoint has approved the final draft.

## Orchestrated Contract

- Orchestrated use requires `07_final_audit/final_draft.md` and `--scope both`.
- Write both HTML and JSON into this stage's own `08_summary_chart/` folder
  (not next to the source draft).
- JSON records the resolved source in `stats.draft_source` and its exact-byte
  SHA-256 in `stats.draft_sha256`, plus scope in `stats.generation_scope` and
  the exact HTML bytes in `stats.html_sha256`.
- Orchestrated completion requires scope `both`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current dual chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- A missing, wrong-source, or stale chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Inputs and Outputs

The orchestrated source is
`review-projects/<project_id>/07_final_audit/final_draft.md`. Optional
`05_first_draft/draft_bundle.json`'s `citation_map` field maps numeric
callouts to paper IDs, and the blueprint and topic input can enrich labels.

`08_summary_chart/` receives:

```text
review_summary_chart.html
review_summary_chart.json
```

`--scope both` is mandatory for orchestrated use because it produces the
full-review chart and all per-section charts in both required artifacts.

## Run

```bash
python skills/review-outline-summary-chart/scripts/generate_review_summary_chart.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --scope both
```

For standalone inspection, the selector may fall back to
`05_first_draft/first_draft.md` and then
`03_section_drafting/section_drafts.md`. Those outputs still land in
`08_summary_chart/` and are not accepted as final-stage artifacts.

## Chart Contents

The HTML contains metadata, statistics, a full-review Mermaid flowchart, and
per-section cards/charts. JSON contains the same outline, summaries, callouts,
mapped paper IDs, source path, draft digest, generation scope, and HTML digest. Citation callouts remain countable
when `draft_bundle.json`'s `citation_map` is absent, but paper leaves cannot then be resolved.
