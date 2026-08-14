# Repository Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove reproducible local artifacts and proven-dead SQLite/provider-settings code while preserving formal migration evidence, user data, and every hosted scientific workflow.

**Architecture:** The hosted FastAPI process remains the only application runtime. MinerU subprocesses inherit the authenticated task environment built by `ScientificRunner`; migration tests build a minimal legacy SQLite fixture directly, so neither removed `view` module remains an application dependency.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, SQLite fixture generation, PostgreSQL 17, Docker Compose, PowerShell, unittest.

## Global Constraints

- Preserve `migration-backups/`, `migration-reports/`, hosted workspaces, uploaded PDFs, MinerU outputs, project artifacts, the original migrated `workflow.sqlite3`, and all Docker volumes.
- Preserve all scientific scripts, dashboard/Ketcher assets, examples, and current FastAPI services.
- Never modify or push `dy`.
- Resolve and validate every recursive-deletion target under the active worktree before deletion.
- Do not use wildcard-derived recursive deletion targets.
- Use test-first changes for both removed Python modules.

---

### Task 1: Retire File-Backed Provider Settings

**Files:**
- Modify: `view/local_pdf_ingestion_checks.py`
- Modify: `review_writer_api/tests/test_no_legacy_runtime.py`
- Modify: `view/local_pdf_ingestion.py`
- Delete: `view/provider_settings.py`

**Interfaces:**
- Consumes: task-scoped provider environment already installed in `os.environ` by `ScientificRunner.run(..., secret_env=...)`.
- Produces: `_run_mineru_parser(review_root: Path, pdf_path: Path, slug: str) -> dict[str, Any]` that passes `dict(os.environ)` to the nested MinerU process and never reads workspace provider JSON or `.env` files.

- [ ] **Step 1: Add failing retirement and environment-forwarding checks**

Add `ROOT / "view" / "provider_settings.py"` to `REMOVED_RUNTIME` and `"view.provider_settings"` to `LEGACY_IMPORTS` in `review_writer_api/tests/test_no_legacy_runtime.py`.

Add a `check_mineru_inherits_task_scoped_environment()` check to `view/local_pdf_ingestion_checks.py`. It must set a sentinel `MINERU_API_TOKEN`, write a conflicting token to `<review-root>/.env`, replace `ingestion.subprocess.run` with a fake that records `env`, creates `markdown`, `full.md`, `content_list.json`, and manifest files under `ingestion._mineru_artifact_paths(...)`, then assert the captured environment contains the task sentinel rather than the workspace value. The check must restore the environment and original runner in `finally`.

- [ ] **Step 2: Run focused checks and verify RED**

Run:

```powershell
python -m unittest review_writer_api.tests.test_no_legacy_runtime -v
python view/local_pdf_ingestion_checks.py
```

Expected: the no-legacy test fails because `view/provider_settings.py` still exists; the ingestion check demonstrates the current path still imports the old module.

- [ ] **Step 3: Use only the task environment and delete the old module**

In `view/local_pdf_ingestion.py`, delete `_load_dotenv_if_present`, remove its call from `_run_mineru_parser`, remove the `from provider_settings import provider_subprocess_environment` import, and replace:

```python
env=provider_subprocess_environment(root),
```

with:

```python
env=dict(os.environ),
```

Delete `view/provider_settings.py` with `apply_patch`.

- [ ] **Step 4: Run focused checks and verify GREEN**

Run the two commands from Step 2. Expected: all tests/checks pass and importing the hosted app does not load or recreate provider settings files.

- [ ] **Step 5: Commit Task 1**

```powershell
git add view/local_pdf_ingestion.py view/local_pdf_ingestion_checks.py view/provider_settings.py review_writer_api/tests/test_no_legacy_runtime.py
git commit -m "refactor: remove file-backed provider settings runtime"
```

---

### Task 2: Retire the SQLite Workflow Runtime

**Files:**
- Modify: `review_writer_api/tests/test_workflow_migration.py`
- Modify: `review_writer_api/tests/test_no_legacy_runtime.py`
- Modify: `view/repository_hygiene_checks.py`
- Delete: `view/workflow_store.py`

**Interfaces:**
- Consumes: `sqlite3.connect(database_path)` and the legacy table contract read by `review_writer_api.workflow_migration`.
- Produces: `create_legacy_schema(database_path: Path) -> None` inside the migration test, containing only `projects`, `stage_runs`, `stage_state`, `artifact_versions`, `current_artifacts`, `artifact_dependencies`, `jobs`, and `current_jobs` with the same columns and foreign keys used by the fixture.

- [ ] **Step 1: Add failing runtime-absence checks**

Add `ROOT / "view" / "workflow_store.py"` to `REMOVED_RUNTIME` in `test_no_legacy_runtime.py`, and add both `view/workflow_store.py` and `view/provider_settings.py` to the forbidden file list in `view/repository_hygiene_checks.py`.

- [ ] **Step 2: Run the hygiene tests and verify RED**

Run:

```powershell
python -m unittest review_writer_api.tests.test_no_legacy_runtime -v
python -m unittest view.repository_hygiene_checks -v
```

Expected: failure identifies the still-present `view/workflow_store.py`.

- [ ] **Step 3: Make the migration fixture self-contained**

Remove:

```python
from view.workflow_store import WorkflowStore
```

Add a `create_legacy_schema(database_path: Path) -> None` helper using `sqlite3.connect`, `PRAGMA foreign_keys=ON`, and `executescript`. Define the eight tables listed in Interfaces with the exact column names inserted later by `create_legacy_fixture`; include primary keys and the artifact/job foreign keys required by the migrator.

