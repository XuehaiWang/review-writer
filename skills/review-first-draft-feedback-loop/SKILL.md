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
3. Resolve each paragraph's paper IDs, treating figure/caption source identity as authoritative over source-paper bracket labels; retrieve page-anchored passages from local MinerU text with chemistry-aware English/Chinese expansion (falling back to PDF text), and perform the original-source check inside the same optimization run.
4. Score all rubric dimensions and every paragraph using retrieved original passages, citation metadata, and evidence boundaries.
5. Write evaluation, original-source check, findings, gate status, rewrite queue, and polish queue artifacts.
6. If the goal is unmet, rewrite `section_rewrite` paragraphs and source-recheck paragraphs that have readable original text plus explicit unsupported claims. For the latter, only remove or qualify the listed unsupported claims.
7. Apply tiered integrity checks. Reject changes to citation callouts, numeric facts, explicit chemical identities/formulas, stereochemical values, explicit intermediate/compound labels, images, or figure metadata. Normalize generic chemical-class singular/plural forms and treat ordinary terminology changes as non-blocking warnings. An explicitly unsupported hard-protected value may be deleted, never substituted.
8. Treat loop rewrites as working candidates. Require every candidate to remain one prose block, preserve its paragraph marker boundary, pass protected-fact validation, and improve its own paragraph score over the exact pre-loop source paragraph.
9. After every evaluated iteration, retain the best safe candidate independently for each paragraph in `batch_review_candidates.json`. Build the review draft from the unchanged source plus those paragraph candidates; never discard all local gains merely because the whole-draft score did not exceed a previous best.
10. Repeat until the goal is met or a bounded stop condition is reached. Restore the highest fully evaluated working draft and matching evaluation artifacts for the loop's internal result, while keeping the independent paragraph candidates available for explicit human review.
11. In an interactive host, show every retained batch candidate as a source-versus-candidate comparison with source score, candidate score, and a checkbox. Do not change the current manuscript until the user saves selected candidates or saves all; allow the entire batch to be discarded.
12. When selected candidates are saved, reuse their immutable paragraph evaluations, replace only those paragraph bodies, update the full-draft score by their equal-weight paragraph deltas, and leave every unselected paragraph unchanged. Do not call the model again or claim that unchanged global rubric dimensions were rescored.
13. For an interactive one-paragraph rewrite, use the same generate → integrity-check → paragraph-score → compare → human-decision sequence. When the stored route is `human_confirmation` or an evidence-poor `local_source_recheck`, permit only an explicitly labeled style-only candidate: preserve all scientific propositions and source/figure relationships, do not claim the conflict is resolved, and keep the manual-confirmation requirement visible after saving. Do not enable this fallback in unattended batch rewriting.

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

- Treat evaluation, rewrite candidates, targeted improvement, and human approval as Stage-8 actions depending only on the current saved draft.
- Keep Stage 9 read-only with respect to the first draft; it only assembles and audits a human-approved Stage-8 version.
- Pass provider settings through the host's normal runtime configuration; do not add skill-specific API keys.
- Persist live status in `feedback_loop_status.json` so navigation or page refresh does not lose progress.
- Keep every run under `04_first_draft/feedback_loop/runs/<run-id>/`.
- Ensure every prose paragraph has a stable marker before scoring; never treat an empty paragraph set as passing.
- Re-index the modified first draft through the host handoff/hash mechanism after completion.
- Replay an overlay only when both paragraph ID and original paragraph hash still match.
- Never overwrite a newer upstream paragraph when an overlay conflicts; report the conflict instead.
- Bind scores, rewrite candidates, and human approval to exact draft hashes. Any later paragraph or full-text edit makes the previous result visibly out of date.
- Generate one-paragraph AI candidates without applying them, score each candidate before presenting it, and show the source-versus-candidate paragraph scores with the text comparison.
- Let an explicit user request generate a style-only candidate for `human_confirmation` or unresolved source-recheck items, but label it as still requiring manual confirmation. Never let the candidate hide, clear, or claim to resolve the underlying evidence or figure-identity issue.
- Accept a candidate only after a human decision, reuse its precomputed immutable evaluation when publishing, and record the accepted edit in paragraph history. Keep the previous manuscript current if candidate evaluation or integrity validation fails.
- For batch optimization, publish only independently improved paragraph candidates to the review interface. Keep them pending across refreshes, support per-paragraph selection, and leave the saved manuscript untouched until the user confirms.
- Mark incremental quality explicitly and retain the last full evaluation as its baseline; require a full evaluation after arbitrary manual or full-text edits.

## Release and Safety

Release only when the total score reaches the overall goal, every paragraph meets the paragraph goal, and no hard-gate failure remains. The overall goal may be higher than the rubric threshold but never lower than 90. If the model is unavailable, evaluation JSON is malformed, score improvement plateaus, or protected content changes, retain the highest fully evaluated draft when available and return an explicit status for human review.

Do not silently fabricate evidence, references, chemical details, or scores. Do not broaden a paragraph rewrite into a whole-draft rewrite.
