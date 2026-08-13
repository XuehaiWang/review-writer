# PostgreSQL-Native FastAPI Workflow Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the runtime SQLite, Prefect, compatibility gateway, and `DashboardHandler` workflow core with native FastAPI services and PostgreSQL without losing current seven-stage functionality or legacy user data.

**Architecture:** Versioned FastAPI domain routers call focused services and PostgreSQL repositories. Scientific scripts run through an explicit staging runner; immutable files remain in user workspaces while PostgreSQL owns stage, job, artifact, approval, and migration state. A stopped, idempotent migration imports every legacy SQLite workflow database and writes a readiness marker only after validation.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 17, psycopg 3, `concurrent.futures.ThreadPoolExecutor`, unittest, Docker Compose.

## Global Constraints

- Work only on `dy-launch`; `dy` and `origin/dy` must remain at `f397e3e`.
- Preserve the pre-rewrite rollback point `9eea953`.
- PostgreSQL is the only runtime structured-state store; normal execution must never create or open `workflow.sqlite3`.
- Do not add Redis, Celery, Kafka, Prefect, an organization model, email verification, object storage, or horizontal scheduling.
- Preserve the seven user-facing stages and all manual review, editing, bilingual, export, and safety features listed in the approved design.
- Use `/api/v1` for all active workflow APIs; do not forward production requests into the old handler.
- Store large files in user-isolated workspaces and store only workspace-relative paths plus hashes and metadata in PostgreSQL.
- Every behavior change follows test-driven development: create a failing test, observe the expected failure, implement the minimum behavior, and rerun the focused and affected suites.
- Commit each task separately to `dy-launch`; push only after the task's verification gate passes.

## Planned file structure

- `review_writer_api/errors.py`: stable error codes and FastAPI exception mapping.
- `review_writer_api/workflow_models.py`: SQLAlchemy workflow, job, artifact, approval, and migration models.
- `review_writer_api/workflow_contracts.py`: stage IDs, states, composite seven-stage mapping, and transition rules.
- `review_writer_api/workflow_repository.py`: user-scoped PostgreSQL persistence and transaction-safe job claiming.
- `review_writer_api/artifact_service.py`: staging paths, validation metadata, immutable publication, and file lookup.
- `review_writer_api/job_service.py`: bounded executor, idempotency, cancellation, interruption, and polling.
- `review_writer_api/scientific_runner.py`: subprocess execution with task-scoped provider environment and staging output.
- `review_writer_api/workflow_migration.py`: read-only SQLite inventory, import, validation, and readiness marker.
- `review_writer_api/migrate_workflow.py`: maintenance CLI entry point.
- `review_writer_api/container.py`: application service wiring stored on `app.state`.
- `review_writer_api/domain_services/*.py`: Library, Discovery, Planning, Sections, Figures, Drafts, and Final services.
- `review_writer_api/routers/*.py`: native `/api/v1` domain, jobs, files, and dashboard page routers.
- `review_writer_api/workflow_schemas.py`: request and response models shared by workflow routers.
- `migrations/versions/20260813_0002_postgres_workflow.py`: explicit workflow schema migration.
- `docs/workflow-feature-parity.md`: current-to-native behavior inventory and removal gate.
- `review_writer_api/tests/`: unit, API, PostgreSQL, migration, and parity tests.

---

### Task 1: Freeze the current functional contract

**Files:**
- Create: `docs/workflow-feature-parity.md`
- Create: `review_writer_api/parity.py`
- Create: `review_writer_api/tests/__init__.py`
- Create: `review_writer_api/tests/test_workflow_contract_inventory.py`
- Modify: `.github/workflows/api-foundation.yml`

**Interfaces:**
- Consumes: current unversioned dashboard endpoints, seven HTML pages, current checks, and `DashboardHandler` route dispatch.
- Produces: a machine-checkable parity table whose row IDs are referenced by later domain tests.

- [x] **Step 1: Write the failing inventory test**

