---
name: review-draft-merge-polish
description: Merge separately drafted section files into one coherent first review draft and polish transitions, terminology, and figure placement.
---

# Review Draft Merge Polish

Goal: merge section files into one complete review draft.

## Inputs

```text
review-projects/<project_id>/01_matrix_outline/selected_outline.md
review-projects/<project_id>/01_matrix_outline/literature_matrix.json
review-projects/<project_id>/03_section_drafting/sections/*.md
review-projects/<project_id>/03_section_drafting/figure_candidates.json
review-projects/<project_id>/03_section_drafting/section_drafting_report.md
```

If available, also use:

```text
review-projects/<project_id>/04_figure_redraw/redrawn_figure_manifest.json
```

## Merge Rules

```text
Keep the selected outline order.
Merge all section files.
Polish transitions and terminology.
Preserve paper-to-paragraph and figure-to-paragraph links.
Do not delete caveats or no_figure_reason notes silently.
Do not invent new papers, claims, or figures.
```

## Citation Numbering

Section drafts cite evidence inline by `paper_id` (e.g. `(P034)`). During merge, convert these into numbered citations and build a formatted reference list:

1. Scan the merged manuscript top to bottom (reading order, not outline order) and assign each distinct `paper_id` a number in the order it is **first** encountered: the first cited paper is `[1]`, the second distinct paper is `[2]`, and so on.
2. Replace every inline `paper_id` mention with its assigned bracket number, e.g. `(P034)` -> `[1]`. Multiple citations in one spot become adjacent bracket numbers, e.g. `(P034, P035)` -> `[1][2]`.
3. If a citation parenthetical carries extra annotation beyond the bare paper_id(s) — a figure/table pointer (`(P034; **Table 3**, "...")`) or trailing prose (`(P049, introduced in Section 2 for its retrieval-method implications)`) — swap only the `paper_id` token(s) for their bracket number(s) in place and keep everything else in the parenthetical unchanged: `(P034; **Table 3**, "...")` -> `([1]; **Table 3**, "...")`. Only strip the parentheses entirely for the plain case with nothing but bare paper_id(s) inside, per rule 2 above.
4. Do not renumber per-section; numbering is global across the whole merged manuscript.
5. Append a `## References` section at the end of `first_draft.md`, one entry per cited paper, ordered by its assigned number. Pull bibliographic fields (authors, year, journal, volume, pages) as follows:
   - If `review-library/metadata/papers/<paper_id>.metadata.json` exists (local, MinerU-parsed papers), use that.
   - If it does not exist (web-sourced paper_ids, e.g. `W1`, that were never downloaded/parsed locally), use that project's `00_discovery/selected_discovery_results.json` -> `web_papers` entry with the matching title. `discover.py`'s Crossref search records `journal`, `volume`, `pages`, and `doi` there; use them the same way as local metadata.
6. Watch for foreign bracket numbers: a figure/table caption quoted verbatim from a source paper can already contain that source paper's own `[N]`-style citations to its own bibliography (e.g. `"FEVER-3 is [68]"`). These are not our citations and must not be left as bracket numbers once this scheme is in use, because they will silently collide with our numbering (a reader cannot tell `[4]` referring to our reference list from `[4]` quoted from someone else's). Reword them out of bracket form, e.g. `"FEVER-3 is from the source paper's own reference (68)"`, before finalizing the draft.

## Reference List Format

Each reference entry must follow this pattern (ACS journal-article citation style):

```text
[N] Last1, F.; Last2, F.; ...; LastN, F. Title of the paper, sentence case. *Journal* **Year**, *Volume*, StartPage–EndPage.
```

Real examples of the exact target punctuation:

```text
[24] Vermeer, P.; Meijer, J.; de Graaf, C.; Schreurs, H. Copper(I) halide catalysed ring-opening of acetylenic epoxides. Synthesis of allenic alcohols. *Recl. Trav. Chim. Pays-Bas* **1974**, *93*, 46–47.
[25] Alexakis, A.; Marek, I.; Mangeney, P.; Normant, J. F. Diastereoselective syn or anti opening of propargylic epoxides. Synthesis of α-allenic alcohols. *Tetrahedron* **1991**, *47*, 1677–1696.
[26] Fürstner, A.; Méndez, M. Iron-Catalyzed Cross-Coupling Reactions: Efficient Synthesis of 2,3-Allenol Derivatives. *Angew. Chem., Int. Ed.* **2003**, *42*, 5355–5357.
```

Formatting rules, in exact order:

```text
Authors: "Last, Initial." separated by "; ", ending with a period after the final author's initials, then the title starts. Use whatever the metadata actually has for name order/initials -- do not invent full first names.
Title: plain text (no italic, no bold), sentence case, ending with a period. A second period-separated clause (a subtitle, e.g. "Synthesis of allenic alcohols.") is normal and stays plain text too.
Journal: italic (*Journal*). Use the metadata's journal value as-is; do not invent or expand/abbreviate it yourself.
No comma between the journal and the year -- they are separated only by a space, e.g. "*Journal* **Year**".
Year: bold (**Year**), directly after the journal with no comma.
Volume: italic (*Volume*), preceded by a comma after the year. Include the issue in parentheses after the volume only if the metadata combined them, e.g. *38(4)*.
Pages: plain text, preceded by a comma after the volume, using an en dash (`–`) between start and end page (e.g. `46–47`), not a hyphen. Placed last, entry ends with a period.
```

Treat `authors`, `journal`, `volume`, and `pages` as absent — and omit that piece from the entry — whenever the metadata value is the literal string `not specified`, `null`, an empty string, or an empty list. Do not print any of those placeholder values into the reference list. This applies per-field independently: an entry can have real authors and a real year but no journal/volume/pages (common for preprints), or the reverse. Never invent a value for a field the metadata does not actually have.

```text
[3] Lewis, P.; et al. Retrieval-augmented generation for knowledge-intensive NLP tasks. *arXiv preprint* **2020**.
[9] Detection and Simulation of Urban Heat Islands Using a Fine-Tuned Geospatial Foundation Model.
```

The second example shows a paper with no extracted authors, year, or journal at all (common for stub/rules-only metadata that hasn't been through LLM tagging yet) — the entry degrades gracefully to just `[N] Title.` rather than blocking the merge or fabricating placeholder authors/years.

Use the exact Markdown emphasis markers shown above (`*...*` for italic, `**...**` for bold) — `review-export-docx` renders these directly into italic/bold runs in the final `.docx`, so do not substitute other formatting conventions.

## Outputs

Write under:

```text
review-projects/<project_id>/05_first_draft/
```

Required files:

```text
draft_bundle.json
first_draft.md
merge_report.md
remaining_issues.md
```

`first_draft.md` must be a continuous review manuscript, not a list of section notes.

`draft_bundle.json` is a machine-readable summary of what went into the merge (used by `review-writing-orchestrator`'s status check). It must include at least:

```text
project_id
review_topic
outline_source
section_sources (list of the sections/*.md files merged)
figure_source (path to figure_candidates.json)
figure_mode ("redrawn" or "source_candidates", matching insert_figures_into_draft.py's output mode)
first_draft (path to the merged first_draft.md)
paper_ids_used (all paper_id values referenced anywhere in the merged draft)
citation_map (object mapping each paper_id to its assigned bracket number, e.g. {"P034": 1, "P035": 2})
```

`first_draft.md` must end with the `## References` section described above; do not leave raw `paper_id` citations in the body once numbering has been applied.

Stop after this stage for human check.
