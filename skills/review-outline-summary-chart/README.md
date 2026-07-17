# Review Outline Summary Chart

This deterministic skill creates a full-review chart and per-section charts
between final-audit approval and DOCX export.

## Orchestrated Contract

- Orchestrated use requires `05_final_audit/final_draft.md` and `--scope both`.
- Write both HTML and JSON next to the selected draft.
- JSON records resolved `stats.draft_source`, `stats.draft_sha256`,
  `stats.generation_scope`, and `stats.html_sha256`; the draft digest is an
  exact-byte SHA-256.
- Orchestrated completion requires scope `both`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current dual chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- A missing, wrong-source, or stale chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Quick Start

```bash
python scripts/generate_review_summary_chart.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --scope both
```

The command writes `review_summary_chart.html` and
`review_summary_chart.json` beside the selected draft. The JSON includes the
section hierarchy, summaries, numeric callouts, mapped paper IDs, source path,
source digest, generation scope, and HTML digest.

Optional `04_first_draft/citations.json` resolves `[n]` callouts to paper IDs.
The generator itself is standard-library processing; opening the HTML may
request Mermaid from its configured CDN.