```python
class WorkflowContractInventoryTests(unittest.TestCase):
    def test_required_parity_rows_exist_and_have_native_test_targets(self):
        rows = load_parity_rows(ROOT / "docs" / "workflow-feature-parity.md")
        required = {"LIB-001", "DIS-001", "PLN-001", "SEC-001", "FIG-001", "DRF-001", "FIN-001", "ISO-001"}
        self.assertTrue(required.issubset({row.row_id for row in rows}))
        self.assertTrue(all(row.native_test for row in rows))
```

- [x] **Step 2: Run the test and verify it fails because the inventory loader or document is absent**

Run: `.venv\Scripts\python.exe -m unittest review_writer_api.tests.test_workflow_contract_inventory -v`

Expected: failure naming the missing parity document or loader.

- [x] **Step 3: Add an explicit parity table**

Use columns `ID`, `Stage`, `Current route/action`, `Inputs`, `Observable result`, `Artifacts/state`, `Native test`, and `Status`. Include every current GET/POST/PUT/DELETE handler and every user-facing control, including PDF range viewing, top-N selection, custom blank outlines, Ketcher, SVG crop/save, human warning approval, issue images, rewrite states, overview editing, and DOCX export.

- [x] **Step 4: Implement the minimal Markdown table loader in `review_writer_api/parity.py` and make missing fields fail clearly**

```python
@dataclass(frozen=True)
class ParityRow:
    row_id: str
    native_test: str
    status: str
```

- [x] **Step 5: Expand CI to run maintained check suites and all package tests**

CI commands must include:

```yaml
- run: python -m unittest discover -s view -p "*_checks.py"
- run: python -m unittest discover -s view/tests -p "test_*.py"
- run: python -m unittest discover -s review_writer_api/tests -p "test_*.py"
```

Do not add the currently stale root tests until Task 11 classifies and replaces them.

- [x] **Step 6: Verify and commit**

Run the inventory test, `git diff --check`, and YAML syntax inspection. Commit:

```text
test: freeze seven-stage workflow parity contract
```

---

### Task 2: Add explicit PostgreSQL workflow schema

**Files:**
- Create: `review_writer_api/workflow_models.py`
- Create: `review_writer_api/workflow_contracts.py`
- Create: `migrations/versions/20260813_0002_postgres_workflow.py`
- Create: `review_writer_api/tests/test_workflow_models.py`
- Modify: `review_writer_api/database.py`
- Modify: `migrations/env.py`

**Interfaces:**
- Consumes: `review_writer_api.database.Base`, existing `Project` and `User` UUIDs, legacy `STAGE_SPECS` meanings.
- Produces: `WorkflowStageState`, `WorkflowStageRun`, `WorkflowArtifact`, `WorkflowCurrentArtifact`, `WorkflowArtifactDependency`, `WorkflowJob`, `WorkflowCurrentJob`, `WorkflowApproval`, `WorkflowMigration`, and `WorkflowSystemState` models.

- [ ] **Step 1: Write failing model tests**

Create tests that construct two users with the same project slug, persist independent stage rows, reject duplicate `(project_id, stage_id)`, preserve JSON snapshots, and cascade project deletion into workflow rows. Assert the seven-stage composite mapping returns the expected user-facing stage.

- [ ] **Step 2: Run the focused test and verify missing models fail**

Run: `.venv\Scripts\python.exe -m unittest review_writer_api.tests.test_workflow_models -v`

- [ ] **Step 3: Define workflow constants and pure state functions**

```python
INTERNAL_STAGES = ("discovery", "matrix", "blueprint", "sections", "figure-review", "figures", "draft", "final")
USER_STAGES = ("library", "discovery", "planning", "sections", "images", "draft", "final")
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}
```

Implement `composite_stage(internal_stage: str) -> str` and `current_user_stage(states: Mapping[str, str]) -> str` as pure tested functions.

- [ ] **Step 4: Implement SQLAlchemy models with explicit constraints**

