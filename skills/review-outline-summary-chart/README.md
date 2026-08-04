# Review Outline Summary Chart

This deterministic skill creates one full-review chart between final-audit
approval and DOCX export.

## Orchestrated Contract

- Orchestrated use requires `05_final_audit/final_draft.md` and `--scope full`.
- Write HTML, JSON, and one full-review PNG next to the selected draft.
- JSON records resolved `stats.draft_source`, `stats.draft_sha256`,
  `stats.generation_scope`, and `stats.html_sha256`; the draft digest is an
  exact-byte SHA-256.
- Orchestrated completion requires scope `full`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- JSON `stats.image_manifest` records each PNG path and exact-byte SHA-256.
- A missing, wrong-source, stale, or hash-mismatched chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Quick Start

```bash
python scripts/generate_review_summary_chart.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --scope full
```

The command writes `review_summary_chart.html`, `review_summary_chart.json`,
and `review_summary_chart.png` beside the selected draft. The JSON includes the
section hierarchy, summaries, numeric callouts, mapped paper IDs, source path,
source digest, generation scope, HTML digest, and PNG image manifest.

Optional `04_first_draft/citations.json` resolves `[n]` callouts to paper IDs.
PNG generation uses Pillow and is fully offline; opening the HTML may request
Mermaid from its configured CDN.
