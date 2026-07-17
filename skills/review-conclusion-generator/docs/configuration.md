# Configuration

## Orchestrated Contract

- Commands use and default to `--mode orchestrated`; this selects only the approved first draft.
- The orchestrated input is the approved `04_first_draft/first_draft.md`.
- `04_first_draft/citations.json` is the authoritative allowed-paper and numeric-callout map.
- Output Markdown is one clean heading plus 2-3 paragraphs with numeric `[n]`
  callouts and no raw `P001`, model, timestamp, or editor metadata.
- Validation must reject injected numeric callouts in model-authored content and
  reject blank paragraphs before rendering mapped callouts.
- Required outputs are `conclusion_generated.md` and
  `conclusion_quality_report.json`; completion requires that both exist and
  `validation.passes_validation` is `true`.
- No-API/manual mode remains incomplete: no-API or API failure invalidates old
  required outputs and leaves the stage incomplete.
- Standalone draft fallbacks remain supported only with explicit
  `--mode standalone`, which retains final > first > section draft order but
  does not change or satisfy the orchestrated approval gate.

## Command

```bash
python scripts/generate_conclusion1.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --mode orchestrated \
  --api-key <key> \
  --base-url <base-url> \
  --model <model>
```

| Argument | Environment | Meaning |
|---|---|---|
| `--review-root` | - | Root containing `review-projects/` |
| `--project-id` | - | Required project directory name |
| `--mode` | - | Defaults to `orchestrated`; use `standalone` only for fallback inspection |
| `--api-key` | `OPENAI_API_KEY` | API credential |
| `--base-url` | `OPENAI_BASE_URL` | OpenAI-compatible endpoint |
| `--model` | `REVIEW_CONCLUSION_MODEL` | Model override |

CLI values take precedence over environment values and `.env` values. If no
API key resolves, `conclusion_context.json` and `conclusion_prompt.txt` are
written to `04_first_draft/`; they are aids for manual completion, not stage
outputs that pass the orchestrator gate.