Use UUID primary keys for new rows, nullable project IDs only for library-scoped jobs, `JSON` fields for portable unit tests, timezone-aware timestamps, named unique constraints, and indexes for user/project/status polling. Add `legacy_id` columns where IDs must be preserved.

- [ ] **Step 5: Write an explicit Alembic upgrade and downgrade**

Use `op.create_table`, `op.create_index`, foreign keys, unique constraints, and server-safe defaults. Do not call `Base.metadata.create_all` in the new revision.

- [ ] **Step 6: Verify SQLite model tests and real PostgreSQL migration**

Run model tests against an in-memory database. Start the Compose PostgreSQL service, run `alembic upgrade head`, inspect the created tables, then run `alembic downgrade 20260811_0001` and upgrade again.

- [ ] **Step 7: Commit**

```text
feat: add PostgreSQL workflow schema
```

---

### Task 3: Implement user-scoped workflow repositories and readiness

**Files:**
- Create: `review_writer_api/workflow_repository.py`
- Create: `review_writer_api/errors.py`
- Create: `review_writer_api/tests/test_workflow_repository.py`
- Modify: `review_writer_api/app.py`

**Interfaces:**
- Consumes: Task 2 workflow models and authenticated `Principal`.
- Produces: `WorkflowRepository` methods for stage runs, state revisions, artifacts, jobs, approvals, migration ledger, and readiness.

- [ ] **Step 1: Write failing ownership, revision, and job-claim tests**

Tests must prove a second user cannot read or mutate another user's workflow, an outdated stage revision raises `WorkflowConflict`, the same idempotency key returns the existing active job, and only one transaction claims a queued job.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest review_writer_api.tests.test_workflow_repository -v`

- [ ] **Step 3: Define stable domain errors**

```python
class WorkflowError(Exception):
    code = "WORKFLOW_ERROR"
    status_code = 400
    retryable = False

class WorkflowConflict(WorkflowError):
    code = "STATE_CONFLICT"
    status_code = 409
```

Register one FastAPI exception handler that returns the approved nested `error` object.

- [ ] **Step 4: Implement repository methods with ownership in every query**

Required signatures include:

```python
def get_stage_state(self, user_id: str, project_id: str, stage_id: str) -> StageStateRecord | None
def compare_and_set_stage(self, user_id: str, project_id: str, stage_id: str, expected_revision: int, **changes) -> StageStateRecord
def create_or_get_job(self, user_id: str, project_id: str | None, scope: str, job_type: str, idempotency_key: str, payload: dict) -> JobRecord
def claim_job(self, job_id: str) -> JobRecord | None
def mark_running_jobs_interrupted(self) -> int
```

- [ ] **Step 5: Add workflow-ready startup enforcement**

If a legacy source inventory is recorded but `WorkflowSystemState(key="workflow_ready")` is not `ready`, workflow routers return `503 WORKFLOW_MIGRATION_REQUIRED`. Identity and health routes remain available.

- [ ] **Step 6: Verify and commit**

Run repository tests, API foundation tests, and `git diff --check`. Commit:

```text
feat: add isolated PostgreSQL workflow repository
```

---

### Task 4: Build the stopped, idempotent SQLite migration

**Files:**
- Create: `review_writer_api/workflow_migration.py`
- Create: `review_writer_api/migrate_workflow.py`
- Create: `review_writer_api/tests/test_workflow_migration.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes: read-only legacy schema from `view/workflow_store.py`, Task 3 repository, hosted workspace layout, and explicit local owner email.
- Produces: `MigrationInventory`, `MigrationReport`, `inventory_legacy_workflows()`, `migrate_legacy_workflows()`, and `validate_migrated_workflows()`.

- [ ] **Step 1: Write representative legacy SQLite fixtures in tests**

Create SQLite files at test runtime containing all legacy tables, two projects, stage runs and states, artifact versions and dependencies, current pointers, project and library jobs, stale states, duplicate content hashes, one available file, and one pre-existing missing file.

- [ ] **Step 2: Write failing dry-run, import, idempotency, and failure-marker tests**

Assert dry-run changes no PostgreSQL rows; successful import preserves IDs/counts/JSON/timestamps; rerun does not duplicate; a broken foreign key or ownership mapping leaves readiness unset; missing files are imported as `availability=missing` and require explicit acknowledgement.

- [ ] **Step 3: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest review_writer_api.tests.test_workflow_migration -v`

