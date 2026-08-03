---
name: review-conclusion-generator
description: Use when a merged review draft has been human-approved and needs a grounded conclusion, challenges, and future-insights section before final audit.
---

# Review Conclusion Generator

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Generate a grounded ending that synthesizes the approved review without
recapping it or introducing unmapped papers.

## Orchestrated Contract

- Commands use and default to `--mode orchestrated`; this selects only the approved first draft.
- The orchestrated input is the approved `04_first_draft/first_draft.md`.
- `04_first_draft/citations.json` is the authoritative allowed-paper and numeric-callout map.
- Output Markdown is one clean heading plus 2-3 paragraphs with numeric `[n]`
  callouts and no raw `P001`, model, timestamp, or editor metadata.
- Validation must reject injected numeric callouts in model-authored content and
  reject blank paragraphs before rendering mapped callouts.
- Write `conclusion_generated.md` and `conclusion_quality_report.json` under
  `04_first_draft/`; the stage is complete only when both exist and
  `validation.passes_validation` is `true`.
- No-API/manual mode remains incomplete: no-API or API failure invalidates old
  required outputs and leaves the stage incomplete, even when the no-API
  command exits successfully.
- Standalone draft fallbacks remain supported only with explicit
  `--mode standalone`, which retains final > first > section draft order but
  does not change or satisfy the orchestrated approval gate.

## Supporting Inputs

Use the literature matrix, section blueprint, paper reading notes, and topic
input when available. The approved draft and `citations.json` remain binding:
paper IDs are used internally for grounding, while rendered prose uses only
their mapped numeric callouts.

## Output Quality

The heading and paragraphs cover overall conclusions, concrete
challenges/limitations, and defensible future insights. They should abstract,
compare, and judge across the review, stay connected to the full draft, avoid
vague outlooks, and use more than one mapped source where evidence permits.

Validation requires 2-3 substantive paragraphs, valid numeric callouts,
allowed mapped papers, adequate length, and no blocking issues. Warnings still
require review but do not change the meaning of `passes_validation`.

## Run

```bash
python skills/review-conclusion-generator/scripts/generate_conclusion1.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --mode orchestrated \
  --api-key <key> \
  --base-url <base-url> \
  --model <model>
```

The command reads CLI values, then environment values, then built-in defaults.
With no resolved API key it writes `conclusion_context.json` and
`conclusion_prompt.txt`; generation and validation must still be completed
before final audit.
