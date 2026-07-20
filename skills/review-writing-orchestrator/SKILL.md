---
name: review-writing-orchestrator
description: Use when a review-writing project needs stage ordering, artifact gates, and human approval checkpoints from discovery through DOCX export.
---

# Review Writing Orchestrator

Use this skill to identify the next stage, enforce its artifact contract, and
pause only at the established human checkpoints.

## Workflow

```text
1. review-topic-paper-discovery
2. review-literature-matrix-outline
3. review-section-blueprint
4. review-section-drafting-figure-picking
5. review-figure-style-redraw
6. review-draft-merge-polish
7. review-conclusion-generator
8. review-final-audit-release
9. review-outline-summary-chart
10. review-export-docx
```

## Stage Contract

1. Discover 20-30 papers for the confirmed topic.
2. Build the fixed-field literature matrix and approved outline.
3. Map sections, paragraphs, papers, and figures in the blueprint.
4. Draft one file per section and select figure candidates.
5. Redraw approved figures or record the permitted no-figure reason.
6. Merge and polish the section files into `04_first_draft/first_draft.md`.
7. Generate and validate the grounded conclusion without adding a checkpoint.
8. Integrate that conclusion, then audit and release `05_final_audit/final_draft.md`.
9. Generate full-review and per-section charts without adding a checkpoint.
10. Export DOCX only from the approved, current final-draft artifacts.

## Human Check Points

Pause after discovery, matrix/outline, blueprint, section drafting, figure
redraw, first draft, final audit, and final DOCX styling review. In particular:

- first-draft approval is the gate before conclusion generation;
- final-audit approval is the gate before summary-chart generation;
- Conclusion generation and summary-chart generation add no separate human confirmation.

Do not skip a human check unless the user explicitly says to continue.

## Hard Gates

- First draft: `04_first_draft/first_draft.md` and a readable `citations.json`
  contract must exist; figures, numeric callouts, references, and image paths
  must pass the existing draft checks. Malformed, empty, or unsupported maps
  report `invalid_citations_json` and block progress.
- Conclusion: both `conclusion_generated.md` and
  `conclusion_quality_report.json` must exist, validation must pass, and the
  generated Markdown must contain at least two paragraphs, numeric `[n]`
  callouts, and no raw paper IDs. Substantive 2-3 paragraph parity is required
  between the Markdown and report, including nonblank content and word/count fields.
- Final audit: `final_draft.md` must contain exactly one integrated conclusion before `References`.
  Receipt validation requires current exact-source hashes and the generated
  heading/callout identities in `conclusion_integration.json`; the integrated
  conclusion must be scanned and the audit must have no blocking issues before
  the final-audit checkpoint can be approved.
- Summary chart: both HTML and JSON must be generated from the current
  `05_final_audit/final_draft.md`; `stats.draft_source` must resolve to that
  file and `stats.draft_sha256` must be a matching SHA-256 of its exact bytes.
  The dual chart bundle additionally requires `stats.generation_scope` equal
  to `both`, `stats.html_sha256` matching the exact HTML bytes, and a complete
  `stats.image_manifest` covering the full-review PNG and every body-section
  PNG with matching exact-byte SHA-256 values.
- DOCX: a missing, wrong-source, stale, hash-mismatched, or unmatched chart
  blocks export. The exporter inserts the full chart before the body and each
  section chart after its matching heading.

The existing manuscript gates remain binding: a figure (or approved
`03_figure_redraw/skip_reason.md`), inline citations, a non-empty References
section, complete callout/reference mapping, resolvable image paths, and no
unresolved source-figure placeholders or final-audit blockers.

All-ten-stage completion is reported only when every stage artifact and semantic
gate above passes in the exact workflow order.

## Status

```bash
python skills/review-writing-orchestrator/scripts/project_status.py \
  --review-root <review-root> \
  --project-id <project_id>
```
