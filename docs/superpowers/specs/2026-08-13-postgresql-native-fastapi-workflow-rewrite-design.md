# PostgreSQL-Native FastAPI Workflow Rewrite Design

## Status and baseline

This design was approved section by section on 2026-08-13. The implementation target is the `dy-launch` branch only. The `dy` branch must remain unchanged.

The complete pre-rewrite application is preserved by merge commit `9eea953` on `dy-launch`. That commit is the code rollback point before the PostgreSQL-native workflow rewrite.

## Objective

Replace the runtime SQLite workflow store, Prefect wrapper, compatibility gateway, and `DashboardHandler` business core with native FastAPI services backed by PostgreSQL, while preserving the existing seven-stage user workflow and all current manual review and editing capabilities.

Existing user data must be preserved through a one-time, idempotent migration. Migration failure must not enable the new runtime or silently discard records.

## Scope

The rewrite includes:

- PostgreSQL persistence for workflow stages, runs, jobs, artifacts, dependencies, approvals, and migration reports.
- Native FastAPI routers and services for all seven user-facing stages.
- A small in-process background executor with PostgreSQL-persisted task state.
- User- and project-isolated streaming file access.
- A read-only SQLite-to-PostgreSQL migration command.
- Frontend migration to versioned `/api/v1` endpoints.
- Removal of SQLite, Prefect, `workflow_compat.py`, `dashboard_executor.py`, and `DashboardHandler` from the final runtime.
- Regression and functional-parity verification against the current application.

The rewrite does not include:

- Organizations, teams, email verification, billing, or complex role hierarchies.
- Redis, Celery, Kafka, or a distributed scheduler.
- Object storage. Files remain in the user-isolated server workspace for this small deployment.
- Horizontal multi-instance execution. The initial deployment runs one API process with a bounded worker pool.
- Reimplementation of the scientific algorithms inside the existing skill scripts. The scripts remain algorithm units and receive explicit inputs and staging output directories from native services.
- Visual redesign of the seven-stage dashboard.

## Target architecture

The final request path is:

```text
Browser
  -> FastAPI authentication and project authorization
  -> Domain router
  -> Domain service
  -> PostgreSQL repositories
  -> Scientific runner or direct editor
  -> Staging output validation
  -> Atomic file publication
  -> PostgreSQL artifact and stage-state commit
```

FastAPI is the only HTTP application. Production requests must not import or invoke `view/serve_review_dashboard.py`, `review_writer_api/workflow_compat.py`, or `review_writer_api/dashboard_executor.py`.

PostgreSQL is authoritative for all structured application and workflow state. The filesystem stores large or user-editable artifacts such as PDF, Markdown, SVG, PNG, JSON, and DOCX. Database artifact records use paths relative to the owning user's workspace and never store deployment-specific paths such as `/app/...` or a Windows drive path.

The seven user-facing stages remain:

1. Library
2. Discovery
3. Analysis and Planning
4. Sections
5. Image Processing
6. Draft
7. Final

Matrix and Blueprint remain internal substages of Analysis and Planning. Figure Review and Redraw/Edit remain internal substages of Image Processing. The backend may store these granular stage identifiers while exposing one composite state for each user-facing stage.

## Domain boundaries

### Library

Owns PDF upload, file validation, MinerU submission and result ingestion, metadata extraction, duplicate detection, library search, document viewing, and library deletion. Upload and download use streaming APIs and authenticated artifact identifiers instead of arbitrary local paths.

### Discovery

Owns topic configuration, keyword plans, local and external retrieval, candidate ranking, candidate statistics, explicit Matrix selection, top-N selection, confirmation, and Matrix synchronization.

### Planning

Owns the literature matrix, row editing, outline templates, custom blank outlines, reference-outline upload, manual outline editing, Blueprint generation, and the gate into section drafting.

### Sections

Owns section tasks, section drafting, progress, retry state, chapter Blueprint visibility, section editing, and the gate into image processing.

### Figures

Owns source candidate review, candidate selection, manuscript-anchor checks, AI redraw, batch redraw, safety and integrity validation, SVG cropping and editing, Ketcher chemical-structure editing, human approval, and the gate into Draft.

### Drafts

Owns first-draft assembly, paragraph manifests, paragraph and full-text editing, live quality evaluation, issue expansion with corresponding images, AI rewrite candidates, accept/reject/undo operations, optimization progress, and approval into Final.

### Final

Owns final merge, overview figure generation and editing, conclusion generation, reports, final validation, Markdown delivery, and Word export.

### Jobs and files

Jobs provide task creation, status, progress, cancellation, interruption, and retry across all domains. Files resolve database artifact IDs to paths only after user and project ownership checks.

## Service and repository rules

Routers only parse HTTP input, invoke authorization dependencies, call services, and map domain results to response schemas. They do not contain scientific or filesystem business logic.

