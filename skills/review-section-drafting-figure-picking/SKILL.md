---
name: review-section-drafting-figure-picking
description: Draft each review section from section_blueprint.json, literature matrix, and writing rules; each section is a separate output file and should be written by a separate subagent when possible.
---

# Review Section Drafting Figure Picking

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Goal: write each section as a separate file, with figures tied to paragraphs.

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
Use one subagent per section when parallel execution is available.
Each paragraph normally corresponds to one paper's work.
Each paragraph must have one figure/scheme/table tied to that paper.
If no useful figure exists, write an explicit no_figure_reason.
Use the literature matrix main_content as the starting evidence, but verify against Markdown/PDF.
Do not write short examples; write complete review prose.
```

Follow the template review paragraph mode:

```text
1. introduce why this paper/method matters in the section
2. describe the paper's main transformation or principle
3. attach the corresponding scheme/figure/table
4. explain what the scheme shows: substrate, product, catalyst, selectivity, mechanism, or limitation
5. close with a review-level judgment or transition
```

Each paragraph must carry a stable `paragraph_id` and explicit citation IDs.
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

The merge stage uses these markers (not free-text matching) to anchor
figures. Missing markers will silently fall back to heading-level placement.

## Hard Output Requirements

Every section Markdown file must satisfy all of:

```text
at least one image reference using ![](...) when figure_need is not explicitly "none"
every paragraph that cites a paper carries an inline `[n]` callout
the section file ends with (or is accompanied by) a numbered reference list
  so that downstream merge can collect callouts and assemble the global
  References section.
```

Without these the merge stage will produce a draft that fails the final
audit's hard gate (`draft_has_no_figures`,
`draft_has_no_citation_callouts`, `missing_references_section`).

## Figure Rules

Before writing, run:

```bash
python <review-root>/skills/review-section-drafting-figure-picking/scripts/build_paper_figure_inventory.py \
  --review-root <review-root> \
  --project-id <project_id>
```

Use real source figures/schemes/tables from MinerU/PDF. Do not invent figures.

## Automated Script Path

This skill also ships two scripts that automate the manual writing workflow above.
Run them in this exact order — the later scripts depend on files the earlier ones
produce:

```bash
# 1. Build the figure inventory (see Figure Rules above).
python <review-root>/skills/review-section-drafting-figure-picking/scripts/build_paper_figure_inventory.py \
  --review-root <review-root> --project-id <project_id>

# 2. Create section_tasks.json from section_blueprint.json before running either
#    script below. This is not auto-generated; build it directly from
#    section_blueprint.json.sections[] (section_id, title->heading,
#    section_thesis->core_argument, major_papers->allowed_papers,
#    review_claims[].claim->must_cover_points, avoid_patterns->avoid_points,
#    figure_or_table_needs[0].purpose->figure_need).

# 3. Rank figure candidates per paper (writes figure_candidates.json).
python <review-root>/skills/review-section-drafting-figure-picking/scripts/select_initial_figure_candidates.py \
  --review-root <review-root> --project-id <project_id>

# 4. Draft every section with an LLM and auto-embed the ranked figures + a
#    References list into each section file.
python <review-root>/skills/review-section-drafting-figure-picking/scripts/generate_section_drafts.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --model gpt-4.1-mini \
  --base-url <openai-compatible-base-url> \
  --wire-api chat-completions \
  --api-key <key>
```

Step 4 must run after step 3, not before: `generate_section_drafts.py` reads
`figure_candidates.json` (if present) to embed the best-ranked figure for each
paper right before that paragraph's `<!-- paragraph_id: ... -->` marker, and
writes a `## References` list per section using a shared global citation
numbering (the same paper always gets the same `[n]` across every section).
Running step 4 before step 3 still produces valid prose, but with zero figures
embedded — check `section_drafting_report.md`'s "Figures embedded" line to
confirm this didn't happen silently; if it shows `0/0`, run step 3 and re-run
step 4.

`--wire-api chat-completions` with a mini model (e.g. `gpt-4.1-mini`) is the
tested-working configuration for this relay/model combination — strict
`json_schema` mode on `/v1/responses` was found unreliable for this task (see
`review-metadata-prep/SKILL.md`'s remediation notes for the same underlying
issue in a different skill).

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

`figure_candidates.json` items must carry `target_paragraph_id` (the paragraph_id this figure should attach to). Free-text `fits_paragraph_or_claim` stays optional and human-readable.

`section_tasks.json` must be a list. Each item must contain:

```text
section_id
heading
core_argument
allowed_papers
must_cover_points
avoid_points
figure_need
```

Use `section_blueprint.json.sections[].major_papers` as the source for `allowed_papers`.

`sections/<section_id>.md` is mandatory for every section. `section_drafts.md` concatenates the section files for preview only.

Stop after this stage for human check.
