---
name: review-first-draft-feedback-loop
description: Review manuscript coherence and propose bounded, source-bound paragraph improvements through the current Draft dialogue and batch workflow. Preserve saved text until candidates are accepted; do not restore score-driven iteration.
---

# Draft analysis and revision

The online entry is `POST /draft/dialogue-batch`, dispatched by
`review_writer_api/job_handlers/draft_execution.py` through `revise_paragraph.py`.
The directory name is retained for compatibility; it does not prescribe the old
score/iteration feedback loop.

## Current workflow

1. Snapshot the current manuscript with stable paragraph identities. Run one
   bounded whole-manuscript coherence plan: Introduction promises, chapter
   responsibilities, meaningful comparisons and Abstract/Conclusion consistency.
   Locate instructions in actual text and record every paragraph dependency.
2. Keep unchanged covered paragraphs without generating replacement candidates.
   If global planning is unavailable or partial, record its actual scope and use
   the same local revision executor. Never silently run the legacy scoring loop.
3. Retrieve local source passages for affected paragraphs, allowing at most one
   targeted lookup round. Context, conversation and the editorial plan are not
   scientific evidence. Produce independent in-place candidates only: no paragraph
   deletion, merging, reordering or information transfer across paragraphs.
4. Validate structure and citation identity. Source-check changed automatic
   candidates, including causal/negative meaning, scope, conditions, key evidence,
   counterexamples and qualifications. Registered refs alone do not prove support.
   A failed check rejects the candidate, preserves its rejected text and leaves
   the current manuscript intact; it does not create a workflow gate.
5. Store candidate text, source snapshots, verification fingerprint and actual
   paragraph dependencies. Acceptance checks target, dependencies and source
   freshness. Unrelated changes must not invalidate independent candidates.
   Keep the current human save and stage-confirmation controls.
6. Checkpoint global planning and completed paragraph work. Resume unfinished
   work rather than replay successful calls. Do not promise exactly-once billing
   for provider requests whose completion cannot be queried.

Author-directed chapter edits remain proposals for author review; author
acceptance is not scientific verification. Automatic verified candidates retain
verification only for the exact saved text and actual source snapshot.

## Code reuse and compatibility

`feedback_loop.py` still supplies source retrieval, parsing and protected-field
helpers to the current executor. Do not delete that module while it has callers.
Its legacy `run_feedback_loop` CLI and score queues are not the online batch
workflow and must not be reintroduced as a fallback. Historical artifacts remain
readable; new candidates use the current immutable dialogue artifact flow.

No new quality-score, word-count or narrative-completeness gate is introduced.
Do not fabricate missing source support or reinterpret a failed lookup as proof
that the original paper lacks a fact.
