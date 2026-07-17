# Review Conclusion Generator Overview

## Purpose

Create the grounded ending after first-draft approval and before final audit.
The result synthesizes the review's conclusions, limitations, and future
insights rather than restating section summaries.

## Orchestrated Contract

- Commands use and default to `--mode orchestrated`; this selects only the approved first draft.
- The orchestrated input is the approved `04_first_draft/first_draft.md`.
- `04_first_draft/citations.json` is the authoritative allowed-paper and numeric-callout map.
- The Markdown is one clean heading plus 2-3 paragraphs with numeric `[n]`
  callouts and no raw `P001`, model, timestamp, or editor metadata.
- Validation must reject injected numeric callouts in model-authored content and
  reject blank paragraphs before rendering mapped callouts.
- Both `conclusion_generated.md` and `conclusion_quality_report.json` must
  exist, and `validation.passes_validation` is `true`, before the stage is
  complete.
- No-API/manual mode remains incomplete: no-API or API failure invalidates old
  required outputs and leaves the stage incomplete.
- Standalone draft fallbacks remain supported only with explicit
  `--mode standalone`, which retains final > first > section draft order but
  does not change or satisfy the orchestrated approval gate.

## Processing Model

1. Parse the complete heading hierarchy and section summaries.
2. Load mapped callouts and allowed papers from `citations.json`.
3. Add available matrix limitations, trends, blueprint claims, and notes.
4. Generate structured conclusion/challenges/insights paragraphs.
5. Render only numeric callouts and validate the saved result.

## Validation

Blocking checks cover too few paragraphs, absent/invalid citations, insufficient
length, raw metadata, and unmapped evidence. Review warnings cover excess
length, low source diversity, body overlap, and vague outlook language. Manual
review remains appropriate even when validation passes.