Services own stage gates, transaction boundaries, script invocation, output validation, and artifact publication. Each service receives explicit repositories, workspace paths, provider settings, and task execution dependencies.

Repositories own PostgreSQL reads and writes. Every project query is scoped by both owning `user_id` and project ID. A caller cannot obtain a project-scoped repository object without passing an authenticated principal.

Scientific scripts remain callable units. A shared runner supplies explicit input paths, provider environment values, timeouts, cancellation checks, and a per-run staging directory. Provider secrets are passed only to the child process environment for the duration of that run and are removed from in-process runtime caches after completion.

## PostgreSQL model

The existing `users`, `user_sessions`, `projects`, and `provider_credentials` tables remain. Workflow state is normalized into the following tables.

### `workflow_stage_states`

One row per project and internal stage. It stores status, revision, current run ID, input and output fingerprints, error code and message, and timestamps. The `(project_id, stage_id)` pair is unique. The revision is used for optimistic concurrency and stale-form protection.

### `workflow_stage_runs`

One row per execution attempt. It stores the original legacy run ID when migrated, project, stage, requesting user, status, attempt number, progress, input and output snapshots, fingerprints, metadata, error information, and start/update/finish times.

### `workflow_artifacts`

One row per artifact version. It stores the original legacy artifact ID when migrated, project, logical name, artifact type, workspace-relative path, content SHA-256, size, modification timestamp, availability, producer stage and run, metadata, and creation time. The existing uniqueness of project, logical name, and content hash is preserved.

### `workflow_current_artifacts`

One row per project and logical artifact name pointing to its current artifact version.

### `workflow_artifact_dependencies`

Links output artifact versions to input artifact versions and preserves the dependency role.

### `workflow_jobs`

Stores project- or library-scoped background work. Every row has an owning user. Project-scoped rows also have a project ID. Fields include original legacy job ID, scope, type, status, idempotency key, payload, result, progress, cancellation flag, error information, and timestamps.

### `workflow_current_jobs`

Points from `(user_id, project_id or library scope, job_type)` to the current job.

### `workflow_approvals`

Records human approval, rejection, manual warning override, and rewrite acceptance as append-only audit events. Each event includes user, project, stage, target type and ID, decision, prior and resulting artifact IDs where applicable, reason, and timestamp. Existing file-embedded approval metadata is preserved during migration and normalized when it can be mapped unambiguously.

### `workflow_migrations`

Records the SQLite source path, source SHA-256, owning user, status, source and imported row counts, validation results, errors, start and finish times, and migration tool version. The source hash is unique for a successful import, making reruns idempotent.

Static stage dependency definitions remain version-controlled application configuration. Migrated dependency rows are included in the migration report and compared with the current configuration instead of becoming editable production data.

## File publication protocol

Each stage run receives a staging directory under the owning project. Scientific scripts write only to this directory. Services then:

1. Validate required output files and their scientific integrity gates.
2. Calculate hashes, sizes, media types, and metadata.
3. Atomically move validated files to immutable versioned artifact paths on the same filesystem.
4. In one PostgreSQL transaction, insert artifact versions, dependencies, current pointers, run completion, and stage state.

If file publication succeeds but the PostgreSQL transaction fails, the files remain unreachable orphan versions and are reported for later cleanup. A database pointer can never reference a file that has not already been published. Current artifact files are not overwritten in place.

Manual editors use the same protocol. Saving SVG, cropped SVG, Ketcher output, Markdown, or overview text creates a new artifact version and an audit event rather than destroying the previous version.

## Background execution

The initial deployment uses one bounded `ThreadPoolExecutor`, with a default of two workers and an environment-configurable maximum. It does not use Prefect or a separate message broker.

A long action first creates a queued PostgreSQL job and commits it. The executor then claims that job and changes it to running. The API returns HTTP 202 with the job ID, and the browser polls the versioned Jobs API.

An idempotency key prevents double-clicks from launching duplicate work. Project and job-type locks prevent conflicting operations on the same stage while allowing different users and projects to run independently.

Task states are:

```text
queued -> running -> succeeded
                  -> failed
                  -> cancel_requested -> cancelled
                  -> interrupted
```

Cancellation is cooperative at safe checkpoints. API startup marks previously running in-process jobs as interrupted because their Python callbacks cannot survive a process restart. The user may explicitly retry an interrupted job, producing a new job row linked to the prior attempt.

This executor is intentionally limited to the single-instance deployment. Multi-instance execution would require a separate future design.

## API and frontend contract

All active frontend requests move to `/api/v1`. Old endpoints are not retained in the final runtime. During development, current handler outputs may be invoked in tests as a comparison oracle, but the new application does not forward production requests to the old handler.

Success schemas are explicit Pydantic models. Error responses use:

