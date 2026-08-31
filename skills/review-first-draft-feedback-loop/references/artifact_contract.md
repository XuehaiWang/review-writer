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
`source_evidence_refs`, `unsupported_claims`, `evidence_problem_type`,
`failed_coverage_fields`, and hard-validated `claim_fact_bindings`. A binding
contains the paragraph's exact `claim_text`, `paper_id`, original `source_ref`,
an exact `support_excerpt`, and normalized Fact semantics. It is optional: an
ordinary Evidence hit must not be promoted to a Matrix Fact merely because it
was retrieved. `original_source_check.json`
stores the page-anchored MinerU/PDF passages used by the optimization run and
their paragraph/paper mapping. Passage refs have the form `P001:p5:b2`.

When the host accepts a repair containing validated bindings, it publishes the
incremental Matrix, repaired Evidence Package, Draft, Quality, and repair
history in one atomic version switch. Rejecting the candidate changes none of
those current pointers.

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
rewrite_accepted
rewrite_rejected
rewrite_items[]:
  paragraph_id, status, attempt, errors[], warnings[]
review_candidate_count
review_candidate_score
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

## Batch review candidates

`batch_review_candidates.json` is the durable human-review handoff produced by
batch optimization. It never authorizes an automatic manuscript overwrite.
It contains:

```text
schema_version
project_id
source_score
candidate_score
candidate_draft_text
changes[]:
  paragraph_id
  original_text
  candidate_text
  source_paragraph_score
  candidate_paragraph_score
  score_delta
  overall_score_delta
  iteration
  validation_warnings[]
  candidate_evaluation
excluded[]:
  paragraph_id
  reasons[]
created_at
```

Only a single-block candidate that passes protected-fact validation and has a
strictly higher paragraph score than the exact pre-loop source may appear in
`changes`. The host must reconstruct a selective save from `changes`, show the
text and score comparison, and publish only paragraph IDs explicitly confirmed
by the user. Unselected paragraphs retain their source text. Reuse the stored
single-paragraph evaluations to update quality incrementally without another
model call.

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
text, source score, precomputed candidate score and paragraph-only evaluation,
status, blocking validation errors, non-blocking terminology warnings, and
review timestamps. Accept a candidate only while its source
paragraph hash still matches; otherwise mark it as a conflict.

An interactively requested candidate may additionally record
`rewrite_mode = human_review_style_only` and
`requires_manual_confirmation = true`. Such a candidate may improve wording
but does not clear the underlying source or figure-identity finding. The host
must display that limitation during comparison and after acceptance.

## Stage-8 human approval

`draft_approval.json` records the exact evaluated draft hash, score, target,
approval time, and any explicit below-target override. Stage 9 may use the
draft only while the approval hash equals the current `first_draft.md` hash.
Any later manual or AI edit invalidates the approval without deleting its
audit record.

## Accepted-candidate paragraph evaluation

`paragraph_candidate_evaluation.json` is produced immediately after one rewrite
candidate is generated and before human review. It contains the replacement paragraph score,
local rubric evidence, local deterministic findings, source-check evidence, and
integrity warnings. The host stores this evaluation with the pending candidate,
shows the score comparison, and reuses the same immutable result if the human
accepts it. The host then replaces only that paragraph's quality record and
updates the previous full-draft score by the paragraph's equal-weight score
delta. Unchanged global rubric dimensions remain attached to the last full
evaluation and must not be presented as freshly rescored.
