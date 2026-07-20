---
name: review-export-docx
description: Convert a finalized review Markdown draft into a Word DOCX that matches the bundled ACS-style review_template.docx. Use after the review writing pipeline has produced a stable first_draft.md or final_draft.md and the user wants a deliverable .docx with proper section styles, captions, tables, and math.
---

# Review Export DOCX

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

```bash
pip install python-docx Pillow latex2word
```

If `latex2word` is missing, math is rendered as italic plain text and a warning is printed.

## Run

Default (final draft):

```bash
python3 /home/ps/review-writer/skills/review-export-docx/scripts/md2docx.py \
  --input  /home/ps/review-writer/review-projects/<project_id>/05_final_audit/final_draft.md \
  --output /home/ps/review-writer/review-projects/<project_id>/05_final_audit/final_draft.docx
```

First draft:

```bash
python3 /home/ps/review-writer/skills/review-export-docx/scripts/md2docx.py \
  --input  /home/ps/review-writer/review-projects/<project_id>/04_first_draft/first_draft.md \
  --output /home/ps/review-writer/review-projects/<project_id>/04_first_draft/first_draft.docx
```

Custom template:

```bash
python3 /home/ps/review-writer/skills/review-export-docx/scripts/md2docx.py \
  --input    /abs/path/review.md \
  --output   /abs/path/review.docx \
  --template /abs/path/custom_template.docx
```

The default template is `/home/ps/review-writer/skills/review-export-docx/review_template.docx`.

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
that it was generated from the current draft with `generation_scope: both`.
Validate every PNG path and SHA-256 in `stats.image_manifest`, then insert the
full-review chart before the manuscript body and each section chart immediately
after its matching section heading. Missing, stale, unsafe, or unmatched chart
entries stop export with a clear error instead of being skipped.

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
