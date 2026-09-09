---
name: review-section-blueprint
description: Prepare provisional chapter objectives, research questions, retrieval directions, paper assignments, and shared writing rules from the selected outline and literature matrix.
---

# Review Section Blueprint

Goal: create the writing blueprint used by section workers.

The Web/API/PostgreSQL workflow is authoritative. This Skill is an offline and
repair entry point for the same Blueprint meaning; writing local files alone
does not advance or approve a Web stage.

Boundary: the initializer prepares an offline scaffold and writing constraints.
It does not produce an approved scientific argument. The Web workflow uses
`review_writer_core.stages.planning.academic_planning` by default to propose
structure and paper roles from Topic/Scope, selected-paper titles, abstracts,
classification, and available local source excerpts. Saved manual headings are
preserved. The user reviews and confirms the candidate through the Web API.

The current planner uses `chapter-planning/3`: bounded current-topic fact
analysis, one structure proposal, and one proposal per body chapter, with
bounded concurrency and reusable checkpoints. Plans describe questions and
writing objectives, not verified scientific conclusions. Verified facts may
make those questions and paper roles more specific; missing facts do not make a
selected paper irrelevant. Fact-provider failure falls back to local source
excerpts. Planning does not run a scientific claim audit or a second automatic
evidence-repair loop. JSON shape, section identity, paper ownership, and
saved-heading checks remain.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/01_matrix_outline/paper_reading_notes.json
<review-root>/skills/review-section-blueprint/references/rule_packs.json
```

Default rule pack:

```text
references/rule_packs/general/
```

The Web service and this initializer use the same selector and SHA-256 version.
The specialized pack is selected only for explicit `allene`, `allenation`, or
`联烯` topics. Use the rule pack as writing constraints only. Do not import
facts from it. If the selected pack or one of its declared files is missing,
stop before replacing the previous successful Blueprint.

## Required Blueprint

Run initializer if useful:

```bash
python <review-root>/skills/review-section-blueprint/scripts/init_section_blueprint.py \
  --review-root <review-root> \
  --project-id <project_id>
```

Then edit/complete:

```text
review-projects/<project_id>/01_matrix_outline/section_blueprint.json
review-projects/<project_id>/01_matrix_outline/section_writing_plan.md
```

Current body plans expose these writing inputs (alongside legacy-compatible
fields retained by the builder):

```text
section_id
title
section_role
writing_objective
review_problem
questions_to_answer
retrieval_directions
candidate_papers
target_paragraphs
target_words
dominant_logic
major_papers
primary_papers
supporting_papers
paper_roles
figure_or_table_needs
depth_requirements
section_transition
avoid_patterns
```

The Blueprint must also record `rule_pack`, `rule_pack_path`,
`rule_pack_files`, and `rule_pack_sha256`. Sections read this locked selection;
they do not re-select a pack from Topic text.

New plans use `evidence_mode: source_passages/1`. `section_thesis` and
`scientific_thesis.text` retain the provisional writing purpose for compatible
consumers; `scientific_thesis` also carries comparison axes, boundaries, and
open questions. `scientific_claims` and `argument_order` are empty, as are the
old targeted-fact extraction requirements. Do not populate them with claims
that must be proven before drafting. Historical claim/fact records remain
readable for old artifacts, without becoming prerequisites for new plans.

`figure_or_table_needs` names the purpose and candidate papers when a visual
helps the argument; do not impose fixed comparison quotas. Actual claims and
source bindings are produced during section writing and checked against the
retrieved original passages.

## Hard Rules

```text
No section may be only a title.
Each paper may have at most one primary body-section owner.
`major_papers` and `primary_papers` are the same primary-owner set.
Introduction and conclusion keep `major_papers` empty and use a bounded
`supporting_papers` set for concise framing or cross-section synthesis.
Repeated assignments in later body sections become `supporting_papers` and
must not trigger a second full paper description.
Every body section needs a research question and provisional writing objective.
Evidence gaps remain open questions; they do not trigger a second fact-extraction loop after planning.
Every selected paper has a role or a specific unused_papers reason confirmed with the candidate.
Every section must have figure_or_table_needs, or explicitly state no figure/table is useful.
The blueprint is a plan, not prose. Keep it compact and enforceable.
The assignment policy is domain-independent. Do not hard-code topic names,
paper IDs, catalyst classes, diseases, materials, or other subject entities.
```

Stop after blueprint for human check if interactive.
