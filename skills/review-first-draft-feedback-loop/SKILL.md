---
name: review-first-draft-feedback-loop
description: Evaluate every paragraph in a literature-review first draft against a weighted rubric, create actionable queues, and iteratively rewrite only failed paragraphs while protecting citations and scientific facts. Use after a merged first draft exists and before conclusion or final-draft generation, or when a user asks to score, audit, improve, or gate review paragraphs to a target quality score.
---

# Review First Draft Feedback Loop

## Overview

Run a bounded quality loop over `04_first_draft/first_draft.md`. Score the full draft and every marked paragraph, route findings by severity, rewrite only paragraphs below the configured goal, and stop on release, iteration limit, plateau, unsafe rewrite, or user request.

The host review-writer integration is intentionally additive. It must not make conclusion, overview-figure, final-draft, or Word generation depend on this optional gate. Rewrites are stored as Stage-8 overlays and never mutate Stage-5 `section_drafts.json`.

## Workflow

1. Verify the current first draft and its paragraph markers exist.
2. Run deterministic preflight checks before any model evaluation.
3. Score all rubric dimensions and every paragraph using source evidence and citation metadata.
4. Write evaluation, findings, gate status, rewrite queue, and polish queue artifacts.
5. If the goal is unmet, rewrite only `section_rewrite` paragraphs.
6. Reject rewrites that change citation callouts, numeric facts, stereochemical terms, or required labels.
7. Record accepted changes in `feedback_loop_rewrites.json` and the current first draft.
8. Repeat until the goal is met or a bounded stop condition is reached.

Read [artifact_contract.md](references/artifact_contract.md) for inputs and outputs and [unified_rubric.json](references/unified_rubric.json) for the scoring model.

## Commands

Evaluate and improve:

```powershell
python scripts/feedback_loop.py --review-root <review-root> --project-id <project-id> --goal 90 --paragraph-goal 85 --max-iterations 3
```

Evaluate without rewriting:

```powershell
python scripts/feedback_loop.py --review-root <review-root> --project-id <project-id> --evaluate-only
```

Run only deterministic checks:

```powershell
python scripts/preflight_first_draft.py --review-root <review-root> --project-id <project-id>
```

Replay safe overlays after rebuilding a draft:

```powershell
python scripts/apply_feedback_overlays.py --review-root <review-root> --project-id <project-id>
```

## Host Integration Rules

- Treat the loop as an optional Stage-9 action depending only on the current Stage-8 draft.
- Preserve all existing independent Stage-9 actions.
- Pass provider settings through the host's normal runtime configuration; do not add skill-specific API keys.
- Persist live status in `feedback_loop_status.json` so navigation or page refresh does not lose progress.
- Keep every run under `04_first_draft/feedback_loop/runs/<run-id>/`.
- Re-index the modified first draft through the host handoff/hash mechanism after completion.
- Replay an overlay only when both paragraph ID and original paragraph hash still match.
- Never overwrite a newer upstream paragraph when an overlay conflicts; report the conflict instead.

## Release and Safety

Release only when the total score reaches the overall goal, every paragraph meets the paragraph goal, and no hard-gate failure remains. If the model is unavailable, evaluation JSON is malformed, score improvement plateaus, or protected content changes, retain the last valid draft and return an explicit status for human review.

Do not silently fabricate evidence, references, chemical details, or scores. Do not broaden a paragraph rewrite into a whole-draft rewrite.