```json
{
  "error": {
    "code": "FIGURE_APPROVAL_REQUIRED",
    "message": "Selected figures still require approval.",
    "retryable": false,
    "details": {}
  }
}
```

The frontend translates stable error codes for Chinese and English instead of matching English messages. Each job response includes current status, progress, safe user-facing error information, and available actions such as cancel or retry.

## Stage gates

Server-side gates remain authoritative:

- Discovery confirmation requires at least one explicitly selected candidate and synchronizes exactly that selection to Matrix.
- Planning completion requires a current Matrix selection and an approved, structurally valid outline.
- Sections require every Blueprint paper reference to resolve to a selected paper.
- Image Processing requires every manuscript-selected figure to be usable, safely validated, or explicitly approved by a user with an audit event.
- Draft-to-Final requires a current assembled draft and explicit draft approval. Quality issues may remain visible when the user explicitly accepts permitted warnings; blocking integrity failures cannot be overridden silently.
- Final export requires current artifact pointers and passes reference, figure, and file-availability validation.

A changed upstream input marks downstream stages stale using artifact fingerprints. A stale stage cannot be presented as current merely because its old files still exist.

## Error handling and concurrency

PostgreSQL row locking protects job claiming and destructive transitions. Optimistic revision checks protect form and editor saves. A save based on an old revision returns HTTP 409 with `STATE_CONFLICT` and current revision information.

HTTP 429, model 503, connection timeout, and narrowly classified temporary provider errors may retry up to three times with bounded exponential backoff. Validation errors, permission errors, integrity gates, and human-review gates are never retried automatically. A completed scientific call is not repeated because a separate summary write failed.

Files are written to staging and published only after validation. Failed runs keep error and diagnostic metadata without changing current artifact pointers.

Project deletion soft-deletes the PostgreSQL project and moves its workspace to a user-scoped trash directory. Permanent cleanup is an explicit maintenance action and is not part of the request transaction.

## Security and isolation

Every project route verifies authenticated ownership before resolving services or files. File APIs accept an artifact ID, not an arbitrary browser-supplied path. Resolved paths must remain inside the owning workspace.

Provider URLs are parsed and validated. Public deployment blocks private-network and loopback provider targets unless an explicit trusted-LAN setting enables them. Plain HTTP is rejected outside explicitly permitted local use.

Credentials remain encrypted in PostgreSQL. Decrypted secrets are task-scoped, excluded from payloads, logs, and errors, and cleared after task completion. Logs include request, job, user, and project identifiers but exclude cookies, passwords, API keys, PDF text, and manuscript content.

Login and registration receive lightweight single-instance rate limiting. AI image outputs continue through safety, chemical integrity, aspect ratio, and completeness validation. Warning outputs remain viewable but cannot enter the manuscript without explicit human approval where policy permits approval.

## One-time SQLite migration

The migration is a maintenance command and never runs automatically inside a normal API request.

### Preparation

1. Stop the API so SQLite and project files cannot change.
2. Back up PostgreSQL.
3. Use the SQLite Backup API to create consistent copies of every `workflow.sqlite3`, including WAL content.
4. Produce a source inventory containing file path, SHA-256, owning workspace, table counts, and project slugs.

### Ownership mapping

Hosted workspace directories map to a PostgreSQL user UUID. Each SQLite project slug maps to the active PostgreSQL project owned by that user. A local legacy workspace without a hosted user requires an explicit `--owner-email`; the migrator never guesses an owner.

The SQLite pseudo-project used for Library jobs maps to user-scoped `scope=library` jobs and is not inserted as a real project.

### Import and validation

The migrator preserves legacy IDs, timestamps, statuses, attempts, snapshots, metadata, current pointers, and dependency links. Absolute artifact paths are converted to workspace-relative paths. The original path is retained only inside migration metadata for diagnostics.

Existing missing files do not cause records to disappear. Their artifact rows are imported with `availability=missing` and listed explicitly. Any current artifact that is missing makes the validation report non-clean and requires operator acknowledgement before cutover.

Each source import is transactional and idempotent. After all sources import, validation compares:

- Source and destination row counts by table and project.
- Legacy primary IDs and all foreign-key relationships.
- Current stage, artifact, and job pointers.
- Artifact hashes and file sizes for available files.
- User and project ownership.
- Stage and task payload JSON values.
- Approval and warning metadata that can be mapped.

A global workflow-ready marker is written only after every source succeeds and the operator accepts any pre-existing missing-file report. The new API refuses workflow requests without this marker when legacy sources were detected. A clean new installation with no legacy sources receives the marker during database initialization.

Failure leaves the marker unset and the SQLite backups unchanged. The command prints a machine-readable JSON report and a concise operator summary. Re-running the same successful source hash does not duplicate rows.

After cutover, SQLite files are retained read-only as backups but are never opened by the application.

## Functional parity requirements

