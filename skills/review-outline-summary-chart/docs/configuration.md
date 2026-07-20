# Configuration

## Orchestrated Contract

- Orchestrated use requires `05_final_audit/final_draft.md` and `--scope both`.
- Write HTML, JSON, a full-review PNG, and all manuscript-body section PNGs
  next to the selected draft.
- JSON records resolved `stats.draft_source`, `stats.draft_sha256`,
  `stats.generation_scope`, and `stats.html_sha256`; the draft digest is an
  exact-byte SHA-256.
- Orchestrated completion requires scope `both`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current dual chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- Validate `stats.image_manifest` paths and SHA-256 values before DOCX export.
- A missing, wrong-source, stale, or hash-mismatched chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Command

```bash
python scripts/generate_review_summary_chart.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --scope both
```

| Argument | Requirement |
|---|---|
| `--review-root` | Root containing `review-projects/` |
| `--project-id` | Required project directory |
| `--scope` | Use `both` for orchestration; other values are standalone-only |

The script writes HTML/JSON plus `review_summary_chart.png` and numbered
`review_section_chart_*.png` files beside whichever draft it selects. Standalone
selection checks `05_final_audit/final_draft.md`, then
`04_first_draft/first_draft.md`, then
`02_section_drafting/section_drafts.md`. There are no environment variables or
API credentials. Pillow is required for offline PNG rendering.