At the start of `create_legacy_fixture`, replace `WorkflowStore(review_root)` with:

```python
database_path = review_root / ".review-writer" / "workflow.sqlite3"
database_path.parent.mkdir(parents=True, exist_ok=True)
create_legacy_schema(database_path)
```

Delete `view/workflow_store.py` using `apply_patch`.

- [ ] **Step 4: Run migration and hygiene checks and verify GREEN**

Run:

```powershell
python -m unittest review_writer_api.tests.test_workflow_migration review_writer_api.tests.test_no_legacy_runtime -v
python -m unittest view.repository_hygiene_checks -v
```

Expected: migration fixtures import without `view.workflow_store`, exercise the same legacy rows, and all removal checks pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add review_writer_api/tests/test_workflow_migration.py review_writer_api/tests/test_no_legacy_runtime.py view/repository_hygiene_checks.py view/workflow_store.py
git commit -m "refactor: remove retired SQLite workflow runtime"
```

---

### Task 3: Remove Superseded Documentation and Generated Artifacts

**Files:**
- Delete: `集成说明.txt`
- Delete local generated directories listed below; they are ignored and are not committed.

**Interfaces:**
- Consumes: the exact approved deletion list and active worktree root.
- Produces: a clean worktree containing no test cache, smoke state, Prefect test state, or rehearsal copies while retaining formal migration evidence.

- [ ] **Step 1: Record and validate deletion targets**

Resolve the active worktree with `git rev-parse --show-toplevel`. For each target, use `[IO.Path]::GetFullPath(...)`, require its parent chain to remain under that root, and reject `migration-backups`, `migration-reports`, `.review-writer/hosted-workspaces`, or any path outside the worktree.

Exact generated targets:

```text
.pytest_cache
.review-writer/prefect-codexsandboxoffline
migration-rehearsal-20260814
migration-rehearsal-clean-20260814
migration-reports-smoke
migration-backups-smoke
```

Enumerate every `__pycache__` directory separately, validate each resolved path under the worktree, and record total file count and byte size before deletion.

- [ ] **Step 2: Delete the obsolete tracked document**

Delete `集成说明.txt` with `apply_patch`. Confirm its current content is covered by `README.md`, `docs/workflow-feature-parity.md`, and `docs/postgresql-workflow-migration.md`.

- [ ] **Step 3: Delete only validated generated targets**

Use native PowerShell `Remove-Item -LiteralPath <validated-absolute-path> -Recurse -Force` for each exact target. Do not pass the targets to another shell and do not delete formal backup/report directories.

- [ ] **Step 4: Verify preserved and removed paths**

Assert all approved targets and every `__pycache__` are absent. Assert these paths remain:

```text
migration-backups
migration-reports
docs/postgresql-workflow-migration.md
examples/reference-reviews
skills
view/assets/dashboard
```

Inside the formal API container, verify the original `workflow.sqlite3` SHA-256 remains `7fc2b85ef459a2f9dfa0f8cd515e10cd54d0f0ee6e632bb222ff928474089f23`.

- [ ] **Step 5: Commit Task 3**

```powershell
git add 集成说明.txt
git commit -m "chore: remove superseded integration notes"
```

---

### Task 4: Full Regression and Deployment Verification

**Files:**
- No planned source changes. If a regression appears, stop and diagnose it before modifying code.

**Interfaces:**
- Consumes: the cleaned source tree and existing `.env.hosted` deployment configuration.
- Produces: verified commits, a healthy formal API, and an exact cleanup report.

- [ ] **Step 1: Run complete host test suites**

Run:

```powershell
python -m unittest discover -s review_writer_api/tests -p "test_*.py" -q
Push-Location view
python -m unittest discover -s . -p "*_checks.py" -q
Pop-Location
python -m unittest discover -s view/tests -p "test_*.py" -q
python -m unittest discover -s tests -p "test_*.py" -q
```

Expected: zero failures.

- [ ] **Step 2: Run real PostgreSQL conditional tests**

Create a disposable database in an isolated PostgreSQL container, run Alembic and the conditional migration/repository/Library test classes with `REVIEW_WRITER_RUN_POSTGRES_TESTS=1`, and drop the disposable database in `finally`.

Expected: all 15 PostgreSQL tests pass and the temporary database is absent afterward.

- [ ] **Step 3: Run static verification**

Run `python -m compileall -q review_writer_api review_writer_core view`, `node --check` for every tracked dashboard JavaScript file, and `git diff --check`.

Expected: every command exits zero. Remove any newly generated `__pycache__` directories after testing, then confirm `git status --short` contains only intentional commits/changes.

- [ ] **Step 4: Rebuild and restart formal Compose deployment**

Run:

```powershell
docker compose --env-file D:\work\old1\review-writer-main\.env.hosted up -d --build
```

Expected: `postgres`, `migrate`, and `api` are the only configured services; migration reports `already_migrated`/`ready: true`; API becomes healthy at `http://192.168.0.5:8770/api/v1/health`.

- [ ] **Step 5: Confirm no legacy runtime returned**

Use `docker top review-writer-api-1` to confirm there is no Prefect process. Find `workflow.sqlite3` files and verify the retained original hash is unchanged. Confirm the API container has neither `/app/view/workflow_store.py` nor `/app/view/provider_settings.py`.

- [ ] **Step 6: Report completion**

Report removed paths, reclaimed bytes, commits, test counts, formal health status, preserved backup paths, and that no remote branch was pushed unless separately authorized.
