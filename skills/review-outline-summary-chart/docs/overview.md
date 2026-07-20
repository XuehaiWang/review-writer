# Review Summary Chart Overview

## Purpose

Summarize an approved final review as a full-review Mermaid flowchart and
per-section evidence charts before document export.

## Orchestrated Contract

- Orchestrated use requires `05_final_audit/final_draft.md` and `--scope both`.
- Write HTML, JSON, a full-review PNG, and all manuscript-body section PNGs
  next to the selected draft.
- JSON includes resolved `stats.draft_source`, `stats.draft_sha256`,
  `stats.generation_scope`, and `stats.html_sha256`; the draft digest is an
  exact-byte SHA-256.
- Orchestrated completion requires scope `both`, the JSON/current-draft hash,
  and the exact HTML-byte hash to match the current dual chart bundle.
- JSON-only/HTML-only output cannot satisfy the stage.
- Fallback artifacts do not satisfy the orchestrated summary stage;
  standalone selection remains final > first > section draft.
- JSON records every PNG path and SHA-256 in `stats.image_manifest`.
- A missing, wrong-source, stale, or hash-mismatched chart blocks DOCX export.
- Generation makes no network request, though rendered HTML may load Mermaid from a CDN.

## Processing

1. Select the draft and read its exact bytes.
2. Parse Markdown headings into a section hierarchy.
3. Classify sections and extract concise summaries.
4. Resolve numeric callouts through `citations.json` when available.
5. Generate the full-review and per-section Mermaid definitions.
6. Render an offline full-review PNG and one PNG per manuscript body section.
7. Save HTML, JSON, PNGs, and exact-byte provenance beside the selected draft.

## JSON Provenance

The `stats` object contains section/word/citation counts plus the absolute,
resolved draft source, draft digest, generation scope, and exact HTML-byte
digest plus `image_manifest`. The status gate compares the draft, HTML, and
every PNG hash; editing any artifact after chart generation makes the bundle
stale.

## HTML Behavior

The HTML presents metadata, statistics, section cards, and Mermaid charts.
File generation is offline. Rendering Mermaid in a browser can use the CDN
referenced by the HTML, so viewing and generating have different network
behavior.
