# Artifact contract

Write all artifacts under:

```text
review-projects/<project_id>/04_first_draft/
```

## Deterministic preflight

`first_draft_preflight.json` contains:

```text
project_id
draft_path
case_word_range
checks
paragraph_findings
hard_regressions
hash_manifest_created = false
```

## Model rubric evaluation

`rubric_evaluation.json` contains:

```text
rubric_model = readability_first_unified_review_rubric
pass_threshold = 90
total_score
decision = PASS | REGENERATE_SECTIONS
dimension_scores[]:
  id, weight, level, weighted, evidence
hard_gate_failures[]
paragraph_failures[]:
  paragraph_id, failed_dimensions, severity, diagnosis, route
paragraph_scores[]:
  paragraph_id, score, severity, route, failed_dimensions, diagnosis
```

Each paragraph score also records `source_check_status`, validated
`source_evidence_refs`, and `unsupported_claims`. `original_source_check.json`
stores the page-anchored MinerU/PDF passages used by the optimization run and
their paragraph/paper mapping. Passage refs have the form `P001:p5:b2`.

Every dimension in `unified_rubric.json` must appear exactly once and the
weights must total 100.

## Independent review findings

`reviewer_findings.json` is a JSON list. Each item contains:

```text
id
reviewer
severity = critical | major | minor
paragraph_id
location
fragment
diagnosis
recommended_direction
confidence
route = section_rewrite | local_source_recheck | final_polish | human_confirmation
```

## Queues and status

`first_draft_rewrite_queue.json` contains blocking items that return to
structured section writing.

`first_draft_final_polish_queue.json` contains nonblocking language or
evidence-strength items eligible only after first-draft release.

`first_draft_gate_status.json` contains:

```text
status = REWRITE_REQUIRED | RELEASED_FOR_CONCLUSION_AND_SELECTIVE_FINAL_POLISH
gate_decision = GATE_HOLD_* | GATE_RELEASE
unified_rubric_score
rewrite_queue_path
final_polish_queue_path
next_action
hash_manifest_created = false
```

Do not treat an empty rewrite queue as release when the score is below 90 or a
hard regression remains.

The configured overall goal may raise this threshold but must never lower it
below the rubric's `pass_threshold`.

## Bounded loop status

`feedback_loop_status.json` is the durable status used by the web interface:

```text
project_id
run_id
status = running | completed | needs_human_review | stopped | failed
phase = preflight | source_checking | scoring | evaluated | rewriting | released | plateau |
        rewrite_blocked | iteration_limit | stopped | failed
iteration
max_iterations
goal
paragraph_goal
score
best_score
best_iteration
best_score_restored
paragraph_total
paragraph_completed
paragraph_scores[]
rewrite_total
rewrite_completed
current_paragraph_id
source_draft_sha256
output_draft_sha256
error
started_at
updated_at
finished_at
```

Each immutable run is stored below
`feedback_loop/runs/<run-id>/`, including the pre-loop draft, per-iteration
preflight/evaluation data, rejected candidates, and the final accepted draft.

## Safe rewrite overlay

`feedback_loop_rewrites.json` contains accepted Stage-8-only changes:

```text
schema_version
project_id
policy
entries.<paragraph_id>:
  paragraph_id
  source_text_sha256
  rewritten_text
  updated_at
```

The source hash is the original deterministic Stage-8 paragraph hash and must
be preserved across multiple rewrite iterations. Replay an entry only when the
paragraph ID and original hash both match. Record nonmatching entries as
conflicts instead of overwriting newer upstream content.

In the host project, `feedback_loop_handoff.json` may additionally record the
current input/output hashes. This host-level handoff does not replace the
portable skill artifacts above.

## Human-reviewed rewrite candidates

`feedback_rewrite_candidates.json` stores per-paragraph AI proposals that have
passed protected-fact validation but have not yet changed the manuscript. Each
entry records the source paragraph hash, draft hash, original text, candidate
text, status, and review timestamps. Accept a candidate only while its source
paragraph hash still matches; otherwise mark it as a conflict.

## Stage-8 human approval

`draft_approval.json` records the exact evaluated draft hash, score, target,
approval time, and any explicit below-target override. Stage 9 may use the
draft only while the approval hash equals the current `first_draft.md` hash.
Any later manual or AI edit invalidates the approval without deleting its
audit record.