- [ ] **Step 4: Implement consistent SQLite backup and inventory**

Open sources read-only, use `sqlite3.Connection.backup()` into a timestamped backup directory, hash the backup, record table counts, and reject a local workspace without `--owner-email`.

- [ ] **Step 5: Implement transactional import and validation**

Map hosted directory user UUIDs and project slugs to PostgreSQL UUIDs. Convert absolute paths to safe relative paths. Map `_library_` jobs to `scope=library`. Preserve legacy IDs and original diagnostic paths in migration metadata. Insert the ready marker only after every report is successful and any missing-file report is explicitly accepted.

- [ ] **Step 6: Add the maintenance CLI**

Expose:

```text
review-writer-migrate-workflow inventory --workspace-root PATH --report report.json
review-writer-migrate-workflow migrate --workspace-root PATH --backup-root PATH --report report.json
review-writer-migrate-workflow validate --workspace-root PATH --report report.json
```

`migrate` requires a stopped-mode confirmation flag and refuses to run when an application heartbeat is current.

- [ ] **Step 7: Verify, document rollback, and commit**

Run migration tests twice, inspect the JSON report, and document backup, dry run, migration, validation, acknowledgement, and rollback commands. Commit:

```text
feat: migrate legacy workflow state to PostgreSQL
```

---

### Task 5: Add immutable artifact publication and secure streaming

**Files:**
- Create: `review_writer_api/artifact_service.py`
- Create: `review_writer_api/container.py`
- Create: `review_writer_api/routers/__init__.py`
- Create: `review_writer_api/routers/files.py`
- Create: `review_writer_api/tests/test_artifact_service.py`
- Create: `review_writer_api/tests/test_file_api.py`
- Modify: `review_writer_api/app.py`

**Interfaces:**
- Consumes: workflow repository, hosted workspace manager, and authenticated project ownership.
- Produces: `ArtifactService.stage_run_directory()`, `publish()`, `resolve_owned_artifact()`, and `/api/v1/artifacts/{artifact_id}/content`.

- [ ] **Step 1: Write failing publication and path-escape tests**

Assert publication creates immutable version paths, updates the current pointer only after validation, preserves the prior version, rejects `..` and external paths, and leaves a DB-invisible orphan if the simulated DB commit fails.

- [ ] **Step 2: Write failing streaming tests**

Assert unauthenticated and cross-user reads fail, `Range: bytes=0-99` returns 206 with correct headers, normal reads stream without buffering the entire file, and missing files return stable `ARTIFACT_FILE_MISSING` errors.

- [ ] **Step 3: Verify RED**

Run both focused test modules.

- [ ] **Step 4: Implement staging and publication**

Use `<project>/.staging/<run-id>` for temporary outputs and `<project>/.artifacts/<logical-name>/<artifact-id>/<filename>` for immutable publication. Require same-filesystem `Path.replace`, calculate SHA-256 and size before repository commit, and never overwrite a current artifact.

- [ ] **Step 5: Implement authenticated streaming**

Resolve only database artifact IDs after ownership checks. Use `FileResponse` for complete files and a bounded iterator for one validated byte range. Add `Accept-Ranges`, `Content-Range`, `ETag`, and safe content disposition.

Implement project deletion as a PostgreSQL soft delete followed by an atomic move into a user-scoped trash directory. Permanent recursive cleanup is a separate maintenance operation and never runs inside the delete request.

- [ ] **Step 6: Verify and commit**

Run artifact/file tests and FastAPI foundation tests. Commit:

