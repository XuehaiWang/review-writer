# Repository Cleanup Design

## Objective

Remove reproducible local artifacts and proven-dead legacy code without deleting user content, PostgreSQL data, formal migration evidence, or scientific workflow capabilities.

## Scope

The cleanup removes four classes of content:

1. Reproducible caches: `.pytest_cache/` and every `__pycache__/` directory in the active worktree.
2. Completed acceptance artifacts: `migration-rehearsal-20260814/`, `migration-rehearsal-clean-20260814/`, `migration-reports-smoke/`, `migration-backups-smoke/`, and the local `.review-writer/prefect-codexsandboxoffline/` test state.
3. Proven-dead runtime code: `view/workflow_store.py`, the former SQLite workflow runtime, and `view/provider_settings.py`, the former file-backed provider-settings runtime.
4. Superseded documentation: the root `集成说明.txt`, whose branch-specific compatibility instructions are replaced by `README.md` and `docs/postgresql-workflow-migration.md`.

The cleanup explicitly preserves:

- `migration-backups/` and `migration-reports/`;
- every hosted user workspace, uploaded PDF, MinerU output, project artifact, and trash record;
- the original migrated `workflow.sqlite3` retained as rollback evidence;
- PostgreSQL and Docker data volumes;
- all scientific scripts, Ketcher assets, dashboard assets, example/reference inputs, and current FastAPI services;
- the `dy` branch.

## Code Changes

`review_writer_api/tests/test_workflow_migration.py` will create the minimum legacy SQLite schema it needs inside the test fixture instead of importing `view.workflow_store.WorkflowStore`. This keeps migration coverage while removing the obsolete application runtime.

`view/local_pdf_ingestion.py` will pass a copy of its existing task-scoped process environment to the MinerU subprocess. In hosted mode, `ScientificRunner` already constructs that environment from the authenticated user's encrypted PostgreSQL provider settings. The old `.review-writer/provider-settings.json`, workspace `.env`, module alias, and in-process registry behavior will not remain available.

The no-legacy-runtime checks will list both deleted modules as forbidden runtime files/imports. A focused ingestion test will verify that MinerU receives the already injected environment without importing the deleted settings module.

## Deletion Safety

Before recursive deletion, every target will be resolved and checked to be a direct child or descendant of the active worktree. Formal backup/report directories and hosted workspace paths are excluded by exact name. No wildcard-derived target will be passed to another shell.

The two rehearsal directories are disposable copies from the completed migration exercise. Their deletion does not alter the formal PostgreSQL database or the verified formal backup and migration report.

## Verification

Verification consists of:

1. Focused migration, ingestion, and repository-hygiene tests.
2. The complete `review_writer_api` test suite.
3. Maintained `view/*_checks.py`, `view/tests`, and root `tests` suites.
4. Python compilation, JavaScript syntax checks, and `git diff --check`.
5. Real PostgreSQL conditional tests against a disposable database.
6. A rebuilt formal container, health check, and confirmation that no Prefect process or newly written SQLite workflow database exists.
7. A final deletion manifest showing removed paths and reclaimed bytes.

The cleanup is successful only if all verification passes and the formal API remains healthy at the configured LAN address.
