---
name: review-literature-matrix-outline
description: Read the 20-30 selected papers, build a concise fixed-field literature matrix, and draft review outline options using the writing-rule skill.
---

# Review Literature Matrix Outline

Goal: read selected papers and create the literature matrix plus outline options.

## Inputs

All paths are relative to `<review-root>` — the root directory of this review-writer project (the folder that contains `skills/`, `review-projects/`, and `review-library/`).

```text
review-projects/<project_id>/00_discovery/selected_discovery_results.json
review-projects/<project_id>/00_discovery/topic_input.md
skills/review-section-blueprint/SKILL.md
skills/review-section-blueprint/references/rule_packs.json
```

For each paper, open:

```text
review-library/metadata/papers/<paper_id>.metadata.json
linked Markdown (path from metadata source_paths.markdown)
linked PDF when choosing figures or checking technical details (path from metadata source_paths.pdf)
```

## Scripted Matrix Build (cost control, preferred when an API key is available)

Reading papers inside the agent conversation re-carries the whole accumulated context on every turn — roughly quadratic cost in paper count. When an OpenAI-compatible API key is available, build the matrix rows with isolated, resumable per-paper API calls instead:

```bash
python3 <skill-root>/scripts/build_matrix_llm.py \
  --review-root <review-root> \
  --project-id <project_id> \
  --model <model> --base-url <base-url> --reasoning-effort medium
```

Each selected paper gets one bounded call (metadata + first ~9k chars of Markdown in; one matrix row JSON out, written to `01_matrix_outline/rows/<paper_id>.row.json`). Existing rows are skipped, so re-running after failures retries only the failed papers (`--force` rebuilds). `literature_matrix.json` and `.csv` are assembled from the row files at the end of every run; `matrix_build_report.json` records per-paper status. Use `--dry-run` to validate input resolution without spending tokens, `--limit 2` for a smoke test.

After the script finishes, the agent's job is reduced to: spot-check 2-3 rows against their papers, fix any garbled titles/figures, write `paper_reading_notes.json` (row provenance is in `matrix_build_report.json`), and proceed to the outline step below — do not re-read the papers wholesale.

If no API key is available, fall back to the agent reading papers directly under the budget below.

## Reading Budget (cost control, agent-read fallback)

Reading the full Markdown of every paper in one continuous pass is the single largest token cost in this skill. Bound it:

```text
Read only the first ~8,000-10,000 characters of each paper's Markdown by default (title, abstract, intro, and enough of the methods/results to identify the main output, method, and key figure) -- this cost-control cap keeps per-paper reads bounded regardless of document length.
Only read further into the paper (a later section, or the full document) when that initial excerpt genuinely lacks what main_content needs -- e.g. the main quantitative result is reported later, or the most relevant figure's caption isn't in the excerpt.
Process papers in small batches (e.g. 3-5 at a time), writing their matrix rows to literature_matrix.json as you go, rather than holding all 20-30 papers' full text in context simultaneously before writing anything. This bounds peak context size regardless of paper count.
Reuse the paper's metadata (abstract, structured_tags) as the first source before re-deriving the same facts from the raw Markdown -- metadata_prep already extracted these once; don't pay to re-extract them.
```

## Matrix Rules

For every selected paper, every matrix row must contain all fields:

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
keywords: use the 8 structured tag values from metadata.
abstract: use metadata abstract if reliable; if missing or poor, write "abstract unavailable or unreliable" and continue.
main_content: around 1000 English words; summarize the paper's actual work, not just the abstract.
most_relevant_figure: the figure/scheme/table that best reflects the principle or main work of the paper; include source label, caption, page hint, image path if available, and why it is relevant.
```

Do not omit any field. Do not exclude a paper only because its abstract is poor.

### Optional per-paper fields used by the blueprint stage

`review-section-blueprint` reads a few additional per-paper fields when scoring which papers belong in which section and when drafting section theses/claims: `output`, `input`, `method`, `process_type`, `limitation`, `selectivity`. These are not in the required field list above, but including them (copied or summarized from the paper's `structured_tags` in its metadata) noticeably improves blueprint quality — without them, section assignment for generic section titles (e.g. "Introduction", "Conclusion") can fail to find matches, and generated theses/claims fall back to generic placeholder phrasing.

When a field does not apply to a given paper or topic, leave it out of that paper's row entirely rather than writing a literal value like `"not applicable"` or `"none"`. The blueprint script only falls back to its own neutral default phrasing when a field is absent — a literal placeholder string gets treated as real content and produces awkward generated sentences (e.g. "...by how they control not applicable...").

## Outline Rules

After the matrix is complete, use:

```text
review topic
literature matrix
review-section-blueprint writing rules / rule pack
template review organization summary
```

Create `2-3` outline options. Each option must include section titles, purpose, assigned papers, and expected figures.

The outline must imitate the template reviews' organization mode. Choose and name one primary structure:

```text
problem-progressive
category-coverage
entry-classified
reaction-type-classified
application-oriented
```

Each major section must have a clear review question, assigned papers, and scheme/figure plan. Do not make a plain title list.

## Outputs

Write under `<review-root>/review-projects/<project_id>/01_matrix_outline/`:

Required files:

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
