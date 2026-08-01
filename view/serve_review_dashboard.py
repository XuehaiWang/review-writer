#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import importlib.util
import json
import math
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from PIL import Image


_VIEW_SCRIPTS = Path(__file__).resolve().parent
if str(_VIEW_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_VIEW_SCRIPTS))
from workflow_store import WorkflowStore
from prefect_runtime import (
    configure_prefect_environment,
    prefect_orchestration_enabled,
    run_batch_redraw_with_prefect,
    run_literature_acquisition_with_prefect,
    run_stage_with_prefect,
)


_PARAGRAPH_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "review-draft-merge-polish" / "scripts"
if str(_PARAGRAPH_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PARAGRAPH_SCRIPTS))
from paragraph_editor import ParagraphEditor
from paragraph_manifest_builder import build_manifest


_LITERATURE_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "review-literature-acquisition"
    / "scripts"
)
if str(_LITERATURE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_LITERATURE_SCRIPTS))
from literature_acquisition import acquire_candidate, load_dotenv_if_present, search_crossref


_DISCOVERY_MODULE = None
_BATCH_REDRAW_LOCK = threading.RLock()
_BATCH_REDRAW_JOBS: dict[str, dict[str, object]] = {}
_LITERATURE_JOB_LOCK = threading.RLock()
_LITERATURE_THREADS: dict[str, threading.Thread] = {}
_LIBRARY_WORKFLOW_PROJECT = "__library__"
_WORKFLOW_STORE_LOCK = threading.RLock()
_WORKFLOW_STORES: dict[str, WorkflowStore] = {}
FULL_SVG_MAX_DIMENSION = 1600
FULL_SVG_WHITE_THRESHOLD = 245
FULL_SVG_COLOR_STEP = 24


def workflow_store(review_root: Path) -> WorkflowStore:
    """Return the durable workflow metadata store for one workspace."""
    key = str(Path(review_root).resolve())
    with _WORKFLOW_STORE_LOCK:
        store = _WORKFLOW_STORES.get(key)
        if store is None:
            store = WorkflowStore(Path(review_root))
            _WORKFLOW_STORES[key] = store
        return store


def workflow_context_for_path(path: Path) -> tuple[WorkflowStore, str] | None:
    """Infer workspace and project IDs from a path below review-projects."""
    resolved = Path(path).resolve()
    parts = resolved.parts
    try:
        marker = parts.index("review-projects")
    except ValueError:
        return None
    if marker == 0 or marker + 1 >= len(parts):
        return None
    review_root = Path(*parts[:marker])
    project_id = parts[marker + 1]
    return workflow_store(review_root), project_id


def execute_dashboard_stage(review_root: Path, project_id: str, stage_id: str) -> dict[str, Any]:
    """Execute one existing scientific stage inside a Prefect task."""
    project = Path(review_root) / "review-projects" / project_id
    if stage_id == "sections":
        result = regenerate_section_drafting(review_root, project_id)
        next_stage = "figure-review"
    elif stage_id == "figure-review":
        result = validate_figure_review(project, project_id)
        next_stage = "figures"
    elif stage_id == "figures":
        result = regenerate_figures_and_first_draft(review_root, project_id)
        next_stage = "draft"
    elif stage_id == "draft":
        result = regenerate_first_draft(review_root, project_id)
        next_stage = "final"
    elif stage_id == "final-conclusion":
        result = generate_final_conclusion(review_root, project_id)
        next_stage = "final"
    elif stage_id == "final-overview-figure":
        result = generate_final_overview_figure(review_root, project_id)
        next_stage = "final"
    elif stage_id == "final":
        result = regenerate_final_draft_bundle(review_root, project_id)
        next_stage = None
    else:
        raise ValueError("This page does not have a runnable generation step.")
    return {"result": result, "next_stage": next_stage}


def discovery_script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "review-topic-paper-discovery"
        / "scripts"
        / "discover.py"
    )


