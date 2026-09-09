"""Canonical logical names for versioned workflow artifacts.

These names are cross-stage contracts. Keeping them outside any one API
service prevents downstream stages from importing upstream service modules
merely to learn a file name.
"""

DISCOVERY_REVIEW = "discovery/review.json"

MATRIX = "matrix/literature_matrix.json"
PLANNING_OUTLINE = "planning/selected_outline.json"
PLANNING_REFERENCE_OUTLINES = "planning/reference_outlines.json"
BLUEPRINT = "blueprint/section_blueprint.json"

SECTION_DRAFTS = "sections/section_drafts.json"
SECTION_EVIDENCE_PACKAGE = "sections/evidence_package.json"
SECTION_SYNTHESIS_STATE = "sections/synthesis_state.json"
SECTION_WRITING_PLAN = "sections/writing_plan.json"
SECTION_PAPER_FIGURE_CANDIDATES = "sections/paper_figure_candidates.json"
SECTION_FIGURE_CANDIDATES = "sections/figure_candidates.json"
SECTION_DEFAULT_FIGURE_REVIEWS = "sections/default_figure_reviews.json"

FIGURE_REVIEW_SELECTIONS = "figure-review/selections.json"
FIGURE_REVIEW_INPUTS = "figure-review/selected_figures.json"
FIGURE_MANIFEST = "figures/manifest.json"

DRAFT_MANUSCRIPT = "draft/manuscript.md"
DRAFT_QUALITY_REPORT = "draft/quality.json"
DRAFT_REWRITE_CANDIDATES = "draft/rewrite-candidates.json"
DRAFT_OPTIMIZATION_PROPOSALS = "draft/optimization-proposals.json"
DRAFT_REWRITE_OVERLAYS = "draft/rewrite-overlays.json"
DRAFT_APPROVAL = "draft/approval.json"

FINAL_CONCLUSION = "final/conclusion.md"
FINAL_CONCLUSION_REPORT = "final/conclusion-report.json"
FINAL_OVERVIEW_IMAGE = "final/overview.png"
FINAL_OVERVIEW_TEXT = "final/overview-text.json"
FINAL_FRONT_MATTER = "final/front-matter.json"
FINAL_MANUSCRIPT = "final/manuscript.md"
FINAL_VALIDATION = "final/validation.json"
FINAL_RELEASE = "final/release.json"
FINAL_DOCX = "final/manuscript.docx"
FINAL_DOCX_QA = "final/docx-qa.json"
FINAL_MANUSCRIPT_STATE = "final/manuscript_state.json"
FINAL_RENDER_MANIFEST = "final/render_manifest.json"
FINAL_TEX = "final/manuscript.tex"
FINAL_PDF = "final/manuscript.pdf"
FINAL_PDF_QA = "final/pdf-qa.json"
FINAL_PDF_COMPILE_LOG = "final/pdf-compile.log"