```text
feat: publish and stream isolated workflow artifacts
```

---

### Task 6: Replace Prefect with a bounded PostgreSQL job executor

**Files:**
- Create: `review_writer_api/job_service.py`
- Create: `review_writer_api/routers/jobs.py`
- Create: `review_writer_api/scientific_runner.py`
- Create: `review_writer_api/tests/test_job_service.py`
- Create: `review_writer_api/tests/test_scientific_runner.py`
- Modify: `review_writer_api/container.py`
- Modify: `review_writer_api/app.py`

**Interfaces:**
- Consumes: workflow repository, artifact service, encrypted provider settings, and script paths.
- Produces: `JobService.submit()`, `status()`, `request_cancel()`, `retry_interrupted()`, lifecycle startup/shutdown hooks, and `ScientificRunner.run()`.

- [ ] **Step 1: Write failing executor lifecycle tests**

Assert bounded concurrency, idempotent duplicate submit, per-project/job-type conflict prevention, queued-to-running-to-succeeded state, cooperative cancellation, exception-to-failed mapping, startup interruption, and explicit retry producing a linked new job.

- [ ] **Step 2: Write failing runner security and retry tests**

Assert secrets exist only in the child environment, logs redact known keys, staging output is required, cancellation terminates the child safely, transient 429/503/timeouts retry at most three times, and validation or permission errors never retry.

- [ ] **Step 3: Verify RED**

Run both focused modules.

- [ ] **Step 4: Implement the bounded executor**

Default to two worker threads. Commit the queued job before submitting a callable. Claim through the repository, poll cancellation at safe checkpoints, persist progress, and mark uncompleted running jobs interrupted during app startup.

- [ ] **Step 5: Implement the scientific runner**

Use argument lists without `shell=True`, explicit working directories, task-scoped environment dictionaries, bounded captured diagnostics, timeout handling, and staging-output validation. Remove secrets from all retained command and error documents.

- [ ] **Step 6: Add versioned job endpoints and verify**

Expose `GET /api/v1/jobs/{job_id}`, `POST /api/v1/jobs/{job_id}/cancel`, and `POST /api/v1/jobs/{job_id}/retry`. Run tests and commit:

```text
feat: replace Prefect with persisted job execution
```

---

### Task 7: Migrate Library and Discovery to native services

**Files:**
- Create: `review_writer_api/domain_services/library.py`
- Create: `review_writer_api/domain_services/__init__.py`
- Create: `review_writer_api/domain_services/discovery.py`
- Create: `review_writer_api/routers/library.py`
- Create: `review_writer_api/routers/discovery.py`
- Create: `review_writer_api/tests/test_library_v1.py`
- Create: `review_writer_api/tests/test_discovery_v1.py`
- Create: `review_writer_api/workflow_schemas.py`
- Modify: `review_writer_api/container.py`
- Modify: `review_writer_api/app.py`
- Modify: `view/assets/dashboard/library.html`
- Modify: `view/assets/dashboard/discovery.html`

**Interfaces:**
- Consumes: artifact, job, runner, project, credential, and workflow services.
- Produces: native Library and Discovery `/api/v1` contracts and updated frontend calls.

- [ ] **Step 1: Write failing parity tests for all `LIB-*` and `DIS-*` rows**

Cover upload streaming, MinerU token errors, duplicates, local search fields, metadata, Markdown/PDF access, literature search/download jobs, topic restart staging and rollback, candidate counts, category filtering, explicit selection, top-N ranking, save, confirmation, and exact Matrix synchronization.

- [ ] **Step 2: Verify RED against missing native endpoints**

Run both focused modules and record expected 404 failures.

- [ ] **Step 3: Implement Library service and router**

Move reusable pure functions out of `serve_review_dashboard.py` or `local_pdf_ingestion.py` into focused core modules. Stream uploads to staging, publish admitted PDFs and parsed artifacts, and persist long acquisition tasks through JobService.

