---
name: review-paragraph-edit
description: Paragraph-level incremental editing for review drafts. Supports targeted modification of individual paragraphs by paragraph_id, with automatic citation renumbering, figure binding maintenance, and version history for rollback.
---

# Review Paragraph Edit

Goal: Enable precise, paragraph-level editing of review drafts without affecting other paragraphs, citations, or figure bindings.

## When to Use

Use this skill when:
- A reviewer requests changes to a specific paragraph
- A single paragraph needs correction, expansion, or deletion
- Citations need to be added/removed from a specific paragraph
- A figure needs to be replaced or rebound to a different paragraph
- Rolling back a previous paragraph modification

Do NOT use this skill for:
- Full manuscript restructuring (use review-draft-merge-polish)
- Adding entirely new sections (use review-section-drafting-figure-picking)
- Final audit corrections (use review-final-audit-release)

## Inputs

```text
review-projects/<project_id>/04_first_draft/first_draft.md
review-projects/<project_id>/04_first_draft/paragraph_manifest.json
review-projects/<project_id>/04_first_draft/citations.json
review-projects/<project_id>/02_section_drafting/figure_candidates.json
```

## Paragraph Identification

Each paragraph is identified by a `paragraph_id` in format `secN-pM`:
- `secN` = section number (e.g., `sec2` for Section 2)
- `pM` = paragraph number within section (e.g., `p1` for first paragraph)

Markers appear in the draft as HTML comments:
```html
<!-- paragraph_id: sec2-p1 -->
```

## Operations

### 1. Update Paragraph Text

Modify the text content of a specific paragraph while preserving:
- The paragraph_id marker
- Citation callouts `[n]` (unless explicitly changed)
- Figure bindings

**Constraints:**
- Do not change the paragraph_id
- Maintain all existing `[n]` callouts unless removing a citation
- Keep figure markdown `![alt](path)` intact unless replacing figure

### 2. Insert New Paragraph

Add a new paragraph after an existing paragraph:
- New paragraph_id is auto-generated (e.g., `sec2-p4` if `sec2-p3` exists)
- No citations initially (add separately if needed)
- No figures initially (bind separately if needed)

### 3. Delete Paragraph

Remove a paragraph and its marker:
- Citations used ONLY by this paragraph are removed from citations.json
- Citations used by other paragraphs are preserved
- All callout numbers are renumbered to stay sequential (1, 2, 3, ...)
- References section is updated to match new numbering
- Figures bound to deleted paragraph become orphaned (flagged for review)

### 4. Add Citation to Paragraph

Add a `[n]` callout to a paragraph:
- If paper_id already has a callout, reuse that number
- If paper_id is new, append to citations.json with next available number
- Paper must exist in literature_matrix.json

### 5. Remove Citation from Paragraph

Remove a specific `[n]` callout from paragraph text:
- Only removes from this paragraph's text
- Citation remains in citations.json if used elsewhere
- Triggers renumbering if citation becomes unused

### 6. Replace Figure

Change the image bound to a paragraph:
- Update the `![alt](path)` markdown in the draft
- Update figure_candidates.json target_paragraph_id if rebinding
- Old figure file is NOT deleted (may be used elsewhere)

### 7. Rollback

Restore a paragraph to a previous version:
- Version history is stored in paragraph_history.json
- Full draft snapshots are stored in versions/ directory
- Rolling back an "insert" operation deletes the paragraph

## Citation Renumbering Rules

When citations change, renumbering follows these rules:

1. Collect all `[n]` callouts currently used in the draft body
2. Sort them in order of first appearance
3. Map to sequential numbers: first used 鈫?[1], second used 鈫?[2], etc.
4. Update all callouts in body text
5. Update citations.json callout numbers
6. Update References section numbering

Example: If [1], [3], [5] are used (after [2] and [4] paragraphs deleted):
- [1] 鈫?[1] (unchanged)
- [3] 鈫?[2] (renumbered)
- [5] 鈫?[3] (renumbered)

## Figure Binding Rules

- Each figure has a `target_paragraph_id` in figure_candidates.json
- When a paragraph is deleted, its figures become "orphaned"
- Orphaned figures should be reviewed and either:
  - Rebound to a nearby paragraph
  - Removed from the draft
- Never silently delete figure files

## Version History

Every modification creates:
1. A snapshot of first_draft.md in `versions/first_draft_TIMESTAMP.md`
2. An entry in `paragraph_history.json`:
```json
{
  "timestamp": "20240101_120000",
  "paragraph_id": "sec2-p1",
  "operation": "update: reason text",
  "old_text": "previous paragraph content...",
  "snapshot_file": "first_draft_20240101_120000.md"
}
```

## Hard Constraints

```text
NEVER modify paragraphs outside the target paragraph_id
NEVER change section headings (## ...) via paragraph edit
NEVER modify the References section directly (it auto-updates)
NEVER delete figure files (only unbind from paragraphs)
NEVER invent new paper_ids (must exist in literature_matrix.json)
ALWAYS preserve paragraph_id markers
ALWAYS renumber citations after deletions
ALWAYS save version snapshot before modification
```

## API Endpoints

The dashboard backend provides these endpoints:

```
GET    /api/project/<id>/paragraphs           List all paragraphs
GET    /api/project/<id>/paragraph/<pid>      Get paragraph with metadata
PUT    /api/project/<id>/paragraph/<pid>      Update paragraph text
POST   /api/project/<id>/paragraph            Insert new paragraph
DELETE /api/project/<id>/paragraph/<pid>      Delete paragraph
GET    /api/project/<id>/paragraph/<pid>/history   Get modification history
POST   /api/project/<id>/paragraph/<pid>/rollback  Rollback to version
```

## Outputs

Modified files:
```text
review-projects/<project_id>/04_first_draft/first_draft.md
review-projects/<project_id>/04_first_draft/citations.json
review-projects/<project_id>/04_first_draft/paragraph_manifest.json
review-projects/<project_id>/04_first_draft/paragraph_history.json
review-projects/<project_id>/04_first_draft/versions/first_draft_*.md
```

## Integration with Other Skills

- **After editing**: Run `paragraph_manifest_builder.py` to regenerate manifest
- **Before final audit**: Verify all citations resolve to matrix papers
- **If structure changes significantly**: Consider re-running review-draft-merge-polish

Stop after paragraph edit for human review of changes.
