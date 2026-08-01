"""Durable workflow state, artifact versions, and dependency tracking.

The review application deliberately keeps scientific artifacts as normal files
so they remain inspectable and portable.  This module stores only orchestration
metadata in SQLite: stage/job state, immutable file versions, and the lineage
between those versions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
WORKFLOW_DIRECTORY = ".review-writer"
WORKFLOW_DATABASE = "workflow.sqlite3"


STAGE_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "library": {
        "depends_on": (),
        "optional_depends_on": (),
        "inputs": (),
        "outputs": (),
    },
    "discovery": {
        "depends_on": ("library",),
        "optional_depends_on": (),
        "inputs": (),
        "outputs": (
            "00_discovery/query_plan.draft.json",
            "00_discovery/selected_discovery_results.json",
            "00_discovery/human_check_state.json",
        ),
    },
    "matrix": {
        "depends_on": ("discovery",),
        "optional_depends_on": (),
        "inputs": ("00_discovery/selected_discovery_results.json",),
        "outputs": (
            "01_matrix_outline/literature_matrix.json",
            "01_matrix_outline/paper_reading_notes.json",
        ),
    },
    "blueprint": {
        "depends_on": ("matrix",),
        "optional_depends_on": (),
        "inputs": (
            "01_matrix_outline/literature_matrix.json",
            "01_matrix_outline/selected_outline.md",
        ),
        "outputs": (
            "01_matrix_outline/section_blueprint.json",
            "02_section_drafting/section_tasks.json",
        ),
    },
    "sections": {
        "depends_on": ("blueprint",),
        "optional_depends_on": (),
        "inputs": (
            "01_matrix_outline/section_blueprint.json",
            "02_section_drafting/section_tasks.json",
        ),
        "outputs": (
            "02_section_drafting/section_drafts.json",
            "02_section_drafting/section_drafts.md",
            "02_section_drafting/figure_candidates.json",
            "02_section_drafting/paper_figure_candidates.json",
        ),
    },
    "figure-review": {
        "depends_on": ("sections",),
        "optional_depends_on": (),
        "inputs": (
            "02_section_drafting/figure_candidates.json",
            "02_section_drafting/paper_figure_candidates.json",
        ),
        "outputs": ("02_section_drafting/human_figure_review.json",),
    },
    "figures": {
        "depends_on": ("figure-review",),
        "optional_depends_on": (),
        "inputs": (
            "02_section_drafting/section_drafts.json",
            "02_section_drafting/figure_candidates.json",
            "02_section_drafting/paper_figure_candidates.json",
            "02_section_drafting/human_figure_review.json",
        ),
        "outputs": (
            "03_figure_redraw/redrawn_figure_manifest.json",
            "03_figure_redraw/redrawn/*.png",
            "03_figure_redraw/manual_arrow_edits/*.svg",
        ),
    },
    "draft": {
        "depends_on": ("sections", "figures"),
        "optional_depends_on": (),
        "inputs": (
            "02_section_drafting/section_drafts.json",
            "02_section_drafting/human_figure_review.json",
            "03_figure_redraw/redrawn_figure_manifest.json",
        ),
        "outputs": (
            "04_first_draft/first_draft.md",
            "04_first_draft/citations.json",
            "04_first_draft/figures/*",
        ),
    },
    "final-conclusion": {
        "depends_on": ("draft",),
        "optional_depends_on": (),
        "inputs": ("04_first_draft/first_draft.md",),
        "outputs": (
            "04_first_draft/conclusion_generated.md",
            "04_first_draft/conclusion_quality_report.json",
        ),
    },
    "final-overview-figure": {
        "depends_on": ("blueprint",),
        "optional_depends_on": ("draft",),
        "inputs": (
            "00_discovery/query_plan.draft.json",
            "00_discovery/selected_discovery_results.json",
            "01_matrix_outline/selected_outline.md",
            "04_first_draft/first_draft.md",
        ),
        "outputs": ("05_final_audit/overview_figure.png",),
    },
    "final": {
        "depends_on": ("draft",),
        "optional_depends_on": ("final-conclusion", "final-overview-figure"),
        "inputs": (
            "04_first_draft/first_draft.md",
            "04_first_draft/citations.json",
            "04_first_draft/conclusion_generated.md",
            "05_final_audit/overview_figure.png",
        ),
        "outputs": (
            "05_final_audit/final_draft.md",
            "05_final_audit/figures/*",
            "05_final_audit/review_summary_chart.png",
            "05_final_audit/final_draft*.docx",
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(records: Sequence[Mapping[str, Any]]) -> str:
    stable = [
        {
            "logical_name": str(item.get("logical_name") or ""),
            "exists": bool(item.get("exists")),
            "sha256": str(item.get("sha256") or ""),
        }
        for item in records
    ]
    return hashlib.sha256(_json(stable).encode("utf-8")).hexdigest()


class WorkflowStore:
    """SQLite-backed metadata store for one review-writer workspace."""

    def __init__(self, review_root: Path):
        self.review_root = Path(review_root).resolve()
        self.database_path = self.review_root / WORKFLOW_DIRECTORY / WORKFLOW_DATABASE
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_dependencies (
                    stage_id TEXT NOT NULL,
                    depends_on_stage_id TEXT NOT NULL,
                    dependency_kind TEXT NOT NULL,
                    PRIMARY KEY(stage_id, depends_on_stage_id)
                );
                CREATE TABLE IF NOT EXISTS stage_runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    stage_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    input_fingerprint TEXT NOT NULL DEFAULT '',
                    input_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    output_fingerprint TEXT NOT NULL DEFAULT '',
                    output_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS stage_runs_project_stage
                    ON stage_runs(project_id, stage_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS stage_state (
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    stage_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_run_id TEXT,
                    input_fingerprint TEXT NOT NULL DEFAULT '',
                    output_fingerprint TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, stage_id)
                );
                CREATE TABLE IF NOT EXISTS artifact_versions (
                    artifact_version_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    logical_name TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    producer_stage TEXT NOT NULL DEFAULT '',
                    producer_run_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, logical_name, content_sha256)
                );
                CREATE INDEX IF NOT EXISTS artifact_versions_project_name
                    ON artifact_versions(project_id, logical_name, created_at DESC);
                CREATE TABLE IF NOT EXISTS current_artifacts (
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    logical_name TEXT NOT NULL,
                    artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, logical_name)
                );
                CREATE TABLE IF NOT EXISTS artifact_dependencies (
                    output_artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                    input_artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(artifact_version_id) ON DELETE CASCADE,
                    dependency_role TEXT NOT NULL DEFAULT 'input',
                    PRIMARY KEY(output_artifact_version_id, input_artifact_version_id, dependency_role)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_project_type
                    ON jobs(project_id, job_type, updated_at DESC);
                CREATE TABLE IF NOT EXISTS current_jobs (
                    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
                    job_type TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, job_type)
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_info(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            for stage_id, spec in STAGE_SPECS.items():
                for dependency in spec["depends_on"]:
                    connection.execute(
                        "INSERT OR REPLACE INTO stage_dependencies VALUES(?, ?, 'required')",
                        (stage_id, dependency),
                    )
                for dependency in spec["optional_depends_on"]:
                    connection.execute(
                        "INSERT OR REPLACE INTO stage_dependencies VALUES(?, ?, 'optional')",
                        (stage_id, dependency),
                    )

    def project_root(self, project_id: str) -> Path:
        return (self.review_root / "review-projects" / project_id).resolve()

    def ensure_project(self, project_id: str) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO projects(project_id, root_path, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET root_path=excluded.root_path, updated_at=excluded.updated_at
                """,
                (project_id, str(self.project_root(project_id)), now, now),
            )

    def logical_name(self, project_id: str, path: Path) -> str:
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.project_root(project_id)).as_posix()
        except ValueError:
            return resolved.as_posix()

    def _artifact_type(self, path: Path) -> str:
        suffix = path.suffix.lower().lstrip(".")
        return suffix or "file"

    def register_artifact(
        self,
        project_id: str,
        path: Path,
        *,
        producer_stage: str = "",
        producer_run_id: str | None = None,
        dependencies: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        path = Path(path).resolve()
        if not path.is_file():
            return None
        self.ensure_project(project_id)
        stat = path.stat()
        digest = sha256_file(path)
        logical_name = self.logical_name(project_id, path)
        version_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_versions(
                    artifact_version_id, project_id, logical_name, artifact_type, path,
                    content_sha256, size_bytes, mtime_ns, producer_stage, producer_run_id,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    logical_name,
                    self._artifact_type(path),
                    str(path),
                    digest,
                    stat.st_size,
                    stat.st_mtime_ns,
                    producer_stage,
                    producer_run_id,
                    _json(dict(metadata or {})),
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM artifact_versions
                WHERE project_id=? AND logical_name=? AND content_sha256=?
                """,
                (project_id, logical_name, digest),
            ).fetchone()
            assert row is not None
            version_id = str(row["artifact_version_id"])
            connection.execute(
                """
                INSERT INTO current_artifacts(project_id, logical_name, artifact_version_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(project_id, logical_name) DO UPDATE SET
                    artifact_version_id=excluded.artifact_version_id,
                    updated_at=excluded.updated_at
                """,
                (project_id, logical_name, version_id, now),
            )
            for dependency_id in dict.fromkeys(str(item) for item in dependencies if item):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO artifact_dependencies(
                        output_artifact_version_id, input_artifact_version_id, dependency_role
                    ) VALUES(?, ?, 'input')
                    """,
                    (version_id, dependency_id),
                )
            return dict(row)

    def capture_paths(
        self,
        project_id: str,
        paths: Sequence[Path],
        *,
        producer_stage: str = "",
        producer_run_id: str | None = None,
        dependencies: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in paths:
            resolved = Path(path).resolve()
            row = self.register_artifact(
                project_id,
                resolved,
                producer_stage=producer_stage,
                producer_run_id=producer_run_id,
                dependencies=dependencies,
            )
            records.append(
                {
                    "logical_name": self.logical_name(project_id, resolved),
                    "path": str(resolved),
                    "exists": bool(row),
                    "artifact_version_id": str(row["artifact_version_id"]) if row else None,
                    "sha256": str(row["content_sha256"]) if row else None,
                    "size_bytes": int(row["size_bytes"]) if row else 0,
                }
            )
        return records

    def expand_stage_paths(self, project_id: str, patterns: Sequence[str]) -> list[Path]:
        root = self.project_root(project_id)
        paths: list[Path] = []
        for pattern in patterns:
            if any(token in pattern for token in ("*", "?", "[")):
                paths.extend(sorted(path for path in root.glob(pattern) if path.is_file()))
            else:
                paths.append(root / pattern)
        return list(dict.fromkeys(path.resolve() for path in paths))

    def stage_input_snapshot(self, project_id: str, stage_id: str) -> list[dict[str, Any]]:
        spec = STAGE_SPECS.get(stage_id, {})
        return self.capture_paths(project_id, self.expand_stage_paths(project_id, spec.get("inputs", ())))

    def start_stage_run(
        self,
        project_id: str,
        stage_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        self.ensure_project(project_id)
        run_id = str(uuid.uuid4())
        snapshot = self.stage_input_snapshot(project_id, stage_id)
        fingerprint = _fingerprint(snapshot)
        now = utc_now()
        with self._lock, self._connection() as connection:
            attempt_row = connection.execute(
                "SELECT COUNT(*) AS count FROM stage_runs WHERE project_id=? AND stage_id=?",
                (project_id, stage_id),
            ).fetchone()
            attempt = int(attempt_row["count"] if attempt_row else 0) + 1
            connection.execute(
                """
                INSERT INTO stage_runs(
                    run_id, project_id, stage_id, status, attempt, input_fingerprint,
                    input_snapshot_json, metadata_json, started_at, updated_at
                ) VALUES(?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (run_id, project_id, stage_id, attempt, fingerprint, _json(snapshot), _json(dict(metadata or {})), now, now),
            )
            connection.execute(
                """
                INSERT INTO stage_state(project_id, stage_id, status, current_run_id, input_fingerprint, updated_at)
                VALUES(?, ?, 'running', ?, ?, ?)
                ON CONFLICT(project_id, stage_id) DO UPDATE SET
                    status='running', current_run_id=excluded.current_run_id,
                    input_fingerprint=excluded.input_fingerprint, error_message='',
                    updated_at=excluded.updated_at
                """,
                (project_id, stage_id, run_id, fingerprint, now),
            )
        return run_id

    def finish_stage_run(
        self,
        run_id: str,
        status: str,
        *,
        error_message: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            run = connection.execute("SELECT * FROM stage_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return
        project_id = str(run["project_id"])
        stage_id = str(run["stage_id"])
        input_snapshot = json.loads(str(run["input_snapshot_json"]) or "[]")
        dependency_ids = [
            str(item["artifact_version_id"])
            for item in input_snapshot
            if isinstance(item, dict) and item.get("artifact_version_id")
        ]
        output_snapshot: list[dict[str, Any]] = []
        output_fingerprint = ""
        if status == "completed":
            patterns = STAGE_SPECS.get(stage_id, {}).get("outputs", ())
            output_snapshot = self.capture_paths(
                project_id,
                self.expand_stage_paths(project_id, patterns),
                producer_stage=stage_id,
                producer_run_id=run_id,
                dependencies=dependency_ids,
            )
            output_fingerprint = _fingerprint(output_snapshot)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE stage_runs SET status=?, output_fingerprint=?, output_snapshot_json=?,
                    error_message=?, metadata_json=?, updated_at=?, finished_at=?
                WHERE run_id=?
                """,
                (
                    status,
                    output_fingerprint,
                    _json(output_snapshot),
                    error_message,
                    _json(dict(metadata or {})),
                    now,
                    now,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE stage_state SET status=?, output_fingerprint=?, error_message=?, updated_at=?
                WHERE project_id=? AND stage_id=?
                """,
                (status, output_fingerprint, error_message, now, project_id, stage_id),
            )

    def set_stage_state(self, project_id: str, stage_id: str, status: str, *, error_message: str = "") -> None:
        self.ensure_project(project_id)
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO stage_state(project_id, stage_id, status, error_message, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(project_id, stage_id) DO UPDATE SET
                    status=excluded.status, error_message=excluded.error_message, updated_at=excluded.updated_at
                """,
                (project_id, stage_id, status, error_message, now),
            )

    def write_handoff(
        self,
        project_id: str,
        path: Path,
        source_stage: str,
        source_paths: Sequence[Path],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_snapshot = self.capture_paths(project_id, source_paths)
        payload: dict[str, Any] = {
            "schema_version": 2,
            "handoff_id": str(uuid.uuid4()),
            "source_stage": source_stage,
            "generated_at": utc_now(),
            "source_artifacts": [str(Path(item).resolve()) for item in source_paths],
            "source_fingerprint": _fingerprint(source_snapshot),
            "source_versions": source_snapshot,
            "output_fingerprint": "",
            "output_versions": [],
        }
        payload.update(dict(metadata or {}))
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        dependencies = [
            str(item["artifact_version_id"])
            for item in source_snapshot
            if item.get("artifact_version_id")
        ]
        self.register_artifact(project_id, path, producer_stage=source_stage, dependencies=dependencies)
        return payload

    def complete_handoff(
        self,
        project_id: str,
        path: Path,
        output_paths: Sequence[Path],
        *,
        producer_stage: str,
        producer_run_id: str | None = None,
    ) -> dict[str, Any]:
        path = Path(path)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        source_snapshot = payload.get("source_versions") if isinstance(payload, dict) else []
        if not isinstance(source_snapshot, list):
            source_paths = payload.get("source_artifacts") if isinstance(payload, dict) else []
            if not isinstance(source_paths, list):
                source_paths = []
            if not source_paths and isinstance(payload, dict):
                # Legacy handoffs commonly stored a named path such as
                # ``source_blueprint`` but no version snapshot. Upgrade those
                # records before declaring them schema v2.
                source_paths = [
                    value
                    for key, value in payload.items()
                    if key.startswith("source_")
                    and key not in {"source_stage", "source_fingerprint", "source_versions"}
                    and isinstance(value, str)
                    and Path(value).is_file()
                ]
            source_snapshot = self.capture_paths(
                project_id,
                [Path(item) for item in source_paths],
            )
            payload.update(
                {
                    "handoff_id": str(payload.get("handoff_id") or uuid.uuid4()),
                    "source_artifacts": [str(Path(item).resolve()) for item in source_paths],
                    "source_fingerprint": _fingerprint(source_snapshot),
                    "source_versions": source_snapshot,
                }
            )
        dependencies = [
            str(item["artifact_version_id"])
            for item in source_snapshot
            if isinstance(item, dict) and item.get("artifact_version_id")
        ]
        output_snapshot = self.capture_paths(
            project_id,
            output_paths,
            producer_stage=producer_stage,
            producer_run_id=producer_run_id,
            dependencies=dependencies,
        )
        payload.update(
            {
                "schema_version": 2,
                "completed_at": utc_now(),
                "output_fingerprint": _fingerprint(output_snapshot),
                "output_versions": output_snapshot,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        self.register_artifact(project_id, path, producer_stage=producer_stage, dependencies=dependencies)
        return payload

    def update_handoff_metadata(
        self,
        project_id: str,
        path: Path,
        metadata: Mapping[str, Any],
        *,
        producer_stage: str = "",
    ) -> dict[str, Any]:
        """Update orchestration-only handoff metadata without rebasing lineage."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Handoff payload must be an object: {path}")
        payload.update(dict(metadata))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        self.register_artifact(
            project_id,
            path,
            producer_stage=producer_stage or str(payload.get("source_stage") or ""),
        )
        return payload

    def handoff_freshness(
        self,
        project_id: str,
        handoff_path: Path,
        output_paths: Sequence[Path],
    ) -> dict[str, Any]:
        handoff_path = Path(handoff_path)
        payload = json.loads(handoff_path.read_text(encoding="utf-8")) if handoff_path.is_file() else {}
        source_versions = payload.get("source_versions") if isinstance(payload, dict) else None
        if not isinstance(source_versions, list):
            return {"handoff": payload, "versioned": False}

        outdated_sources: list[str] = []
        for record in source_versions:
            if not isinstance(record, dict):
                continue
            path = Path(str(record.get("path") or ""))
            if not path.is_file() or sha256_file(path) != str(record.get("sha256") or ""):
                outdated_sources.append(str(path))

        recorded_outputs = {
            str(item.get("logical_name") or ""): item
            for item in payload.get("output_versions") or []
            if isinstance(item, dict)
        }
        outdated_outputs: list[str] = []
        for path in output_paths:
            path = Path(path).resolve()
            logical_name = self.logical_name(project_id, path)
            record = recorded_outputs.get(logical_name)
            if not record or not path.is_file() or sha256_file(path) != str(record.get("sha256") or ""):
                outdated_outputs.append(str(path))
        return {
            "handoff": payload,
            "versioned": True,
            "stale": bool(outdated_sources or outdated_outputs),
            "outdated_artifacts": outdated_outputs,
            "outdated_sources": outdated_sources,
            "source_fingerprint": str(payload.get("source_fingerprint") or ""),
            "output_fingerprint": str(payload.get("output_fingerprint") or ""),
        }

    def save_job(self, project_id: str, job_type: str, state: Mapping[str, Any]) -> dict[str, Any]:
        self.ensure_project(project_id)
        payload = dict(state)
        job_id = str(payload.get("job_id") or uuid.uuid4())
        payload["job_id"] = job_id
        status = str(payload.get("status") or "pending")
        now = utc_now()
        started_at = str(payload.get("started_at") or now)
        finished_at = str(payload.get("finished_at") or "") or None
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs(job_id, project_id, job_type, status, payload_json, started_at, updated_at, finished_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status, payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at, finished_at=excluded.finished_at
                """,
                (job_id, project_id, job_type, status, _json(payload), started_at, now, finished_at),
            )
            connection.execute(
                """
                INSERT INTO current_jobs(project_id, job_type, job_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(project_id, job_type) DO UPDATE SET
                    job_id=excluded.job_id, updated_at=excluded.updated_at
                """,
                (project_id, job_type, job_id, now),
            )
        return payload

    def load_job(self, project_id: str, job_type: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT jobs.payload_json FROM current_jobs
                JOIN jobs ON jobs.job_id=current_jobs.job_id
                WHERE current_jobs.project_id=? AND current_jobs.job_type=?
                """,
                (project_id, job_type),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row else None

    def bootstrap_project(self, project_id: str) -> None:
        """Register existing canonical files without rewriting project content."""
        self.ensure_project(project_id)
        for stage_id, spec in STAGE_SPECS.items():
            input_snapshot = self.capture_paths(
                project_id,
                self.expand_stage_paths(project_id, spec.get("inputs", ())),
            )
            dependency_ids = [
                str(item["artifact_version_id"])
                for item in input_snapshot
                if item.get("artifact_version_id")
            ]
            output_snapshot = self.capture_paths(
                project_id,
                self.expand_stage_paths(project_id, spec.get("outputs", ())),
                producer_stage=stage_id,
                dependencies=dependency_ids,
            )
            if not any(item.get("exists") for item in output_snapshot):
                continue
            now = utc_now()
            with self._lock, self._connection() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO stage_state(
                        project_id, stage_id, status, input_fingerprint,
                        output_fingerprint, updated_at
                    ) VALUES(?, ?, 'materialized', ?, ?, ?)
                    """,
                    (project_id, stage_id, _fingerprint(input_snapshot), _fingerprint(output_snapshot), now),
                )

    def workflow_snapshot(self, project_id: str) -> dict[str, Any]:
        self.bootstrap_project(project_id)
        with self._lock, self._connection() as connection:
            states = [dict(row) for row in connection.execute(
                "SELECT * FROM stage_state WHERE project_id=? ORDER BY updated_at",
                (project_id,),
            )]
            recent_runs = [dict(row) for row in connection.execute(
                """
                SELECT * FROM stage_runs
                WHERE project_id=? ORDER BY started_at DESC LIMIT 50
                """,
                (project_id,),
            )]
            artifacts = [dict(row) for row in connection.execute(
                """
                SELECT av.* FROM current_artifacts ca
                JOIN artifact_versions av ON av.artifact_version_id=ca.artifact_version_id
                WHERE ca.project_id=? ORDER BY av.logical_name
                """,
                (project_id,),
            )]
            dependencies = [dict(row) for row in connection.execute(
                "SELECT * FROM stage_dependencies ORDER BY stage_id, dependency_kind, depends_on_stage_id"
            )]
            jobs = [dict(row) for row in connection.execute(
                """
                SELECT jobs.job_id, jobs.job_type, jobs.status, jobs.started_at, jobs.updated_at, jobs.finished_at
                FROM current_jobs JOIN jobs ON jobs.job_id=current_jobs.job_id
                WHERE current_jobs.project_id=? ORDER BY jobs.updated_at DESC
                """,
                (project_id,),
            )]
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "database_path": str(self.database_path),
            "stage_dependencies": dependencies,
            "stage_state": states,
            "recent_stage_runs": recent_runs,
            "current_artifacts": artifacts,
            "jobs": jobs,
        }

    def delete_project(self, project_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
