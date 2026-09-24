---
name: review-section-drafting-figure-picking
description: Draft chapters directly from retrieved original passages, check the claims actually used, preserve resumable section outputs, and prepare source-figure candidates.
---

# Review Section Drafting Figure Picking

Goal: write each section as a separate file and build a reviewed paper-level
figure asset pool with evidence-supported placement suggestions.

## Current Web execution

The native Sections handler calls `scripts/generate_section_drafts.py`, whose
writer is `review_writer_core.stages.sections.source_writing` under
`source_passages/1`. The PostgreSQL workflow owns artifacts and confirmation;
local output files alone do not advance a Web stage.

1. Read the confirmed chapter questions, writing objectives, and paper assignments.
2. Use the existing section retrieval routes to obtain source-addressable
   passages. Preserve lexical/vector retrieval, per-paper recovery, supporting
   information ownership, source lineage, and assertion ceilings.
3. Use any verified, current Matrix facts as compact semantic guides for
   retrieval and claim focus, then generate prose directly from the registered
   passages with atomic claims and exact supporting quotes. There is no
   separate predeclared-claim plan or mandatory fact-card quota.
4. Repair malformed source/prose mappings or missing planned comparison records
   in at most one checkpointed batch of affected paragraphs, using the same
   supplied passages. Revalidate repaired mappings before use; clean paragraphs
   are not replayed. Failed optional repairs preserve usable original material.
   Invalid table cells are recorded separately and do not discard valid prose.
   Check only the proposed claims with valid source spans, in one semantic
   checking call. Keep supported wording, accept checked narrower wording, and
   exclude only concretely unsupported assertions. Unresolved or malformed audit
   responses retain the candidate in the authoring checkpoint, never silently
   becoming unsupported. One separate content-repair batch may recover the
   original or narrow it; a mapping repair does not spend that budget.
   The normal nonempty path uses one writing call
   and one used-claim check; local repair and provider retries may add requests. Supported
   verdicts return the claim ID and status with empty text/reason; the program
   retains the exact current wording. Revisions return replacement text. Missing,
   duplicate and foreign verdict IDs never imply support. Source quotes and
   conditions remain available; downstream ledgers keep their complete format.
   A supported verdict retains original wording even if the provider redundantly
   returns a paraphrase. A checked replacement is not rejected because the style
   request budget was already spent. Processing notes alone do not establish a
   narrative defect. Automatic word targets are guidance, with tolerance for
   small differences; stale derived budgets are reconciled with paragraph roles.
5. Render citations and paragraph markers, preserve source identities and
   fingerprints, and checkpoint completed sections. Retry jobs reuse compatible
   completed sections rather than regenerate the entire manuscript.

Independent chapters run with at most two workers. Both receive the same
chapter responsibilities (questions, boundaries, paper roles and assignments),
which are also checked in the existing audit. Optional `depends_on_sections`
orders chapters that require completed chapter results; cycles, missing or
failed prerequisites leave the dependent chapter unfinished. Only the
coordinator publishes progress and checkpoints, and final outputs follow
outline order. Changing shared responsibilities invalidates affected cached
inputs. The model gateway's global and per-user limits still apply; both must
permit at least two requests for model calls to overlap.

Progress reports each active chapter as preparing, writing prose or checking
sources, at the actual call boundary rather than after the audit has finished.

Quantitative comparisons carry source-specific `result_context` records;
ordinary narrative does not require them. Abstracts support broadly attributed
framing only. Retrieval context that is not claim-eligible cannot independently
support prose. Standalone conclusions are generated in Draft composition, not
in this stage. Old outline conclusion entries are filtered for compatibility;
there is no chapter-stage conclusion generation or inherited body-evidence path.

