---
name: review-paragraph-edit
description: Use when a user wants to edit, rewrite, review, or restore one marked paragraph in the current PostgreSQL-native review Draft.
---

# Review Paragraph Edit

Edit only through the native Draft API. The current Draft, revision, immutable versions, rewrite candidates, and approvals are PostgreSQL-owned workflow state; never open or modify workspace files directly.

## Supported workflow

1. Read `GET /api/v1/projects/<id>/draft`.
2. Confirm `freshness.editing_blocked` is false and locate the requested item in `paragraphs` by `paragraph_id`.
3. Show the current paragraph and proposed replacement to the human reviewer.
4. Send the current response `revision` with the write request.
5. Read the Draft again and verify the returned immutable artifact is current.

### Manual paragraph update

```http
PUT /api/v1/projects/<id>/draft/paragraphs/<pid>
Content-Type: application/json

{"revision": 7, "text": "Replacement paragraph text with preserved [1] citations."}
```

The service preserves the paragraph marker and publishes a new immutable Draft version. Preserve citation callouts and figure Markdown unless the reviewer explicitly requests a full-Draft structural edit.

### AI rewrite candidate

Start with `POST /api/v1/projects/<id>/draft/paragraphs/<pid>/rewrite-jobs` and an `Idempotency-Key`. Poll the returned `/api/v1/jobs/<job-id>`. A successful job creates a candidate only; inspect it before sending:

```http
POST /api/v1/projects/<id>/draft/rewrite-candidates/<candidate>/<accept-or-reject>
Content-Type: application/json

{"revision": 7}
```

Only accept a candidate tied to the current Draft and current quality artifact.

### Undo

Choose an artifact from `versions`, then call:

```http
POST /api/v1/projects/<id>/draft/restore
Content-Type: application/json

{"revision": 8, "artifact_id": "<immutable-version-id>"}
```

## Conflict handling

HTTP `409` means the Draft, evaluation, or candidate lineage changed. Do not retry with a guessed revision. Read the Draft again, reapply the intended change to the new paragraph text, and request human confirmation when the content differs.

## Unsupported operations

Dedicated paragraph insert/delete, automatic citation renumbering, direct figure rebinding, and per-paragraph history endpoints are not supported. For a deliberate structural edit, use `PUT /api/v1/projects/<id>/draft` with the complete current text and current `revision`, then run evaluation again. Never claim those unsupported side effects occurred.

Stop after the saved result is displayed for human review.
