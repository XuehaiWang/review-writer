# Review Conclusion Generator

This skill produces a grounded conclusion, challenges, and future-insights
section between approved first-draft review and final audit.

## Orchestrated Contract

- Commands use and default to `--mode orchestrated`; this selects only the approved first draft.
- The orchestrated input is the approved `04_first_draft/first_draft.md`.
- `04_first_draft/citations.json` is the authoritative allowed-paper and numeric-callout map.
- Output Markdown is one clean heading plus 2-3 paragraphs with numeric `[n]`
  callouts and no raw `P001`, model, timestamp, or editor metadata.
- Validation must reject injected numeric callouts in model-authored content and
  reject blank paragraphs before rendering mapped callouts.
- Required outputs are `conclusion_generated.md` and
  `conclusion_quality_report.json`; the stage is complete only when both exist
  and `validation.passes_validation` is `true`.
- No-API/manual mode remains incomplete: no-API or API failure invalidates old
  required outputs and leaves the stage incomplete.
- Standalone draft fallbacks remain supported only with explicit
  `--mode standalone`, which retains final > first > section draft order but
  does not change or satisfy the orchestrated approval gate.

## Grounding and Validation

The generator parses the full draft structure and combines it with the
literature matrix, blueprint claims, and reading-note limitations. Paper IDs
remain internal grounding keys. The validator checks paragraph count, mapped
citations, reference diversity, excessive recap, vague outlooks, and length.

## Quick Start

```bash
python scripts/generate_conclusion1.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --mode orchestrated \
  --api-key <key> --base-url <base-url> --model <model>
```

Outputs are written to `review-projects/<project_id>/04_first_draft/`.
Without an API key, complete the manual generation and validation workflow;
an exit code of zero alone does not complete the stage.
