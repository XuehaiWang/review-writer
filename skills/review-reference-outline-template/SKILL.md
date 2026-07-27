---
name: review-reference-outline-template
description: Extract the structure and writing conventions from an uploaded reference review PDF, DOCX, Markdown, or text file, then create a selectable project outline that assigns the current Matrix papers to the reference structure. Use when a review project should follow the organization of a similar published review.
---

# Reference Review Outline Template

Use this skill after Discovery has a confirmed paper set and before Blueprint.

1. Run `scripts/analyze_reference_review.py` with the uploaded review and the project's `literature_matrix.json`.
2. Inspect the emitted heading hierarchy, writing-style signals, and candidate Markdown.
3. Let the user select the reference-derived candidate in Matrix. Do not select it automatically.
4. Treat the saved `selected_outline.md` as the sole structure input for Blueprint, Sections, Draft, and Final.

The script preserves the reference review's section order and heading vocabulary. It assigns every current Matrix paper to a numbered body section and records the assignment in `Assigned papers:` lines. It never imports scientific claims or citations from the reference review.

Inputs: `.pdf`, `.docx`, `.md`, or `.txt`; `01_matrix_outline/literature_matrix.json`.

Outputs: a JSON candidate containing source metadata, heading hierarchy, writing-style signals, and Markdown suitable for `selected_outline.md`.
