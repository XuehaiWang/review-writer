---
name: review-writing-orchestrator
description: Explain current Web review-writing stage boundaries and the separate legacy filesystem status workflow without introducing extra scientific fact-card or planning gates.
---

# Review Writing Orchestrator

Use this skill to identify the next stage and its actual runtime owner. The
Web/API/PostgreSQL workflow is authoritative for hosted projects; local status
files do not approve or advance its stages.

## Current Web workflow

1. Discover papers and let the user confirm the selected set.
2. Prepare Matrix context and choose/edit the outline. Discovery confirmation
   does not launch a separate fact job.
3. Generate provisional chapter objectives, questions, retrieval directions,
   and paper roles with `review_writer_core.stages.planning.academic_planning`.
   The same Planning job first performs bounded current-topic fact analysis,
   reusing Matrix enrichment and checkpoints. Verified facts guide questions
   and paper roles but do not predeclare claims. Provider failure falls back to
   registered source passages. Preserve saved headings and confirm the candidate
   through the current Planning API; confirmation atomically promotes the exact
   Matrix, Outline, and Blueprint chain.
4. Retrieve original passages and generate each chapter through
   `review-section-drafting-figure-picking`. Check actual written claims once;
   preserve supported prose and pending/limited-evidence outcomes. Resume
   compatible completed sections when retrying.
5. Review source-figure candidates, redraw selected figures when requested,
   and use current source/output-bound approval for manuscript insertion.
6. Assemble the Draft with current citations and paragraph/figure anchors.
   Evaluation, optional optimization, and paragraph edits use the native Draft
   API and immutable versions. The optional optimization target is not a new
   mandatory publication gate; use the API's current approval/readiness state.
7. The Final service assembles the approved current Draft, front matter, and any
   generated conclusion/Overview. Present optional artifacts must be complete
   and current. Overview generation uses `review-figure-style-redraw`, not the
   Mermaid summary-chart skill. Final validation, release, and exports are
   governed by `review_writer_api.domain_services.final`.
8. Export the current final artifact to Word or PDF through the native jobs.

The cross-study synthesis skill is prompt text included in chapter writing,
not a separate model stage. The explicit fact helper remains available for
retry/offline use, but the normal path is integrated into chapter planning.
Neither section writing nor Draft optimization reconstructs the old fact-card
repair chain. Preserve source identity, citation, version, and figure-approval
checks that the runtime still enforces.

## Legacy filesystem workflow

The remaining sections document the standalone `project_status.py` contract.
They apply when explicitly using that filesystem workflow, not as extra gates
on a PostgreSQL-backed Web project. In particular, its ten-stage ordering and
mandatory Mermaid chart do not describe the current Web Final builder.

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

## Legacy stage contract

1. Discover the complete qualifying candidate set for the confirmed topic and use only the papers explicitly selected by the human reviewer.
2. Build the fixed-field literature matrix and approved outline.
3. Map sections, paragraphs, papers, and figures in the blueprint.
4. Draft one file per section and select figure candidates.
5. Redraw approved figures or record the permitted no-figure reason.
6. Merge and polish the section files into `04_first_draft/first_draft.md`.
7. Generate and validate the grounded conclusion without adding a checkpoint.
8. Integrate that conclusion, then audit and release `05_final_audit/final_draft.md`.
9. Generate the single full-review chart without adding a checkpoint.
10. Export DOCX only from the approved, current final-draft artifacts.

## Legacy human checkpoints

Pause after discovery, matrix/outline, blueprint, section drafting, figure
redraw, first draft, final audit, and final DOCX styling review. In particular:

- first-draft approval is the gate before conclusion generation;
- final-audit approval is the gate before summary-chart generation;
- Conclusion generation and summary-chart generation add no separate human confirmation.

Do not skip a human check unless the user explicitly says to continue.

## Legacy filesystem gates

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
  The chart bundle additionally requires `stats.generation_scope` equal to
  `full`, `stats.html_sha256` matching the exact HTML bytes, and a complete
  `stats.image_manifest` covering the full-review PNG with a matching
  exact-byte SHA-256 value.
- DOCX: a missing, wrong-source, stale, or hash-mismatched chart blocks export.
  The exporter inserts the full chart at the managed manuscript position.

The existing manuscript gates remain binding: a figure (or approved
`03_figure_redraw/skip_reason.md`), inline citations, a non-empty References
section, complete callout/reference mapping, resolvable image paths, and no
unresolved source-figure placeholders or final-audit blockers.

All-ten-stage completion is reported only when every stage artifact and semantic
gate above passes in the exact workflow order.

## Standalone status command

```bash
python skills/review-writing-orchestrator/scripts/project_status.py \
  --review-root <review-root> \
  --project-id <project_id>
```