Before migration work changes behavior, the implementation must create a parity inventory for every current page action, endpoint, stage gate, input artifact, output artifact, and state mutation. The inventory is an acceptance checklist, not optional documentation.

At minimum, parity covers:

- PDF upload, MinerU parsing, duplicates, LAN-safe PDF viewing, and library search.
- Discovery statistics, candidate pool, explicit Matrix selection, top-N selection, and exact Matrix synchronization.
- Matrix editing, custom outlines, blank custom-outline initialization, manual outline editing, reference outlines, and Chinese/English switching.
- Section generation, Blueprint visibility, progress, reports, and provider error handling.
- Source-figure review, image/table candidates, manuscript anchors, batch and individual redraw, safety warnings, manual approval, SVG crop/edit/save, Ketcher editing, and figure-to-Draft confirmation.
- Draft assembly, paragraph and full-text editing, live quality score, issue expansion with images, rewrite progress, rewrite acceptance/rejection/undo, and manual approval.
- Final merge, overview figure, editable overview text, conclusion, reports, validations, Markdown, and Word export.
- Authentication, project isolation, API credential isolation, direct-file denial, and two-user concurrency.

The old handler may remain importable only by parity tests until the corresponding native domain passes. It must be removed from runtime imports immediately and deleted after all parity rows pass.

## Implementation sequence

The work is divided into independently testable deliverables:

1. Freeze API, artifact, gate, and parity contracts for the current application.
2. Add explicit Alembic migrations and PostgreSQL workflow models and repositories.
3. Add the SQLite backup, import, validation, reporting, and readiness-marker command.
4. Add authenticated artifact streaming, staging publication, audit recording, and the bounded job executor.
5. Migrate Library and Discovery to native routers and services.
6. Migrate Planning and Sections.
7. Migrate Figures, including all manual editors and gates.
8. Migrate Draft and Final.
9. Move the frontend to `/api/v1` and complete Chinese/English contract translation.
10. Run the real-data dry run, stopped migration, browser regression, and two-user isolation checks.
11. Remove runtime and dependency references to SQLite and Prefect, then delete compatibility gateway and old handler code.
12. Update deployment, backup, restore, migration, and launch documentation.

Each deliverable follows test-driven development and is committed separately to `dy-launch`. The `dy` branch is never changed or force-pushed.

## Test strategy

Unit tests cover stage gates, services, repositories, error mapping, path validation, idempotency, fingerprinting, and cancellation decisions. Unit tests may use SQLAlchemy's in-memory database where PostgreSQL-specific behavior is not involved.

PostgreSQL integration tests run against a real PostgreSQL service and cover migrations, JSON fields, transactions, row locking, project ownership, job claiming, optimistic revisions, and cascade behavior.

Migration tests create representative SQLite databases with all legacy tables, duplicate hashes, Library pseudo-jobs, missing files, stale stages, current pointers, and multiple hosted users. They prove dry-run behavior, successful import, idempotent rerun, failed validation without a ready marker, and exact count and ID preservation.

API tests cover every `/api/v1` endpoint with two users, cross-user denial, file range requests, task interruption, provider retries, and stable error codes.

Functional parity tests use fixture projects to compare the current handler's observable results with native services where the contract is unchanged. Tests that assert behavior intentionally removed by the approved seven-stage design are replaced with tests for the approved behavior and documented in the parity inventory.

Frontend checks cover seven-stage navigation, composite substages, controls, task status, error translation, and both Chinese and English. A final browser walkthrough exercises the entire workflow on a representative project.

CI compiles all application modules, runs the full maintained suite rather than two selected modules, runs Alembic against PostgreSQL, and performs a container health smoke test.

## Acceptance and removal gate

The rewrite is complete only when:

- The functional parity inventory has no unresolved row.
- All maintained tests pass; stale tests are explicitly replaced rather than ignored.
- PostgreSQL migration, login, two-user isolation, and seven-stage integration tests pass against a real server.
- Source and destination stage, run, task, artifact, dependency, and approval counts match.
- Available artifact hashes match; pre-existing missing files are separately acknowledged.
- Normal application use creates no `workflow.sqlite3` or Prefect runtime directory.
- Production requests never import or call the old handler or compatibility gateway.
- The seven-stage browser workflow works in Chinese and English.
- Backup, migration, restore, and launch instructions are verified.

Only after these conditions pass may the implementation remove SQLite and Prefect dependencies and delete the old runtime code.

## Rollback

Before public launch, rollback uses the `9eea953` code baseline, the PostgreSQL pre-migration dump, and the read-only SQLite backup set. No user writes are permitted between final migration validation and the go/no-go decision. This ensures rollback cannot lose post-cutover work.

After the first accepted production write on the PostgreSQL-native system, rollback to SQLite is no longer supported. Recovery then uses PostgreSQL and workspace backups, not reverse migration into SQLite.