When only some assertions survive checking, retain the supported prose and
record `limited_evidence`. If no usable prose can be produced, retain the
authoring checkpoint and leave the section unfinished for recovery; do not
report an empty/pending placeholder as a successfully generated section.
When rechecking existing content, incomplete new checks must not replace a
still-valid saved chapter with a smaller subset. Revalidate any retained
baseline against current source inputs. Historical pending artifacts remain readable.
Do not restore a missing-fact-card gate or a second automatic fact-repair loop.
When a model selects a fact, retain that binding only when it belongs to the
cited paper and its registered evidence keys are also included in the claim's
support spans; otherwise drop the binding and validate the prose from its
source spans. Invalid
configuration, storage failures, or other unhandled errors can still fail a job;
this recovery path is not a guarantee that every failure is suppressed.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/section_blueprint.json
review-projects/<project_id>/01_matrix_outline/section_writing_plan.md
<review-root>/skills/review-section-blueprint/references/rule_packs.json
<review-root>/examples/reference-reviews/template_summary.md (optional reference-review example)
```

For every assigned paper, reopen:

```text
metadata JSON
linked Markdown
PDF when checking figures/schemes/tables
```

## Writing Rules

```text
Write by section.
Each section outputs one independent Markdown file.
Use the host's existing section execution and checkpoint mechanism.
Write claim-centered synthesis rather than one-paper-one-paragraph summaries.
Organize complete discussions within paragraphs; do not split every fact into
its own paragraph or merge unrelated systems to meet a word target. Detailed
treatment belongs to the paper's primary chapter. Other chapters use only the
context or comparison needed for their own question. Conclusions synthesize
differences and boundaries instead of repeating lists of experimental results.
A paragraph may compare several papers and must list all of them in
`cited_paper_ids`.
Each paper receives detailed treatment only in its primary body section.
Introductions can retrieve contextual sources from all papers assigned to the
selected review; body ownership and primary coverage obligations are unchanged.
Introductions use representative sources briefly and must not
repeat full methods, conditions, results, or limitations from the body.
Introduction develops supported scientific background, the research problem,
and the scientific scope of the review. Never insert corpus assembly,
deduplication, screening counts, retrieval dates, or internal workflow narration.
Do not invent significance or a literature gap when sources are sparse.
Figures remain tied to stable paragraph anchors; a synthesis paragraph may
anchor a representative figure from any cited paper.
Study-specific figures prefer a citing body paragraph; conceptual overview
figures may appear in the introduction. Keep source-supported anchors.
If no useful figure exists, write an explicit no_figure_reason.
Use Matrix metadata for organization; ground manuscript claims in retrieved original passages.
Do not write short examples; write complete review prose.
```

Follow the template review paragraph mode:

```text
1. lead with the scientific claim or comparison
2. synthesize the relevant evidence, grouping compatible studies where useful
3. identify meaningful differences, evidence limits, or exceptions
4. attach a representative scheme/figure/table when it materially supports the claim
5. close with a review-level judgment or transition
```

Each paragraph must carry a stable `paragraph_id` and explicit citation IDs.
The display assembler may share a citation across adjacent same-source claims
within a paragraph when their assertion type is unchanged and attribution is
clear. Source changes, quotations and interpretive claims retain local markers.
Every claim keeps its own source bindings and checked wording internally.
Content-length, structural and omission notices are advisory, not new stage gates.
This is the anchor the merge stage uses to bind figures and aggregate citations.

```text
paragraph_id           e.g. sec3-p2, unique inside section_drafts.json
paper_id               primary paper for that paragraph
cited_paper_ids        list of every paper_id the paragraph relies on
claim or topic sentence
main work of the paper
why it matters to the review topic
figure reference or no_figure_reason
inline citation callout `[n]` keyed to the section reference list
```

## Paragraph ID Markers

Every paragraph in `sections/<section_id>.md` must end with an HTML comment
that exposes its `paragraph_id`, for example:

```markdown
... last sentence of the paragraph. [3]

<!-- paragraph_id: sec3-p2 -->
```

The merge stage uses these markers to anchor figures and edits. Preserve them
and current source bindings; do not infer a safe figure placement from heading
order when a paragraph anchor is unavailable.

## Output integrity

Scientific prose needs the current claim-to-source ledger, explicit paper
identities, and mapped citations. Do not require an embedded image in every
chapter: figure selection and approved manuscript insertion are separate
actions. Preserve pending/limited-evidence records as such, without treating
editorial notices as sourced scientific claims. The normal writer records
`writing_plan.json` and `synthesis_state.json` as outputs of source writing;
their names do not imply separate mandatory AI planning stages.

## Figure Rules

Before writing, run:

```bash
python <review-root>/skills/review-section-drafting-figure-picking/scripts/build_paper_figure_inventory.py \
  --review-root <review-root> \
  --project-id <project_id>
```

Use real source figures/schemes/tables from MinerU/PDF. Do not invent figures.

The inventory preserves original captions and attempts local recovery of split
captions, uniquely matched Markdown image captions, and unambiguous PDF-page
captions. A paper title or nearby generic prose is not sufficient to identify
a figure. Recovered text retains its source reference and image identity.

Selected figures use `review_writer_core.figure_caption`: keep a short complete
source caption when available; otherwise perform one optional bounded batch
compression request using only the figure-specific source text. Aim for one
specific sentence of 15-35 English words, with extra room for multiple panels.
Cache successful results against the source text, source reference, image and
caption contract. Missing evidence or a provider failure leaves the caption
pending without blocking section completion. Do not produce a generic
"representative figure" placeholder or run a separate fact-card/audit loop.

## Outputs

Write under:

```text
review-projects/<project_id>/02_section_drafting/
```

Required files:

```text
section_tasks.json
sections/<section_id>.md
section_drafts.json
section_drafts.md
paper_figure_inventory.json
paper_figure_candidates.json
figure_candidates.json
section_drafting_report.md
```

`section_drafts.json` must contain, for every section, a `paragraphs` list. Each paragraph item carries `paragraph_id`, `paper_id`, `cited_paper_ids`, and (when applicable) `figure_candidate_id`. The aggregated `draft_md` is still kept for preview.

`figure_candidates.json` items should carry `target_paragraph_id` when the
current section draft contains a defensible placement. A reviewed figure may
remain in the paper-level asset pool with an empty target when no safe anchor
exists; this is not a reason to discard the asset or block the next stage.
Final manuscript assembly selects a conservative subset and inserts only rows
with an approved insertion decision. Free-text `fits_paragraph_or_claim` stays
optional and human-readable.

`section_tasks.json` must be a list. Each item must contain:

```text
section_id
heading
core_argument
section_role
primary_papers
supporting_papers
allowed_papers
must_cover_points
avoid_points
figure_need
```

Build `allowed_papers` from primary plus supporting papers. Detailed study
coverage is required only for `primary_papers`; supporting papers are for
brief comparison, framing, or cross-section synthesis.

`sections/<section_id>.md` is mandatory for every section. `section_drafts.md` concatenates the section files for preview only.

Stop after this stage for human check.
