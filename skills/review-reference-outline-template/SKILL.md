---
name: review-reference-outline-template
description: Use AI to extract only the format, hierarchy, rhetorical organization, and writing conventions of an uploaded reference review PDF, DOCX, Markdown, or text file, then transfer that abstract style onto the current Matrix without copying source topics, claims, headings, citations, or scientific content. Use when a review project should imitate how a reference review is organized and written, not what it says.
---

# Reference Review Outline Template

Use this skill after Discovery has a confirmed paper set and before Blueprint. Keep this workflow isolated from the built-in outline, Blueprint, Sections, Draft, and Final skills.

1. Run `scripts/analyze_reference_review.py` with the uploaded review and the project's `literature_matrix.json`.
2. Require pass 1 to analyze only reusable form: heading syntax, hierarchy depth, abstract section roles, paragraph moves, transition style, evidence placement, conclusion pattern, and section granularity.
3. Enforce the content firewall. Retry profiles that report source-content leakage. Precisely remove isolated exact source-heading repetitions (including self-check notes) before pass 2, and reject only when repeated extraction cannot produce a clean abstract profile. Never allow topic vocabulary, chemical content, claims, authors, citations, examples, or findings into the transferred outline.
4. Require pass 2 to receive only the abstract style profile plus the current Matrix. Never include the reference text or reference headings in the transfer prompt.
5. Generate the wording and scientific meaning of every heading level—title, body section, subsection, and deeper label—only from the current topic and Matrix papers. Transfer hierarchy depth, heading length, grammatical form, and rhetorical order only. Never translate, paraphrase, reconstruct, or semantically imitate a source-review heading.
6. Validate every body heading against current-Matrix vocabulary and against source-heading semantics. Retry an ungrounded or source-derived outline up to three times; reject it if no isolated result is produced.
7. Build numbered body sections from current Matrix vocabulary and assign every Matrix paper exactly once. Repair missing or duplicate assignments deterministically. Mark introduction and conclusion roles explicitly so the application can attach current-Matrix evidence without copying reference content.
8. Let the user select the candidate in Matrix. Do not select it automatically.
9. Treat the resulting `selected_outline.md` as the sole structure input for Blueprint and later stages.

Do not preserve source heading vocabulary or source heading meaning at any depth. Preserve only abstract heading patterns and rhetorical sequence. The generated Markdown must state that its scientific content source is the current Matrix and must retain `Assigned papers:` lines for Blueprint compatibility.

Inputs: `.pdf`, `.docx`, `.md`, or `.txt`; `01_matrix_outline/literature_matrix.json`.

Outputs: a JSON candidate containing source metadata, non-content structure metrics, the AI style profile, Matrix-grounded outline sections, isolation status, and Markdown suitable for `selected_outline.md`.

Configuration precedence:

- `REVIEW_REFERENCE_OUTLINE_BASE_URL`, then `REVIEW_WRITING_BASE_URL`, then `OPENAI_BASE_URL`.
- `REVIEW_REFERENCE_OUTLINE_API_KEY`, then `REVIEW_WRITING_API_KEY`, then `OPENAI_API_KEY`.
- `REVIEW_REFERENCE_OUTLINE_MODEL`, then `REVIEW_WRITING_MODEL`.
- `REVIEW_REFERENCE_OUTLINE_WIRE_API`, then `REVIEW_WRITING_WIRE_API`.
