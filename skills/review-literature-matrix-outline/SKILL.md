---
name: review-literature-matrix-outline
description: Read every paper explicitly selected by the human reviewer, build a concise fixed-field literature matrix, and draft review outline options using the writing-rule skill.
---

# Review Literature Matrix Outline

Goal: read selected papers and create the literature matrix plus outline options.

Boundary: this skill prepares selected-paper information and high-level
structure. Blueprint then plans chapter questions and retrieval directions;
actual scientific claims are generated from original passages during drafting.

## Current Web workflow

Discovery confirmation creates the selected-paper input without starting a
separate fact-extraction job. When the reviewer generates the chapter plan,
the same Planning job automatically performs bounded, current-topic fact
analysis before it plans chapter questions and paper roles. It reuses the
existing Matrix enrichment contract, cached coverage, and checkpoints.
The first evidence-bounded extraction also attempts the primary paper route;
run a separate route recheck only when that response and deterministic/formal
classification both remain unresolved. Reuse semantic fact verdicts only when
the fact fingerprint, validation contract, and immutable source spans still
match. Once baseline evidence is review-ready, retain optional supplement
requests for later question-scoped repair instead of exhaustively expanding
every paper before Blueprint exists.

The enriched Matrix is candidate-scoped: generating a plan does not move the
current Matrix pointer or invalidate downstream work. Confirming the reviewed
Blueprint atomically promotes its exact Matrix, Outline, and Blueprint input
chain. A model/provider failure in fact analysis degrades to registered source
passages and must not block chapter planning. Missing fact cards are never
negative evidence and do not impose a per-paper quota.

`scripts/enrich_matrix_facts.py` remains available for retry, repair, and
standalone interchange use; it is no longer the normal user-facing prerequisite.
The Web/API/PostgreSQL services own Matrix, Outline, and Blueprint state; the
paths below describe scientific interchange files, not an alternate way to
advance a Web stage.

## Inputs

```text
review-projects/<project_id>/00_discovery/selected_discovery_results.json
review-projects/<project_id>/00_discovery/topic_input.md
<review-root>/skills/review-section-blueprint/SKILL.md
<review-root>/skills/review-section-blueprint/references/rule_packs.json
<review-root>/examples/reference-reviews/template_summary.md (optional reference-review example)
```

For each paper, open:

```text
review-library/metadata/papers/<paper_id>.metadata.json
linked Markdown
linked PDF when choosing figures or checking chemistry
```

## Matrix Rules

The interchange matrix uses these paper fields:

```text
paper_id
title
authors
keywords
abstract
main_content
most_relevant_figure
```

Field requirements:

```text
keywords: retain available paper keywords; use project classification separately and do not promote neutral Library Tags into verified classifications.
abstract: use metadata abstract if reliable; if missing or poor, write "abstract unavailable or unreliable" and continue.
main_content: preserve available reading notes; a lengthy per-paper summary is not required before planning and is not a substitute for original-source support.
most_relevant_figure: retain a source-linked candidate when available; detailed figure selection can occur after section writing.
```

Do not fabricate missing field values. Do not exclude a paper only because
its abstract is poor or fact cards are absent. The current planner can use
bounded local source excerpts when abstracts are unavailable.

External `web_papers` (SciAtlas/Crossref) from discovery are reference-only:
they do not get a local `paper_id` or become selected Matrix evidence. A search
result alone does not authorize a manuscript claim or citation; acquire,
register, and select the source through the normal workflow before using it
as chapter evidence.

## Outline Rules

For outline organization, use the available selected-paper context:

```text
review topic
literature matrix
review-section-blueprint writing rules / rule pack
template review organization summary
```

Offer structures appropriate to the topic rather than requiring a fixed number
of AI-generated alternatives. Each candidate describes chapter titles and
purposes, with paper assignments and visual intentions where available.

Use the chosen organization mode. Possible structures include:

```text
problem-progressive
category-coverage
entry-classified
reaction-type-classified
application-oriented
```

Each major section must have a clear review question, assigned papers, and scheme/figure plan. Do not make a plain title list.

## Outputs

Write under:

```text
review-projects/<project_id>/01_matrix_outline/
```

Standalone/interchange files may include:

```text
paper_reading_notes.json
literature_matrix.json
literature_matrix.csv
outline_options.md
matrix_outline_report.md
```

Stop after this stage for human outline selection. The preferred human artifact is:

```text
selected_outline.md
```

The dashboard may create this artifact from a built-in structure, a reference
review, or a custom outline. A newly selected custom outline starts completely
blank and is not ready for Blueprint until the reviewer writes and saves it.
Reference-review uploads must run through `review-reference-outline-template`:
learn only hierarchy, section-role sequence, pacing, and writing conventions,
then generate all heading wording and scientific meaning from the current topic
and Matrix. Legacy direct-heading-extraction candidates are unsafe and must not
be offered for selection.
The dashboard's default editor is a visual section-card builder. Reviewers can
edit section titles and purposes, assign papers with checkboxes, request a
paper recommendation, and reorder sections without knowing the Markdown
syntax. The editor allows papers to remain unassigned while titles and writing
goals are prepared; recommendations can then fill assignments for review.
Use current API validation for save and confirmation readiness. Verified facts
may make questions, comparisons, and paper roles more specific, but do not
require a fact-card count or invent an extra per-section paper quota. Blueprint
defines provisional questions and directions; claim wording is still created
and checked against original passages during section drafting.

Advanced reviewers may switch to Markdown editing. Keep major sections as
level-2 headings (`## Section title` or `## 1. Section title`) and use
`Assigned papers: P001, P002.` for every major section. The visual editor and
Markdown editor must round-trip the same selected outline. Saved manual edits
are authoritative for Blueprint and all later stages.