- [ ] **Step 4: Implement Discovery service and router**

Use one project ID/topic validator shared with project creation. Run discovery into a staging project area. Publish only successful results. Store explicit selection state and perform Matrix synchronization in one service operation with matching selected and synchronized counts.

- [ ] **Step 5: Switch the two frontend pages to `/api/v1`**

Use stable error codes and artifact IDs. Preserve current button positions, counts, top-N behavior, restart warnings, and Chinese/English translation.

- [ ] **Step 6: Mark parity rows and verify**

Run focused tests, existing Library/Discovery checks, API isolation tests, and a browser smoke test. Mark only proven rows `passed`. Commit:

```text
feat: migrate library and discovery to native FastAPI
```

---

### Task 8: Migrate Planning and Sections

**Files:**
- Create: `review_writer_api/domain_services/planning.py`
- Create: `review_writer_api/domain_services/sections.py`
- Create: `review_writer_api/routers/planning.py`
- Create: `review_writer_api/routers/sections.py`
- Create: `review_writer_api/tests/test_planning_v1.py`
- Create: `review_writer_api/tests/test_sections_v1.py`
- Modify: `view/assets/dashboard/matrix.html`
- Modify: `view/assets/dashboard/blueprint.html`
- Modify: `view/assets/dashboard/sections.html`

**Interfaces:**
- Consumes: confirmed Discovery selection, job service, runner, artifacts, and stage transitions.
- Produces: native Matrix, Blueprint, custom outline, section task, section drafting, edit, and handoff APIs.

- [ ] **Step 1: Write failing `PLN-*` and `SEC-*` parity tests**

Cover unlimited selected literature, exact refresh after reconfirmation, Matrix row edits, three templates plus blank custom outline, manual outline changes, candidate comparison, reference outline upload, missing-paper gate, section progress, reports, provider 503 handling, and bilingual labels.

- [ ] **Step 2: Verify RED**

Run both focused modules.

- [ ] **Step 3: Implement Planning native domain**

Publish Matrix and outline edits as new artifact versions. Use optimistic revisions for manual saves. Keep the custom template blank until the user adds content. Validate Blueprint paper IDs against the current selected Matrix.

- [ ] **Step 4: Implement Sections native domain**

Submit section generation as PostgreSQL jobs. Preserve section-level progress and retry states. Read current Blueprint artifacts through the repository rather than raw guessed paths.

- [ ] **Step 5: Switch frontend calls, verify gates, and commit**

Run focused tests, Matrix/Blueprint/section checks, browser tabs, and parity inventory validation. Commit:

```text
feat: migrate planning and sections to native FastAPI
```

---

### Task 9: Migrate Figure Review, redraw, and manual editing

**Files:**
- Create: `review_writer_api/domain_services/figures.py`
- Create: `review_writer_api/routers/figures.py`
- Create: `review_writer_api/tests/test_figures_v1.py`
- Create: `review_writer_api/tests/test_figure_editors_v1.py`
- Modify: `view/assets/dashboard/figure-review.html`
- Modify: `view/assets/dashboard/figures.html`
- Modify: `view/assets/dashboard/review-i18n.js`

**Interfaces:**
- Consumes: current Sections artifacts, artifact publisher, job service, runner, and approval repository.
- Produces: native candidate review, redraw, editor, approval, progress, and Figure-to-Draft gate APIs.

- [ ] **Step 1: Write failing `FIG-*` parity tests**

Cover default source candidates, image-backed tables, paragraph anchors, individual/batch redraw, stop/retry, aspect-ratio validation, adult-content warnings, chemical integrity warnings, human approval, approve-all-successful, SVG full-editor load/save/crop, Ketcher import/save, immutable revisions, and confirmation blocking until every selected figure is usable.

- [ ] **Step 2: Verify RED**

Run the figure modules.

- [ ] **Step 3: Extract figure rules from the old handler into focused pure modules**

Move validation, anchor matching, aspect-ratio, safety, approval, and gate rules without changing their observable results. Leave HTTP parsing behind.

