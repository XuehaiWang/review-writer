---
name: review-final-audit-release
description: Use when an approved first review draft and validated generated conclusion need integration, final evidence audit, and release preparation.
---

# Review Final Audit Release

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

This stage integrates the already validated conclusion before scanning or
editing the final manuscript. It corrects and releases supported content; it
does not invent new arguments or remap paper identities.

## Required Inputs

Read:

```text
review-projects/<project_id>/04_first_draft/first_draft.md
review-projects/<project_id>/04_first_draft/conclusion_generated.md
review-projects/<project_id>/04_first_draft/conclusion_quality_report.json
review-projects/<project_id>/04_first_draft/citations.json
review-projects/<project_id>/04_first_draft/merge_report.md
review-projects/<project_id>/04_first_draft/remaining_issues.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/02_section_drafting/section_tasks.json
review-projects/<project_id>/02_section_drafting/section_drafts.json
review-projects/<project_id>/02_section_drafting/figure_candidates.json
```

Also read the redraw manifest/report when figures were redrawn. Reopen local
paper metadata or Markdown/PDF for high-risk claims.

## Required Order

Follow this order exactly:

1. Read `conclusion_quality_report.json` and verify `validation.passes_validation` is `true`.
2. From the project root, run `integrate_generated_conclusion.py` to seed
   `05_final_audit/final_draft.md`. It replaces conclusion-like sections and
   places one canonical generated conclusion before `References`, then writes
   `conclusion_integration.json`.
3. Run `final_audit_scan.py` against that integrated `05_final_audit/final_draft.md`.
4. Perform the semantic and format audit without changing mapped paper identities.

Example commands:

```bash
python skills/review-final-audit-release/scripts/integrate_generated_conclusion.py \
  --review-root <review-root> --project-id <project_id>
python skills/review-final-audit-release/scripts/final_audit_scan.py \
  --review-root <review-root> --project-id <project_id>
```

## Blocking Conditions

Release is blocked by an absent conclusion, a conclusion after `References`,
or duplicate conclusion-like sections. It is also blocked by failed conclusion
validation, format-scan `blocking_issues`, missing/empty References, citation
or identity mismatches, broken image paths, unresolved source-figure
placeholders, or an unapproved no-figure manuscript.

## Integration Receipt

`conclusion_integration.json` records the exact-source hashes for the approved
first draft and generated conclusion, their resolved paths, the inserted
heading, generated callout identities, and the integrated final-draft seed
hash. Status re-derives the heading and callouts from the hashed generated
conclusion and validates them against the receipt and current final draft.
Audited prose edits are permitted, but the recorded heading and citation
identity set remain binding; changed mapped citation identities block release.

## Audit Contract

Semantic audit checks evidence support, citation fit, chemistry accuracy,
overclaiming, caveats, outline fit, synthesis quality, comparison axes, and
figure relevance. Format audit checks headings, references, figure/table
callouts, captions, abbreviations, placeholders, and links.

When evidence cannot verify a claim, weaken it, remove it, or record it in
`final_remaining_issues.md`. Do not alter the numeric-callout-to-paper mapping
in `citations.json`.

## Outputs

Create under `review-projects/<project_id>/05_final_audit/`:

```text
format_scan.json
format_scan.md
content_audit_report.md
format_audit_report.md
conclusion_integration.json
final_draft.md
final_remaining_issues.md
release_report.md
```

`final_draft.md` is clean manuscript text with one canonical conclusion and
no inline TODOs or editor notes. The reports list fixes, unresolved risks,
evidence files, the source first draft, and readiness for export.

## Human Check Point

Stop after this stage. The human confirms scientific acceptability, remaining
risks, figures, and references before summary-chart generation.