def discovery_module():
    """Load the discovery helpers so the dashboard writes a script-valid plan."""
    global _DISCOVERY_MODULE
    if _DISCOVERY_MODULE is None:
        script = discovery_script_path()
        script_dir = str(script.parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        spec = importlib.util.spec_from_file_location("review_topic_discover", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the topic discovery helpers.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DISCOVERY_MODULE = module
    return _DISCOVERY_MODULE


class DashboardHandler(BaseHTTPRequestHandler):
    review_root: Path
    library_app_path: Path
    discovery_app_path: Path
    matrix_app_path: Path
    blueprint_app_path: Path
    sections_app_path: Path
    figures_app_path: Path
    figure_review_app_path: Path
    draft_app_path: Path
    final_app_path: Path

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    @property
    def metadata_dir(self) -> Path:
        return self.review_root / "review-library" / "metadata" / "papers"

    @property
    def registry_path(self) -> Path:
        return self.review_root / "review-library" / "registry" / "papers.jsonl"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/library")
            self.end_headers()
        elif parsed.path == "/library":
            self.send_file(self.library_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/discovery":
            self.send_file(self.discovery_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/matrix":
            self.send_file(self.matrix_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/blueprint":
            self.send_file(self.blueprint_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/sections":
            self.send_file(self.sections_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/figures":
            self.send_file(self.figures_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/figure-review":
            self.send_file(self.figure_review_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/draft":
            self.send_file(self.draft_app_path, "text/html; charset=utf-8")
        elif parsed.path == "/final":
            self.send_file(self.final_app_path, "text/html; charset=utf-8")
        elif parsed.path.startswith("/assets/"):
            self.handle_static_asset(parsed.path)
        elif parsed.path == "/api/projects":
            self.handle_projects()
        elif parsed.path == "/api/papers":
            self.handle_papers()
        elif parsed.path == "/api/literature/search/status":
            self.handle_literature_job_status("literature-search")
        elif parsed.path == "/api/literature/download/status":
            self.handle_literature_job_status("literature-download")
        elif parsed.path == "/api/discovery-projects":
            self.handle_discovery_projects()
        elif parsed.path.startswith("/api/project/") and parsed.path.endswith("/draft"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_draft_get(project_id)
        elif parsed.path.startswith("/api/project/") and "/paragraph" in parsed.path:
            self.handle_paragraph_get(parsed.path)
        elif parsed.path.startswith("/api/project/") and parsed.path.endswith("/figures/redraw-all"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"]:
                self.handle_batch_figure_redraw_status(unquote(parts[2]))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "batch redraw status not found")
        elif parsed.path.startswith("/api/project/") and parsed.path.endswith("/workflow-state"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4 and parts[0:2] == ["api", "project"]:
                self.handle_project_workflow_state(unquote(parts[2]))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "workflow state not found")
        elif parsed.path.startswith("/api/project/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 4:
                project_id = unquote(parts[2])
                stage = unquote(parts[3])
                self.handle_project_stage_get(project_id, stage)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "project stage not found")
        elif parsed.path.startswith("/api/discovery/"):
            project_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_discovery_get(project_id)
        elif parsed.path.startswith("/api/metadata/"):
            paper_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_metadata_get(paper_id)
        elif parsed.path.startswith("/api/local/metadata/"):
            paper_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_metadata_get(paper_id)
        elif parsed.path.startswith("/api/markdown/"):
            paper_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_markdown_get(paper_id)
        elif parsed.path.startswith("/api/local/markdown/"):
            paper_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_markdown_get(paper_id)
        elif parsed.path == "/file":
            query = parse_qs(parsed.query)
            path = query.get("path", [""])[0]
            paper_id = query.get("paper_id", [""])[0]
            self.handle_file(path, paper_id)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/metadata/"):
            paper_id = unquote(parsed.path.rsplit("/", 1)[-1])
            self.handle_metadata_put(paper_id)
            return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/draft"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_draft_put(project_id)
            return
        if parsed.path.startswith("/api/project/") and "/paragraph/" in parsed.path:
            self.handle_paragraph_put(parsed.path)
            return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/matrix-outline"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_matrix_outline_put(project_id)
            return
        if parsed.path.startswith("/api/project/") and "/matrix/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"] and parts[3] == "matrix":
                self.handle_project_matrix_row_put(unquote(parts[2]), unquote(parts[4]))
                return
        if parsed.path.startswith("/api/discovery/"):
            project_id = unquote(parsed.path.rsplit("/", 1)[-1])
            query = parse_qs(parsed.query)
            self.handle_discovery_put(project_id, confirm=bool(query.get("confirm")))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/literature/search":
            self.handle_literature_search_start()
            return
        if parsed.path == "/api/literature/download":
            self.handle_literature_download_start()
            return
        if parsed.path == "/api/discovery":
            self.handle_discovery_start()
            return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/reference-outline"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_reference_outline_upload(project_id)
            return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/section-tasks"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_section_tasks_start(project_id)
            return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/figures/redraw-all"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"]:
                self.handle_batch_figure_redraw_start(unquote(parts[2]))
                return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/figures/redraw-all/stop"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 6 and parts[0:2] == ["api", "project"]:
                self.handle_batch_figure_redraw_stop(unquote(parts[2]))
                return
        if parsed.path.startswith("/api/project/") and "/figures/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 6 and parts[0:2] == ["api", "project"] and parts[3] == "figures" and parts[5] == "redraw":
                self.handle_current_figure_redraw(unquote(parts[2]), unquote(parts[4]))
                return
            if len(parts) == 6 and parts[0:2] == ["api", "project"] and parts[3] == "figures" and parts[5] == "human-approve":
                self.handle_figure_human_approval(unquote(parts[2]), unquote(parts[4]))
                return
            if len(parts) == 6 and parts[0:2] == ["api", "project"] and parts[3] == "figures" and parts[5] == "manual-arrow-edit":
                self.handle_manual_arrow_edit(unquote(parts[2]), unquote(parts[4]))
                return
            if len(parts) == 6 and parts[0:2] == ["api", "project"] and parts[3] == "figures" and parts[5] == "full-svg":
                self.handle_full_figure_svg(unquote(parts[2]), unquote(parts[4]))
                return
        if parsed.path.startswith("/api/project/") and "/run/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"] and parts[3] == "run":
                self.handle_project_stage_run(unquote(parts[2]), unquote(parts[4]))
                return
        if parsed.path.startswith("/api/project/") and "/handoff/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"] and parts[3] == "handoff":
                self.handle_project_handoff(unquote(parts[2]), unquote(parts[4]))
                return
        if parsed.path.startswith("/api/project/") and "/figure-review/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 5 and parts[0:2] == ["api", "project"] and parts[3] == "figure-review":
                self.handle_figure_review_post(unquote(parts[2]), unquote(parts[4]))
                return
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/export-docx"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_export_docx(project_id)
            return
        if parsed.path.startswith("/api/project/") and "/paragraph" in parsed.path:
            self.handle_paragraph_post(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/projects/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 3 and parts[0:2] == ["api", "projects"]:
                self.handle_project_delete(unquote(parts[2]))
                return
        if parsed.path.startswith("/api/project/") and "/paragraph/" in parsed.path:
            self.handle_paragraph_delete(parsed.path)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_project_delete(self, project_id: str) -> None:
        try:
            result = delete_review_project(self.review_root, project_id)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except FileNotFoundError:
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, **result})

    def handle_discovery_start(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self.send_json(
                {"ok": False, "error": f"Invalid discovery payload: {exc}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        value, error = validate_discovery_start_payload(
            payload,
            lambda project_id: (self.review_root / "review-projects" / project_id).exists(),
        )
        if error or value is None:
            self.send_json(
                {"ok": False, "error": error or "Invalid discovery payload."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        result = start_discovery(self.review_root, value)
        if not result.get("ok"):
            self.send_json(result, status=HTTPStatus.BAD_GATEWAY)
            return
        self.send_json(result, status=HTTPStatus.CREATED)

    def handle_project_export_docx(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        stage = project / "05_final_audit"
        md_path = stage / "final_draft.md"
        if not md_path.exists():
            self.send_error(HTTPStatus.BAD_REQUEST, "final_draft.md not found")
            return
        docx_path = stage / "final_draft.docx"
        script = self.review_root / "skills" / "review-export-docx" / "scripts" / "run_md2docx.py"
        if not script.exists():
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "DOCX export runner not found")
            return
        try:
            refresh_final_overview_chart(self.review_root, project_id, md_path)
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": f"Could not refresh the overall review chart: {exc}"})
            return
        def export_to(output_path: Path) -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    [sys.executable, str(script), "--input", str(md_path), "--output", str(output_path)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                return None

        result = export_to(docx_path)
        if result is None:
            self.send_error(HTTPStatus.GATEWAY_TIMEOUT, "docx export timeout")
            return

        fallback = False
        export_log = (result.stderr or result.stdout or "").lower()
        access_denied = any(marker in export_log for marker in (
            "permissionerror", "permission denied", "access is denied", "[errno 13]",
        ))
        if (result.returncode != 0 or not docx_path.exists()) and access_denied:
            from datetime import datetime

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            docx_path = stage / f"final_draft_{stamp}.docx"
            suffix = 1
            while docx_path.exists():
                docx_path = stage / f"final_draft_{stamp}_{suffix}.docx"
                suffix += 1
            result = export_to(docx_path)
            if result is None:
                self.send_error(HTTPStatus.GATEWAY_TIMEOUT, "docx export timeout")
                return
            fallback = True

        if result.returncode != 0 or not docx_path.exists():
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-20:]
            self.send_json({
                "ok": False,
                "returncode": result.returncode,
                "error": "\n".join(tail) or "md2docx.py failed",
            })
            return
        (stage / "docx_export.json").write_text(
            json.dumps({
                "output_path": docx_path.name,
                "exported_at": now_utc(),
                "fallback": fallback,
                "draft_sha256": sha256_file(md_path),
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.send_json({
            "ok": True,
            "path": str(docx_path),
            "size": docx_path.stat().st_size,
            "fallback": fallback,
            "message": (
                "final_draft.docx is open or locked; exported a timestamped copy instead."
                if fallback else "DOCX exported."
            ),
        })

    def handle_projects(self) -> None:
        self.send_json(list_review_projects(self.review_root))

    def handle_papers(self) -> None:
        papers = []
        for path in sorted(self.metadata_dir.glob("*.metadata.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            structured_tags = value_of(meta.get("structured_tags")) or {}
            structured_values = list(structured_tags.values()) if isinstance(structured_tags, dict) else []
            papers.append(
                {
                    "paper_id": meta.get("paper_id"),
                    "title": value_of(meta.get("title")),
                    "authors": value_of(meta.get("authors")) or [],
                    "year": value_of(meta.get("year")),
                    "journal": value_of(meta.get("journal")),
                    "doi": value_of(meta.get("doi")),
                    "structured_tags": structured_tags,
                    "tags": structured_values,
                    "human_review_status": (meta.get("human_review") or {}).get("status"),
                    "needs_human_check": (meta.get("quality") or {}).get("needs_human_check"),
                }
            )
        self.send_json(papers)

    def handle_literature_job_status(self, job_type: str) -> None:
        state = current_literature_job(self.review_root, job_type)
        self.send_json({"ok": True, "job": state})

    def handle_literature_search_start(self) -> None:
        try:
            payload = self.read_json_object()
            topic = re.sub(r"\s+", " ", str(payload.get("topic") or "")).strip()
            if len(topic) < 3:
                raise ValueError("Enter a more specific literature topic.")
            year_from = optional_year(payload.get("year_from"))
            year_to = optional_year(payload.get("year_to"))
            if year_from and year_to and year_from > year_to:
                raise ValueError("The starting year cannot be later than the ending year.")
            limit = max(1, min(int(payload.get("limit") or 20), 50))
            email = str(payload.get("email") or "").strip()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        store = workflow_store(self.review_root)
        with _LITERATURE_JOB_LOCK:
            current = current_literature_job(self.review_root, "literature-search")
            if current and current.get("status") in {"queued", "running"}:
                self.send_json(
                    {"ok": False, "error": "A literature search is already running.", "job": current},
                    status=HTTPStatus.CONFLICT,
                )
                return
            state = store.save_job(
                _LIBRARY_WORKFLOW_PROJECT,
                "literature-search",
                {
                    "status": "queued",
                    "operation": "search",
                    "topic": topic,
                    "year_from": year_from,
                    "year_to": year_to,
                    "limit": limit,
                    "email_configured": bool(email),
                    "progress_current": 0,
                    "progress_total": 1,
                    "candidates": [],
                    "error": "",
                    "started_at": now_utc(),
                },
            )
            thread = threading.Thread(
                target=run_literature_search_job,
                args=(self.review_root, state, email),
                name=f"literature-search-{state['job_id']}",
                daemon=True,
            )
            _LITERATURE_THREADS["literature-search"] = thread
            thread.start()
        self.send_json({"ok": True, "job": state}, status=HTTPStatus.ACCEPTED)

    def handle_literature_download_start(self) -> None:
        try:
            payload = self.read_json_object()
            candidate_ids = [
                str(value)
                for value in (payload.get("candidate_ids") or [])
                if str(value).strip()
            ]
            candidate_ids = list(dict.fromkeys(candidate_ids))
            if not candidate_ids:
                raise ValueError("Select at least one candidate.")
            if len(candidate_ids) > 30:
                raise ValueError("Download at most 30 papers in one run.")
            email = str(payload.get("email") or "").strip()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        store = workflow_store(self.review_root)
        search_state = store.load_job(_LIBRARY_WORKFLOW_PROJECT, "literature-search") or {}
        available = {
            str(row.get("candidate_id")): row
            for row in search_state.get("candidates") or []
            if isinstance(row, dict) and row.get("candidate_id")
        }
        missing = [candidate_id for candidate_id in candidate_ids if candidate_id not in available]
        if missing:
            self.send_json(
                {"ok": False, "error": "Some selected candidates are no longer in the current search result."},
                status=HTTPStatus.CONFLICT,
            )
            return
        selected = [available[candidate_id] for candidate_id in candidate_ids]
        with _LITERATURE_JOB_LOCK:
            current = current_literature_job(self.review_root, "literature-download")
            if current and current.get("status") in {"queued", "running"}:
                self.send_json(
                    {"ok": False, "error": "A literature download is already running.", "job": current},
                    status=HTTPStatus.CONFLICT,
                )
                return
            state = store.save_job(
                _LIBRARY_WORKFLOW_PROJECT,
                "literature-download",
                {
                    "status": "queued",
                    "operation": "download",
                    "candidate_ids": candidate_ids,
                    "email_configured": bool(email),
                    "progress_current": 0,
                    "progress_total": len(selected),
                    "success_count": 0,
                    "failed_count": 0,
                    "results": [],
                    "error": "",
                    "started_at": now_utc(),
                },
            )
            thread = threading.Thread(
                target=run_literature_download_job,
                args=(self.review_root, state, selected, email),
                name=f"literature-download-{state['job_id']}",
                daemon=True,
            )
            _LITERATURE_THREADS["literature-download"] = thread
            thread.start()
        self.send_json({"ok": True, "job": state}, status=HTTPStatus.ACCEPTED)

    def read_json_object(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1024 * 1024:
            raise ValueError("Request body must be a JSON object smaller than 1 MB.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def handle_discovery_projects(self) -> None:
        self.send_json([p for p in list_review_projects(self.review_root) if p.get("has_discovery")])

    def handle_discovery_get(self, project_id: str) -> None:
        path = self.discovery_path(project_id)
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "discovery data not found")
            return
        self.send_file(path, "application/json; charset=utf-8")

    def handle_discovery_put(self, project_id: str, confirm: bool = False) -> None:
        path = self.discovery_path(project_id)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid discovery json: {exc}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        selected = selected_from_combined(data.get("results", []), project_id)
        selected["human_confirmed"] = bool(confirm)
        (path.parent / "selected_discovery_results.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (path.parent / "human_check_state.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "status": "confirmed" if confirm else "pending",
                    "confirmed_at": now_utc() if confirm else None,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        matrix_sync = sync_matrix_from_discovery(self.review_root, project_id) if confirm else None
        self.send_json({"ok": True, "confirmed": confirm, "matrix_sync": matrix_sync})

    def handle_project_draft_get(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "project not found")
            return
        if (project / "04_first_draft" / "first_draft.md").exists():
            build_manifest(self.review_root, project_id)
        self.send_json(project_draft_payload(self.review_root, project_id))

    def handle_project_stage_get(self, project_id: str, stage: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "project not found")
            return
        payloads = {
            "matrix": project_matrix_payload,
            "blueprint": project_blueprint_payload,
            "sections": project_sections_payload,
            "figures": project_figures_payload,
            "figure-review": project_figure_review_payload,
            "final": project_final_payload,
        }
        builder = payloads.get(stage)
        if not builder:
            self.send_error(HTTPStatus.NOT_FOUND, "unknown stage")
            return
        self.send_json(builder(self.review_root, project_id))

    def handle_project_workflow_state(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.is_dir():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, **workflow_store(self.review_root).workflow_snapshot(project_id)})

    def handle_figure_review_post(self, project_id: str, paper_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        candidates_path = project / "02_section_drafting" / "paper_figure_candidates.json"
        if not project.exists() or not candidates_path.exists():
            self.send_json({"ok": False, "error": "Figure candidates are not available for this project."}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            candidate_index = payload.get("candidate_index")
            if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
                raise ValueError("candidate_index must be an integer")
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Invalid figure-review payload: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        candidates_data = read_json_if_exists(candidates_path) or {}
        papers = candidates_data.get("papers") if isinstance(candidates_data, dict) else None
        paper = next((item for item in papers or [] if item.get("paper_id") == paper_id), None)
        if not isinstance(paper, dict):
            self.send_json({"ok": False, "error": "Paper figure candidates were not found."}, status=HTTPStatus.NOT_FOUND)
            return
        candidate = next(
            (item for item in paper.get("candidates") or [] if item.get("candidate_index") == candidate_index),
            None,
        )
        if not isinstance(candidate, dict):
            self.send_json({"ok": False, "error": "Selected candidate does not belong to this paper."}, status=HTTPStatus.BAD_REQUEST)
            return
        if candidate.get("source_type") == "table":
            self.send_json(
                {"ok": False, "error": "Table candidates cannot be passed to the figure redraw skill. Select an image or scheme candidate."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        paper["selected_candidate_index"] = candidate_index
        write_json(candidates_path, candidates_data)
        review_path = candidates_path.parent / "human_figure_review.json"
        reviews = read_json_if_exists(review_path) or {"papers": {}}
        reviews.setdefault("papers", {})[paper_id] = {
            "selected_candidate_index": candidate_index,
            "review_note": str(payload.get("review_note") or "").strip()[:2000],
            "reviewed_at": now_utc(),
        }
        write_json(review_path, reviews)
        sync_error = ""
        try:
            sync_selected_candidate_for_redraw(project, paper_id, candidate)
        except RuntimeError as exc:
            sync_error = str(exc)
        # Candidate selection changes both candidate manifests. Capture the
        # handoff only after those writes, otherwise the just-saved selection
        # immediately makes the entire Figure Review globally stale.
        refresh_figure_review_handoff(candidates_path.parent, accept_current=True)
        if sync_error:
            self.send_json(
                {
                    "ok": False,
                    "project_id": project_id,
                    "paper_id": paper_id,
                    "selected_candidate_index": candidate_index,
                    "error": f"Candidate was saved, but could not be prepared for the batch redraw: {sync_error}",
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        self.send_json(
            {
                "ok": True,
                "project_id": project_id,
                "paper_id": paper_id,
                "selected_candidate_index": candidate_index,
                "redraw_pending": True,
            }
        )

    def handle_current_figure_redraw(self, project_id: str, figure_id: str) -> None:
        try:
            result = redraw_current_figure(self.review_root, project_id, figure_id)
        except (RuntimeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self.send_json({"ok": True, "project_id": project_id, **result})

    def handle_figure_human_approval(self, project_id: str, figure_id: str) -> None:
        try:
            result = approve_figure_for_manuscript(self.review_root, project_id, figure_id)
        except (RuntimeError, ValueError, OSError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self.send_json({"ok": True, "project_id": project_id, **result})

    def handle_batch_figure_redraw_start(self, project_id: str) -> None:
        try:
            result = start_batch_figure_redraw(self.review_root, project_id)
        except (RuntimeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self.send_json({"ok": True, "project_id": project_id, **result}, status=HTTPStatus.ACCEPTED)

    def handle_batch_figure_redraw_status(self, project_id: str) -> None:
        self.send_json({"ok": True, "project_id": project_id, **batch_figure_redraw_status(project_id, self.review_root)})

    def handle_batch_figure_redraw_stop(self, project_id: str) -> None:
        self.send_json({"ok": True, "project_id": project_id, **stop_batch_figure_redraw(project_id, self.review_root)})

    def handle_manual_arrow_edit(self, project_id: str, figure_id: str) -> None:
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            data_url = str(payload.get("image_png_data_url") or "")
            if not data_url.startswith("data:image/png;base64,"):
                raise ValueError("image_png_data_url must be a PNG data URL")
            image_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
            operations = payload.get("operations") or []
            if not isinstance(operations, list):
                raise ValueError("operations must be a list")
            base_mode = str(payload.get("base_mode") or "source")
            svg_content = str(payload.get("editable_svg") or "")
            full_vector_svg = str(payload.get("full_vector_svg") or "")
            result = save_manual_arrow_edit(
                self.review_root,
                project_id,
                figure_id,
                image_bytes,
                operations,
                base_mode=base_mode,
                editable_svg=svg_content,
                full_vector_svg=full_vector_svg,
            )
        except (ValueError, OSError, Image.UnidentifiedImageError) as exc:
            self.send_json({"ok": False, "error": f"Manual arrow edit was not saved: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True, "project_id": project_id, **result})

    def handle_full_figure_svg(self, project_id: str, figure_id: str) -> None:
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            result = create_full_figure_svg(
                self.review_root,
                project_id,
                figure_id,
                base_mode=str(payload.get("base_mode") or "source"),
            )
        except (ValueError, OSError, Image.UnidentifiedImageError) as exc:
            self.send_json({"ok": False, "error": f"Full-image SVG was not created: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        self.send_json({"ok": True, "project_id": project_id, **result})

    def handle_section_tasks_start(self, project_id: str) -> None:
        try:
            result = regenerate_section_tasks(self.review_root, project_id)
        except FileNotFoundError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        self.send_json(
            {
                "ok": True,
                "project_id": project_id,
                "task_count": result["task_count"],
                "next_path": f"/sections?project={quote(project_id)}",
            }
        )

    def handle_project_stage_run(self, project_id: str, stage_id: str) -> None:
        """Run the deterministic part of a stage before handing it to the next page."""
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        store = workflow_store(self.review_root)
        run_id = store.start_stage_run(project_id, stage_id, metadata={"trigger": "dashboard"})
        try:
            orchestration = run_stage_with_prefect(
                self.review_root,
                project_id,
                stage_id,
                lambda: execute_dashboard_stage(self.review_root, project_id, stage_id),
            )
            execution = orchestration["result"]
            result = execution["result"]
            next_stage = execution["next_stage"]
        except (RuntimeError, ValueError) as exc:
            store.finish_stage_run(run_id, "failed", error_message=str(exc))
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
        except Exception as exc:
            store.finish_stage_run(run_id, "failed", error_message=f"{type(exc).__name__}: {exc}")
            self.send_json(
                {"ok": False, "error": f"Prefect stage execution failed: {type(exc).__name__}: {exc}"},
                status=HTTPStatus.CONFLICT,
            )
            return
        store.finish_stage_run(
            run_id,
            "completed",
            metadata={
                "result": result,
                "workflow_engine": "prefect",
                "prefect_flow_run_id": orchestration.get("prefect_flow_run_id"),
                "prefect_task_run_id": orchestration.get("prefect_task_run_id"),
            },
        )
        self.send_json({
            "ok": True,
            "project_id": project_id,
            "stage_id": stage_id,
            "result": result,
            "next_stage": next_stage,
            "next_path": f"/{next_stage}?project={quote(project_id)}" if next_stage else "",
        })

    def handle_project_handoff(self, project_id: str, stage_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        next_stages = {
            "library": "discovery",
            "discovery": "matrix",
            "matrix": "blueprint",
            "sections": "figure-review",
            "figure-review": "figures",
            "figures": "draft",
            "draft": "final",
            "final": None,
        }
        if stage_id not in next_stages:
            self.send_json({"ok": False, "error": "This stage has no generic handoff action."}, status=HTTPStatus.BAD_REQUEST)
            return
        error = ""
        if stage_id == "library" and not any(self.metadata_dir.glob("*.metadata.json")):
            error = "Library contains no metadata records."
        elif stage_id == "discovery":
            selected = read_json_if_exists(project / "00_discovery" / "selected_discovery_results.json") or {}
            if not selected.get("human_confirmed"):
                error = "Confirm the Discovery selection before continuing to Matrix."
        elif stage_id == "matrix":
            matrix_stage = project / "01_matrix_outline"
            matrix = read_json_if_exists(matrix_stage / "literature_matrix.json") or {}
            if not matrix_outline_ready(matrix_stage, matrix):
                error = "Choose an outline again after the current Matrix synchronization before continuing to Blueprint."
            else:
                try:
                    regenerate_section_blueprint(self.review_root, project_id)
                except RuntimeError as exc:
                    error = f"Blueprint could not be regenerated: {exc}"
        elif stage_id == "sections":
            stage = project / "02_section_drafting"
            freshness = section_candidate_freshness(stage)
            if freshness["stale"]:
                error = "Blueprint has changed. Regenerate section drafts and figure candidates before continuing to Figure Review."
            elif not (stage / "section_drafts.json").exists() or not (stage / "figure_candidates.json").exists():
                error = "Complete section drafting and figure candidate generation before continuing to Figure Review."
            else:
                write_stage_handoff(
                    project / "03_figure_redraw" / "figure_review_handoff.json",
                    "sections",
                    figure_review_dependency_paths(stage),
                    metadata={"dependency_profile": FIGURE_REVIEW_DEPENDENCY_PROFILE},
                )
        elif stage_id == "figures":
            draft_stage = project / "02_section_drafting"
            freshness = section_source_freshness(draft_stage)
            figure_state = project_figures_payload(self.review_root, project_id)
            figure_freshness = figure_state["freshness"]
            if freshness["stale"]:
                error = "Sections are out of date with Blueprint. Regenerate sections and figure candidates before Draft."
            elif not (project / "03_figure_redraw" / "redrawn_figure_manifest.json").exists():
                error = "Run the selected-figure batch redraw before continuing to Draft."
            elif not figure_freshness["selected_count"]:
                error = "No manuscript figure is selected. Select figures in Figure Review before continuing to Draft."
            elif figure_freshness["stale"]:
                error = (
                    "Figure redraw outputs are incomplete or no longer match the current selections "
                    f"({figure_freshness['usable_count']}/{figure_freshness['selected_count']} usable). "
                    "Redraw the missing figures or approve warning outputs before continuing to Draft."
                )
            else:
                write_stage_handoff(
                    project / "04_first_draft" / "draft_handoff.json",
                    "figures",
                    [draft_stage / "section_drafts.json", draft_stage / "human_figure_review.json", project / "03_figure_redraw" / "redrawn_figure_manifest.json"],
                )
        elif stage_id == "figure-review":
            draft_stage = project / "02_section_drafting"
            source_freshness = section_candidate_freshness(draft_stage)
            review_freshness = artifact_freshness(
                project / "03_figure_redraw" / "figure_review_handoff.json",
                [draft_stage / "human_figure_review.json"],
            )
            candidates = read_json_if_exists(draft_stage / "paper_figure_candidates.json") or {}
            papers = candidates.get("papers") if isinstance(candidates, dict) else []
            reviews = (read_json_if_exists(draft_stage / "human_figure_review.json") or {}).get("papers", {})
            if source_freshness["stale"]:
                error = "Sections are out of date with Blueprint. Regenerate them before using Figure Review results."
            elif review_freshness["stale"]:
                error = "Figure candidates changed. Review and select a figure for every cited paper again."
            elif not papers or not isinstance(reviews, dict) or any(str(row.get("paper_id")) not in reviews for row in papers if isinstance(row, dict)):
                error = "Review and select a figure for every cited paper before continuing to Figures."
            else:
                refresh_figure_review_handoff(draft_stage, accept_current=True)
        elif stage_id == "draft":
            draft_stage = project / "02_section_drafting"
            source_freshness = section_source_freshness(draft_stage)
            draft_freshness = artifact_freshness(
                project / "04_first_draft" / "draft_handoff.json",
                [project / "04_first_draft" / "first_draft.md"],
            )
            if source_freshness["stale"] or draft_freshness["stale"]:
                error = "The first draft is out of date. Regenerate it from the current sections and reviewed figures before continuing to Final."
            elif not (project / "04_first_draft" / "first_draft.md").exists():
                error = "Create and save the first draft before continuing to Final."
            else:
                write_stage_handoff(
                    project / "05_final_audit" / "final_handoff.json",
                    "draft",
                    [project / "04_first_draft" / "first_draft.md"],
                )
        elif stage_id == "final":
            final_freshness = artifact_freshness(
                project / "05_final_audit" / "final_handoff.json",
                [project / "05_final_audit" / "final_draft.md"],
            )
            if final_freshness["stale"]:
                error = "The final audit is out of date. Regenerate final_draft.md from the current first draft."
            elif not (project / "05_final_audit" / "final_draft.md").exists():
                error = "Run the final audit and create final_draft.md before marking the project complete."
        if error:
            self.send_json({"ok": False, "error": error}, status=HTTPStatus.CONFLICT)
            return
        state_path = project / "pipeline_handoffs.json"
        state = read_json_if_exists(state_path) or {"project_id": project_id, "stages": {}}
        state.setdefault("stages", {})[stage_id] = {"completed_at": now_utc(), "next_stage": next_stages[stage_id]}
        write_json(state_path, state)
        workflow_store(self.review_root).set_stage_state(project_id, stage_id, "approved")
        next_stage = next_stages[stage_id]
        self.send_json(
            {
                "ok": True,
                "project_id": project_id,
                "stage_id": stage_id,
                "next_stage": next_stage,
                "next_path": f"/{next_stage}?project={project_id}" if next_stage else "",
            }
        )

    def handle_static_asset(self, path: str) -> None:
        assets_root = Path(__file__).resolve().parent / "assets"
        rel = posixpath.normpath(unquote(path.removeprefix("/assets/"))).lstrip("/")
        candidate = (assets_root / rel).resolve()
        try:
            candidate.relative_to(assets_root.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "asset path outside assets root")
            return
        if not candidate.exists() or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "asset not found")
            return
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_file(candidate, mime)

    def handle_project_draft_put(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "project not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid draft payload: {exc}")
            return
        stage_dir = project / "04_first_draft"
        stage_dir.mkdir(parents=True, exist_ok=True)
        if "first_draft_md" in data:
            (stage_dir / "first_draft.md").write_text(str(data.get("first_draft_md") or ""), encoding="utf-8")
        if "merge_report_md" in data:
            (stage_dir / "merge_report.md").write_text(str(data.get("merge_report_md") or ""), encoding="utf-8")
        if "remaining_issues_md" in data:
            (stage_dir / "remaining_issues.md").write_text(str(data.get("remaining_issues_md") or ""), encoding="utf-8")
        if "draft_bundle" in data and isinstance(data.get("draft_bundle"), dict):
            write_json(stage_dir / "draft_bundle.json", data["draft_bundle"])
        draft_handoff = stage_dir / "draft_handoff.json"
        ensure_stage_handoff(
            draft_handoff,
            "figures",
            [
                project / "02_section_drafting" / "section_drafts.json",
                project / "02_section_drafting" / "human_figure_review.json",
                project / "03_figure_redraw" / "redrawn_figure_manifest.json",
            ],
        )
        record_stage_outputs(
            draft_handoff,
            [
                path
                for path in (
                    stage_dir / "first_draft.md",
                    stage_dir / "citations.json",
                    stage_dir / "merge_report.md",
                    stage_dir / "remaining_issues.md",
                    stage_dir / "draft_bundle.json",
                )
                if path.exists()
            ],
            "draft",
        )
        self.send_json({"ok": True, "project_id": project_id})

    def _paragraph_editor(self, project_id: str) -> ParagraphEditor | None:
        if not (self.review_root / "review-projects" / project_id).is_dir():
            self.send_json({"ok": False, "error": "project not found"}, status=HTTPStatus.NOT_FOUND)
            return None
        return ParagraphEditor(self.review_root, project_id)

    def handle_paragraph_get(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 4:
            self.send_json({"ok": False, "error": "invalid paragraph path"}, status=HTTPStatus.BAD_REQUEST)
            return
        editor = self._paragraph_editor(unquote(parts[2]))
        if editor is None:
            return
        if len(parts) == 4 and parts[3] == "paragraphs":
            self.send_json(editor.list_paragraphs())
            return
        if len(parts) == 5 and parts[3] == "paragraph":
            result = editor.get_paragraph(unquote(parts[4]))
            self.send_json(result, status=HTTPStatus.NOT_FOUND if "error" in result else HTTPStatus.OK)
            return
        if len(parts) == 6 and parts[3] == "paragraph" and parts[5] == "history":
            self.send_json(editor.get_history(unquote(parts[4])))
            return
        self.send_json({"ok": False, "error": "unknown paragraph endpoint"}, status=HTTPStatus.NOT_FOUND)

    def _paragraph_body(self) -> dict | None:
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("payload must be an object")
            return data
        except Exception as exc:
            self.send_json({"ok": False, "error": f"invalid paragraph payload: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return None

    def _paragraph_mutation_editor(self, project_id: str) -> ParagraphEditor | None:
        editor = self._paragraph_editor(project_id)
        if editor and project_draft_payload(self.review_root, project_id).get("freshness", {}).get("stale"):
            self.send_json({"ok": False, "error": "draft is stale"}, status=HTTPStatus.CONFLICT)
            return None
        return editor

    def handle_paragraph_put(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[3] != "paragraph":
            self.send_json({"ok": False, "error": "invalid paragraph path"}, status=HTTPStatus.BAD_REQUEST)
            return
        editor = self._paragraph_mutation_editor(unquote(parts[2]))
        data = self._paragraph_body()
        if editor is None or data is None:
            return
        if not isinstance(data.get("text"), str):
            self.send_json({"ok": False, "error": "text is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        result = editor.update_paragraph(unquote(parts[4]), data["text"], str(data.get("reason") or ""))
        self.send_json(result, status=HTTPStatus.NOT_FOUND if "error" in result else HTTPStatus.OK)

    def handle_paragraph_post(self, path: str) -> None:
        parts = path.strip("/").split("/")
        editor = self._paragraph_mutation_editor(unquote(parts[2])) if len(parts) >= 3 else None
        data = self._paragraph_body()
        if editor is None or data is None:
            return
        if len(parts) == 4 and parts[3] == "paragraph":
            anchor, text = data.get("after_paragraph_id"), data.get("text")
            if not isinstance(anchor, str) or not isinstance(text, str) or not text.strip():
                self.send_json({"ok": False, "error": "after_paragraph_id and text are required"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = editor.insert_paragraph(anchor, text)
        elif len(parts) == 6 and parts[3] == "paragraph" and parts[5] == "rollback":
            result = editor.rollback(unquote(parts[4]), int(data.get("version", -1)))
        else:
            self.send_json({"ok": False, "error": "unknown paragraph endpoint"}, status=HTTPStatus.NOT_FOUND)
            return
        self.send_json(result, status=HTTPStatus.BAD_REQUEST if "error" in result else HTTPStatus.OK)

    def handle_paragraph_delete(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 5 or parts[3] != "paragraph":
            self.send_json({"ok": False, "error": "invalid paragraph path"}, status=HTTPStatus.BAD_REQUEST)
            return
        editor = self._paragraph_mutation_editor(unquote(parts[2]))
        data = self._paragraph_body()
        if editor is None or data is None:
            return
        result = editor.delete_paragraph(unquote(parts[4]), str(data.get("reason") or ""))
        self.send_json(result, status=HTTPStatus.NOT_FOUND if "error" in result else HTTPStatus.OK)

    def handle_project_matrix_outline_put(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            outline_style = str(payload.get("outline_style") or "").strip().casefold()
            if not outline_style.startswith("reference:"):
                outline_style_definition(outline_style)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": f"Invalid outline selection: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        stage = project / "01_matrix_outline"
        matrix = read_json_if_exists(stage / "literature_matrix.json")
        rows = matrix.get("rows") if isinstance(matrix, dict) else matrix
        if not isinstance(rows, list) or not rows:
            self.send_json(
                {"ok": False, "error": "No literature matrix is available. Confirm Discovery before choosing an outline."},
                status=HTTPStatus.CONFLICT,
            )
            return
        selected_at = now_utc()
        try:
            selected_outline = (
                reference_outline_document(stage, outline_style)
                if outline_style.startswith("reference:")
                else selected_outline_document(self.review_root, project_id, rows, outline_style, selected_at)
            )
        except ValueError as exc:
            self.send_json({"ok": False, "error": f"Invalid outline selection: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        (stage / "selected_outline.md").write_text(selected_outline, encoding="utf-8")
        write_json(
            stage / "selected_outline.meta.json",
            {
                "outline_style": outline_style,
                "selection_source": "user",
                "selected_at": selected_at,
                "matrix_synced_at": (matrix.get("sync") or {}).get("synced_at") if isinstance(matrix, dict) else None,
            },
        )
        self.send_json(
            {
                "ok": True,
                "project_id": project_id,
                "outline_style": outline_style,
                "selected_outline_md": selected_outline,
                "blueprint_pending": True,
            }
        )

    def handle_project_matrix_row_put(self, project_id: str, paper_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        stage = project / "01_matrix_outline"
        matrix_path = stage / "literature_matrix.json"
        matrix = read_json_if_exists(matrix_path)
        rows = matrix.get("rows") if isinstance(matrix, dict) else None
        if not project.exists() or not isinstance(rows, list):
            self.send_json({"ok": False, "error": "Literature matrix is not available."}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Invalid matrix update: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        row = next((item for item in rows if isinstance(item, dict) and str(item.get("paper_id")) == paper_id), None)
        if not isinstance(row, dict):
            self.send_json({"ok": False, "error": "Matrix paper was not found."}, status=HTTPStatus.NOT_FOUND)
            return
        main_content = str(payload.get("main_content") if "main_content" in payload else row.get("main_content") or "").strip()
        figure = payload.get("most_relevant_figure", row.get("most_relevant_figure"))
        if figure is not None and not isinstance(figure, dict):
            self.send_json({"ok": False, "error": "most_relevant_figure must be an object."}, status=HTTPStatus.BAD_REQUEST)
            return
        complete = bool(payload.get("mark_complete"))
        if complete and len(re.sub(r"\s+", "", main_content)) < 300:
            self.send_json({"ok": False, "error": "Add at least 300 characters of full-paper reading notes before marking this paper complete."}, status=HTTPStatus.CONFLICT)
            return
        row["main_content"] = main_content
        if isinstance(figure, dict):
            row["most_relevant_figure"] = figure
        row["matrix_status"] = "full_reading_complete" if complete else "needs_full_reading"
        synced_at = now_utc()
        matrix["rows"] = rows
        write_json(matrix_path, matrix)
        write_matrix_reading_artifacts(stage, matrix, synced_at)
        self.send_json({"ok": True, "project_id": project_id, "paper_id": paper_id, "row": row})

    def handle_reference_outline_upload(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        stage = project / "01_matrix_outline"
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        if not (stage / "literature_matrix.json").exists():
            self.send_json(
                {"ok": False, "error": "Confirm Discovery before creating a reference-derived outline."},
                status=HTTPStatus.CONFLICT,
            )
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            filename = Path(str(payload.get("filename") or "")).name
            suffix = Path(filename).suffix.casefold()
            if suffix not in {".pdf", ".docx", ".md", ".txt"}:
                raise ValueError("Upload a PDF, DOCX, Markdown, or text review document.")
            encoded = str(payload.get("content_base64") or "")
            raw = base64.b64decode(encoded, validate=True)
            if not raw:
                raise ValueError("Uploaded file is empty.")
            if len(raw) > 30 * 1024 * 1024:
                raise ValueError("Uploaded file exceeds the 30 MB limit.")
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Invalid reference upload: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        source_dir = project / "00_reference_templates"
        source_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^a-z0-9]+", "-", Path(filename).stem.casefold()).strip("-") or "reference-review"
        candidate_id = f"reference-{stem}"
        source_path = source_dir / f"{candidate_id}{suffix}"
        source_path.write_bytes(raw)
        output_path = source_dir / f"{candidate_id}.analysis.json"
        try:
            candidate = generate_reference_outline(self.review_root, project_id, source_path, output_path, candidate_id)
        except RuntimeError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        candidates_path = stage / "reference_outline_candidates.json"
        saved = read_json_if_exists(candidates_path) or {"project_id": project_id, "candidates": []}
        candidates = [item for item in saved.get("candidates", []) if item.get("candidate_id") != candidate_id]
        candidates.append(candidate)
        saved["project_id"] = project_id
        saved["candidates"] = candidates
        write_json(candidates_path, saved)
        self.send_json({"ok": True, "candidate": candidate})

    def discovery_path(self, project_id: str) -> Path:
        return self.review_root / "review-projects" / project_id / "00_discovery" / "combined_results_by_keyword.json"

    def handle_metadata_get(self, paper_id: str) -> None:
        path = self.metadata_dir / f"{paper_id}.metadata.json"
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "metadata not found")
            return
        self.send_file(path, "application/json; charset=utf-8")

    def handle_metadata_put(self, paper_id: str) -> None:
        path = self.metadata_dir / f"{paper_id}.metadata.json"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid json: {exc}")
            return
        if data.get("paper_id") != paper_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "paper_id mismatch")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        rebuild_registry(self.review_root)
        self.send_json({"ok": True})

    def handle_markdown_get(self, paper_id: str) -> None:
        meta = self.load_meta(paper_id)
        if not meta:
            self.send_error(HTTPStatus.NOT_FOUND, "metadata not found")
            return
        path_value = (meta.get("source_paths") or {}).get("markdown")
        if not path_value:
            self.send_error(HTTPStatus.NOT_FOUND, "markdown path missing")
            return
        path = safe_abs_path(path_value)
        if not path or not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "markdown not found")
            return
        self.send_file(path, "text/markdown; charset=utf-8")

    def handle_file(self, raw_path: str, paper_id: str = "") -> None:
        if not raw_path:
            self.send_error(HTTPStatus.BAD_REQUEST, "missing path")
            return
        path = safe_abs_path(raw_path)
        if not path:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid path")
            return
        if not path.is_absolute():
            resolved = None
            if paper_id:
                meta = self.load_meta(paper_id)
                md_value = ((meta or {}).get("source_paths") or {}).get("markdown")
                if md_value:
                    md_dir = Path(md_value).resolve().parent
                    candidate = (md_dir / path).resolve()
                    if candidate.exists():
                        resolved = candidate
            path = resolved or (self.review_root / path).resolve()
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "file not found")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        # Files exposed through this route are mutable project artifacts. SVG
        # saves deliberately replace the same PNG path, so browser caching can
        # hide the just-saved pixels until a later navigation or hard refresh.
        self.send_file(path, ctype, no_store=True)

    def load_meta(self, paper_id: str) -> dict | None:
        path = self.metadata_dir / f"{paper_id}.metadata.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_file(self, path: Path, content_type: str, *, no_store: bool = False) -> None:
        try:
            size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            if no_store or (
                content_type.startswith("text/html")
                or content_type.startswith("text/css")
                or "javascript" in content_type
            ):
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except BrokenPipeError:
            pass


def value_of(field):
    if isinstance(field, dict) and "value" in field:
        return field.get("value")
    return field


def safe_abs_path(raw: str) -> Path | None:
    raw = unquote(raw)
    if "\x00" in raw:
        return None
    # Keep spaces and unicode; only normalize separators.
    raw = posixpath.normpath(raw)
    return Path(raw).expanduser().resolve() if raw.startswith("/") else Path(raw)


PROJECT_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,79})")


def build_dashboard_query_plan(topic: str, keywords: str) -> dict[str, object]:
    """Build the JSON boundary consumed by discover.py from the dashboard input."""
    discovery = discovery_module()
    intent = discovery.parse_topic_intent(topic)
    unresolved_surfaces = list(intent["unresolved_concepts"])
    supplied_keywords = discovery.split_keywords(keywords)
    usable_keywords = [
        keyword
        for keyword in supplied_keywords
        if keyword.casefold() not in discovery.GENERIC_INSTRUCTION_KEYWORDS
        and not any(discovery.contains_phrase(surface, keyword) for surface in unresolved_surfaces)
    ]

    plan_keywords: list[dict[str, str]] = []
    seen_keywords: set[str] = set()
    for keyword in usable_keywords:
        key = keyword.casefold()
        if key in seen_keywords:
            continue
        seen_keywords.add(key)
        plan_keywords.append(
            {
                "keyword": keyword,
                "category": discovery.classify_keyword(keyword),
                "source": "user",
                "reason": "Provided in the dashboard keyword field.",
            }
        )
    for item in discovery.infer_keywords(topic, usable_keywords, unresolved_surfaces):
        keyword = str(item["keyword"])
        key = keyword.casefold()
        if key in seen_keywords:
            continue
        seen_keywords.add(key)
        plan_keywords.append(
            {
                "keyword": keyword,
                "category": str(item["category"]),
                "source": "agent",
                "reason": str(item.get("reason") or "Derived from the topic."),
            }
        )

    return {
        "schema_version": 1,
        "planner": "dashboard_deterministic",
        "topic": topic,
        "resolved_concepts": [],
        "unresolved_concepts": [
            {
                "surface": surface,
                "reason": "The topic does not provide enough context to expand this abbreviation confidently.",
            }
            for surface in unresolved_surfaces
        ],
        "keywords": plan_keywords,
        "filters": dict(intent["filters"]),
        "group_by": list(intent["group_by"]),
    }


def build_and_validate_query_plan(topic: str, keywords: str) -> dict[str, object]:
    """Generate a plan and apply discover.py's authoritative validation rules."""
    discovery = discovery_module()
    plan = build_dashboard_query_plan(topic, keywords)
    return discovery.validate_query_plan(plan, topic)


def write_query_plan(project: Path, plan: dict[str, object]) -> Path:
    path = project / "00_discovery" / "query_plan.draft.json"
    discovery_module().write_json(path, plan)
    return path


def validate_new_project_id(project_id: object) -> tuple[str | None, str | None]:
    normalized = str(project_id or "").strip()
    if not normalized:
        return None, "Project ID is required."
    if not PROJECT_ID_RE.fullmatch(normalized):
        return None, "Project ID must use lowercase letters, numbers, and hyphens only."
    return normalized, None


def validate_discovery_start_payload(
    payload: object, project_exists: callable
) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "Discovery payload must be a JSON object."
    project_id, error = validate_new_project_id(payload.get("project_id"))
    if error or project_id is None:
        return None, error
    topic = re.sub(r"\s+", " ", str(payload.get("topic") or "").strip())
    if not topic:
        return None, "Topic is required."
    if len(topic) > 500:
        return None, "Topic must be 500 characters or fewer."
    if project_exists(project_id):
        return None, "A project with this ID already exists."
    value: dict[str, object] = {
        "project_id": project_id,
        "topic": topic,
        "web_search": bool(payload.get("web_search", False)),
    }
    keywords = re.sub(r"\s+", " ", str(payload.get("keywords") or "").strip())
    if keywords:
        value["keywords"] = keywords[:500]
    return value, None


def build_discovery_command(
    review_root: Path,
    project_id: str,
    topic: str,
    web_search: bool,
    keywords: str = "",
    query_plan_path: Path | None = None,
) -> list[str]:
    script = discovery_script_path()
    plan_path = query_plan_path or (
        review_root / "review-projects" / project_id / "00_discovery" / "query_plan.draft.json"
    )
    command = [
        sys.executable,
        str(script),
        "--review-root",
        str(review_root),
        "--project-id",
        project_id,
        "--topic",
        topic,
        "--query-plan",
        str(plan_path),
    ]
    if keywords:
        command.extend(["--keywords", keywords])
    if web_search:
        command.append("--web-search")
    return command


def start_discovery(
    review_root: Path,
    payload: dict[str, object],
    runner: callable | None = None,
) -> dict[str, object]:
    project_id = str(payload["project_id"])
    project = review_root / "review-projects" / project_id
    try:
        query_plan = build_and_validate_query_plan(
            str(payload["topic"]),
            str(payload.get("keywords") or ""),
        )
    except Exception as exc:
        return {
            "ok": False,
            "project_id": project_id,
            "error": f"Query plan needs clarification: {exc}",
        }
    project.mkdir(parents=True, exist_ok=False)
    query_plan_path = write_query_plan(project, query_plan)
    command = build_discovery_command(
        review_root,
        project_id,
        str(payload["topic"]),
        bool(payload.get("web_search")),
        str(payload.get("keywords") or ""),
        query_plan_path,
    )
    run = runner or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=180))
    try:
        result = run(command)
    except subprocess.TimeoutExpired:
        return {"ok": False, "project_id": project_id, "error": "Discovery timed out after 180 seconds."}
    output = "\n".join(part for part in [getattr(result, "stdout", ""), getattr(result, "stderr", "")] if part).strip()
    if getattr(result, "returncode", 1) != 0:
        return {
            "ok": False,
            "project_id": project_id,
            "error": "Discovery failed.",
            "output": "\n".join(output.splitlines()[-20:]),
        }
    return {
        "ok": True,
        "project_id": project_id,
        "output": output,
        "query_plan_path": str(query_plan_path),
    }


def optional_year(value: object) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    year = int(value)
    if year < 1600 or year > datetime_year() + 1:
        raise ValueError(f"Year must be between 1600 and {datetime_year() + 1}.")
    return year


def datetime_year() -> int:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).year


def _save_literature_job(review_root: Path, job_type: str, state: dict[str, object]) -> None:
    workflow_store(review_root).save_job(_LIBRARY_WORKFLOW_PROJECT, job_type, state)


def current_literature_job(review_root: Path, job_type: str) -> dict[str, object] | None:
    store = workflow_store(review_root)
    state = store.load_job(_LIBRARY_WORKFLOW_PROJECT, job_type)
    if state and state.get("status") in {"queued", "running"}:
        thread = _LITERATURE_THREADS.get(job_type)
        if thread is None or not thread.is_alive():
            state.update(
                {
                    "status": "interrupted",
                    "error": "The dashboard process stopped before this job finished. Start it again.",
                    "finished_at": now_utc(),
                }
            )
            store.save_job(_LIBRARY_WORKFLOW_PROJECT, job_type, state)
    return state


def run_literature_search_job(review_root: Path, state: dict[str, object], email: str) -> None:
    job_type = "literature-search"

    def on_flow_started(flow_run_id: str) -> None:
        state["prefect_flow_run_id"] = flow_run_id
        _save_literature_job(review_root, job_type, state)

    def action() -> dict[str, object]:
        state.update({"status": "running", "progress_current": 0, "error": ""})
        _save_literature_job(review_root, job_type, state)
        load_dotenv_if_present(review_root)
        candidates = search_crossref(
            str(state.get("topic") or ""),
            year_from=state.get("year_from") if isinstance(state.get("year_from"), int) else None,
            year_to=state.get("year_to") if isinstance(state.get("year_to"), int) else None,
            limit=int(state.get("limit") or 20),
            mailto=email or os.environ.get("CROSSREF_MAILTO") or os.environ.get("UNPAYWALL_EMAIL") or "",
        )
        state.update(
            {
                "status": "completed",
                "progress_current": 1,
                "candidate_count": len(candidates),
                "candidates": candidates,
                "finished_at": now_utc(),
            }
        )
        _save_literature_job(review_root, job_type, state)
        return {"candidate_count": len(candidates)}

    try:
        if prefect_orchestration_enabled():
            try:
                result = run_literature_acquisition_with_prefect(
                    review_root,
                    "search",
                    1,
                    action,
                    on_flow_started=on_flow_started,
                )
                state["prefect_task_run_id"] = result.get("prefect_task_run_id")
                state["orchestration_mode"] = "prefect"
                _save_literature_job(review_root, job_type, state)
            except Exception as prefect_exc:
                state["orchestration_mode"] = "local_fallback"
                state["prefect_error"] = f"{type(prefect_exc).__name__}: {prefect_exc}"
                action()
        else:
            state["orchestration_mode"] = "local"
            action()
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": now_utc(),
            }
        )
        _save_literature_job(review_root, job_type, state)
    finally:
        with _LITERATURE_JOB_LOCK:
            _LITERATURE_THREADS.pop(job_type, None)


def run_literature_download_job(
    review_root: Path,
    state: dict[str, object],
    candidates: list[dict[str, object]],
    email: str,
) -> None:
    job_type = "literature-download"

    def on_flow_started(flow_run_id: str) -> None:
        state["prefect_flow_run_id"] = flow_run_id
        _save_literature_job(review_root, job_type, state)

    def action() -> dict[str, object]:
        state.update({"status": "running", "error": ""})
        _save_literature_job(review_root, job_type, state)
        results: list[dict[str, object]] = []
        successes = 0
        failures = 0
        for index, candidate in enumerate(candidates, start=1):
            state.update(
                {
                    "progress_current": index - 1,
                    "current_candidate_id": candidate.get("candidate_id"),
                    "current_title": candidate.get("title"),
                }
            )
            _save_literature_job(review_root, job_type, state)
            try:
                result = acquire_candidate(review_root, candidate, email=email)
            except Exception as exc:
                result = {
                    "candidate_id": candidate.get("candidate_id"),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            result["title"] = candidate.get("title")
            results.append(result)
            if result.get("status") in {"downloaded", "already_in_library", "duplicate_file"}:
                successes += 1
            else:
                failures += 1
            state.update(
                {
                    "progress_current": index,
                    "success_count": successes,
                    "failed_count": failures,
                    "results": results,
                }
            )
            _save_literature_job(review_root, job_type, state)
        state.update(
            {
                "status": "completed",
                "current_candidate_id": "",
                "current_title": "",
                "finished_at": now_utc(),
            }
        )
        _save_literature_job(review_root, job_type, state)
        return {"success_count": successes, "failed_count": failures}

    try:
        if prefect_orchestration_enabled():
            try:
                result = run_literature_acquisition_with_prefect(
                    review_root,
                    "download",
                    len(candidates),
                    action,
                    on_flow_started=on_flow_started,
                )
                state["prefect_task_run_id"] = result.get("prefect_task_run_id")
                state["orchestration_mode"] = "prefect"
                _save_literature_job(review_root, job_type, state)
            except Exception as prefect_exc:
                state["orchestration_mode"] = "local_fallback"
                state["prefect_error"] = f"{type(prefect_exc).__name__}: {prefect_exc}"
                action()
        else:
            state["orchestration_mode"] = "local"
            action()
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": now_utc(),
            }
        )
        _save_literature_job(review_root, job_type, state)
    finally:
        with _LITERATURE_JOB_LOCK:
            _LITERATURE_THREADS.pop(job_type, None)


def rebuild_registry(review_root: Path) -> None:
    meta_dir = review_root / "review-library" / "metadata" / "papers"
    registry = review_root / "review-library" / "registry" / "papers.jsonl"
    rows = []
    for path in sorted(meta_dir.glob("*.metadata.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_paths = meta.get("source_paths") or {}
        rows.append(
            {
                "paper_id": meta.get("paper_id"),
                "slug": meta.get("slug"),
                "title": value_of(meta.get("title")),
                "authors": value_of(meta.get("authors")),
                "year": value_of(meta.get("year")),
                "journal": value_of(meta.get("journal")),
                "doi": value_of(meta.get("doi")),
                "source_pdf": source_paths.get("pdf"),
                "markdown_path": source_paths.get("markdown"),
                "content_list_path": source_paths.get("content_list"),
                "metadata_path": str(path),
                "parse_status": "done",
                "human_review_status": (meta.get("human_review") or {}).get("status"),
                "needs_human_check": (meta.get("quality") or {}).get("needs_human_check"),
            }
        )
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def now_utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def section_blueprint_script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "review-section-blueprint" / "scripts" / "init_section_blueprint.py"


def reference_outline_script_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "review-reference-outline-template"
        / "scripts"
        / "analyze_reference_review.py"
    )


def figure_redraw_script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "review-figure-style-redraw" / "scripts" / "redraw_figures.py"


def invalidate_redrawn_output_for_source_change(
    project: Path,
    figure_id: str,
    previous_source: str,
    selected_source: str,
) -> bool:
    """Keep a redraw made from an old Stage 6 choice out of Stage 7."""
    manifest_path = project / "03_figure_redraw" / "redrawn_figure_manifest.json"
    manifest = read_json_if_exists(manifest_path) or {}
    rows = manifest.get("figures") if isinstance(manifest, dict) else []
    if not isinstance(rows, list):
        return False
    changed = False
    for row in rows:
        if not isinstance(row, dict) or str(row.get("figure_id") or "") != figure_id:
            continue
        output_fields = (
            "redrawn_image",
            "output_path",
            "redrawn_path",
            "output_image_path",
            "image_path",
            "path",
            "rejected_preview_image",
            "editable_svg",
        )
        superseded = {
            field: row.get(field)
            for field in output_fields
            if row.get(field)
        }
        if superseded:
            row["superseded_output"] = {
                "reason": "stage6_source_selection_changed",
                "previous_source_image": previous_source,
                "replaced_at": now_utc(),
                **superseded,
            }
        for field in output_fields:
            row.pop(field, None)
        row.pop("human_approval", None)
        row.pop("output_disposition", None)
        row["source_image"] = selected_source
        row["status"] = "source_changed"
        row["notes"] = "Stage 6 selected a different source candidate; redraw this figure again."
        changed = True
        break
    if changed:
        write_json(manifest_path, {"project_id": manifest.get("project_id"), "figures": rows})
    return changed


def sync_selected_candidate_for_redraw(project: Path, paper_id: str, candidate: dict[str, Any]) -> None:
    """Promote a Figure Review choice into the redraw skill's actual input list."""
    candidates_path = project / "02_section_drafting" / "figure_candidates.json"
    figures = read_json_if_exists(candidates_path) or []
    if isinstance(figures, dict):
        figures = figures.get("figures") or figures.get("candidates") or []
    if not isinstance(figures, list):
        figures = []
    existing = next(
        (row for row in figures if isinstance(row, dict) and str(row.get("paper_id") or "") == paper_id),
        {},
    )
    previous_source = str(
        existing.get("source_image_path")
        or existing.get("source_path")
        or existing.get("image_path")
        or ""
    )
    selected_source = str(
        candidate.get("source_image_path")
        or candidate.get("source_path")
        or candidate.get("image_path")
        or ""
    )
    selected = {**existing, **candidate}
    selected["paper_id"] = paper_id
    selected["figure_id"] = str(existing.get("figure_id") or f"{paper_id}-F01")
    selected["recommended_action"] = "redraw"
    selected["manuscript_selected"] = True
    selected["resolution_status"] = "ready" if selected.get("source_image_path") else "needs_source_resolution"
    selected["needs_human_check"] = True
    if not selected.get("target_paragraph_id"):
        sections = (read_json_if_exists(project / "02_section_drafting" / "section_drafts.json") or {}).get("sections", [])
        for section in sections if isinstance(sections, list) else []:
            for paragraph in section.get("paragraphs") or []:
                paper_ids = paragraph.get("cited_paper_ids") or [paragraph.get("paper_id")]
                if paper_id not in {str(value) for value in paper_ids if value}:
                    continue
                selected["paragraph_id"] = str(paragraph.get("paragraph_id") or "")
                selected["target_paragraph_id"] = str(paragraph.get("paragraph_id") or "")
                selected["section_id"] = str(section.get("section_id") or "")
                selected["section_heading"] = str(section.get("heading") or "")
                break
            if selected.get("target_paragraph_id"):
                break
    if not selected.get("target_paragraph_id"):
        raise RuntimeError("The selected candidate has no matching manuscript paragraph anchor.")
    source_changed = bool(
        previous_source
        and selected_source
        and os.path.normcase(os.path.abspath(previous_source))
        != os.path.normcase(os.path.abspath(selected_source))
    )
    figures = [row for row in figures if not (isinstance(row, dict) and str(row.get("paper_id") or "") == paper_id)]
    figures.append(selected)
    write_json(candidates_path, figures)
    if source_changed:
        invalidate_redrawn_output_for_source_change(
            project,
            selected["figure_id"],
            previous_source,
            selected_source,
        )


def generate_reference_outline(
    review_root: Path,
    project_id: str,
    source_path: Path,
    output_path: Path,
    candidate_id: str,
) -> dict[str, Any]:
    script = reference_outline_script_path()
    matrix_path = review_root / "review-projects" / project_id / "01_matrix_outline" / "literature_matrix.json"
    if not script.exists():
        raise RuntimeError(f"Reference outline skill is missing: {script}")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--input",
                str(source_path),
                "--matrix",
                str(matrix_path),
                "--output",
                str(output_path),
                "--project-id",
                project_id,
                "--candidate-id",
                candidate_id,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Reference outline analysis timed out.") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Reference outline analysis failed.").strip()
        raise RuntimeError(message[-2000:])
    candidate = read_json_if_exists(output_path)
    if not isinstance(candidate, dict) or not candidate.get("outline_md"):
        raise RuntimeError("Reference outline analysis did not create a usable candidate.")
    return candidate


def reference_outline_document(stage: Path, outline_style: str) -> str:
    candidate_id = outline_style.removeprefix("reference:")
    data = read_json_if_exists(stage / "reference_outline_candidates.json") or {}
    candidate = next(
        (item for item in data.get("candidates", []) if isinstance(item, dict) and item.get("candidate_id") == candidate_id),
        None,
    )
    if not isinstance(candidate, dict) or not str(candidate.get("outline_md") or "").strip():
        raise ValueError("Reference-derived outline candidate was not found.")
    return str(candidate["outline_md"])


def regenerate_section_blueprint(review_root: Path, project_id: str) -> dict[str, Any]:
    script = section_blueprint_script_path()
    if not script.exists():
        raise RuntimeError(f"Blueprint initializer is missing: {script}")
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--review-root", str(review_root), "--project-id", project_id],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Blueprint initializer timed out.") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Blueprint initializer failed.").strip()
        raise RuntimeError(details[-2000:])
    stage = review_root / "review-projects" / project_id / "01_matrix_outline"
    blueprint_path = stage / "section_blueprint.json"
    blueprint = read_json_if_exists(blueprint_path) or {}
    return {
        "status": "generated",
        "generated_at": now_utc(),
        "section_count": len(blueprint.get("sections") or []) if isinstance(blueprint, dict) else 0,
        "source_outline": str(stage / "selected_outline.md"),
    }


def run_project_script(script: Path, review_root: Path, project_id: str, timeout: int = 180, extra: list[str] | None = None) -> str:
    if not script.exists():
        raise RuntimeError(f"Required workflow script is missing: {script}")
    command = [sys.executable, str(script), "--review-root", str(review_root), "--project-id", project_id]
    if extra:
        command.extend(extra)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Workflow script timed out: {script.name}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "workflow script failed").strip()
        raise RuntimeError(details[-2000:])
    return (result.stdout or result.stderr or "").strip()


def refresh_final_overview_chart(review_root: Path, project_id: str, draft_path: Path) -> str:
    """Build only the single full-review overview chart for the final manuscript."""
    chart_script = review_root / "skills" / "review-outline-summary-chart" / "scripts" / "generate_review_summary_chart.py"
    return run_project_script(
        chart_script,
        review_root,
        project_id,
        timeout=300,
        extra=["--scope", "full", "--input-markdown", str(draft_path)],
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def docx_export_is_current(stage: Path, docx_path: Path) -> bool:
    """Only expose a Word download when it was made from the current final draft."""
    draft_path = stage / "final_draft.md"
    manifest = read_json_if_exists(stage / "docx_export.json") or {}
    return bool(
        docx_path.is_file()
        and draft_path.is_file()
        and isinstance(manifest, dict)
        and manifest.get("output_path") == docx_path.name
        and manifest.get("draft_sha256") == sha256_file(draft_path)
    )


def matrix_rows(project: Path) -> list[dict[str, Any]]:
    matrix = read_json_if_exists(project / "01_matrix_outline" / "literature_matrix.json") or {}
    rows = matrix.get("rows") if isinstance(matrix, dict) else matrix
    return [row for row in rows if isinstance(row, dict) and row.get("paper_id")] if isinstance(rows, list) else []


def brief_evidence(row: dict[str, Any]) -> str:
    text = re.sub(r"\s+", " ", str(row.get("main_content") or row.get("abstract") or "")).strip()
    if not text:
        return "The available metadata does not contain enough full-text evidence; this paper requires a source check before final wording."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(sentences[:2]).strip()[:1200]


def regenerate_section_drafting(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "02_section_drafting"
    tasks = read_json_if_exists(stage / "section_tasks.json")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeError("Blueprint tasks are missing. Return to Blueprint and execute the handoff first.")
    rows = {str(row["paper_id"]): row for row in matrix_rows(project)}
    if not rows:
        raise RuntimeError("Literature matrix is empty. Confirm Discovery and build Matrix before drafting sections.")

    # Review prose is produced by the section-writing skill, not by a server-side
    # title/abstract concatenation fallback. The latter created bibliography-like
    # output that looked complete but was not a review.
    scripts = review_root / "skills" / "review-section-drafting-figure-picking" / "scripts"
    run_project_script(scripts / "generate_section_drafts.py", review_root, project_id, timeout=900)
    drafts = read_json_if_exists(stage / "section_drafts.json") or {}
    generated_sections = drafts.get("sections") if isinstance(drafts, dict) else []
    if not isinstance(generated_sections, list) or not generated_sections:
        raise RuntimeError("Section-writing skill did not create usable section drafts.")
    paragraph_by_paper: dict[str, dict[str, str]] = {}
    for section in generated_sections:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if isinstance(paragraph, dict):
                paper_ids = paragraph.get("cited_paper_ids") or [paragraph.get("paper_id")]
                for paper_id in paper_ids:
                    if not paper_id:
                        continue
                    paragraph_by_paper.setdefault(str(paper_id), {
                        "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                        "target_paragraph_id": str(paragraph.get("paragraph_id") or ""),
                        "section_id": str(section.get("section_id") or ""),
                        "section_heading": str(section.get("heading") or ""),
                    })
    run_project_script(scripts / "build_paper_figure_inventory.py", review_root, project_id)
    run_project_script(scripts / "select_initial_figure_candidates.py", review_root, project_id)
    figures = read_json_if_exists(stage / "figure_candidates.json") or []
    if isinstance(figures, dict):
        figures = figures.get("figures") or figures.get("candidates") or []
    if not isinstance(figures, list) or not figures:
        raise RuntimeError("No source figure candidates were generated from MinerU output.")
    figure_counts: dict[str, int] = {}
    for figure in figures:
        if not isinstance(figure, dict):
            continue
        paper_id = str(figure.get("paper_id") or "unknown")
        figure_counts[paper_id] = figure_counts.get(paper_id, 0) + 1
        # A stable ID prevents a one-paper redraw from overwriting another
        # figure's manifest row after a Figure Review selection.
        figure.setdefault("figure_id", f"{re.sub(r'[^A-Za-z0-9_-]+', '-', paper_id)}-F{figure_counts[paper_id]:02d}")
        if paper_id in paragraph_by_paper:
            figure.update(paragraph_by_paper[paper_id])
    write_json(stage / "figure_candidates.json", figures)
    section_handoff = stage / "section_handoff.json"
    ensure_stage_handoff(
        section_handoff,
        "blueprint",
        [project / "01_matrix_outline" / "section_blueprint.json"],
    )
    record_stage_outputs(
        section_handoff,
        [
            stage / "section_drafts.json",
            stage / "section_drafts.md",
            stage / "figure_candidates.json",
            stage / "paper_figure_candidates.json",
        ],
        "sections",
    )
    return {"section_count": len(generated_sections), "figure_candidate_count": len(figures)}


def regenerate_figures(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "02_section_drafting"
    freshness = section_candidate_freshness(draft_stage)
    if freshness["stale"]:
        raise RuntimeError("Blueprint has changed. Regenerate Sections before redrawing figures.")
    figures = read_json_if_exists(draft_stage / "figure_candidates.json")
    if not isinstance(figures, list) or not figures:
        raise RuntimeError("No figure candidates are available. Regenerate Sections first.")
    per_paper = read_json_if_exists(draft_stage / "paper_figure_candidates.json") or {}
    reviewable = [
        str(row.get("paper_id")) for row in per_paper.get("papers", [])
        if isinstance(row, dict) and row.get("paper_id") and row.get("candidates")
    ]
    reviews = (read_json_if_exists(draft_stage / "human_figure_review.json") or {}).get("papers", {})
    missing_reviews = [paper_id for paper_id in reviewable if paper_id not in reviews]
    if missing_reviews:
        raise RuntimeError(f"Complete Figure Review before redrawing ({len(missing_reviews)} paper selections remaining).")
    missing_selected = [
        paper_id for paper_id in reviewable
        if not any(
            isinstance(row, dict)
            and str(row.get("paper_id") or "") == paper_id
            and bool(row.get("manuscript_selected"))
            for row in figures
        )
    ]
    if missing_selected:
        raise RuntimeError(f"Selected Figure Review candidates are missing from the redraw input: {', '.join(missing_selected)}.")
    manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    existing_rows = manifest.get("figures", []) if isinstance(manifest, dict) else []
    existing_by_figure_id = {
        str(row.get("figure_id") or ""): row
        for row in existing_rows
        if isinstance(row, dict) and row.get("figure_id")
    }
    selected_figures = [row for row in figures if isinstance(row, dict) and row.get("manuscript_selected")]
    target_figures = selected_figures or [row for row in figures if isinstance(row, dict)]
    missing_figure_ids = []
    for figure in target_figures:
        figure_id = str(figure.get("figure_id") or "")
        previous = existing_by_figure_id.get(figure_id)
        previous_output = Path(str((previous or {}).get("redrawn_image") or ""))
        if not (
            previous
            and previous.get("status") == "redrawn"
            and previous_output.is_file()
        ):
            missing_figure_ids.append(figure_id)
    figures_handoff = project / "03_figure_redraw" / "figures_handoff.json"
    ensure_stage_handoff(
        figures_handoff,
        "figure-review",
        [
            draft_stage / "section_drafts.json",
            draft_stage / "figure_candidates.json",
            draft_stage / "paper_figure_candidates.json",
            draft_stage / "human_figure_review.json",
        ],
    )
    if missing_figure_ids:
        for figure_id in missing_figure_ids:
            redraw_current_figure(review_root, project_id, figure_id)
        manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    redrawn = [row for row in manifest.get("figures", []) if isinstance(row, dict) and row.get("status") == "redrawn"] if isinstance(manifest, dict) else []
    if not redrawn:
        raise RuntimeError("No figure was redrawn successfully. Resolve source images in Figure Review.")
    redraw_outputs = [project / "03_figure_redraw" / "redrawn_figure_manifest.json"]
    redraw_outputs.extend(
        Path(str(row.get("redrawn_image") or ""))
        for row in redrawn
        if row.get("redrawn_image")
    )
    record_stage_outputs(figures_handoff, redraw_outputs, "figures")
    write_stage_handoff(
        project / "04_first_draft" / "draft_handoff.json",
        "figures",
        [draft_stage / "section_drafts.json", draft_stage / "human_figure_review.json", project / "03_figure_redraw" / "redrawn_figure_manifest.json"],
    )
    return {
        "redrawn_count": len(redrawn),
        "reused_count": len(target_figures) - len(missing_figure_ids),
        "generated_count": len(missing_figure_ids),
    }


def regenerate_section_tasks(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    if not project.is_dir():
        raise FileNotFoundError("Project not found.")
    blueprint_path = project / "01_matrix_outline" / "section_blueprint.json"
    blueprint = read_json_if_exists(blueprint_path)
    sections = blueprint.get("sections") if isinstance(blueprint, dict) else None
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("No Blueprint sections are available. Select an outline and generate Blueprint first.")
    tasks: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict) or not section.get("section_id"):
            continue
        claims = section.get("review_claims") or []
        tasks.append(
            {
                "section_id": str(section["section_id"]),
                "heading": str(section.get("title") or section["section_id"]),
                "core_argument": str(section.get("section_thesis") or section.get("review_problem") or ""),
                "allowed_papers": [str(paper_id) for paper_id in section.get("major_papers") or []],
                "must_cover_points": [
                    str(claim.get("claim") or "")
                    for claim in claims
                    if isinstance(claim, dict) and claim.get("claim")
                ],
                "avoid_points": [str(item) for item in section.get("avoid_patterns") or []],
                "figure_need": section.get("figure_or_table_needs") or [],
                "source_blueprint": str(blueprint_path),
                "created_at": now_utc(),
            }
        )
    if not tasks:
        raise RuntimeError("Blueprint contains no usable sections.")
    stage = project / "02_section_drafting"
    generated_at = now_utc()
    write_json(stage / "section_tasks.json", tasks)
    write_stage_handoff(
        stage / "section_handoff.json",
        "blueprint",
        [blueprint_path],
        metadata={
            "source_blueprint": str(blueprint_path),
            "task_count": len(tasks),
            "section_ids": [task["section_id"] for task in tasks],
        },
    )
    (stage / "section_tasks_handoff.md").write_text(
        "# Blueprint to Sections Handoff\n\n"
        f"Generated {len(tasks)} section tasks from `01_matrix_outline/section_blueprint.json` at {generated_at}.\n",
        encoding="utf-8",
    )
    return {"task_count": len(tasks), "section_ids": [task["section_id"] for task in tasks]}


def regenerate_figures_and_first_draft(review_root: Path, project_id: str) -> dict[str, Any]:
    """Keep the Figures-to-Draft action atomic from the user's perspective."""
    figures = regenerate_figures(review_root, project_id)
    draft = regenerate_first_draft(review_root, project_id)
    return {"figures": figures, "draft": draft}


_MECHANISM_ARROW_PROFILE_PATTERN = re.compile(
    r"\b(?:mechanism|mechanistic|catalytic\s+cycle|photocatalytic)\b|"
    r"反应机理|机理图|催化循环|光催化循环",
    re.IGNORECASE,
)
_DENSE_SCOPE_PROFILE_PATTERN = re.compile(
    r"\b(?:reaction\s+scope|substrate\s+scope|scope\s+(?:summary|for|of)|examples?)\b|"
    r"反应范围|底物范围|反应底物",
    re.IGNORECASE,
)
_COMPLEX_MULTIPANEL_PROFILE_PATTERN = re.compile(
    r"\b(?:strateg(?:y|ies)|background|overview|comparison|rearrangement|applications?|"
    r"protocols?|routes?|methodology|stud(?:y|ies)|kinetic\s+investigations?|"
    r"(?:proposed\s+)?catalytic\s+cycles?|total\s+synthesis)\b|"
    r"策略|背景|概览|对比|重排|应用|路线",
    re.IGNORECASE,
)
_HOLLOW_COLOR_FILL_PROFILE_PATTERN = re.compile(
    r"\b(?:strateg(?:y|ies)|traditional\s+protocols?|research\s+background)\b",
    re.IGNORECASE,
)


def use_mechanism_arrow_straighten_profile(candidate: dict[str, Any]) -> bool:
    """Identify a mechanism/catalytic-cycle figure eligible for strict arrow-only editing."""
    explicit = str(candidate.get("edit_profile") or candidate.get("redraw_profile") or "").strip()
    if explicit == "mechanism-arrow-straighten":
        return True
    fields = (
        "source_label",
        "source_caption_text",
        "what_it_shows",
        "recommended_action",
        "source_type",
    )
    return bool(_MECHANISM_ARROW_PROFILE_PATTERN.search(" ".join(str(candidate.get(field) or "") for field in fields)))


def use_source_faithful_scope_render(candidate: dict[str, Any]) -> bool:
    """Avoid generative redraw for dense multi-product scope schemes."""
    fields = (
        "source_label",
        "source_caption_text",
        "title",
        "section_heading",
        "recommended_action",
    )
    return bool(_DENSE_SCOPE_PROFILE_PATTERN.search(" ".join(str(candidate.get(field) or "") for field in fields)))


def use_source_faithful_multipanel_render(candidate: dict[str, Any]) -> bool:
    """Avoid model reconstruction of complex multi-panel chemistry graphics."""
    fields = (
        "source_label",
        "source_caption_text",
        "title",
        "what_it_shows",
        "section_heading",
    )
    return bool(_COMPLEX_MULTIPANEL_PROFILE_PATTERN.search(" ".join(str(candidate.get(field) or "") for field in fields)))


def use_hollow_color_fill_render(candidate: dict[str, Any]) -> bool:
    fields = ("source_label", "source_caption_text", "title", "what_it_shows")
    return bool(_HOLLOW_COLOR_FILL_PROFILE_PATTERN.search(" ".join(str(candidate.get(field) or "") for field in fields)))


def _svg_number(value: object) -> str:
    number = float(value)
    if not number == number or abs(number) > 1_000_000:
        raise ValueError("SVG coordinate is invalid")
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _svg_points(points: object, scale: float) -> list[tuple[float, float]]:
    if not isinstance(points, list) or len(points) < 2:
        return []
    parsed: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, dict):
            return []
        parsed.append((float(point.get("x") or 0) * scale, float(point.get("y") or 0) * scale))
    return parsed


def build_full_image_vector_svg(base_path: Path, operations: list[dict[str, object]]) -> str:
    """Trace every non-white source pixel into SVG paths, without chemistry OCR/reconstruction."""
    with Image.open(base_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        source_rgb = background.convert("RGB")
        scale = min(1.0, FULL_SVG_MAX_DIMENSION / max(source_rgb.size))
        if scale < 1.0:
            traced = source_rgb.resize(
                (max(1, round(source_rgb.width * scale)), max(1, round(source_rgb.height * scale))),
                Image.Resampling.LANCZOS,
            )
        else:
            traced = source_rgb
        width, height = traced.size
        pixels = traced.load()
        foreground = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                red, green, blue = pixels[x, y]
                if not (
                    red >= FULL_SVG_WHITE_THRESHOLD
                    and green >= FULL_SVG_WHITE_THRESHOLD
                    and blue >= FULL_SVG_WHITE_THRESHOLD
                ):
                    foreground[y * width + x] = 1
        visited = bytearray(width * height)
        vector_objects: list[str] = []
        vector_index = 0
        for y in range(height):
            for x in range(width):
                start = y * width + x
                if not foreground[start] or visited[start]:
                    continue
                visited[start] = 1
                pending = [start]
                cursor = 0
                rows: dict[tuple[int, str], list[int]] = {}
                min_x = max_x = x
                min_y = max_y = y
                while cursor < len(pending):
                    current = pending[cursor]
                    cursor += 1
                    current_y, current_x = divmod(current, width)
                    red, green, blue = pixels[current_x, current_y]
                    color = tuple(
                        min(255, round(channel / FULL_SVG_COLOR_STEP) * FULL_SVG_COLOR_STEP)
                        for channel in (red, green, blue)
                    )
                    hex_color = "#%02x%02x%02x" % color
                    rows.setdefault((current_y, hex_color), []).append(current_x)
                    min_x, max_x = min(min_x, current_x), max(max_x, current_x)
                    min_y, max_y = min(min_y, current_y), max(max_y, current_y)
                    for delta_y in (-1, 0, 1):
                        neighbor_y = current_y + delta_y
                        if neighbor_y < 0 or neighbor_y >= height:
                            continue
                        for delta_x in (-1, 0, 1):
                            if not delta_x and not delta_y:
                                continue
                            neighbor_x = current_x + delta_x
                            if neighbor_x < 0 or neighbor_x >= width:
                                continue
                            neighbor = neighbor_y * width + neighbor_x
                            if foreground[neighbor] and not visited[neighbor]:
                                visited[neighbor] = 1
                                pending.append(neighbor)
                paths_by_color: dict[str, list[str]] = {}
                for (row_y, hex_color), xs in rows.items():
                    xs.sort()
                    run_start = xs[0]
                    previous = xs[0]
                    for current_x in xs[1:]:
                        if current_x == previous + 1:
                            previous = current_x
                            continue
                        paths_by_color.setdefault(hex_color, []).append(
                            f"M{run_start} {row_y}h{previous - run_start + 1}v1h-{previous - run_start + 1}z"
                        )
                        run_start = previous = current_x
                    paths_by_color.setdefault(hex_color, []).append(
                        f"M{run_start} {row_y}h{previous - run_start + 1}v1h-{previous - run_start + 1}z"
                    )
                paths = "".join(
                    f'<path fill="{hex_color}" d="{"".join(parts)}"/>'
                    for hex_color, parts in paths_by_color.items()
                )
                vector_objects.append(
                    f'<g class="vector-object" data-vector-index="{vector_index}" data-vector-kind="source-mark" '
                    f'data-bbox="{min_x},{min_y},{max_x - min_x + 1},{max_y - min_y + 1}">{paths}</g>'
                )
                vector_index += 1
    operation_scale = scale
    overlay_paths: list[str] = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        points = _svg_points(operation.get("points"), operation_scale)
        if len(points) < 2:
            continue
        points_text = " ".join(f"{_svg_number(x)},{_svg_number(y)}" for x, y in points)
        op_type = str(operation.get("type") or "")
        if op_type == "erase":
            width_value = max(1.0, float(operation.get("width") or 8) * operation_scale)
            overlay_paths.append(
                f'<polyline points="{points_text}" fill="none" stroke="#ffffff" stroke-width="{_svg_number(width_value)}" '
                'stroke-linecap="round" stroke-linejoin="round"/>'
            )
        elif op_type == "arrow":
            width_value = max(1.0, float(operation.get("width") or 2) * operation_scale)
            color = str(operation.get("color") or "#111111")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = "#111111"
            last_x, last_y = points[-1]
            previous_x, previous_y = points[-2]
            angle = math.atan2(last_y - previous_y, last_x - previous_x)
            head = max(8.0 * operation_scale, width_value * 5)
            left_x = last_x - head * math.cos(angle - math.pi / 6)
            left_y = last_y - head * math.sin(angle - math.pi / 6)
            right_x = last_x - head * math.cos(angle + math.pi / 6)
            right_y = last_y - head * math.sin(angle + math.pi / 6)
            overlay_paths.append(
                f'<polyline points="{points_text}" fill="none" stroke="{color}" stroke-width="{_svg_number(width_value)}" '
                'stroke-linecap="round" stroke-linejoin="round"/>'
                f'<polygon points="{_svg_number(last_x)},{_svg_number(last_y)} {_svg_number(left_x)},{_svg_number(left_y)} '
                f'{_svg_number(right_x)},{_svg_number(right_y)}" fill="{color}"/>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'data-original-width="{source_rgb.width}" data-original-height="{source_rgb.height}">'
        '<title>Full-image chemistry figure vector trace</title>'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>'
        f'<g id="full-image-vector-trace">{"".join(vector_objects)}</g>'
        f'<g id="editable-arrow-overlays">{"".join(overlay_paths)}</g>'
        '</svg>'
    )


def resolve_redrawn_base_path(
    review_root: Path,
    project: Path,
    stage: Path,
    row: dict[str, Any] | None,
) -> Path | None:
    """Resolve both current and legacy manifest fields to an existing redraw image."""
    if not isinstance(row, dict):
        return None
    fields = (
        "redrawn_image",
        "rejected_preview_image",
        "manual_edit_base_image",
        "output_path",
        "redrawn_path",
        "output_image_path",
        "image_path",
        "path",
    )
    for field in fields:
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        raw_path = Path(value)
        candidates = [raw_path] if raw_path.is_absolute() else [stage / raw_path, project / raw_path, review_root / raw_path, raw_path]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    figure_id = str(row.get("figure_id") or "").strip()
    if figure_id:
        # The Figures payload already recovers an orphaned AI preview from the
        # redraw directory when an earlier failed manifest write left the path
        # fields empty.  The SVG endpoint must use the same recovery rule so the
        # image shown as Redrawn Output is also the image opened in the editor.
        recovered = next(
            (candidate for candidate in (stage / "redrawn").glob(f"{figure_id}.*") if candidate.is_file()),
            None,
        )
        if recovered is not None:
            return recovered.resolve()
    return None


def figure_aspect_ratio_integrity(source_path: Path, output_path: Path, tolerance: float = 0.015) -> dict[str, Any]:
    """Reject redraws whose canvas shape no longer matches the selected source."""
    try:
        with Image.open(source_path) as source, Image.open(output_path) as output:
            source_size = source.size
            output_size = output.size
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    source_ratio = source_size[0] / max(1, source_size[1])
    output_ratio = output_size[0] / max(1, output_size[1])
    relative_difference = abs(output_ratio - source_ratio) / max(source_ratio, 1e-9)
    return {
        "status": "pass" if relative_difference <= tolerance else "failed",
        "source_size": list(source_size),
        "output_size": list(output_size),
        "source_aspect_ratio": source_ratio,
        "output_aspect_ratio": output_ratio,
        "relative_difference": relative_difference,
        "tolerance": tolerance,
    }


def create_full_figure_svg(
    review_root: Path,
    project_id: str,
    figure_id: str,
    *,
    base_mode: str = "source",
) -> dict[str, object]:
    """Create an all-path SVG workspace from a selected source or redraw image.

    This deliberately traces pixels rather than attempting chemistry OCR or molecular
    reconstruction: every visible mark stays in its original position and the SVG has
    no embedded raster-image element.
    """
    if base_mode not in {"source", "redrawn"}:
        raise ValueError("base_mode must be source or redrawn")
    project = review_root / "review-projects" / project_id
    candidates = read_json_if_exists(project / "02_section_drafting" / "figure_candidates.json") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("figures") or candidates.get("candidates") or []
    candidate = next(
        (row for row in candidates if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
        None,
    )
    if not isinstance(candidate, dict):
        raise ValueError("Current figure candidate was not found")
    source_path = Path(str(candidate.get("source_image_path") or candidate.get("image_path") or ""))
    if not source_path.is_file():
        raise ValueError("The current source image is unavailable")
    stage = project / "03_figure_redraw"
    base_path = source_path
    if base_mode == "redrawn":
        manifest = read_json_if_exists(stage / "redrawn_figure_manifest.json") or {}
        rows = manifest.get("figures") if isinstance(manifest, dict) else []
        row = next(
            (item for item in rows or [] if isinstance(item, dict) and str(item.get("figure_id") or "") == figure_id),
            None,
        )
        redrawn_path = resolve_redrawn_base_path(review_root, project, stage, row)
        if redrawn_path is None:
            raise ValueError("The selected AI redraw is unavailable. Switch the editor base to the source image or regenerate this figure.")
        base_path = redrawn_path
    svg = build_full_image_vector_svg(base_path, [])
    if len(svg.encode("utf-8")) > 25 * 1024 * 1024:
        raise ValueError("Full-image vector SVG is larger than the 25 MB editor limit")
    output_path = stage / "manual_arrow_edits" / f"{figure_id}-{base_mode}-full.svg"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    with Image.open(base_path) as base_image:
        base_width, base_height = base_image.size
    return {
        "figure_id": figure_id,
        "base_mode": base_mode,
        "base_image": str(base_path),
        "base_width": base_width,
        "base_height": base_height,
        "full_svg": str(output_path),
        "vectorization": "full-image-pixel-trace",
        "contains_embedded_raster": False,
    }


_INSERTED_FIGURE_METADATA_RE = re.compile(r"<!--\s*inserted_figure:\s*(\{.*?\})\s*-->", re.S)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def sync_edited_figure_to_manuscripts(project: Path, figure_id: str, edited_image: Path) -> dict[str, list[str]]:
    """Replace existing draft assets that belong to a saved SVG/manual figure edit.

    Stage 8 and Stage 9 Markdown use copied ``figures/`` assets rather than the
    Stage 7 manifest path.  Updating those copies at save time prevents a
    correct SVG edit from being stranded in ``03_figure_redraw`` until a full
    draft rebuild is run.
    """
    synced: dict[str, list[str]] = {"04_first_draft": [], "05_final_audit": []}
    for stage_name, manuscript_name in (("04_first_draft", "first_draft.md"), ("05_final_audit", "final_draft.md")):
        stage = project / stage_name
        manuscript_path = stage / manuscript_name
        if not manuscript_path.is_file():
            continue
        manuscript = manuscript_path.read_text(encoding="utf-8", errors="ignore")
        for marker in _INSERTED_FIGURE_METADATA_RE.finditer(manuscript):
            try:
                metadata = json.loads(marker.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict) or str(metadata.get("figure_id") or "") != figure_id:
                continue
            next_marker = _INSERTED_FIGURE_METADATA_RE.search(manuscript, marker.end())
            search_end = next_marker.start() if next_marker else len(manuscript)
            image = _MARKDOWN_IMAGE_RE.search(manuscript, marker.end(), search_end)
            if not image:
                continue
            relative_path = Path(image.group(1).replace("\\", "/"))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                continue
            target = (stage / relative_path).resolve()
            stage_root = stage.resolve()
            if target != stage_root and stage_root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(edited_image, target)
            synced[stage_name].append(str(target))
    return synced


def save_manual_arrow_edit(
    review_root: Path,
    project_id: str,
    figure_id: str,
    image_bytes: bytes,
    operations: list[dict[str, object]],
    *,
    base_mode: str = "source",
    editable_svg: str = "",
    full_vector_svg: str = "",
) -> dict[str, object]:
    """Persist a user-drawn local arrow edit without ever invoking image generation."""
    if len(image_bytes) > 25 * 1024 * 1024:
        raise ValueError("PNG is larger than the 25 MB manual-edit limit")
    project = review_root / "review-projects" / project_id
    candidates = read_json_if_exists(project / "02_section_drafting" / "figure_candidates.json") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("figures") or candidates.get("candidates") or []
    candidate = next(
        (row for row in candidates if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
        None,
    )
    if not isinstance(candidate, dict):
        raise ValueError("Current figure candidate was not found")
    stage = project / "03_figure_redraw"
    draft_stage = project / "02_section_drafting"
    figures_handoff = stage / "figures_handoff.json"
    ensure_stage_handoff(
        figures_handoff,
        "figure-review",
        [
            draft_stage / "section_drafts.json",
            draft_stage / "figure_candidates.json",
            draft_stage / "paper_figure_candidates.json",
            draft_stage / "human_figure_review.json",
        ],
    )
    source_path = Path(str(candidate.get("source_image_path") or candidate.get("image_path") or ""))
    if not source_path.exists():
        raise ValueError("The current source image is unavailable")
    if base_mode not in {"source", "redrawn"}:
        raise ValueError("base_mode must be source or redrawn")
    base_path = source_path
    if base_mode == "redrawn":
        manifest = read_json_if_exists(stage / "redrawn_figure_manifest.json") or {}
        rows = manifest.get("figures") if isinstance(manifest, dict) else []
        row = next(
            (item for item in rows or [] if isinstance(item, dict) and str(item.get("figure_id") or "") == figure_id),
            None,
        )
        redrawn_path = resolve_redrawn_base_path(review_root, project, stage, row)
        if redrawn_path is None:
            raise ValueError("The selected AI redraw is unavailable. Switch the editor base to the source image or regenerate this figure.")
        base_path = redrawn_path
    if editable_svg:
        if len(editable_svg.encode("utf-8")) > 25 * 1024 * 1024:
            raise ValueError("Editable SVG is larger than the 25 MB manual-edit limit")
        if not editable_svg.lstrip().startswith("<svg"):
            raise ValueError("editable_svg must be SVG markup")
    if full_vector_svg:
        if len(full_vector_svg.encode("utf-8")) > 25 * 1024 * 1024:
            raise ValueError("Full-image vector SVG is larger than the 25 MB manual-edit limit")
        if not full_vector_svg.lstrip().startswith("<svg"):
            raise ValueError("full_vector_svg must be SVG markup")
        if "full-image-vector-trace" not in full_vector_svg or re.search(r"<image\b", full_vector_svg, re.IGNORECASE):
            raise ValueError("full_vector_svg must contain the full vector trace and no embedded raster image")
    submitted_canvas_size: tuple[int, int] | None = None
    base_canvas_size: tuple[int, int] | None = None
    normalized_canvas = False
    with Image.open(base_path) as base_image, Image.open(io.BytesIO(image_bytes)) as edited:
        if edited.format != "PNG":
            raise ValueError("Manual edit must be encoded as PNG")
        submitted_canvas_size = edited.size
        base_canvas_size = base_image.size
        if edited.size != base_image.size:
            # Full-image SVG tracing intentionally caps its editing coordinate space
            # at 1600 px.  Older cached editor pages exported that preview-sized PNG
            # instead of rasterizing the vector document at the source resolution.
            # Accept only that narrowly identifiable, aspect-ratio-preserving case;
            # freehand/manual canvas mismatches remain rejected.
            edited_ratio = edited.width / max(1, edited.height)
            base_ratio = base_image.width / max(1, base_image.height)
            scalable_full_svg = bool(
                full_vector_svg
                and "full-image-vector-trace" in full_vector_svg
                and max(edited.size) <= FULL_SVG_MAX_DIMENSION
                and max(base_image.size) > FULL_SVG_MAX_DIMENSION
                and abs(edited_ratio - base_ratio) / max(base_ratio, 1e-9) <= 0.005
            )
            if not scalable_full_svg:
                raise ValueError(f"Canvas size {edited.size} does not match selected base image {base_image.size}")
            normalized = edited.convert("RGBA").resize(base_image.size, Image.Resampling.LANCZOS)
            normalized_bytes = io.BytesIO()
            normalized.save(normalized_bytes, format="PNG")
            image_bytes = normalized_bytes.getvalue()
            if len(image_bytes) > 25 * 1024 * 1024:
                raise ValueError("Full-resolution PNG is larger than the 25 MB manual-edit limit")
            normalized_canvas = True
        edited.load()
    if full_vector_svg:
        editable_svg = full_vector_svg
    elif editable_svg:
        editable_svg = build_full_image_vector_svg(base_path, operations)
        if len(editable_svg.encode("utf-8")) > 25 * 1024 * 1024:
            raise ValueError("Full-image vector SVG is larger than the 25 MB manual-edit limit")

    output_path = stage / "redrawn" / f"{figure_id}-manual.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    audit_path = stage / "manual_arrow_edits" / f"{figure_id}.json"
    editable_svg_path = stage / "manual_arrow_edits" / f"{figure_id}.svg"
    if editable_svg:
        editable_svg_path.parent.mkdir(parents=True, exist_ok=True)
        editable_svg_path.write_text(editable_svg, encoding="utf-8")
    write_json(
        audit_path,
        {
            "figure_id": figure_id,
            "source_image": str(source_path),
            "base_image": str(base_path),
            "base_mode": base_mode,
            "output_image": str(output_path),
            "editable_svg": str(editable_svg_path) if editable_svg else None,
            "saved_at": now_utc(),
            "submitted_canvas_size": list(submitted_canvas_size or ()),
            "output_canvas_size": list(base_canvas_size or ()),
            "canvas_normalized_to_base": normalized_canvas,
            "operations": operations,
            "rule": "User-operated local pixel edit: erase only original curved arrows and draw only straight or orthogonal arrows.",
        },
    )
    manifest_path = stage / "redrawn_figure_manifest.json"
    manifest = read_json_if_exists(manifest_path) or {"project_id": project_id, "figures": []}
    rows = manifest.get("figures") if isinstance(manifest, dict) else []
    if not isinstance(rows, list):
        rows = []
    existing = next((row for row in rows if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id), {})
    row = dict(existing) if isinstance(existing, dict) else {}
    row.update(
        {
            "figure_id": figure_id,
            "section_id": candidate.get("section_id"),
            "section_heading": candidate.get("section_heading"),
            "target_paragraph_id": candidate.get("target_paragraph_id") or candidate.get("paragraph_id"),
            "paper_id": candidate.get("paper_id"),
            "source_label": candidate.get("source_label"),
            "source_type": candidate.get("source_type"),
            "source_caption_text": candidate.get("source_caption_text"),
            "source_image": str(source_path),
            "manual_edit_base_image": str(base_path),
            "manual_edit_base_mode": base_mode,
            "redrawn_image": str(output_path),
            "editable_svg": str(editable_svg_path) if editable_svg else None,
            "render_mode": "manual-arrow-edit",
            "edit_profile": "manual-mechanism-arrow-paths",
            "status": "redrawn",
            "needs_human_check": True,
            "manual_arrow_edit": {
                "status": "saved",
                "audit_path": str(audit_path),
                "editable_svg": str(editable_svg_path) if editable_svg else None,
                "full_image_vector_trace": bool(editable_svg),
                "base_mode": base_mode,
                "base_image": str(base_path),
                "operation_count": len(operations),
                "source_pixels_preserved_outside_user_strokes": True,
            },
            "chemistry_integrity": {
                "status": "needs_human_arrow_check",
                "failures": [],
                "human_check_required": True,
                "manual_check": "Verify every erased stroke was an original curved arrow and every new arrow has the correct endpoint, direction, color, and route.",
            },
            "notes": "Manual local arrow edit; no image-generation model or OCR rewriting was used. The SVG is a full-image vector trace with editable arrow overlays.",
        }
    )
    replacement_rows = [
        row if isinstance(item, dict) and str(item.get("figure_id") or "") == figure_id else item
        for item in rows
    ]
    if not any(isinstance(item, dict) and str(item.get("figure_id") or "") == figure_id for item in rows):
        replacement_rows.append(row)
    write_json(manifest_path, {"project_id": project_id, "figures": replacement_rows})
    synchronized_assets = sync_edited_figure_to_manuscripts(project, figure_id, output_path)
    versioned_outputs = [manifest_path, output_path, audit_path]
    if editable_svg:
        versioned_outputs.append(editable_svg_path)
    record_stage_outputs(figures_handoff, versioned_outputs, "figures")
    return {
        "figure_id": figure_id,
        "render_mode": "manual-arrow-edit",
        "edit_profile": "manual-mechanism-arrow-paths",
        "redrawn_image": str(output_path),
        "editable_svg": str(editable_svg_path) if editable_svg else None,
        "manual_edit_base_mode": base_mode,
        "requires_human_arrow_check": True,
        "synchronized_draft_assets": synchronized_assets,
    }


def _normalized_figure_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _resolve_candidate_source(review_root: Path, project: Path, candidate: dict[str, Any]) -> Path:
    value = str(
        candidate.get("source_image_path")
        or candidate.get("source_path")
        or candidate.get("image_path")
        or ""
    ).strip()
    if not value:
        raise ValueError("Current figure candidate has no source image.")
    raw = Path(value)
    paths = [raw] if raw.is_absolute() else [project / raw, review_root / raw, raw]
    source = next((path for path in paths if path.is_file()), None)
    if source is None:
        raise ValueError("The current figure candidate source image is unavailable.")
    return source.resolve()


def redraw_current_figure(review_root: Path, project_id: str, figure_id: str) -> dict[str, Any]:
    """Create one gated AI line-art redraw and reject altered chemical geometry."""
    project = review_root / "review-projects" / project_id
    draft_stage = project / "02_section_drafting"
    # Reconcile the Stage 6 human choice immediately before every redraw.  This
    # closes the gap where a browser tab retained an older figure_candidates
    # entry after the reviewer selected a different source image.
    ensure_default_figure_reviews(draft_stage)
    figures = read_json_if_exists(draft_stage / "figure_candidates.json") or []
    if isinstance(figures, dict):
        figures = figures.get("figures") or figures.get("candidates") or []
    candidate = next(
        (row for row in figures if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
        None,
    )
    if not isinstance(candidate, dict):
        raise ValueError("Current figure candidate was not found.")
    paper_id = str(candidate.get("paper_id") or "")
    if not paper_id:
        raise ValueError("Current figure candidate has no paper ID.")
    current_source = _resolve_candidate_source(review_root, project, candidate)
    source_sha256_before = sha256_file(current_source)
    figures_handoff = project / "03_figure_redraw" / "figures_handoff.json"
    ensure_stage_handoff(
        figures_handoff,
        "figure-review",
        [
            draft_stage / "section_drafts.json",
            draft_stage / "figure_candidates.json",
            draft_stage / "paper_figure_candidates.json",
            draft_stage / "human_figure_review.json",
        ],
    )

    def persist_redraw_result(result: dict[str, Any]) -> dict[str, Any]:
        outputs = [project / "03_figure_redraw" / "redrawn_figure_manifest.json"]
        image_path = str(result.get("redrawn_image") or "")
        if image_path:
            outputs.append(Path(image_path))
        record_stage_outputs(figures_handoff, outputs, "figures")
        return result

    def preview_result(row: dict[str, Any] | None) -> dict[str, Any] | None:
        explicit = str((row or {}).get("rejected_preview_image") or "")
        preview = Path(explicit) if explicit else None
        if not preview or not preview.is_file():
            previews = list((project / "03_figure_redraw" / "redrawn").glob(f"{figure_id}.*"))
            preview = next((path for path in previews if path.is_file()), None)
        if not preview:
            return None
        return {
            "figure_id": figure_id,
            "paper_id": paper_id,
            "render_mode": str((row or {}).get("render_mode") or "ai-preview"),
            "edit_profile": str((row or {}).get("edit_profile") or ""),
            "requires_human_arrow_check": mechanism_arrow_profile,
            "redrawn_image": str(preview),
            "preview_only": True,
        }
    mechanism_arrow_profile = use_mechanism_arrow_straighten_profile(candidate)
    source_faithful_scope = not mechanism_arrow_profile and use_source_faithful_scope_render(candidate)
    source_faithful_multipanel = (
        not mechanism_arrow_profile
        and not source_faithful_scope
        and use_source_faithful_multipanel_render(candidate)
    )
    hollow_color_fills = source_faithful_multipanel and use_hollow_color_fill_render(candidate)
    extra = ["--figure-id", figure_id, "--model", "gpt-image-2", "--require-redrawn"]
    if mechanism_arrow_profile:
        extra.extend(["--render-mode", "ai-edit", "--edit-profile", "mechanism-arrow-straighten"])
    elif source_faithful_scope:
        extra.extend(["--render-mode", "source-faithful-bw"])
    elif source_faithful_multipanel:
        extra.extend(["--render-mode", "source-faithful-outline-color" if hollow_color_fills else "source-faithful-color"])
    else:
        extra.extend(["--render-mode", "ai-edit"])
    try:
        run_project_script(
            figure_redraw_script_path(),
            review_root,
            project_id,
            timeout=300,
            extra=extra,
        )
    except RuntimeError as exc:
        manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
        rows = manifest.get("figures", []) if isinstance(manifest, dict) else []
        failed = next(
            (row for row in rows if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
            None,
        )
        note = str((failed or {}).get("notes") or "").strip()
        preview = preview_result(failed if isinstance(failed, dict) else None)
        if preview:
            return persist_redraw_result(preview)
        if note:
            if "mechanism integrity retries" in note.lower() or "mechanism-arrow edit" in note.lower():
                raise RuntimeError(
                    "AI 机理箭头局部编辑未通过完整性校验。请使用“在线编辑 SVG”，在全图矢量工作区中修改箭头路径。"
                ) from exc
            raise RuntimeError(f"Current figure redraw failed: {note}") from exc
        raise
    manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    rows = manifest.get("figures", []) if isinstance(manifest, dict) else []
    redrawn = next(
        (row for row in rows if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
        None,
    )
    manifest_source = str((redrawn or {}).get("source_image") or "")
    source_unchanged = current_source.is_file() and sha256_file(current_source) == source_sha256_before
    if (
        not isinstance(redrawn, dict)
        or not manifest_source
        or _normalized_figure_path(manifest_source) != _normalized_figure_path(current_source)
        or not source_unchanged
    ):
        if isinstance(redrawn, dict):
            redrawn["status"] = "source_mismatch"
            redrawn["notes"] = (
                "The redraw output was rejected because it was not produced from "
                "the current Stage 6 source candidate."
            )
            redrawn.pop("redrawn_image", None)
            redrawn.pop("human_approval", None)
            write_json(
                project / "03_figure_redraw" / "redrawn_figure_manifest.json",
                {"project_id": project_id, "figures": rows},
            )
        raise RuntimeError(
            "The Stage 6 source candidate changed during redraw. Reload Stage 7 and redraw this figure again."
        )
    redrawn["source_image"] = str(current_source)
    redrawn["source_image_sha256"] = source_sha256_before
    redrawn["source_selection"] = {
        "status": "current_stage6_candidate",
        "verified_at": now_utc(),
        "source_image": str(current_source),
        "source_sha256": source_sha256_before,
    }
    if redrawn.get("redrawn_image") and Path(str(redrawn["redrawn_image"])).is_file():
        redrawn["output_image_sha256"] = sha256_file(Path(str(redrawn["redrawn_image"])))
    write_json(
        project / "03_figure_redraw" / "redrawn_figure_manifest.json",
        {"project_id": project_id, "figures": rows},
    )
    if not isinstance(redrawn, dict) or redrawn.get("status") != "redrawn" or not redrawn.get("redrawn_image"):
        preview = preview_result(redrawn if isinstance(redrawn, dict) else None)
        if preview:
            return persist_redraw_result(preview)
        raise RuntimeError("The selected figure did not produce a usable redrawn output.")
    if redrawn.get("render_mode") not in {
        "source-faithful-bw", "source-faithful-color", "source-faithful-outline-color", "ai-edit", "ocr-hollow-ai"
    }:
        raise RuntimeError("The selected figure used an unsupported redraw mode.")
    integrity = redrawn.get("chemistry_integrity") or {}
    integrity_warning = bool(
        str(redrawn.get("output_disposition") or "") == "saved_with_integrity_warning"
        or (integrity and integrity.get("status") == "failed")
    )
    return persist_redraw_result({
        "figure_id": figure_id,
        "paper_id": paper_id,
        "render_mode": str(redrawn.get("render_mode") or ""),
        "edit_profile": str(redrawn.get("edit_profile") or ""),
        "requires_human_arrow_check": mechanism_arrow_profile,
        "source_faithful_scope_render": source_faithful_scope,
        "source_faithful_multipanel_render": source_faithful_multipanel,
        "hollow_color_fills": hollow_color_fills,
        "integrity_warning": integrity_warning,
        "redrawn_image": str(redrawn["redrawn_image"]),
    })


def approve_figure_for_manuscript(review_root: Path, project_id: str, figure_id: str) -> dict[str, Any]:
    """Record an explicit human override for a current Stage 7 output.

    The failed automated gate is retained for audit.  Approval is bound to the
    exact current Stage 6 source and output hashes so it cannot follow a stale
    redraw after the source candidate or preview changes.
    """
    project = review_root / "review-projects" / project_id
    draft_stage = project / "02_section_drafting"
    stage = project / "03_figure_redraw"
    ensure_default_figure_reviews(draft_stage)
    candidates = read_json_if_exists(draft_stage / "figure_candidates.json") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("figures") or candidates.get("candidates") or []
    candidate = next(
        (
            row
            for row in candidates
            if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id
        ),
        None,
    )
    if not isinstance(candidate, dict):
        raise ValueError("Current figure candidate was not found.")
    source_path = _resolve_candidate_source(review_root, project, candidate)

    manifest_path = stage / "redrawn_figure_manifest.json"
    manifest = read_json_if_exists(manifest_path) or {}
    rows = manifest.get("figures") if isinstance(manifest, dict) else []
    if not isinstance(rows, list):
        rows = []
    row = next(
        (
            item
            for item in rows
            if isinstance(item, dict) and str(item.get("figure_id") or "") == figure_id
        ),
        None,
    )
    if not isinstance(row, dict):
        raise ValueError("No AI redraw or preview is available for human approval.")
    recorded_source = str(row.get("source_image") or "").strip()
    if (
        not recorded_source
        or _normalized_figure_path(recorded_source) != _normalized_figure_path(source_path)
    ):
        raise ValueError(
            "The preview was generated from an older source candidate. Redraw the current Stage 6 selection before approval."
        )
    output_path = resolve_redrawn_base_path(review_root, project, stage, row)
    if output_path is None:
        raise ValueError("No AI redraw or preview file is available for human approval.")
    aspect_integrity = figure_aspect_ratio_integrity(source_path, output_path)
    if aspect_integrity.get("status") != "pass":
        source_size = aspect_integrity.get("source_size") or []
        output_size = aspect_integrity.get("output_size") or []
        raise ValueError(
            "The redraw canvas aspect ratio does not match the selected source "
            f"({source_size} versus {output_size}). Redraw it with the current aspect-ratio guard before approval."
        )

    source_hash = sha256_file(source_path)
    recorded_source_hash = str(row.get("source_image_sha256") or "")
    if recorded_source_hash and recorded_source_hash != source_hash:
        raise ValueError(
            "The source image contents changed after this redraw. Redraw the current source before approval."
        )
    output_hash = sha256_file(output_path)
    row["source_image"] = str(source_path)
    row["source_image_sha256"] = source_hash
    row["redrawn_image"] = str(output_path)
    row["output_image_sha256"] = output_hash
    row["aspect_ratio_integrity"] = aspect_integrity
    row["status"] = "redrawn"
    row["output_disposition"] = "human_approved_for_manuscript"
    row["human_approval"] = {
        "status": "approved",
        "approved_at": now_utc(),
        "source_image": str(source_path),
        "source_sha256": source_hash,
        "output_image": str(output_path),
        "output_sha256": output_hash,
        "current_source_match": True,
        "current_output_match": True,
        "acknowledgement": (
            "A human reviewer inspected the chemistry, labels, bonds, arrows, "
            "layout, and output quality and explicitly allowed manuscript use."
        ),
    }
    write_json(manifest_path, {"project_id": project_id, "figures": rows})

    figures_handoff = stage / "figures_handoff.json"
    ensure_stage_handoff(
        figures_handoff,
        "figure-review",
        [
            draft_stage / "section_drafts.json",
            draft_stage / "figure_candidates.json",
            draft_stage / "paper_figure_candidates.json",
            draft_stage / "human_figure_review.json",
        ],
    )
    synchronized_assets = sync_edited_figure_to_manuscripts(project, figure_id, output_path)
    record_stage_outputs(figures_handoff, [manifest_path, output_path], "figures")
    return {
        "figure_id": figure_id,
        "redrawn_image": str(output_path),
        "human_approval": row["human_approval"],
        "synchronized_draft_assets": synchronized_assets,
    }


def persist_batch_redraw_job(review_root: Path, project_id: str, job: dict[str, object]) -> None:
    saved = workflow_store(review_root).save_job(project_id, "figure-redraw-all", job)
    job["job_id"] = saved["job_id"]


def public_batch_redraw_state(job: dict[str, object]) -> dict[str, object]:
    result = {
        key: value
        for key, value in job.items()
        if key not in {"figure_ids", "completed_figure_ids"}
    }
    result["errors"] = [dict(item) for item in job.get("errors", []) if isinstance(item, dict)]
    return result


def batch_figure_redraw_status(project_id: str, review_root: Path | None = None) -> dict[str, object]:
    """Return batch state from memory, falling back to durable SQLite state."""
    with _BATCH_REDRAW_LOCK:
        job = _BATCH_REDRAW_JOBS.get(project_id)
        if not job and review_root is not None:
            persisted = workflow_store(review_root).load_job(project_id, "figure-redraw-all")
            if persisted:
                job = dict(persisted)
                if job.get("status") in {"running", "stopping"}:
                    job["status"] = "interrupted"
                    job["current_figure_id"] = ""
                    job["current_source_label"] = ""
                    job["updated_at"] = now_utc()
                    persist_batch_redraw_job(review_root, project_id, job)
        if not job:
            return {
                "status": "idle",
                "total": 0,
                "completed": 0,
                "succeeded": 0,
                "preview_succeeded": 0,
                "failed": 0,
                "current_figure_id": "",
                "stop_requested": False,
                "errors": [],
            }
        return public_batch_redraw_state(job)


def batch_redraw_figure_ids(review_root: Path, project_id: str) -> list[str]:
    candidates = read_json_if_exists(
        review_root / "review-projects" / project_id / "02_section_drafting" / "figure_candidates.json"
    ) or []
    if isinstance(candidates, dict):
        candidates = candidates.get("figures") or candidates.get("candidates") or []
    if not isinstance(candidates, list):
        raise ValueError("Figure candidates are not available. Regenerate Sections first.")
    figure_ids: list[str] = []
    for candidate in candidates:
        figure_id = str((candidate or {}).get("figure_id") or "") if isinstance(candidate, dict) else ""
        if figure_id and figure_id not in figure_ids:
            figure_ids.append(figure_id)
    if not figure_ids:
        raise ValueError("No figure candidates are available for AI redraw.")
    return figure_ids


def stop_batch_figure_redraw(project_id: str, review_root: Path | None = None) -> dict[str, object]:
    """Stop a running batch before it starts another provider request.

    The active image-edit HTTP request is allowed to return so its output and
    manifest write remain atomic.  Every remaining queued figure is skipped.
    """
    with _BATCH_REDRAW_LOCK:
        job = _BATCH_REDRAW_JOBS.get(project_id)
        if not job and review_root is not None:
            persisted = workflow_store(review_root).load_job(project_id, "figure-redraw-all")
            job = dict(persisted) if persisted else None
        if not job:
            return batch_figure_redraw_status(project_id, review_root)
        if job.get("status") in {"running", "stopping"}:
            job["status"] = "stopping"
            job["stop_requested"] = True
            job["stop_requested_at"] = now_utc()
            job["updated_at"] = now_utc()
            if review_root is not None:
                persist_batch_redraw_job(review_root, project_id, job)
        elif job.get("status") == "interrupted":
            job["status"] = "stopped"
            job["stop_requested"] = True
            job["stop_requested_at"] = now_utc()
            job["finished_at"] = now_utc()
            job["updated_at"] = now_utc()
            if review_root is not None:
                persist_batch_redraw_job(review_root, project_id, job)
    return batch_figure_redraw_status(project_id, review_root)


def run_batch_figure_redraw(review_root: Path, project_id: str, figure_ids: list[str]) -> None:
    """Run redraws sequentially so provider requests and manifests cannot race."""
    for figure_id in figure_ids:
        with _BATCH_REDRAW_LOCK:
            job = _BATCH_REDRAW_JOBS.get(project_id)
            if not job:
                return
            if bool(job.get("stop_requested")):
                job["status"] = "stopped"
                job["current_figure_id"] = ""
                job["current_source_label"] = ""
                job["finished_at"] = now_utc()
                job["updated_at"] = now_utc()
                persist_batch_redraw_job(review_root, project_id, job)
                return
            job["current_figure_id"] = figure_id
            job["current_source_label"] = figure_id
            job["updated_at"] = now_utc()
            persist_batch_redraw_job(review_root, project_id, job)
        try:
            result = redraw_current_figure(review_root, project_id, figure_id)
        except (RuntimeError, ValueError) as exc:
            with _BATCH_REDRAW_LOCK:
                job = _BATCH_REDRAW_JOBS.get(project_id)
                if not job:
                    return
                job["failed"] = int(job.get("failed") or 0) + 1
                errors = job.setdefault("errors", [])
                if isinstance(errors, list):
                    errors.append({"figure_id": figure_id, "error": str(exc)})
                    del errors[:-12]
        else:
            with _BATCH_REDRAW_LOCK:
                job = _BATCH_REDRAW_JOBS.get(project_id)
                if not job:
                    return
                job["succeeded"] = int(job.get("succeeded") or 0) + 1
                if result.get("preview_only"):
                    job["preview_succeeded"] = int(job.get("preview_succeeded") or 0) + 1
                job["last_redrawn_image"] = str(result.get("redrawn_image") or "")
        finally:
            with _BATCH_REDRAW_LOCK:
                job = _BATCH_REDRAW_JOBS.get(project_id)
                if job:
                    job["completed"] = int(job.get("completed") or 0) + 1
                    completed_ids = job.setdefault("completed_figure_ids", [])
                    if isinstance(completed_ids, list) and figure_id not in completed_ids:
                        completed_ids.append(figure_id)
                    job["updated_at"] = now_utc()
                    persist_batch_redraw_job(review_root, project_id, job)
    with _BATCH_REDRAW_LOCK:
        job = _BATCH_REDRAW_JOBS.get(project_id)
        if job:
            job["status"] = "stopped" if bool(job.get("stop_requested")) else "completed"
            job["current_figure_id"] = ""
            job["current_source_label"] = ""
            job["finished_at"] = now_utc()
            job["updated_at"] = now_utc()
            persist_batch_redraw_job(review_root, project_id, job)


def run_batch_figure_redraw_orchestrated(review_root: Path, project_id: str, figure_ids: list[str]) -> None:
    """Run the durable sequential queue as a Prefect flow/task."""

    if not prefect_orchestration_enabled():
        with _BATCH_REDRAW_LOCK:
            job = _BATCH_REDRAW_JOBS.get(project_id)
            if job:
                job["workflow_engine"] = "native-fallback"
                persist_batch_redraw_job(review_root, project_id, job)
        run_batch_figure_redraw(review_root, project_id, figure_ids)
        return

    def flow_started(flow_run_id: str) -> None:
        with _BATCH_REDRAW_LOCK:
            job = _BATCH_REDRAW_JOBS.get(project_id)
            if job:
                job["workflow_engine"] = "prefect"
                job["prefect_flow_run_id"] = flow_run_id
                job["updated_at"] = now_utc()
                persist_batch_redraw_job(review_root, project_id, job)

    def action() -> dict[str, Any]:
        run_batch_figure_redraw(review_root, project_id, figure_ids)
        return batch_figure_redraw_status(project_id, review_root)

    try:
        orchestration = run_batch_redraw_with_prefect(
            review_root,
            project_id,
            len(figure_ids),
            action,
            on_flow_started=flow_started,
        )
    except Exception as exc:
        with _BATCH_REDRAW_LOCK:
            job = _BATCH_REDRAW_JOBS.get(project_id)
            if not job:
                return
            job["status"] = "failed"
            job["current_figure_id"] = ""
            job["current_source_label"] = ""
            job["finished_at"] = now_utc()
            job["updated_at"] = now_utc()
            errors = job.setdefault("errors", [])
            if isinstance(errors, list):
                errors.append({"figure_id": "", "error": f"Prefect batch execution failed: {type(exc).__name__}: {exc}"})
                del errors[:-12]
            persist_batch_redraw_job(review_root, project_id, job)
        return
    with _BATCH_REDRAW_LOCK:
        job = _BATCH_REDRAW_JOBS.get(project_id)
        if job:
            job["workflow_engine"] = "prefect"
            job["prefect_flow_run_id"] = orchestration.get("prefect_flow_run_id")
            job["prefect_task_run_id"] = orchestration.get("prefect_task_run_id")
            job["updated_at"] = now_utc()
            persist_batch_redraw_job(review_root, project_id, job)


def start_batch_figure_redraw(review_root: Path, project_id: str) -> dict[str, object]:
    project = review_root / "review-projects" / project_id
    if not project.is_dir():
        raise ValueError("Project not found.")
    figure_ids = batch_redraw_figure_ids(review_root, project_id)
    with _BATCH_REDRAW_LOCK:
        existing = _BATCH_REDRAW_JOBS.get(project_id)
        if existing and existing.get("status") in {"running", "stopping"}:
            return batch_figure_redraw_status(project_id, review_root)
        persisted = workflow_store(review_root).load_job(project_id, "figure-redraw-all")
        resumable = bool(
            persisted
            and persisted.get("status") in {"running", "stopping", "interrupted", "stopped"}
            and list(persisted.get("figure_ids") or []) == figure_ids
        )
        completed_ids = list(persisted.get("completed_figure_ids") or []) if resumable and persisted else []
        remaining_figure_ids = [figure_id for figure_id in figure_ids if figure_id not in completed_ids]
        job: dict[str, object] = {
            "status": "running",
            "total": len(figure_ids),
            "completed": int(persisted.get("completed") or 0) if resumable and persisted else 0,
            "succeeded": int(persisted.get("succeeded") or 0) if resumable and persisted else 0,
            "preview_succeeded": int(persisted.get("preview_succeeded") or 0) if resumable and persisted else 0,
            "failed": int(persisted.get("failed") or 0) if resumable and persisted else 0,
            "current_figure_id": "",
            "current_source_label": "",
            "stop_requested": False,
            "errors": list(persisted.get("errors") or []) if resumable and persisted else [],
            "figure_ids": figure_ids,
            "completed_figure_ids": completed_ids,
            "started_at": str(persisted.get("started_at") or now_utc()) if resumable and persisted else now_utc(),
            "updated_at": now_utc(),
        }
        if resumable and persisted and persisted.get("job_id"):
            job["job_id"] = str(persisted["job_id"])
        persist_batch_redraw_job(review_root, project_id, job)
        _BATCH_REDRAW_JOBS[project_id] = job
        if not remaining_figure_ids:
            job["status"] = "completed"
            job["finished_at"] = now_utc()
            job["updated_at"] = now_utc()
            persist_batch_redraw_job(review_root, project_id, job)
            return batch_figure_redraw_status(project_id, review_root)
    threading.Thread(
        target=run_batch_figure_redraw_orchestrated,
        args=(review_root, project_id, remaining_figure_ids),
        daemon=True,
        name=f"figure-redraw-{project_id}",
    ).start()
    return batch_figure_redraw_status(project_id, review_root)


def validate_figure_review(project: Path, project_id: str) -> dict[str, Any]:
    draft_stage = project / "02_section_drafting"
    candidates = read_json_if_exists(draft_stage / "paper_figure_candidates.json") or {}
    papers = candidates.get("papers") if isinstance(candidates, dict) else candidates
    reviews = (read_json_if_exists(draft_stage / "human_figure_review.json") or {}).get("papers", {})
    reviewable = [str(row.get("paper_id")) for row in papers or [] if isinstance(row, dict) and row.get("candidates")]
    missing = [paper_id for paper_id in reviewable if paper_id not in reviews]
    if missing:
        raise RuntimeError(f"Select a figure for every cited paper before continuing ({len(missing)} remaining).")
    for paper in papers or []:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paper_id") or "")
        selected_index = (reviews.get(paper_id) or {}).get("selected_candidate_index")
        candidate = next(
            (item for item in paper.get("candidates") or [] if item.get("candidate_index") == selected_index),
            None,
        )
        if not isinstance(candidate, dict) or candidate.get("source_type") == "table":
            raise RuntimeError(f"{paper_id} needs an image or scheme candidate before it can be redrawn.")
        sync_selected_candidate_for_redraw(project, paper_id, candidate)
    refresh_figure_review_handoff(draft_stage, accept_current=True)
    return {"reviewed_paper_count": len(reviewable), "redraw_pending": True}


REFERENCE_SECTION_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*(?:references|reference list|bibliography|cited literature|参考文献)\s*$",
    re.I | re.M,
)
REFERENCE_SUP_ELEMENT_RE = re.compile(r"<sup\b[^>\r\n]*>[^\r\n]*?</sup>", re.I)
REFERENCE_SUP_TAG_RE = re.compile(r"</?sup\b[^>]*>", re.I)


def clean_reference_author_text(authors: object) -> str:
    """Remove PDF-extraction affiliation markup from bibliography author names."""
    text = ", ".join(str(author) for author in authors[:3]) if isinstance(authors, list) else str(authors or "")
    text = REFERENCE_SUP_ELEMENT_RE.sub("", text)
    text = REFERENCE_SUP_TAG_RE.sub("", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\s*[§✉†‡*+]\s*", " ", text)
    text = re.sub(r",\s*,+", ",", text)
    text = re.sub(r"\s+([,.;])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,;")


def sanitize_reference_section_markup(markdown: str) -> str:
    """Keep HTML superscript artifacts out of the publication bibliography."""
    match = REFERENCE_SECTION_HEADING_RE.search(markdown or "")
    if not match:
        return markdown
    prefix, references = markdown[: match.end()], markdown[match.end() :]
    references = REFERENCE_SUP_ELEMENT_RE.sub("", references)
    references = REFERENCE_SUP_TAG_RE.sub("", references)
    references = re.sub(r"\[\s*\]", "", references)
    references = re.sub(r",\s*,+", ",", references)
    references = re.sub(r"(?m)^(\s*\[\d+\].*?)[ \t]{2,}", lambda item: re.sub(r"[ \t]{2,}", " ", item.group(0)), references)
    references = re.sub(r"[ \t]+([,.;])", r"\1", references)
    return prefix + references


def sanitize_reference_file(path: Path) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8", errors="ignore")
    cleaned = sanitize_reference_section_markup(original)
    if cleaned == original:
        return False
    path.write_text(cleaned, encoding="utf-8")
    return True


def render_reference_section(project: Path, citation_entries: list[dict[str, Any]]) -> str:
    """Build a clean reference list from authoritative citation and matrix data."""
    rows = {str(row["paper_id"]): row for row in matrix_rows(project)}
    lines = ["## References", ""]
    for entry in citation_entries:
        paper_id = str(entry.get("paper_id") or "")
        row = rows.get(paper_id, {})
        author_text = clean_reference_author_text(row.get("authors") or [])
        lines.append(
            f"[{entry.get('callout')}] {author_text}. {row.get('title') or paper_id}. "
            f"{row.get('journal') or ''} ({row.get('year') or 'n.d.'})."
        )
    return sanitize_reference_section_markup("\n".join(lines).rstrip() + "\n")


def replace_reference_section(markdown: str, reference_section: str) -> str:
    match = REFERENCE_SECTION_HEADING_RE.search(markdown or "")
    body = markdown[: match.start()] if match else markdown
    return body.rstrip() + "\n\n" + reference_section.strip() + "\n"


def refresh_reference_file(project: Path, path: Path, citation_entries: list[dict[str, Any]]) -> bool:
    if not path.is_file() or not citation_entries:
        return False
    original = path.read_text(encoding="utf-8", errors="ignore")
    refreshed = replace_reference_section(original, render_reference_section(project, citation_entries))
    if refreshed == original:
        return False
    path.write_text(refreshed, encoding="utf-8")
    return True


def regenerate_first_draft(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "04_first_draft"
    drafts = read_json_if_exists(project / "02_section_drafting" / "section_drafts.json") or {}
    sections = drafts.get("sections") if isinstance(drafts, dict) else drafts
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("Section drafts are missing. Regenerate Sections first.")
    figure_freshness = project_figures_payload(review_root, project_id)["freshness"]
    if not figure_freshness["selected_count"]:
        raise RuntimeError("No manuscript figure is selected. Complete Figure Review before building the draft.")
    if figure_freshness["stale"]:
        raise RuntimeError(
            "Figure redraw is incomplete or out of date "
            f"({figure_freshness['usable_count']}/{figure_freshness['selected_count']} current outputs are usable). "
            "Redraw missing figures or approve warning outputs before building the draft."
        )
    draft_handoff = stage / "draft_handoff.json"
    ensure_stage_handoff(
        draft_handoff,
        "figures",
        [
            project / "02_section_drafting" / "section_drafts.json",
            project / "02_section_drafting" / "human_figure_review.json",
            project / "03_figure_redraw" / "redrawn_figure_manifest.json",
        ],
    )

    # The merge skill supplies review-level framing and transitions. Do not
    # fall back to concatenating source snippets into a pseudo-manuscript.
    run_project_script(
        review_root / "skills" / "review-draft-merge-polish" / "scripts" / "merge_polish_draft.py",
        review_root,
        project_id,
        timeout=900,
    )
    citations: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        for paragraph in section.get("paragraphs") or []:
            if isinstance(paragraph, dict):
                for paper_id in paragraph.get("cited_paper_ids") or []:
                    paper_id = str(paper_id)
                    if paper_id and paper_id not in citations:
                        citations.append(paper_id)
    if not citations:
        raise RuntimeError("Source-grounded sections contain no citation mapping.")
    stage.mkdir(parents=True, exist_ok=True)
    citation_entries = [{"callout": index, "paper_id": paper_id} for index, paper_id in enumerate(citations, start=1)]
    write_json(stage / "citations.json", {"project_id": project_id, "entries": citation_entries})
    draft_path = stage / "first_draft.md"
    draft_text = draft_path.read_text(encoding="utf-8", errors="ignore")
    draft_path.write_text(replace_reference_section(draft_text, render_reference_section(project, citation_entries)), encoding="utf-8")
    run_project_script(review_root / "skills" / "review-draft-merge-polish" / "scripts" / "insert_figures_into_draft.py", review_root, project_id)
    (stage / "remaining_issues.md").write_text("# Remaining Issues\n\nConfirm the scientific interpretation, scope boundaries, and every redrawn figure before approving the first draft.\n", encoding="utf-8")
    record_stage_outputs(
        draft_handoff,
        [stage / "first_draft.md", stage / "citations.json", stage / "remaining_issues.md"],
        "draft",
    )
    write_stage_handoff(project / "05_final_audit" / "final_handoff.json", "draft", [stage / "first_draft.md", stage / "citations.json"])
    return {"citation_count": len(citations), "first_draft": str(stage / "first_draft.md")}


OVERVIEW_FIGURE_ID = "OVERVIEW-F01"
OVERVIEW_FIGURE_FILENAME = "overview_figure.png"
OVERVIEW_FIGURE_BLOCK_RE = re.compile(
    r"\n*<!-- review_overview_figure:start -->.*?<!-- review_overview_figure:end -->\n*",
    re.S,
)


def final_draft_contains_overview_figure(text: str) -> bool:
    """Return whether the manuscript contains the managed overview visual."""
    return OVERVIEW_FIGURE_ID in text and f"figures/{OVERVIEW_FIGURE_FILENAME}" in text


def inject_final_overview_figure(review_root: Path, project_id: str) -> dict[str, Any]:
    """Place the generated overview figure into the publication manuscript.

    The overview is stored next to the final-stage output for previewing, but
    Markdown and DOCX exports resolve inserted figures from ``figures/``.  This
    helper keeps those two locations synchronized and uses the same metadata
    marker as all other manuscript figures so the normal numbering pass sees it.
    """
    project = review_root / "review-projects" / project_id
    final_stage = project / "05_final_audit"
    source_path = final_stage / OVERVIEW_FIGURE_FILENAME
    draft_path = final_stage / "final_draft.md"
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        return {"available": False, "included": False, "reason": "overview figure is missing"}
    if not draft_path.is_file():
        return {"available": True, "included": False, "reason": "final draft is not available yet"}

    figure_dir = final_stage / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    figure_path = figure_dir / OVERVIEW_FIGURE_FILENAME
    shutil.copy2(source_path, figure_path)

    manuscript = draft_path.read_text(encoding="utf-8", errors="ignore")
    manuscript = OVERVIEW_FIGURE_BLOCK_RE.sub("\n", manuscript)
    metadata = {
        "figure_id": OVERVIEW_FIGURE_ID,
        "target_paragraph_id": "",
        "source_label": "Figure 1",
        "published_label": "Figure 1",
        "role": "review_overview",
    }
    block = (
        "<!-- review_overview_figure:start -->\n"
        "<!-- inserted_figure: "
        + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        + " -->\n\n"
        "![Figure 1](figures/overview_figure.png)\n\n"
        "Figure 1. Overview of the substrate classes, transformation logic, and representative reaction platforms covered in this review.\n"
        "<!-- review_overview_figure:end -->"
    )
    first_section = re.search(r"^##\s+", manuscript, re.M)
    if first_section:
        insert_at = first_section.start()
        manuscript = manuscript[:insert_at].rstrip() + "\n\n" + block + "\n\n" + manuscript[insert_at:].lstrip()
    else:
        manuscript = manuscript.rstrip() + "\n\n" + block + "\n"
    draft_path.write_text(manuscript, encoding="utf-8")
    return {"available": True, "included": True, "asset": str(figure_path)}


def regenerate_final_audit(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "04_first_draft"
    final_stage = project / "05_final_audit"
    conclusion = draft_stage / "conclusion_generated.md"
    report = draft_stage / "conclusion_quality_report.json"
    quality = read_json_if_exists(report) or {}
    draft_path = draft_stage / "first_draft.md"
    if not draft_path.exists():
        raise RuntimeError("Create the first draft before generating the final audit.")
    final_handoff = final_stage / "final_handoff.json"
    ensure_stage_handoff(
        final_handoff,
        "draft",
        [draft_path, draft_stage / "citations.json"],
    )
    conclusion_current = conclusion_artifacts_current(draft_path, conclusion, quality)
    if conclusion_current:
        run_project_script(
            review_root / "skills" / "review-final-audit-release" / "scripts" / "integrate_generated_conclusion.py",
            review_root,
            project_id,
        )
        conclusion_mode = "integrated_current_conclusion"
    else:
        # Conclusion generation is optional.  The final manuscript can always
        # be assembled from the current first draft, while a later validated
        # conclusion simply becomes an optional enhancement on the next run.
        final_stage.mkdir(parents=True, exist_ok=True)
        fallback_text = re.sub(
            r"(?m)^\s*<!--\s*paragraph_id:\s*[A-Za-z0-9_.:-]+\s*-->\s*\n?",
            "",
            draft_path.read_text(encoding="utf-8", errors="ignore"),
        )
        (final_stage / "final_draft.md").write_text(fallback_text, encoding="utf-8")
        source_figures = draft_stage / "figures"
        if source_figures.is_dir():
            shutil.copytree(source_figures, final_stage / "figures", dirs_exist_ok=True)
        write_json(
            final_stage / "conclusion_integration.json",
            {
                "schema_version": 1,
                "mode": "first_draft_without_optional_conclusion",
                "first_draft_path": str(draft_path.resolve()),
                "figure_assets_path": str((final_stage / "figures").resolve()) if source_figures.is_dir() else None,
            },
        )
        conclusion_mode = "first_draft_without_optional_conclusion"
    citations_payload = read_json_if_exists(draft_stage / "citations.json") or {}
    citation_entries = citations_payload.get("entries") if isinstance(citations_payload, dict) else []
    if isinstance(citation_entries, list) and citation_entries:
        refresh_reference_file(project, final_stage / "final_draft.md", citation_entries)
    else:
        sanitize_reference_file(final_stage / "final_draft.md")
    overview = inject_final_overview_figure(review_root, project_id)
    # The final manuscript is the publication boundary. Normalize again after
    # conclusion integration so any future template or manual ordering change
    # cannot leave captions/references with draft-manifest numbering.
    run_project_script(
        review_root / "skills" / "review-draft-merge-polish" / "scripts" / "renumber_figures_in_draft.py",
        review_root,
        project_id,
        extra=["--stage", "05_final_audit"],
    )
    run_project_script(review_root / "skills" / "review-final-audit-release" / "scripts" / "final_audit_scan.py", review_root, project_id)
    final_outputs = [final_stage / "final_draft.md"]
    if (final_stage / "overview_figure.png").exists():
        final_outputs.append(final_stage / "overview_figure.png")
    final_outputs.extend(sorted(path for path in (final_stage / "figures").glob("*") if path.is_file()))
    record_stage_outputs(final_handoff, final_outputs, "final")
    return {
        "final_draft": str(final_stage / "final_draft.md"),
        "overview": overview,
        "conclusion_mode": conclusion_mode,
    }


def generate_final_overview_figure(review_root: Path, project_id: str) -> dict[str, str]:
    """Generate the template-matched overview figure for the Final workspace."""
    project = review_root / "review-projects" / project_id
    outline_path = project / "01_matrix_outline" / "selected_outline.md"
    if not outline_path.is_file():
        raise RuntimeError("Choose a selected outline before generating the overview figure.")
    final_stage = project / "05_final_audit"
    output_path = final_stage / "overview_figure.png"
    overview_handoff = final_stage / "overview_figure_handoff.json"
    ensure_stage_handoff(
        overview_handoff,
        "blueprint",
        [
            project / "00_discovery" / "query_plan.draft.json",
            project / "00_discovery" / "selected_discovery_results.json",
            outline_path,
            project / "04_first_draft" / "first_draft.md",
        ],
    )
    overview_script = review_root / "skills" / "review-figure-style-redraw" / "scripts" / "generate_overview_figure.py"
    run_project_script(
        overview_script,
        review_root,
        project_id,
        timeout=600,
        extra=["--model", "gpt-image-2", "--output", str(output_path)],
    )
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise RuntimeError("Overview figure generation did not produce a PNG output.")
    record_stage_outputs(overview_handoff, [output_path], "final-overview-figure")
    integration = inject_final_overview_figure(review_root, project_id)
    if integration.get("included"):
        # Keep the preview action and the full final-draft action on the same
        # numbering path.  This is intentionally safe to repeat: the injector
        # removes its managed block before adding the updated overview figure.
        run_project_script(
            review_root / "skills" / "review-draft-merge-polish" / "scripts" / "renumber_figures_in_draft.py",
            review_root,
            project_id,
            extra=["--stage", "05_final_audit"],
        )
    final_handoff = final_stage / "final_handoff.json"
    if final_handoff.exists():
        outputs = [output_path]
        if (final_stage / "final_draft.md").exists():
            outputs.append(final_stage / "final_draft.md")
        record_stage_outputs(final_handoff, outputs, "final-overview-figure")
    return {
        "overview_figure": str(output_path),
        "included_in_final_draft": str(bool(integration.get("included"))).lower(),
    }


def regenerate_final_draft_bundle(review_root: Path, project_id: str) -> dict[str, str]:
    """Regenerate the final manuscript and its single full-review overview chart."""
    audit = regenerate_final_audit(review_root, project_id)
    final_draft = Path(str(audit["final_draft"]))
    # Keep this final guard after every audit-side script.  It protects the
    # overview asset from future integrations that replace final_draft.md
    # after the first insertion point has run.
    overview = inject_final_overview_figure(review_root, project_id)
    if overview.get("available") and not overview.get("included"):
        raise RuntimeError("The generated overview figure could not be inserted into final_draft.md.")
    if overview.get("included"):
        run_project_script(
            review_root / "skills" / "review-draft-merge-polish" / "scripts" / "renumber_figures_in_draft.py",
            review_root,
            project_id,
            extra=["--stage", "05_final_audit"],
        )
        final_text = final_draft.read_text(encoding="utf-8", errors="ignore")
        if not final_draft_contains_overview_figure(final_text):
            raise RuntimeError("The generated overview figure was not retained in final_draft.md.")
    refresh_final_overview_chart(review_root, project_id, final_draft)
    full_png = final_draft.parent / "review_summary_chart.png"
    if not full_png.is_file():
        raise RuntimeError("Overall review chart generation did not produce a PNG.")
    record_stage_outputs(
        final_draft.parent / "final_handoff.json",
        [final_draft, full_png, final_draft.parent / "overview_figure.png"],
        "final",
    )
    return {"final_draft": str(final_draft), "final_full_png": str(full_png)}


def generate_final_conclusion(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "04_first_draft"
    draft_path = draft_stage / "first_draft.md"
    if not draft_path.exists():
        raise RuntimeError("Create the first draft before generating a conclusion.")
    run_project_script(
        review_root / "skills" / "review-conclusion-generator" / "scripts" / "generate_conclusion1.py",
        review_root,
        project_id,
        timeout=900,
        extra=["--mode", "orchestrated"],
    )
    report = read_json_if_exists(draft_stage / "conclusion_quality_report.json") or {}
    if not (report.get("validation") or {}).get("passes_validation"):
        raise RuntimeError("Conclusion generation did not pass validation. Correct the draft or writing-model configuration before final audit.")
    return {"conclusion": str(draft_stage / "conclusion_generated.md"), "validation": "passed"}


def selected_from_combined(groups: list[dict], project_id: str) -> dict:
    selected = {"project_id": project_id, "keywords": [], "local_papers": {}, "web_papers": []}
    for group in groups:
        if group.get("keep") is False:
            continue
        selected["keywords"].append({"keyword": group.get("keyword"), "category": group.get("category")})
        for row in group.get("local_results", []):
            if row.get("keep") is False:
                continue
            pid = row.get("paper_id")
            if not pid:
                continue
            item = selected["local_papers"].setdefault(
                pid,
                {
                    "paper_id": pid,
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "journal": row.get("journal"),
                    "role": row.get("role", "uncertain"),
                    "matched_keywords": [],
                    "best_score": 0,
                    "keep": True,
                },
            )
            item["matched_keywords"].append(group.get("keyword"))
            item["best_score"] = max(item.get("best_score", 0), row.get("score", 0))
        for row in group.get("web_results", []):
            if row.get("keep") is not False:
                selected["web_papers"].append({**row, "matched_keyword": group.get("keyword")})
    selected["local_papers"] = sorted(
        selected["local_papers"].values(), key=lambda x: x.get("best_score", 0), reverse=True
    )[:30]
    selected["web_papers"] = selected["web_papers"][:30]
    return selected


def matrix_row_from_metadata(paper_id: str, metadata: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    structured_tags = value_of(metadata.get("structured_tags")) or {}
    keywords = [str(value) for value in structured_tags.values() if str(value).strip()] if isinstance(structured_tags, dict) else []
    abstract = str(value_of(metadata.get("abstract")) or "abstract unavailable or unreliable")
    row = {
        "paper_id": paper_id,
        "title": value_of(metadata.get("title")) or paper_id,
        "authors": value_of(metadata.get("authors")) or [],
        "keywords": keywords,
        "abstract": abstract,
        "main_content": "",
        "most_relevant_figure": {
            "source_label": "pending figure review",
            "caption": "Choose a source figure during the figure review stage.",
            "page_hint": "",
            "image_path": "",
            "relevance": "Pending full-paper review.",
        },
        "year": value_of(metadata.get("year")),
        "journal": value_of(metadata.get("journal")) or "",
        "doi": value_of(metadata.get("doi")) or "",
        "matrix_status": "needs_full_reading",
    }
    if existing:
        for key in ("main_content", "most_relevant_figure", "matrix_status"):
            if existing.get(key):
                row[key] = existing[key]
    return row


def matrix_reading_progress(rows: list[dict[str, Any]]) -> tuple[int, int]:
    total = len(rows)
    completed = sum(
        1 for row in rows
        if str(row.get("matrix_status") or "") == "full_reading_complete"
    )
    return completed, total


def matrix_outline_ready(stage: Path, matrix: dict[str, Any]) -> bool:
    selection = read_json_if_exists(stage / "selected_outline.meta.json") or {}
    sync = matrix.get("sync") if isinstance(matrix, dict) else {}
    return bool(
        (stage / "selected_outline.md").exists()
        and selection.get("selection_source") == "user"
        and selection.get("matrix_synced_at") == (sync or {}).get("synced_at")
    )


def write_matrix_reading_artifacts(stage: Path, matrix: dict[str, Any], saved_at: str) -> None:
    rows = matrix.get("rows") if isinstance(matrix, dict) else []
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    with (stage / "literature_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_id", "title", "authors", "keywords", "abstract", "main_content", "year", "journal", "doi", "matrix_status"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "paper_id": row.get("paper_id", ""),
                "title": row.get("title", ""),
                "authors": "; ".join(str(item) for item in row.get("authors") or []),
                "keywords": "; ".join(str(item) for item in row.get("keywords") or []),
                "abstract": row.get("abstract", ""),
                "main_content": row.get("main_content", ""),
                "year": row.get("year") or "",
                "journal": row.get("journal", ""),
                "doi": row.get("doi", ""),
                "matrix_status": row.get("matrix_status", "needs_full_reading"),
            })
    completed, total = matrix_reading_progress(rows)
    write_json(stage / "paper_reading_notes.json", {
        "source": "matrix_human_review",
        "saved_at": saved_at,
        "completed_count": completed,
        "total_count": total,
        "papers": [{"paper_id": row.get("paper_id"), "status": row.get("matrix_status")} for row in rows],
    })


def discovery_topic(project: Path) -> str:
    candidates = (
        project / "00_discovery" / "selected_discovery_results.json",
        project / "00_discovery" / "combined_results_by_keyword.json",
        project / "00_discovery" / "keyword_set.draft.json",
    )
    for path in candidates:
        data = read_json_if_exists(path) or {}
        if not isinstance(data, dict):
            continue
        for key in ("review_topic", "topic", "user_topic", "raw_topic"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    topic_path = project / "00_discovery" / "topic_input.md"
    if topic_path.is_file():
        text = read_text_if_exists(topic_path)
        text = re.sub(r"(?m)^#+\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return project.name.replace("-", " ")


def discovery_outline_hints(project: Path, tag_key: str) -> list[str]:
    data = read_json_if_exists(project / "00_discovery" / "keyword_set.draft.json") or {}
    if not isinstance(data, dict):
        return []
    topic = discovery_topic(project).casefold()
    accepted = {
        "substrate": {"substrate"},
        "catalyst_or_method": {"catalyst_or_method"},
        "reaction_type": {"reaction_type"},
    }.get(tag_key, {tag_key})
    hints: list[str] = []
    if tag_key == "substrate" and "substrate" in topic:
        hints.extend(str(item).strip() for item in data.get("user_keywords") or [])
    for item in data.get("merged_keywords") or []:
        if not isinstance(item, dict) or item.get("keep") is False:
            continue
        if str(item.get("category") or "") not in accepted:
            continue
        hints.append(str(item.get("keyword") or "").strip())
    result: list[str] = []
    for hint in hints:
        normalized = re.sub(r"\s+", " ", hint).strip(" .")
        if not normalized or normalized.casefold() in {"allene", "allenes", "method", "methods"}:
            continue
        if normalized.casefold() not in {item.casefold() for item in result}:
            result.append(normalized)
    return result[:10]


def _outline_search_text(row: dict[str, Any]) -> str:
    # This path is used specifically when structured classification has
    # collapsed into one label. Do not feed those same derived keywords back
    # into the fallback classifier; use paper evidence fields only.
    values = [row.get("title"), row.get("abstract"), row.get("main_content")]
    return re.sub(r"[^a-z0-9]+", " ", " ".join(str(value or "") for value in values).casefold()).strip()


def _outline_hint_score(hint: str, text: str) -> int:
    normalized = re.sub(r"[^a-z0-9]+", " ", hint.casefold()).strip()
    if not normalized:
        return 0
    score = 100 + len(normalized) if re.search(rf"\b{re.escape(normalized)}\b", text) else 0
    if "propargylic" in normalized and "derivative" in normalized:
        derivative_markers = (
            "acetate", "carbonate", "phosphate", "ester", "ether", "halide", "bromide",
            "chloride", "carbamate", "sulfinate", "sulfonate", "mesylate", "tosylate",
            "epoxide", "derivative", "alkynyl carbonate",
        )
        if ("propargylic" in text or "propargyl" in text or "alkynyl" in text) and any(
            marker in text for marker in derivative_markers
        ):
            score = max(score, 180)
    elif "propargylic alcohol" in normalized:
        if "propargylic alcohol" in text or "propargyl alcohol" in text or "tertiary alcohol" in text:
            score = max(score, 190)
    elif "terminal alkyne" in normalized:
        if "terminal alkyne" in text or "1 alkyne" in text or "acetylene" in text:
            score = max(score, 190)
    elif "enyne" in normalized:
        if any(marker in text for marker in ("enyne", "enynone", "1 6 addition", "1 6 conjugate")):
            score = max(score, 185)
    return score


def semantic_outline_groups(rows: list[dict[str, Any]], hints: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {hint: [] for hint in hints}
    related: list[str] = []
    for row in rows:
        paper_id = str(row.get("paper_id") or "")
        if not paper_id:
            continue
        text = _outline_search_text(row)
        title_text = re.sub(r"[^a-z0-9]+", " ", str(row.get("title") or "").casefold()).strip()
        ranked = sorted(
            (
                (_outline_hint_score(hint, title_text) * 10 + _outline_hint_score(hint, text), index, hint)
                for index, hint in enumerate(hints)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if ranked and ranked[0][0] > 0:
            groups[ranked[0][2]].append(paper_id)
        else:
            related.append(paper_id)
    result = {label: paper_ids for label, paper_ids in groups.items() if paper_ids}
    if related:
        result["Other and related methods"] = related
    return result


def outline_groups(review_root: Path, project_id: str, rows: list[dict[str, Any]], tag_key: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in rows:
        paper_id = str(row["paper_id"])
        metadata = read_json_if_exists(review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json") or {}
        tags = value_of(metadata.get("structured_tags")) or {}
        label = str(tags.get(tag_key) or "Other or unspecified").strip()
        if not label or label.casefold() == "not specified":
            label = "Other or unspecified"
        groups.setdefault(label, []).append(paper_id)
    meaningful = [paper_ids for label, paper_ids in groups.items() if not label.casefold().startswith("other")]
    largest_share = max((len(paper_ids) for paper_ids in meaningful), default=0) / max(1, len(rows))
    if len(rows) < 6 or (len(meaningful) >= 2 and largest_share < 0.85):
        return groups
    project = review_root / "review-projects" / project_id
    hints = discovery_outline_hints(project, tag_key)
    semantic = semantic_outline_groups(rows, hints) if len(hints) >= 2 else {}
    semantic_meaningful = [paper_ids for label, paper_ids in semantic.items() if not label.casefold().startswith("other")]
    return semantic if len(semantic_meaningful) >= 2 else groups


def outline_sections(groups: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    for index, (label, paper_ids) in enumerate(groups.items(), start=1):
        lines.extend(
            [
                f"## {index}. {label}",
                f"Assigned papers: {', '.join(paper_ids)}.",
                "Purpose: compare the selected papers within this review category.",
                "",
            ]
        )
    return lines


OUTLINE_STYLES = {
    "substrate": {
        "option_title": "Option A: Substrate-classified",
        "selected_title": "Substrate-classified",
        "tag_key": "substrate",
        "introduction": "Purpose: define the review scope and explain why substrate class is the primary comparison axis.",
    },
    "catalyst": {
        "option_title": "Option B: Catalyst and method-classified",
        "selected_title": "Catalyst and method-classified",
        "tag_key": "catalyst_or_method",
        "introduction": "Purpose: compare how catalytic systems control allene construction and stereochemical outcome.",
    },
    "reaction": {
        "option_title": "Option C: Reaction-type-classified",
        "selected_title": "Reaction-type-classified",
        "tag_key": "reaction_type",
        "introduction": "Purpose: organize the literature by transformation logic and mechanistic strategy.",
    },
}


def outline_style_definition(outline_style: str) -> dict[str, str]:
    definition = OUTLINE_STYLES.get(outline_style)
    if definition is None:
        allowed = ", ".join(OUTLINE_STYLES)
        raise ValueError(f"outline_style must be one of: {allowed}")
    return definition


def selected_outline_document(
    review_root: Path,
    project_id: str,
    rows: list[dict[str, Any]],
    outline_style: str,
    generated_at: str,
) -> str:
    definition = outline_style_definition(outline_style)
    groups = outline_groups(review_root, project_id, rows, definition["tag_key"])
    return "\n".join(
        [
            "# Selected Outline",
            "",
            f"Primary structure: {definition['selected_title']}.",
            f"Generated from {len(rows)} confirmed papers at {generated_at}.",
            "This working outline is used by Blueprint and later stages.",
            "",
            "## Introduction",
            "Purpose: define the review scope, terms, and comparison criteria.",
            "",
            *outline_sections(groups),
            "## Cross-category comparison and conclusion",
            "Purpose: compare substrate availability, catalyst requirements, selectivity, scope, and limitations.",
            "",
        ]
    )


def write_matrix_outline_documents(review_root: Path, project_id: str, rows: list[dict[str, Any]], synced_at: str) -> None:
    project = review_root / "review-projects" / project_id
    stage = project / "01_matrix_outline"
    options = [
        "# Outline Options",
        "",
        "These options were generated from the confirmed Discovery paper set. Review and refine the working outline before Blueprint.",
        "",
    ]
    for style, definition in OUTLINE_STYLES.items():
        options.extend(
            [
                f"# {definition['option_title']}",
                "",
                "## Introduction",
                definition["introduction"],
                "",
                *outline_sections(outline_groups(review_root, project_id, rows, definition["tag_key"])),
            ]
        )
    (stage / "outline_options.md").write_text("\n".join(options), encoding="utf-8")


def sync_matrix_from_discovery(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    discovery_path = project / "00_discovery" / "selected_discovery_results.json"
    selected = read_json_if_exists(discovery_path) or {}
    if not selected.get("human_confirmed"):
        raise ValueError("Discovery must be confirmed before synchronizing the literature matrix.")
    selected_rows = selected.get("local_papers") or []
    paper_ids = [str(row.get("paper_id")) for row in selected_rows if isinstance(row, dict) and row.get("paper_id")]
    if not paper_ids:
        raise ValueError("The confirmed discovery set contains no local papers.")

    stage = project / "01_matrix_outline"
    previous = read_json_if_exists(stage / "literature_matrix.json") or {}
    previous_rows = previous.get("rows", []) if isinstance(previous, dict) else previous
    existing_by_id = {
        str(row.get("paper_id")): row
        for row in previous_rows or []
        if isinstance(row, dict) and row.get("paper_id")
    }
    rows = []
    for paper_id in paper_ids:
        metadata = read_json_if_exists(review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json") or {}
        rows.append(matrix_row_from_metadata(paper_id, metadata, existing_by_id.get(paper_id)))

    stage.mkdir(parents=True, exist_ok=True)
    synced_at = now_utc()
    topic = discovery_topic(project)
    comparison_axes = discovery_outline_hints(project, "substrate")
    matrix = {
        "project_id": project_id,
        "review_topic": topic,
        "comparison_axes": comparison_axes,
        "rows": rows,
        "sync": {
            "source": "00_discovery/selected_discovery_results.json",
            "selected_paper_count": len(rows),
            "synced_at": synced_at,
            "status": "needs_full_reading",
        },
    }
    write_json(stage / "literature_matrix.json", matrix)
    write_matrix_reading_artifacts(stage, matrix, synced_at)
    write_matrix_outline_documents(review_root, project_id, rows, synced_at)
    previous_selection = read_json_if_exists(stage / "selected_outline.meta.json") or {}
    if previous_selection:
        write_json(
            stage / "selected_outline.meta.json",
            {**previous_selection, "selection_source": "stale", "matrix_synced_at": None},
        )
    (stage / "matrix_outline_report.md").write_text(
        "# Matrix Synchronization\n\n"
        f"Synchronized {len(rows)} confirmed Discovery papers at {synced_at}. "
        "Full-paper reading progress is tracked but does not block the workflow. Outline options were regenerated from this paper set; select an outline before creating Blueprint.\n",
        encoding="utf-8",
    )
    return {
        "selected_paper_count": len(rows),
        "synced_at": synced_at,
        "status": "needs_full_reading",
    }


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def infer_artifact_stage(path: Path) -> str:
    """Map a canonical project path to its producing workflow stage."""
    normalized = Path(path).as_posix()
    name = Path(path).name
    if "/00_discovery/" in normalized:
        return "discovery"
    if "/01_matrix_outline/" in normalized:
        return "blueprint" if name in {"selected_outline.md", "section_blueprint.json", "section_writing_plan.md"} else "matrix"
    if "/02_section_drafting/" in normalized:
        return "figure-review" if name == "human_figure_review.json" else "sections"
    if "/03_figure_redraw/" in normalized:
        return "figures"
    if "/04_first_draft/" in normalized:
        return "final-conclusion" if name.startswith("conclusion_") else "draft"
    if "/05_final_audit/" in normalized:
        return "final-overview-figure" if name == "overview_figure.png" else "final"
    return ""


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    context = workflow_context_for_path(path)
    if context:
        store, project_id = context
        store.register_artifact(project_id, path, producer_stage=infer_artifact_stage(path))


def artifact_freshness(handoff_path: Path, artifacts: list[Path]) -> dict[str, Any]:
    """Determine whether outputs still match their recorded SHA-256 versions.

    Legacy handoffs are upgraded in place against their current files.  The
    former timestamp fallback was inherently ambiguous: a correctly produced
    output is normally older than its handoff file, which made old projects
    appear stale immediately after loading them.
    """
    handoff = read_json_if_exists(handoff_path) or {}
    if not handoff_path.exists():
        return {
            "handoff": handoff,
            "versioned": False,
            "untracked": True,
            "stale": True,
            "outdated_artifacts": [str(path) for path in artifacts],
        }
    context = workflow_context_for_path(handoff_path)
    if not context:
        return {
            "handoff": handoff,
            "versioned": False,
            "stale": True,
            "migration_required": True,
            "outdated_artifacts": [str(path) for path in artifacts],
        }
    store, project_id = context
    if not isinstance(handoff, dict) or int(handoff.get("schema_version") or 0) < 2:
        producer_stage = infer_artifact_stage(artifacts[0]) if artifacts else str(handoff.get("source_stage") or "")
        store.complete_handoff(
            project_id,
            handoff_path,
            artifacts,
            producer_stage=producer_stage,
        )
    return store.handoff_freshness(project_id, handoff_path, artifacts)


def conclusion_artifacts_current(
    draft_path: Path,
    conclusion_path: Path,
    report: dict[str, Any],
) -> bool:
    """Validate conclusion lineage by content hash instead of file timestamps."""
    expected = str(report.get("first_draft_sha256") or "")
    return bool(
        draft_path.is_file()
        and conclusion_path.is_file()
        and re.fullmatch(r"[0-9a-f]{64}", expected)
        and sha256_file(draft_path) == expected
        and (report.get("validation") or {}).get("passes_validation") is True
    )


def section_candidate_dependency_fingerprint(section_drafts_path: Path) -> str:
    """Hash only the section structure that controls figure candidate routing."""
    def id_list(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if item]
        return [str(value)] if value else []

    payload = read_json_if_exists(section_drafts_path)
    sections = payload.get("sections") if isinstance(payload, dict) else payload
    if not isinstance(sections, list):
        return ""
    normalized: list[dict[str, Any]] = []
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        paragraphs: list[dict[str, Any]] = []
        for paragraph_index, paragraph in enumerate(section.get("paragraphs") or []):
            if not isinstance(paragraph, dict):
                continue
            paper_ids = id_list(paragraph.get("cited_paper_ids")) or id_list(paragraph.get("paper_id"))
            paragraphs.append(
                {
                    "index": paragraph_index,
                    "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                    "paper_ids": paper_ids,
                }
            )
        normalized.append(
            {
                "index": section_index,
                "section_id": str(section.get("section_id") or ""),
                "heading": str(section.get("heading") or section.get("title") or ""),
                "paper_ids": id_list(section.get("paper_ids")),
                "paragraph_markers": section.get("paragraph_markers") or [],
                "paragraphs": paragraphs,
            }
        )
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _section_handoff_output_paths(section_stage: Path, handoff: dict[str, Any]) -> list[Path]:
    recorded = [
        Path(str(item.get("path") or ""))
        for item in handoff.get("output_versions") or []
        if isinstance(item, dict) and item.get("path")
    ]
    defaults = [
        section_stage / "section_drafts.json",
        section_stage / "section_drafts.md",
        section_stage / "figure_candidates.json",
        section_stage / "paper_figure_candidates.json",
    ]
    return list(dict.fromkeys([*recorded, *defaults]))


def ensure_versioned_section_handoff(section_stage: Path) -> dict[str, Any]:
    """Upgrade an old Section handoff once and establish candidate lineage."""
    handoff_path = section_stage / "section_handoff.json"
    handoff = read_json_if_exists(handoff_path) or {}
    if not handoff_path.exists():
        return handoff
    context = workflow_context_for_path(handoff_path)
    if not context:
        return handoff
    store, project_id = context
    fingerprint = section_candidate_dependency_fingerprint(section_stage / "section_drafts.json")
    has_recorded_outputs = bool(isinstance(handoff, dict) and handoff.get("output_versions"))
    needs_upgrade = (
        not isinstance(handoff, dict)
        or int(handoff.get("schema_version") or 0) < 2
        or not isinstance(handoff.get("source_versions"), list)
        or (has_recorded_outputs and not str(handoff.get("candidate_dependency_fingerprint") or ""))
    )
    if needs_upgrade:
        store.complete_handoff(
            project_id,
            handoff_path,
            _section_handoff_output_paths(section_stage, handoff if isinstance(handoff, dict) else {}),
            producer_stage="sections",
        )
        handoff = store.update_handoff_metadata(
            project_id,
            handoff_path,
            {
                "candidate_dependency_fingerprint": fingerprint,
                "dependency_profile": "section-candidate-routing-v1",
            },
            producer_stage="sections",
        )
    return handoff


def section_source_freshness(section_stage: Path) -> dict[str, Any]:
    """Check the full Section output used by Draft and later stages.

    ``figure_candidates.json`` and ``paper_figure_candidates.json`` are updated
    when Stage 6 stores automatic or manual candidate selections. They are
    mutable Figure Review state, so only the authored Section JSON is checked.
    """
    ensure_versioned_section_handoff(section_stage)
    return artifact_freshness(
        section_stage / "section_handoff.json",
        [section_stage / "section_drafts.json"],
    )


def section_candidate_freshness(section_stage: Path) -> dict[str, Any]:
    """Check only changes that can invalidate candidate-to-paragraph routing."""
    handoff_path = section_stage / "section_handoff.json"
    handoff = ensure_versioned_section_handoff(section_stage)
    if not handoff_path.exists():
        return {
            "handoff": handoff,
            "versioned": False,
            "untracked": True,
            "stale": True,
            "outdated_artifacts": [str(section_stage / "section_drafts.json")],
        }
    context = workflow_context_for_path(handoff_path)
    if not context:
        return {
            "handoff": handoff,
            "versioned": False,
            "stale": True,
            "migration_required": True,
            "outdated_artifacts": [str(section_stage / "section_drafts.json")],
        }
    store, project_id = context
    state = store.handoff_freshness(project_id, handoff_path, [])
    recorded = str(handoff.get("candidate_dependency_fingerprint") or "")
    current = section_candidate_dependency_fingerprint(section_stage / "section_drafts.json")
    routing_changed = not current or current != recorded
    outdated = list(state.get("outdated_artifacts") or [])
    if routing_changed:
        outdated.append(str(section_stage / "section_drafts.json"))
    return {
        **state,
        "stale": bool(state.get("outdated_sources") or routing_changed),
        "outdated_artifacts": list(dict.fromkeys(outdated)),
        "candidate_dependency_fingerprint": current,
    }


def write_stage_handoff(
    path: Path,
    source_stage: str,
    source_artifacts: list[Path],
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    context = workflow_context_for_path(path)
    if context:
        store, project_id = context
        store.write_handoff(project_id, path, source_stage, source_artifacts, metadata=metadata)
        return
    payload = {
        "source_stage": source_stage,
        "generated_at": now_utc(),
        "source_artifacts": [str(artifact) for artifact in source_artifacts],
    }
    payload.update(metadata or {})
    write_json(path, payload)


def record_stage_outputs(
    handoff_path: Path,
    output_artifacts: list[Path],
    stage_id: str,
    *,
    producer_run_id: str | None = None,
) -> None:
    """Attach immutable output versions and their input lineage to a handoff."""
    context = workflow_context_for_path(handoff_path)
    if not context or not handoff_path.exists():
        return
    store, project_id = context
    store.complete_handoff(
        project_id,
        handoff_path,
        output_artifacts,
        producer_stage=stage_id,
        producer_run_id=producer_run_id,
    )
    if stage_id == "sections":
        section_stage = handoff_path.parent
        store.update_handoff_metadata(
            project_id,
            handoff_path,
            {
                "candidate_dependency_fingerprint": section_candidate_dependency_fingerprint(
                    section_stage / "section_drafts.json"
                ),
                "dependency_profile": "section-candidate-routing-v1",
            },
            producer_stage="sections",
        )


def ensure_stage_handoff(path: Path, source_stage: str, source_artifacts: list[Path]) -> None:
    """Create a new input boundary only when its content dependencies changed."""
    context = workflow_context_for_path(path)
    handoff = read_json_if_exists(path) or {}
    if (
        not context
        or not path.exists()
        or int(handoff.get("schema_version") or 0) < 2
        or not isinstance(handoff.get("source_versions"), list)
    ):
        write_stage_handoff(path, source_stage, source_artifacts)
        return
    store, project_id = context
    state = store.handoff_freshness(project_id, path, [])
    if state.get("outdated_sources"):
        write_stage_handoff(path, source_stage, source_artifacts)


def infer_project_topic(project: Path) -> str:
    discovery = read_json_if_exists(project / "00_discovery" / "combined_results_by_keyword.json")
    if isinstance(discovery, dict) and discovery.get("topic"):
        return str(discovery.get("topic"))
    topic_input = project / "00_discovery" / "topic_input.md"
    if topic_input.exists():
        for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
    bundle = read_json_if_exists(project / "04_first_draft" / "draft_bundle.json")
    if isinstance(bundle, dict) and bundle.get("topic"):
        return str(bundle.get("topic"))
    return ""


def list_review_projects(review_root: Path) -> list[dict[str, Any]]:
    base = review_root / "review-projects"
    projects: list[dict[str, Any]] = []
    if not base.exists():
        return projects
    for project in sorted(p for p in base.iterdir() if p.is_dir()):
        discovery_state = read_json_if_exists(project / "00_discovery" / "human_check_state.json") or {}
        projects.append(
            {
                "project_id": project.name,
                "topic": infer_project_topic(project),
                "has_discovery": (project / "00_discovery" / "combined_results_by_keyword.json").exists(),
                "discovery_status": discovery_state.get("status") or "pending",
                "has_matrix_outline": (project / "01_matrix_outline" / "literature_matrix.json").exists(),
                "has_blueprint": (project / "01_matrix_outline" / "section_blueprint.json").exists(),
                "has_section_drafting": (project / "02_section_drafting" / "section_drafts.md").exists(),
                "has_figure_redraw": (project / "03_figure_redraw" / "redrawn_figure_manifest.json").exists(),
                "has_first_draft": (project / "04_first_draft" / "first_draft.md").exists(),
                "has_final_audit": (project / "05_final_audit" / "final_draft.md").exists(),
            }
        )
    return projects


def delete_review_project(review_root: Path, project_id: str) -> dict[str, str]:
    """Permanently remove one validated direct child of review-projects."""
    if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
        raise ValueError("Invalid project ID.")
    base = (review_root / "review-projects").resolve()
    target = (base / project_id).resolve()
    if target.parent != base:
        raise ValueError("Invalid project ID.")
    if not target.is_dir():
        raise FileNotFoundError(project_id)
    shutil.rmtree(target)
    workflow_store(review_root).delete_project(project_id)
    with _BATCH_REDRAW_LOCK:
        _BATCH_REDRAW_JOBS.pop(project_id, None)
    return {"deleted_project_id": project_id}


def project_summary(review_root: Path, project_id: str) -> dict[str, Any] | None:
    return next((p for p in list_review_projects(review_root) if p["project_id"] == project_id), None)


def project_matrix_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "01_matrix_outline"
    discovery = read_json_if_exists(project / "00_discovery" / "selected_discovery_results.json") or {}
    matrix = read_json_if_exists(stage / "literature_matrix.json")
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "paper_reading_notes": read_json_if_exists(stage / "paper_reading_notes.json"),
        "literature_matrix": matrix,
        "literature_matrix_csv": read_text_if_exists(stage / "literature_matrix.csv"),
        "outline_options_md": read_text_if_exists(stage / "outline_options.md"),
        "reference_outline_candidates": (read_json_if_exists(stage / "reference_outline_candidates.json") or {}).get("candidates", []),
        "selected_outline_md": read_text_if_exists(stage / "selected_outline.md"),
        "outline_selection": read_json_if_exists(stage / "selected_outline.meta.json"),
        "matrix_outline_report_md": read_text_if_exists(stage / "matrix_outline_report.md"),
        "discovery_selection": {
            "human_confirmed": bool(discovery.get("human_confirmed")),
            "selected_paper_count": len(discovery.get("local_papers") or []),
        },
        "matrix_sync": matrix.get("sync") if isinstance(matrix, dict) else None,
        "paths": {"stage_dir": str(stage)},
    }


def project_blueprint_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "01_matrix_outline"
    matrix = read_json_if_exists(stage / "literature_matrix.json")
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "section_blueprint": read_json_if_exists(stage / "section_blueprint.json"),
        "section_writing_plan_md": read_text_if_exists(stage / "section_writing_plan.md"),
        "selected_outline_md": read_text_if_exists(stage / "selected_outline.md"),
        "upstream": {
            "selected_outline_md": read_text_if_exists(stage / "selected_outline.md"),
            "literature_matrix": matrix,
            "paper_reading_notes": read_json_if_exists(stage / "paper_reading_notes.json"),
        },
        "paths": {"stage_dir": str(stage)},
    }


def project_sections_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "02_section_drafting"
    matrix_stage = project / "01_matrix_outline"
    section_files = []
    sections_dir = stage / "sections"
    if sections_dir.exists():
        for path in sorted(sections_dir.glob("*.md")):
            section_files.append({"name": path.name, "path": str(path), "content": read_text_if_exists(path)})
    handoff_path = stage / "section_handoff.json"
    freshness = section_source_freshness(stage)
    handoff = read_json_if_exists(handoff_path) or {}
    has_existing_drafts = (stage / "section_drafts.json").is_file()
    if isinstance(handoff, dict):
        handoff = {
            **handoff,
            "drafts_stale": bool(freshness.get("stale")),
            "has_existing_drafts": has_existing_drafts,
            "freshness_mode": "sha256",
        }
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "section_tasks": read_json_if_exists(stage / "section_tasks.json"),
        "section_drafts": read_json_if_exists(stage / "section_drafts.json"),
        "section_drafts_md": read_text_if_exists(stage / "section_drafts.md"),
        "section_files": section_files,
        "paper_figure_candidates": read_json_if_exists(stage / "paper_figure_candidates.json"),
        "figure_candidates": read_json_if_exists(stage / "figure_candidates.json"),
        "section_drafting_report_md": read_text_if_exists(stage / "section_drafting_report.md"),
        "handoff": handoff,
        "upstream": {
            "selected_outline_md": read_text_if_exists(matrix_stage / "selected_outline.md"),
            "section_blueprint": read_json_if_exists(matrix_stage / "section_blueprint.json"),
            "literature_matrix": read_json_if_exists(matrix_stage / "literature_matrix.json"),
        },
        "paths": {"stage_dir": str(stage), "sections_dir": str(sections_dir)},
    }


def project_figures_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "02_section_drafting"
    ensure_default_figure_reviews(draft_stage)
    stage = project / "03_figure_redraw"
    source_freshness = section_candidate_freshness(draft_stage)
    redraw_freshness = artifact_freshness(stage / "figures_handoff.json", [stage / "redrawn_figure_manifest.json"])
    candidate_data = read_json_if_exists(draft_stage / "figure_candidates.json") or []
    candidate_rows = (
        candidate_data.get("figures") or candidate_data.get("candidates") or []
        if isinstance(candidate_data, dict)
        else candidate_data
    )
    candidate_by_id = {
        str(row.get("figure_id") or ""): row
        for row in candidate_rows or []
        if isinstance(row, dict) and row.get("figure_id")
    }
    redrawn_manifest = read_json_if_exists(stage / "redrawn_figure_manifest.json")
    if isinstance(redrawn_manifest, dict):
        redrawn_manifest = dict(redrawn_manifest)
        rows: list[dict[str, Any]] = []
        for raw_row in redrawn_manifest.get("figures") or []:
            row = dict(raw_row) if isinstance(raw_row, dict) else raw_row
            if isinstance(row, dict):
                figure_id = str(row.get("figure_id") or "")
                candidate = candidate_by_id.get(figure_id)
                approval = dict(row.get("human_approval") or {})
                if approval and isinstance(candidate, dict):
                    try:
                        current_source = _resolve_candidate_source(review_root, project, candidate)
                        current_source_hash = sha256_file(current_source)
                        source_match = bool(
                            _normalized_figure_path(str(approval.get("source_image") or ""))
                            == _normalized_figure_path(current_source)
                            and str(approval.get("source_sha256") or "") == current_source_hash
                        )
                    except (OSError, ValueError):
                        source_match = False
                    output = resolve_redrawn_base_path(review_root, project, stage, row)
                    output_match = bool(
                        output
                        and str(approval.get("output_sha256") or "")
                        and sha256_file(output) == str(approval.get("output_sha256") or "")
                    )
                    approval["current_source_match"] = source_match
                    approval["current_output_match"] = output_match
                    row["human_approval"] = approval
                elif approval:
                    approval["current_source_match"] = False
                    approval["current_output_match"] = False
                    row["human_approval"] = approval
                if isinstance(candidate, dict):
                    try:
                        ratio_source = _resolve_candidate_source(review_root, project, candidate)
                        ratio_output = resolve_redrawn_base_path(review_root, project, stage, row)
                        if ratio_output:
                            row["aspect_ratio_integrity"] = figure_aspect_ratio_integrity(
                                ratio_source,
                                ratio_output,
                            )
                    except (OSError, ValueError):
                        row["aspect_ratio_integrity"] = {"status": "unavailable"}
                integrity_status = str((row.get("chemistry_integrity") or {}).get("status") or "")
                requires_approval = bool(
                    integrity_status in {"failed", "needs_human_arrow_check"}
                    or str(row.get("output_disposition") or "") == "saved_with_integrity_warning"
                )
                human_approved = bool(
                    approval.get("status") == "approved"
                    and approval.get("current_source_match")
                    and approval.get("current_output_match")
                )
                if requires_approval and not human_approved:
                    preview = resolve_redrawn_base_path(review_root, project, stage, row)
                    if preview:
                        row["rejected_preview_image"] = str(preview)
                        row["rejected_preview_status"] = "awaiting_human_approval"
                elif not row.get("redrawn_image") and not row.get("rejected_preview_image"):
                    previews = list((stage / "redrawn").glob(f"{figure_id}.*")) if figure_id else []
                    preview = next((path for path in previews if path.is_file()), None)
                    if preview:
                        row["rejected_preview_image"] = str(preview)
                        row["rejected_preview_status"] = "not_approved_for_manuscript"
            rows.append(row)
        redrawn_manifest["figures"] = rows
    selected_ids = {
        str(row.get("figure_id") or "")
        for row in candidate_rows or []
        if isinstance(row, dict)
        and row.get("figure_id")
        and row.get("manuscript_selected") is not False
    }
    usable_ids: set[str] = set()
    for row in (redrawn_manifest or {}).get("figures") or []:
        if not isinstance(row, dict) or str(row.get("status") or "") != "redrawn":
            continue
        figure_id = str(row.get("figure_id") or "")
        candidate = candidate_by_id.get(figure_id)
        if not figure_id or not isinstance(candidate, dict):
            continue
        try:
            current_source = _resolve_candidate_source(review_root, project, candidate)
        except (OSError, ValueError):
            continue
        recorded_source = str(row.get("source_image") or "")
        if not recorded_source or _normalized_figure_path(recorded_source) != _normalized_figure_path(current_source):
            continue
        recorded_source_hash = str(row.get("source_image_sha256") or "")
        if recorded_source_hash and sha256_file(current_source) != recorded_source_hash:
            continue
        output = resolve_redrawn_base_path(review_root, project, stage, row)
        if output is None:
            continue
        if figure_aspect_ratio_integrity(current_source, output).get("status") != "pass":
            continue
        integrity_status = str((row.get("chemistry_integrity") or {}).get("status") or "")
        requires_approval = bool(
            integrity_status in {"failed", "needs_human_arrow_check"}
            or str(row.get("output_disposition") or "") == "saved_with_integrity_warning"
        )
        approval = row.get("human_approval") or {}
        if requires_approval and not (
            approval.get("status") == "approved"
            and approval.get("current_source_match")
            and approval.get("current_output_match")
        ):
            continue
        usable_ids.add(figure_id)
    semantic_redraw_stale = bool(selected_ids - usable_ids)
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "figure_candidates": candidate_data,
        "redrawn_manifest": redrawn_manifest,
        "batch_redraw": batch_figure_redraw_status(project_id),
        "figure_redraw_report_md": read_text_if_exists(stage / "figure_redraw_report.md"),
        "freshness": {
            "source_stale": source_freshness["stale"],
            "redraw_stale": redraw_freshness["stale"] or semantic_redraw_stale,
            "semantic_redraw_stale": semantic_redraw_stale,
            "selected_count": len(selected_ids),
            "usable_count": len(usable_ids & selected_ids),
            "stale": source_freshness["stale"] or redraw_freshness["stale"] or semantic_redraw_stale,
        },
        "upstream": {
            "section_drafts": read_json_if_exists(draft_stage / "section_drafts.json"),
            "section_drafts_md": read_text_if_exists(draft_stage / "section_drafts.md"),
            "figure_candidates": candidate_data,
        },
        "paths": {"stage_dir": str(stage), "draft_stage_dir": str(draft_stage)},
    }


def default_redrawable_candidate(paper: dict[str, Any]) -> dict[str, Any] | None:
    """Return a stable highest-scoring Figure Review default, excluding tables."""
    candidates = []
    for candidate in paper.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("source_type") == "table" or not candidate.get("source_image_path"):
            continue
        if not isinstance(candidate.get("candidate_index"), int):
            continue
        try:
            score = float(candidate.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        candidates.append((score, -candidate["candidate_index"], candidate))
    return max(candidates, default=(0.0, 0, None), key=lambda item: (item[0], item[1]))[2]


def sync_reviewed_figure_candidates(stage: Path, candidates_data: dict[str, Any], reviews: dict[str, Any]) -> bool:
    """Make Stage 7 immediately reflect every saved Stage 6 selection."""
    papers = candidates_data.get("papers") if isinstance(candidates_data, dict) else []
    if not isinstance(papers, list) or not isinstance(reviews, dict):
        return False
    manifest_path = stage / "figure_candidates.json"
    manifest = read_json_if_exists(manifest_path) or []
    if isinstance(manifest, dict):
        manifest = manifest.get("figures") or manifest.get("candidates") or []
    if not isinstance(manifest, list):
        manifest = []
    changed = False
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paper_id") or "")
        review = reviews.get(paper_id)
        selected_index = review.get("selected_candidate_index") if isinstance(review, dict) else None
        if not paper_id or isinstance(selected_index, bool) or not isinstance(selected_index, int):
            continue
        candidate = next(
            (item for item in paper.get("candidates") or [] if isinstance(item, dict) and item.get("candidate_index") == selected_index),
            None,
        )
        if not isinstance(candidate, dict) or candidate.get("source_type") == "table" or not candidate.get("source_image_path"):
            continue
        existing = [item for item in manifest if isinstance(item, dict) and str(item.get("paper_id") or "") == paper_id]
        if len(existing) == 1 and existing[0].get("source_image_path") == candidate.get("source_image_path"):
            continue
        try:
            sync_selected_candidate_for_redraw(stage.parent, paper_id, candidate)
            changed = True
        except RuntimeError:
            # A candidate without a paragraph anchor remains available for a manual
            # review; do not make the whole Figure Review page unavailable.
            continue
        manifest = read_json_if_exists(manifest_path) or []
        if isinstance(manifest, dict):
            manifest = manifest.get("figures") or manifest.get("candidates") or []
        if not isinstance(manifest, list):
            manifest = []
    return changed


FIGURE_REVIEW_DEPENDENCY_PROFILE = "candidate-manifests-and-selected-sources-v2"


def figure_review_dependency_paths(stage: Path) -> list[Path]:
    """Return candidate manifests plus only the sources selected by Stage 6."""
    paths = [stage / "figure_candidates.json", stage / "paper_figure_candidates.json"]
    raw_sources: list[str] = []
    reviews_data = read_json_if_exists(stage / "human_figure_review.json") or {}
    reviews = reviews_data.get("papers") if isinstance(reviews_data, dict) else {}
    selected_by_paper = (
        {
            str(paper_id): review.get("selected_candidate_index")
            for paper_id, review in reviews.items()
            if isinstance(review, dict)
        }
        if isinstance(reviews, dict)
        else {}
    )
    figures = read_json_if_exists(paths[0]) or []
    if isinstance(figures, dict):
        figures = figures.get("figures") or figures.get("candidates") or []
    for candidate in figures if isinstance(figures, list) else []:
        if isinstance(candidate, dict) and candidate.get("manuscript_selected"):
            raw = candidate.get("source_image_path") or candidate.get("source_path") or candidate.get("image_path")
            if raw:
                raw_sources.append(str(raw))
    paper_candidates = read_json_if_exists(paths[1]) or {}
    papers = paper_candidates.get("papers") if isinstance(paper_candidates, dict) else []
    for paper in papers if isinstance(papers, list) else []:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paper_id") or "")
        selected_index = selected_by_paper.get(paper_id)
        for candidate in paper.get("candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("candidate_index") != selected_index:
                continue
            raw = candidate.get("source_image_path") or candidate.get("source_path") or candidate.get("image_path")
            if raw:
                raw_sources.append(str(raw))
    project = stage.parent
    review_root = project.parent.parent
    for raw in raw_sources:
        candidate = Path(raw)
        if candidate.is_absolute():
            paths.append(candidate)
            continue
        options = [stage / candidate, project / candidate, review_root / candidate]
        paths.append(next((item for item in options if item.is_file()), options[0]))
    return list(dict.fromkeys(Path(item).resolve() for item in paths))


def refresh_figure_review_handoff(stage: Path, *, accept_current: bool = False) -> None:
    """Migrate or deliberately rebase the Stage 6 review dependency boundary."""
    review_path = stage / "human_figure_review.json"
    if not review_path.is_file():
        return
    handoff_path = stage.parent / "03_figure_redraw" / "figure_review_handoff.json"
    handoff = read_json_if_exists(handoff_path) or {}
    migration_required = (
        not handoff_path.is_file()
        or not isinstance(handoff, dict)
        or int(handoff.get("schema_version") or 0) < 2
        or handoff.get("dependency_profile") != FIGURE_REVIEW_DEPENDENCY_PROFILE
    )
    if not migration_required and not accept_current:
        return
    write_stage_handoff(
        handoff_path,
        "sections",
        figure_review_dependency_paths(stage),
        metadata={"dependency_profile": FIGURE_REVIEW_DEPENDENCY_PROFILE},
    )
    record_stage_outputs(handoff_path, [review_path], "figure-review")


def ensure_default_figure_reviews(stage: Path) -> dict[str, Any]:
    """Persist defaults only for papers without an existing user or prior default choice."""
    candidates_path = stage / "paper_figure_candidates.json"
    candidates_data = read_json_if_exists(candidates_path) or {}
    papers = candidates_data.get("papers") if isinstance(candidates_data, dict) else []
    if not isinstance(papers, list):
        return {"papers": {}}
    review_path = stage / "human_figure_review.json"
    reviews_data = read_json_if_exists(review_path) or {"papers": {}}
    if not isinstance(reviews_data, dict):
        reviews_data = {"papers": {}}
    reviews = reviews_data.setdefault("papers", {})
    if not isinstance(reviews, dict):
        reviews = {}
        reviews_data["papers"] = reviews
    candidates_changed = False
    reviews_changed = False
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("paper_id") or "")
        if not paper_id or isinstance(reviews.get(paper_id), dict):
            continue
        candidate = default_redrawable_candidate(paper)
        if not candidate:
            continue
        selected_index = candidate["candidate_index"]
        if paper.get("selected_candidate_index") != selected_index:
            paper["selected_candidate_index"] = selected_index
            paper["status"] = "auto_selected"
            candidates_changed = True
        reviews[paper_id] = {
            "selected_candidate_index": selected_index,
            "selected_source_image_path": str(candidate.get("source_image_path") or ""),
            "review_note": "Automatically selected as the highest-scoring redrawable candidate.",
            "selection_source": "automatic_top_score",
            "reviewed_at": now_utc(),
        }
        reviews_changed = True
    if candidates_changed:
        write_json(candidates_path, candidates_data)
    if reviews_changed:
        reviews_data.setdefault("source", "automatic_top_score")
        reviews_data["generated_at"] = now_utc()
        write_json(review_path, reviews_data)
    redraw_inputs_changed = sync_reviewed_figure_candidates(stage, candidates_data, reviews)
    if candidates_changed or reviews_changed or redraw_inputs_changed:
        refresh_figure_review_handoff(stage, accept_current=True)
    return reviews_data


def project_figure_review_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "02_section_drafting"
    source_freshness = section_candidate_freshness(stage)
    reviews = ensure_default_figure_reviews(stage)
    refresh_figure_review_handoff(stage)
    candidates_data = read_json_if_exists(stage / "paper_figure_candidates.json") or {}
    review_freshness = artifact_freshness(
        project / "03_figure_redraw" / "figure_review_handoff.json",
        [stage / "human_figure_review.json"],
    )
    review_rows = reviews.get("papers") if isinstance(reviews, dict) else {}
    papers: list[dict[str, Any]] = []
    for row in candidates_data.get("papers", []) if isinstance(candidates_data, dict) else []:
        if not isinstance(row, dict) or not row.get("paper_id"):
            continue
        paper_id = str(row["paper_id"])
        meta = read_json_if_exists(review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json") or {}
        item = dict(row)
        item["title"] = value_of(meta.get("title")) or paper_id
        human_review = review_rows.get(paper_id, {}) if isinstance(review_rows, dict) else {}
        item["human_review"] = human_review
        selected_index = human_review.get("selected_candidate_index") if isinstance(human_review, dict) else None
        if isinstance(selected_index, int) and not isinstance(selected_index, bool):
            item["selected_candidate_index"] = selected_index
        papers.append(item)
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "papers": papers,
        "freshness": {
            "source_stale": source_freshness["stale"],
            "review_stale": review_freshness["stale"],
            "stale": source_freshness["stale"] or review_freshness["stale"],
        },
        "paths": {"stage_dir": str(stage)},
    }


def latest_final_docx_path(stage: Path) -> Path:
    """Return the latest successful DOCX export without following paths outside its stage."""
    default_path = stage / "final_draft.docx"
    manifest = read_json_if_exists(stage / "docx_export.json") or {}
    output_name = manifest.get("output_path") if isinstance(manifest, dict) else None
    if isinstance(output_name, str) and output_name:
        candidate = stage / output_name
        try:
            candidate.resolve().relative_to(stage.resolve())
        except ValueError:
            candidate = default_path
        if candidate.suffix.lower() == ".docx" and candidate.exists():
            return candidate

    # Covers exports made before docx_export.json was introduced, including
    # manually named revisions created while the primary output was locked.
    candidates = [
        path for path in stage.glob("final_draft*.docx")
        if not path.name.startswith("~$")
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else default_path


def project_final_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "05_final_audit"
    draft_stage = project / "04_first_draft"
    docx_path = latest_final_docx_path(stage)
    docx_current = docx_export_is_current(stage, docx_path)
    section_stage = project / "02_section_drafting"
    source_freshness = section_source_freshness(section_stage)
    draft_dependency_freshness = project_draft_payload(review_root, project_id)["freshness"]
    final_draft_path = stage / "final_draft.md"
    final_freshness = artifact_freshness(stage / "final_handoff.json", [final_draft_path])
    draft_path = draft_stage / "first_draft.md"
    conclusion_path = draft_stage / "conclusion_generated.md"
    conclusion_report = read_json_if_exists(draft_stage / "conclusion_quality_report.json") or {}
    overview_figure = stage / "overview_figure.png"
    release_full_png = stage / "review_summary_chart.png"
    final_draft_text = read_text_if_exists(final_draft_path)
    overview_included = final_draft_contains_overview_figure(final_draft_text)
    overview_freshness = artifact_freshness(
        stage / "overview_figure_handoff.json",
        [overview_figure],
    ) if overview_figure.exists() else {"stale": False, "outdated_artifacts": []}
    overview_stale = overview_figure.exists() and (not overview_included or overview_freshness["stale"])
    conclusion_current = conclusion_artifacts_current(
        draft_path,
        conclusion_path,
        conclusion_report,
    )
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "final_draft_md": final_draft_text,
        "final_audit_report_md": read_text_if_exists(stage / "final_audit_report.md"),
        "release_report_md": read_text_if_exists(stage / "release_report.md"),
        "conclusion_generated_md": read_text_if_exists(conclusion_path),
        "conclusion_current": conclusion_current,
        "upstream": {
            "first_draft_md": read_text_if_exists(project / "04_first_draft" / "first_draft.md"),
            "selected_outline_md": read_text_if_exists(project / "01_matrix_outline" / "selected_outline.md"),
            "section_drafts": read_json_if_exists(project / "02_section_drafting" / "section_drafts.json"),
            "figure_candidates": read_json_if_exists(project / "02_section_drafting" / "figure_candidates.json"),
        },
        "final_draft_docx_path": str(docx_path),
        "final_draft_docx_exists": docx_current,
        "final_draft_docx_stale": docx_path.exists() and not docx_current,
        "release_chart_full_png_path": str(release_full_png),
        "release_chart_full_png_exists": release_full_png.exists(),
        "overview_figure_path": str(overview_figure),
        "overview_figure_exists": overview_figure.exists(),
        "overview_figure_included": overview_included,
        "freshness": {
            "source_stale": source_freshness["stale"],
            "draft_stale": draft_dependency_freshness["stale"],
            "final_stale": final_freshness["stale"] or overview_stale,
            "overview_stale": overview_stale,
            "stale": source_freshness["stale"] or draft_dependency_freshness["stale"] or final_freshness["stale"] or overview_stale,
        },
        "paths": {"stage_dir": str(stage)},
    }


def project_draft_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage_dir = project / "04_first_draft"
    figures_manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    draft_bundle = read_json_if_exists(stage_dir / "draft_bundle.json")
    section_drafts = read_json_if_exists(project / "02_section_drafting" / "section_drafts.json")
    section_stage = project / "02_section_drafting"
    source_freshness = section_source_freshness(section_stage)
    draft_freshness = artifact_freshness(stage_dir / "draft_handoff.json", [stage_dir / "first_draft.md"])
    figures_freshness = project_figures_payload(review_root, project_id)["freshness"]
    redrawn = []
    for row in (figures_manifest.get("figures") or []):
        if isinstance(row, dict):
            redrawn.append(row)
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": next((p for p in list_review_projects(review_root) if p["project_id"] == project_id), None),
        "draft_bundle": draft_bundle,
        "first_draft_md": read_text_if_exists(stage_dir / "first_draft.md"),
        "merge_report_md": read_text_if_exists(stage_dir / "merge_report.md"),
        "remaining_issues_md": read_text_if_exists(stage_dir / "remaining_issues.md"),
        "section_drafts": section_drafts,
        "redrawn_figures": redrawn,
        "freshness": {
            "source_stale": source_freshness["stale"],
            "draft_stale": draft_freshness["stale"],
            "figures_stale": figures_freshness["stale"],
            "upstream_stale": source_freshness["stale"] or figures_freshness["stale"],
            "stale": source_freshness["stale"] or draft_freshness["stale"] or figures_freshness["stale"],
        },
        "upstream": {
            "selected_outline_md": read_text_if_exists(project / "01_matrix_outline" / "selected_outline.md"),
            "literature_matrix": read_json_if_exists(project / "01_matrix_outline" / "literature_matrix.json"),
            "section_blueprint": read_json_if_exists(project / "01_matrix_outline" / "section_blueprint.json"),
            "section_drafts": section_drafts,
            "figure_candidates": read_json_if_exists(project / "02_section_drafting" / "figure_candidates.json"),
            "redrawn_figure_manifest": figures_manifest,
        },
        "paths": {
            "stage_dir": str(stage_dir),
            "first_draft_base_dir": str(stage_dir),
            "first_draft": str(stage_dir / "first_draft.md"),
            "merge_report": str(stage_dir / "merge_report.md"),
            "remaining_issues": str(stage_dir / "remaining_issues.md"),
        },
    }


def dashboard_assets(view_root: Path) -> tuple[Path, ...]:
    dashboard = view_root / "assets" / "dashboard"
    library_path = dashboard / "library.html"
    discovery_path = dashboard / "discovery.html"
    matrix_path = dashboard / "matrix.html"
    blueprint_path = dashboard / "blueprint.html"
    sections_path = dashboard / "sections.html"
    figures_path = dashboard / "figures.html"
    figure_review_path = dashboard / "figure-review.html"
    draft_path = dashboard / "draft.html"
    final_path = dashboard / "final.html"
    paths = [library_path, discovery_path, matrix_path, blueprint_path, sections_path, figures_path, figure_review_path, draft_path, final_path]
    if any(not path.exists() for path in paths):
        raise FileNotFoundError(f"dashboard assets not found under {view_root / 'assets' / 'dashboard'}")
    return tuple(paths)


def run(args: argparse.Namespace) -> int:
    review_root = Path(args.review_root).resolve()
    os.environ["REVIEW_WRITER_PREFECT_ENABLED"] = "true"
    configure_prefect_environment(review_root)
    view_root = Path(__file__).resolve().parent
    (
        library_app_path,
        discovery_app_path,
        matrix_app_path,
        blueprint_app_path,
        sections_app_path,
        figures_app_path,
        figure_review_app_path,
        draft_app_path,
        final_app_path,
    ) = dashboard_assets(view_root)
    if not (review_root / "review-library" / "metadata" / "papers").exists():
        print("ERROR: metadata files not found. Run prepare_metadata.py first.", file=sys.stderr)
        return 2
    store = workflow_store(review_root)
    projects_root = review_root / "review-projects"
    if projects_root.is_dir():
        for project in sorted(path for path in projects_root.iterdir() if path.is_dir()):
            try:
                store.bootstrap_project(project.name)
            except OSError as exc:
                print(f"WARNING: workflow metadata bootstrap skipped {project.name}: {exc}", file=sys.stderr)
    DashboardHandler.review_root = review_root
    DashboardHandler.library_app_path = library_app_path
    DashboardHandler.discovery_app_path = discovery_app_path
    DashboardHandler.matrix_app_path = matrix_app_path
    DashboardHandler.blueprint_app_path = blueprint_app_path
    DashboardHandler.sections_app_path = sections_app_path
    DashboardHandler.figures_app_path = figures_app_path
    DashboardHandler.figure_review_app_path = figure_review_app_path
    DashboardHandler.draft_app_path = draft_app_path
    DashboardHandler.final_app_path = final_app_path
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Serving dashboard at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve local review metadata dashboard.")
    parser.add_argument("--review-root", default="/home/ps/review-writer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