- [ ] **Step 4: Implement native Figures service and router**

Use jobs for AI redraw, artifacts for every generated or edited version, and append-only approvals. Edited canvas dimensions become the new artifact's valid dimensions; source-dimension mismatch remains a warning that a human can explicitly approve when integrity checks permit it.

- [ ] **Step 5: Switch image frontend calls and verify**

Preserve current merged Image Processing tabs, button layout, editor controls, bilingual text, and progress status. Run all figure, SVG, Ketcher, gate, and browser tests. Commit:

```text
feat: migrate figure workflow and editors to native FastAPI
```

---

### Task 10: Migrate Draft quality, editing, Final, and export

**Files:**
- Create: `review_writer_api/domain_services/drafts.py`
- Create: `review_writer_api/domain_services/final.py`
- Create: `review_writer_api/routers/drafts.py`
- Create: `review_writer_api/routers/final.py`
- Create: `review_writer_api/tests/test_drafts_v1.py`
- Create: `review_writer_api/tests/test_final_v1.py`
- Modify: `view/assets/dashboard/draft.html`
- Modify: `view/assets/dashboard/final.html`
- Modify: `view/assets/dashboard/review-i18n.js`

**Interfaces:**
- Consumes: approved current Figures artifacts, job service, artifact publisher, paragraph marker core, evaluation scripts, overview generator, and DOCX exporter.
- Produces: native Draft and Final editing, evaluation, rewrite, approval, generation, report, and export APIs.

- [ ] **Step 1: Write failing `DRF-*` and `FIN-*` parity tests**

Cover draft assembly, paragraph IDs, paragraph and full text versioning, live score, issue expansion with matching images, rewrite running/completed/failed states, no-op rewrite rejection, accept/reject/undo audit events, prevention of later regression, Draft approval, final merge, overview generation and editable text, conclusion, reports, validation, Markdown, and DOCX export.

- [ ] **Step 2: Verify RED**

Run both modules.

- [ ] **Step 3: Implement Draft service and router**

Publish each edit and accepted rewrite as an immutable draft artifact. Compute quality results from the current artifact revision. Store issue and rewrite status in PostgreSQL. Refuse acceptance when normalized text is unchanged, and record manual decisions.

- [ ] **Step 4: Implement Final service and router**

Use current artifact IDs rather than guessed paths. Publish overview and conclusion versions, validate all referenced figures and sources, and stream DOCX as a registered artifact.

- [ ] **Step 5: Switch frontend calls and verify**

Preserve live score, expanded content/images, segment rewrite status, manual editing, initial-quality landing route, final views, and bilingual behavior. Run draft/final/export/browser checks. Commit:

```text
feat: migrate draft and final workflow to native FastAPI
```

---

### Task 11: Complete API cutover and remove runtime compatibility

**Files:**
- Modify: `review_writer_api/app.py`
- Modify: `review_writer_api/web/app.js`
- Modify: all files under `view/assets/dashboard/` that still reference unversioned APIs
- Delete: `review_writer_api/workflow_compat.py`
- Delete: `review_writer_api/dashboard_executor.py`
- Delete or reduce to non-runtime script helpers: `view/serve_review_dashboard.py`
- Delete: `view/prefect_runtime.py`
- Delete: `view/prefect_flows.py`
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `review_writer_api/auth.py`
- Modify: `review_writer_api/credentials.py`
- Modify: `.github/workflows/api-foundation.yml`
- Modify: stale tests under `tests/`
- Create: `review_writer_api/tests/test_no_legacy_runtime.py`

**Interfaces:**
- Consumes: all native routers and passed parity rows.
- Produces: a runtime with no SQLite, Prefect, compatibility gateway, or old handler dependency.

- [ ] **Step 1: Write failing legacy-runtime tests**

