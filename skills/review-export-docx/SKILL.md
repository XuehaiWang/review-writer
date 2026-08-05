---
name: review-export-docx
description: Convert a finalized review Markdown draft into a Word DOCX that matches the bundled ACS-style review_template.docx. Use after the review writing pipeline has produced a stable first_draft.md or final_draft.md and the user wants a deliverable .docx with proper section styles, captions, tables, and math.
---

# Review Export DOCX

## FounDryClaw Location Rules

When this skill runs inside FounDryClaw, do not assume the old `review-writer` repository path. Resolve locations in this order:

1. Use environment variables when present: `FOUNDRYCLAW_REVIEW_ROOT`, `FOUNDRYCLAW_REVIEW_LIBRARY_ROOT`, `FOUNDRYCLAW_REVIEW_PROJECTS_ROOT`, `FOUNDRYCLAW_MINERU_OUTPUT_ROOT`, `FOUNDRYCLAW_REVIEW_PDF_ROOT`, `FOUNDRYCLAW_REVIEW_SKILLS_ROOT`.
2. If the user provides `--review-root`, use it.
3. Otherwise treat the current FounDryClaw Claude workdir as the review root.
4. Store project artifacts under `<review-root>/review-projects/<project_id>/` and library metadata under `<review-root>/review-library/`.
5. Run bundled scripts by path relative to this skill folder, for example `python scripts/<script>.py`; the scripts contain a shared resolver for the paths above.

For lower-capability backend models: before running a script, identify `review_root` explicitly and pass `--review-root <review_root>` when uncertain. Never use `<review-root>` as a real path in FounDryClaw.

Convert a finalized review Markdown into Word DOCX using the bundled ACS-style template.

## When To Use

```text
final delivery of a review draft as .docx
the source markdown is stable (first_draft.md or final_draft.md)
the document must match the bundled ACS-style template
the markdown may contain pipe tables, images, and LaTeX math
```

Do not use this skill to revise content, fix citations, or validate evidence.

## Inputs

```text
review-projects/<project_id>/04_first_draft/first_draft.md
or
review-projects/<project_id>/05_final_audit/final_draft.md
```

## Dependencies

The dashboard uses `scripts/run_md2docx.py`, which installs `python-docx` and
Pillow once into `review-export-docx/.deps/` when they are not already present.
This keeps the global Python installation unchanged. `latex2word` remains
optional; without it, math is rendered as italic plain text and a warning is
printed.

## Run

Default (final draft):

```bash
python <review-root>/skills/review-export-docx/scripts/md2docx.py \
  --input  <review-root>/review-projects/<project_id>/05_final_audit/final_draft.md \
  --output <review-root>/review-projects/<project_id>/05_final_audit/final_draft.docx
```

First draft:

```bash
python <review-root>/skills/review-export-docx/scripts/md2docx.py \
  --input  <review-root>/review-projects/<project_id>/04_first_draft/first_draft.md \
  --output <review-root>/review-projects/<project_id>/04_first_draft/first_draft.docx
```

Custom template:

```bash
python <review-root>/skills/review-export-docx/scripts/md2docx.py \
  --input    /abs/path/review.md \
  --output   /abs/path/review.docx \
  --template /abs/path/custom_template.docx
```

The default template is `<review-root>/skills/review-export-docx/review_template.docx`.

## Style Mapping

```text
# Title           -> BA_Title
## Section        -> TA_Main_Text bold
### Sub-section   -> TA_Main_Text bold italic
#### ...          -> TA_Main_Text italic
body paragraph    -> TA_Main_Text
## Abstract       -> BD_Abstract
## Keywords       -> BG_Keywords
## References     -> TF_References_Section
## Acknowledgments-> TD_Acknowledgments
## Supporting Information -> TE_Supporting_Information
Figure N. ...     -> VA_Figure_Caption
Table N.  ...     -> VD_Table_Title
Scheme N. ...     -> VC_Scheme_Title
Chart N.  ...     -> VB_Chart_Title
table cell        -> TC_Table_Body
$...$  / $$...$$  -> OMML via latex2word (or italic plain text fallback)
```

## Supported Markdown

```text
ATX headings # .. ######
bold / italic / bold-italic / inline code
fenced code blocks
inline math $...$ and display math $$...$$
unordered and ordered lists (nested up to 3 levels)
pipe tables with optional separator row
standalone image lines ![alt](path) -> picture + auto caption
horizontal rules (treated as section separators, not visual borders)
YAML front matter (silently skipped)
```

## Image Paths

Relative image paths in the Markdown are resolved against the Markdown file's directory. Make sure redrawn or source images are reachable when the script runs.

## Summary-chart bridge

When `review_summary_chart.json` exists beside the selected Markdown, validate
that it was generated from the current draft with `generation_scope: full` or
the legacy-compatible `both`. Validate every PNG path and SHA-256 in
`stats.image_manifest`, then insert the full-review chart at the managed
manuscript position. Missing, stale, or unsafe chart entries stop export with a
clear error instead of being skipped.

## Boundary

```text
use only after review content is stable
do not rewrite, polish, or revise content
do not modify or polish manuscript content
do not run this skill in place of the final audit skill
```

## Files

```text
review-export-docx/
  SKILL.md
  review_template.docx
  scripts/
    md2docx.py
```
