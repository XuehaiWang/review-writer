---
name: review-export-docx
description: Convert a finalized review Markdown draft into a Word DOCX matching a manuscript template. Use after the review writing pipeline has produced a stable first_draft.md or final_draft.md and the user wants a deliverable .docx with proper section styles, captions, tables, and math.
---

# Review Export DOCX

Convert a finalized review Markdown into a Word DOCX using a manuscript template.

## Template Selection (LLM step, required before running the script)

This skill does not assume any specific publisher, journal, or subject area. Before running, determine which template is appropriate:

1. If the user has specified a target journal/publisher or provided their own `.docx` template, use it via `--template`.
2. If the user hasn't specified one, ask them whether a specific manuscript template is required.
3. If no specific template is required, fall back to the bundled `review_template.docx`. This bundled template is a plain, neutral academic manuscript layout (Times New Roman, standard section styles) — it is not tied to any specific publisher's branding or a specific subject area's conventions. Treat it as a reasonable default, not a fixed requirement.

Any template used (bundled or custom) must define the named paragraph styles listed under "Style Mapping" below, since `md2docx.py` applies content to those named styles. If a custom template doesn't define them, either add the missing styles to that template first or fall back to the bundled default.

## When To Use

```text
final delivery of a review draft as .docx
the source markdown is stable (first_draft.md or final_draft.md)
the markdown may contain pipe tables, images, and LaTeX math
```

Do not use this skill to revise content, fix citations, or validate evidence.

## Inputs

```text
review-projects/<project_id>/05_first_draft/first_draft.md
or
review-projects/<project_id>/06_final_audit/final_draft.md
```

## Output

DOCX files are written to their own stage folder, not next to the source Markdown:

```text
review-projects/<project_id>/07_docx_export/
```

## Dependencies

```bash
pip install python-docx latex2word
```

If `latex2word` is missing, math is rendered as italic plain text and a warning is printed.

## Run

Default (final draft):

```bash
python3 <skill-root>/scripts/md2docx.py \
  --input  <review-root>/review-projects/<project_id>/06_final_audit/final_draft.md \
  --output <review-root>/review-projects/<project_id>/07_docx_export/final_draft.docx
```

First draft:

```bash
python3 <skill-root>/scripts/md2docx.py \
  --input  <review-root>/review-projects/<project_id>/05_first_draft/first_draft.md \
  --output <review-root>/review-projects/<project_id>/07_docx_export/first_draft.docx
```

Custom template (see Template Selection above):

```bash
python3 <skill-root>/scripts/md2docx.py \
  --input    /abs/path/review.md \
  --output   /abs/path/review.docx \
  --template /abs/path/custom_template.docx
```

`<skill-root>` is the directory containing this `SKILL.md`. The default template (used when `--template` is omitted) is `<skill-root>/review_template.docx`.

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

`Scheme N.` and `Chart N.` are optional caption types recognized only if the Markdown actually uses that numbering (common in some subject areas' procedural/process diagrams and data charts). If the review's Markdown never produces `Scheme N.` or `Chart N.` captions, those styles simply go unused — no action needed.

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

## Boundary

```text
use only after review content is stable
do not rewrite, polish, or revise content
do not use this skill to read or normalize MinerU output
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
