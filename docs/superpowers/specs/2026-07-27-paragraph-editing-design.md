# Paragraph-Level Draft Editing Design

## Goal

Add paragraph-scoped authoring to the dashboard's **Draft** stage. An author can edit a paragraph, insert one after it, delete it, inspect its revision history, and restore a prior version without replacing the project's current dashboard functionality.

## Scope

The feature applies to `04_first_draft/first_draft.md` for a selected review project.

Supported operations:

- update the prose of one identified paragraph;
- insert a new paragraph after an existing paragraph;
- delete a paragraph;
- view paragraph metadata and history;
- restore a saved version.

It does not introduce section restructuring, direct reference-list editing, invented citations, or deletion of image files. The existing full-draft editor remains available and the current freshness guard remains authoritative.

## Compatibility-First Architecture

The supplied patch is based on an older dashboard server and page. Its complete replacement would remove newer discovery, matrix, and draft-freshness behaviour. The integration therefore adds only the paragraph-specific pieces to the current codebase:

1. `paragraph_manifest_builder.py` assigns stable IDs and creates `paragraph_manifest.json`.
2. `paragraph_editor.py` owns all read/write transformations of a first draft.
3. `serve_review_dashboard.py` exposes narrowly scoped paragraph endpoints while retaining all existing routes.
4. `draft.html` augments the current preview with paragraph selection and dialogs while retaining project selection, freshness protection, and the whole-document edit tab.
5. `review-paragraph-edit/SKILL.md` documents the workflow and constraints for future agents.

The marker convention is an HTML comment immediately **after** its paragraph:

```md
Paragraph prose and citation [4].

<!-- paragraph_id: sec2-p1 -->
```

The renderer and editor must both follow this convention. This explicitly corrects the supplied preview code, which assigned each marker to the following paragraph.

## Data and Operation Flow

When a draft has no paragraph markers, the manifest builder groups prose within each heading and injects deterministic IDs. The draft's references section is excluded from this process.

For every mutation, the editor:

1. validates the project and target paragraph;
2. saves the whole-draft snapshot under `04_first_draft/versions/` and records the operation, old content, and snapshot name in `paragraph_history.json`;
3. changes only the requested paragraph region or insertion point;
4. rebuilds body citation callouts in first-appearance order, updates `citations.json`, and rebuilds the reference list;
5. regenerates `paragraph_manifest.json`.

Deletion may orphan a figure binding, but it must never remove the underlying asset. Citation management only retains citations still used in the body. Rollback is implemented as a normal mutation of the selected paragraph, so it creates an auditable additional history entry.

## Dashboard API

The server will add these routes, each enforcing the same project existence and freshness conditions as the current draft-write endpoint:

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/project/<id>/paragraphs` | List paragraph summaries |
| `GET` | `/api/project/<id>/paragraph/<pid>` | Fetch text, citations, figures, and recent history |
| `PUT` | `/api/project/<id>/paragraph/<pid>` | Update text |
| `POST` | `/api/project/<id>/paragraph` | Insert after a paragraph |
| `DELETE` | `/api/project/<id>/paragraph/<pid>` | Delete with optional reason |
| `GET` | `/api/project/<id>/paragraph/<pid>/history` | Fetch full history |
| `POST` | `/api/project/<id>/paragraph/<pid>/rollback` | Restore a historical version |

Malformed JSON, missing text, an unknown paragraph, a missing draft, or an outdated draft return an explanatory 4xx response. Unexpected mutation failures return a 5xx response without partially writing sidecar files.

## User Interface

The Draft preview will render only marked draft paragraphs as selectable blocks. The leading title, headings, tables, reference list, and unmarked content remain preview-only. Selecting a paragraph reveals actions to edit, insert after, or delete.

The edit dialog presents the paragraph ID, editable Markdown, cited papers, bound figures, and history. Restoring a history item requires a browser confirmation. Insert and delete also require clear confirmation, with deletion explaining that it affects citation numbering and creates a recoverable snapshot. All mutation controls are disabled when the draft is stale.

After a successful operation, the client reloads the current draft payload and regenerates the preview; it does not attempt to patch the document in memory.

## Verification

Automated tests will use a temporary review project to verify marker injection and each operation:

- marker assignment uses the paragraph preceding the marker;
- update changes only the target paragraph and creates a snapshot/history record;
- insert produces a unique ID and preserves the anchor paragraph;
- delete removes the target, drops unreferenced citations, and renumbers retained citations/references;
- rollback restores saved text;
- malformed or unknown API targets produce the correct failure response;
- stale projects reject all paragraph mutations.

The server will also be syntax-checked and exercised through its HTTP endpoints against a temporary project. Existing dashboard source is preserved outside the paragraph-specific additions.