Assert normal app startup and a complete seven-stage API smoke flow do not import `view.serve_review_dashboard`, `view.workflow_store`, `view.prefect_runtime`, or `review_writer_api.workflow_compat`; no `workflow.sqlite3` or Prefect directory appears; and every frontend fetch targets `/api/v1`.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest review_writer_api.tests.test_no_legacy_runtime -v`

- [ ] **Step 3: Remove compatibility route registration and legacy dependencies**

Mount native dashboard pages and assets directly through FastAPI. Remove `prefect` from requirements. Remove SQLite workflow imports. Retain only reusable scientific helpers that have explicit non-HTTP modules; delete dead HTTP dispatch and global thread registries.

Add lightweight single-instance login and registration throttling. Parse provider URLs structurally, reject embedded credentials, reject non-permitted plain HTTP, and block loopback/private destinations in public mode unless the trusted-LAN setting explicitly enables them.

- [ ] **Step 4: Classify and repair stale root tests**

Replace assertions for intentionally removed functions with approved native behavior tests. Keep paragraph marker formatting tests aligned to one canonical representation. Run all root tests and include them in CI only after every maintained test passes.

- [ ] **Step 5: Run full static and test verification**

Run Python compile, Node syntax, `git diff --check`, every unittest directory, and real PostgreSQL migration/integration tests. Verify `rg` finds no production import of removed modules and no frontend unversioned workflow fetch.

- [ ] **Step 6: Commit**

```text
refactor: complete native FastAPI workflow cutover
```

---

### Task 12: Real-data migration rehearsal, deployment, and launch documentation

**Files:**
- Modify: `compose.yaml`
- Modify: `Dockerfile.api`
- Modify: `.env.hosted.example`
- Modify: `README.md`
- Modify: `review_writer_core/CONFIGURATION.md`
- Create: `docs/postgresql-workflow-migration.md`
- Create: `review_writer_api/tests/test_container_smoke.py`
- Update: `docs/workflow-feature-parity.md`

**Interfaces:**
- Consumes: completed native application, legacy backup set, migration CLI, and Docker Compose.
- Produces: verified migration/restore/runbook, green parity inventory, and launchable `dy-launch` branch.

- [ ] **Step 1: Write failing configuration and container smoke tests**

Assert Compose contains PostgreSQL, migrate, and API services but no Prefect; the API requires PostgreSQL workflow readiness; health, registration, login, project creation, and one workflow request succeed in a built container.

- [ ] **Step 2: Verify RED and implement deployment updates**

Remove Prefect environment and volume assumptions. Add migration report and backup mounts or documented host paths. Keep PostgreSQL bound to loopback by default and API bind configurable for trusted LAN use.

- [ ] **Step 3: Run a migration rehearsal on copies of real workspace data**

Inventory and back up every source, perform a dry run, migrate into a disposable PostgreSQL database, compare counts and hashes, inspect missing-file acknowledgement, and delete only the disposable destination after saving the report.

- [ ] **Step 4: Perform the stopped final migration**

Stop the API, make final SQLite and PostgreSQL backups, run migration and validation, write readiness only on success, start the native API, and retain SQLite copies as read-only backups.

- [ ] **Step 5: Complete end-to-end acceptance**

Using two accounts, exercise all seven stages in Chinese and English, including PDF viewing from another LAN device, candidate selection, custom outline, section generation, figure review/redraw/manual edit, Draft score/issues/rewrite/manual edit, overview generation, Final, and DOCX export.

- [ ] **Step 6: Verify the removal and rollback gates**

Confirm no runtime SQLite/Prefect files are created, all parity rows are `passed`, the rollback procedure points to `9eea953` plus database/workspace backups, and `dy` still equals `f397e3e`.

- [ ] **Step 7: Run final verification, commit, and push**

Run the complete CI-equivalent suite, `docker compose config --quiet`, container health smoke, migration validation, `git diff --check`, and a clean status check. Commit:

```text
docs: finalize PostgreSQL workflow launch runbook
```

Push `dy-launch` without force and verify the remote head. Do not push or merge into `dy`.
