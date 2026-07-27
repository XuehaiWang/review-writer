#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import importlib.util
import json
import mimetypes
import posixpath
import re
import shutil
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


_PARAGRAPH_SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "review-draft-merge-polish" / "scripts"
if str(_PARAGRAPH_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PARAGRAPH_SCRIPTS))
from paragraph_editor import ParagraphEditor
from paragraph_manifest_builder import build_manifest


_DISCOVERY_MODULE = None
_CONCLUSION_INTEGRATION_MODULE = None


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


def conclusion_integration_module(review_root: Path):
    """Load the final-audit integration helper without duplicating its placement rules."""
    global _CONCLUSION_INTEGRATION_MODULE
    if _CONCLUSION_INTEGRATION_MODULE is None:
        script = review_root / "skills" / "review-final-audit-release" / "scripts" / "integrate_generated_conclusion.py"
        spec = importlib.util.spec_from_file_location("review_generated_conclusion_integration", script)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the conclusion integration helper.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONCLUSION_INTEGRATION_MODULE = module
    return _CONCLUSION_INTEGRATION_MODULE


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
        elif parsed.path == "/api/discovery-projects":
            self.handle_discovery_projects()
        elif parsed.path.startswith("/api/project/") and parsed.path.endswith("/draft"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_draft_get(project_id)
        elif parsed.path.startswith("/api/project/") and "/paragraph" in parsed.path:
            self.handle_paragraph_get(parsed.path)
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
        if parsed.path.startswith("/api/project/") and "/figures/" in parsed.path:
            parts = parsed.path.strip("/").split("/")
            if len(parts) == 6 and parts[0:2] == ["api", "project"] and parts[3] == "figures" and parts[5] == "redraw":
                self.handle_current_figure_redraw(unquote(parts[2]), unquote(parts[4]))
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
        try:
            sync_selected_candidate_for_redraw(project, paper_id, candidate)
        except RuntimeError as exc:
            self.send_json(
                {
                    "ok": False,
                    "project_id": project_id,
                    "paper_id": paper_id,
                    "selected_candidate_index": candidate_index,
                    "error": f"Candidate was saved, but could not be prepared for the batch redraw: {exc}",
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

    def handle_section_tasks_start(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        blueprint_path = project / "01_matrix_outline" / "section_blueprint.json"
        blueprint = read_json_if_exists(blueprint_path)
        sections = blueprint.get("sections") if isinstance(blueprint, dict) else None
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        if not isinstance(sections, list) or not sections:
            self.send_json(
                {"ok": False, "error": "No Blueprint sections are available. Select an outline and generate Blueprint first."},
                status=HTTPStatus.CONFLICT,
            )
            return
        tasks = []
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
                    "must_cover_points": [str(claim.get("claim") or "") for claim in claims if isinstance(claim, dict) and claim.get("claim")],
                    "avoid_points": [str(item) for item in section.get("avoid_patterns") or []],
                    "figure_need": section.get("figure_or_table_needs") or [],
                    "source_blueprint": str(blueprint_path),
                    "created_at": now_utc(),
                }
            )
        if not tasks:
            self.send_json({"ok": False, "error": "Blueprint contains no usable sections."}, status=HTTPStatus.CONFLICT)
            return
        stage = project / "02_section_drafting"
        generated_at = now_utc()
        write_json(stage / "section_tasks.json", tasks)
        write_json(
            stage / "section_handoff.json",
            {
                "source_stage": "blueprint",
                "source_blueprint": str(blueprint_path),
                "generated_at": generated_at,
                "task_count": len(tasks),
                "section_ids": [task["section_id"] for task in tasks],
            },
        )
        (stage / "section_tasks_handoff.md").write_text(
            "# Blueprint to Sections Handoff\n\n"
            f"Generated {len(tasks)} section tasks from `01_matrix_outline/section_blueprint.json` at {generated_at}.\n",
            encoding="utf-8",
        )
        self.send_json(
            {
                "ok": True,
                "project_id": project_id,
                "task_count": len(tasks),
                "next_path": f"/sections?project={quote(project_id)}",
            }
        )

    def handle_project_stage_run(self, project_id: str, stage_id: str) -> None:
        """Run the deterministic part of a stage before handing it to the next page."""
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_json({"ok": False, "error": "Project not found."}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            if stage_id == "sections":
                result = regenerate_section_drafting(self.review_root, project_id)
                next_stage = "figure-review"
            elif stage_id == "figure-review":
                result = validate_figure_review(project, project_id)
                next_stage = "figures"
            elif stage_id == "figures":
                result = regenerate_figures(self.review_root, project_id)
                next_stage = "draft"
            elif stage_id == "draft":
                result = regenerate_first_draft(self.review_root, project_id)
                next_stage = "final"
            elif stage_id == "final-conclusion":
                result = generate_final_conclusion(self.review_root, project_id)
                next_stage = "final"
            elif stage_id == "final-outline-chart":
                result = generate_outline_chart_preview(self.review_root, project_id)
                next_stage = "final"
            elif stage_id == "final":
                result = regenerate_final_draft_bundle(self.review_root, project_id)
                next_stage = None
            else:
                raise ValueError("This page does not have a runnable generation step.")
        except (RuntimeError, ValueError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return
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
            freshness = artifact_freshness(
                stage / "section_handoff.json",
                [stage / "section_drafts.json", stage / "figure_candidates.json", stage / "paper_figure_candidates.json"],
            )
            if freshness["stale"]:
                error = "Blueprint has changed. Regenerate section drafts and figure candidates before continuing to Figure Review."
            elif not (stage / "section_drafts.json").exists() or not (stage / "figure_candidates.json").exists():
                error = "Complete section drafting and figure candidate generation before continuing to Figure Review."
            else:
                write_stage_handoff(
                    project / "03_figure_redraw" / "figure_review_handoff.json",
                    "sections",
                    [stage / "section_drafts.json", stage / "figure_candidates.json", stage / "paper_figure_candidates.json"],
                )
        elif stage_id == "figures":
            draft_stage = project / "02_section_drafting"
            freshness = artifact_freshness(
                draft_stage / "section_handoff.json",
                [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json"],
            )
            if freshness["stale"]:
                error = "Sections are out of date with Blueprint. Regenerate sections and figure candidates before Draft."
            elif not (project / "03_figure_redraw" / "redrawn_figure_manifest.json").exists():
                error = "Run the selected-figure batch redraw before continuing to Draft."
            else:
                write_stage_handoff(
                    project / "04_first_draft" / "draft_handoff.json",
                    "figures",
                    [draft_stage / "section_drafts.json", draft_stage / "human_figure_review.json", project / "03_figure_redraw" / "redrawn_figure_manifest.json"],
                )
        elif stage_id == "figure-review":
            draft_stage = project / "02_section_drafting"
            source_freshness = artifact_freshness(
                draft_stage / "section_handoff.json",
                [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json"],
            )
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
                write_stage_handoff(
                    project / "03_figure_redraw" / "figure_review_handoff.json",
                    "figure-review",
                    [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json", draft_stage / "human_figure_review.json"],
                )
        elif stage_id == "draft":
            draft_stage = project / "02_section_drafting"
            source_freshness = artifact_freshness(
                draft_stage / "section_handoff.json",
                [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json"],
            )
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
        self.send_file(path, ctype)

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

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            size = path.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
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
    figures = [row for row in figures if not (isinstance(row, dict) and str(row.get("paper_id") or "") == paper_id)]
    figures.append(selected)
    write_json(candidates_path, figures)


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
    return {"section_count": len(generated_sections), "figure_candidate_count": len(figures)}


def regenerate_figures(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "02_section_drafting"
    freshness = artifact_freshness(draft_stage / "section_handoff.json", [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json"])
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
    # The handoff is the input boundary for redraw. It must precede the files
    # produced below, otherwise the freshness check would mark them stale.
    write_stage_handoff(
        project / "03_figure_redraw" / "figures_handoff.json",
        "figure-review",
        [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json", draft_stage / "human_figure_review.json"],
    )
    script = figure_redraw_script_path()
    run_project_script(script, review_root, project_id, timeout=300, extra=["--render-mode", "source-faithful-bw", "--require-redrawn"])
    manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    redrawn = [row for row in manifest.get("figures", []) if isinstance(row, dict) and row.get("status") == "redrawn"] if isinstance(manifest, dict) else []
    if not redrawn:
        raise RuntimeError("No figure was redrawn successfully. Resolve source images in Figure Review.")
    write_stage_handoff(
        project / "04_first_draft" / "draft_handoff.json",
        "figures",
        [draft_stage / "section_drafts.json", draft_stage / "human_figure_review.json", project / "03_figure_redraw" / "redrawn_figure_manifest.json"],
    )
    return {"redrawn_count": len(redrawn)}


def redraw_current_figure(review_root: Path, project_id: str, figure_id: str) -> dict[str, Any]:
    """Create one gated AI line-art redraw and reject altered chemical geometry."""
    project = review_root / "review-projects" / project_id
    figures = read_json_if_exists(project / "02_section_drafting" / "figure_candidates.json") or []
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
    try:
        run_project_script(
            figure_redraw_script_path(),
            review_root,
            project_id,
            timeout=300,
            extra=["--figure-id", figure_id, "--render-mode", "ocr-hollow-ai", "--model", "gpt-image-2", "--require-redrawn"],
        )
    except RuntimeError as exc:
        manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
        rows = manifest.get("figures", []) if isinstance(manifest, dict) else []
        failed = next(
            (row for row in rows if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
            None,
        )
        note = str((failed or {}).get("notes") or "").strip()
        if note:
            raise RuntimeError(f"Current figure redraw failed: {note}") from exc
        raise
    manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    rows = manifest.get("figures", []) if isinstance(manifest, dict) else []
    redrawn = next(
        (row for row in rows if isinstance(row, dict) and str(row.get("figure_id") or "") == figure_id),
        None,
    )
    if not isinstance(redrawn, dict) or redrawn.get("status") != "redrawn" or not redrawn.get("redrawn_image"):
        raise RuntimeError("The selected figure did not produce a usable redrawn output.")
    if redrawn.get("render_mode") == "ocr-hollow-ai":
        if (redrawn.get("content_fidelity") or {}).get("status") != "pass":
            raise RuntimeError("The selected figure failed the content-fidelity check and was rejected.")
        if (redrawn.get("structural_fidelity") or {}).get("status") != "pass":
            raise RuntimeError("The selected figure changed chemical line geometry and was rejected.")
    elif redrawn.get("render_mode") != "source-faithful-bw":
        raise RuntimeError("The selected figure used an unsupported redraw mode.")
    return {
        "figure_id": figure_id,
        "paper_id": paper_id,
        "render_mode": str(redrawn.get("render_mode") or ""),
        "redrawn_image": str(redrawn["redrawn_image"]),
    }


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
    write_stage_handoff(
        project / "03_figure_redraw" / "figure_review_handoff.json",
        "figure-review",
        [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json", draft_stage / "human_figure_review.json"],
    )
    return {"reviewed_paper_count": len(reviewable), "redraw_pending": True}


def regenerate_first_draft(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "04_first_draft"
    drafts = read_json_if_exists(project / "02_section_drafting" / "section_drafts.json") or {}
    sections = drafts.get("sections") if isinstance(drafts, dict) else drafts
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("Section drafts are missing. Regenerate Sections first.")
    figures_handoff = project / "03_figure_redraw" / "figures_handoff.json"
    if artifact_freshness(figures_handoff, [project / "03_figure_redraw" / "redrawn_figure_manifest.json"])["stale"]:
        raise RuntimeError("Figure redraw is out of date. Run Figures before building the draft.")

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
    rows = {str(row["paper_id"]): row for row in matrix_rows(project)}
    reference_lines = ["", "## References", ""]
    for entry in citation_entries:
        row = rows.get(entry["paper_id"], {})
        authors = row.get("authors") or []
        author_text = ", ".join(authors[:3]) if isinstance(authors, list) else str(authors)
        reference_lines.append(f"[{entry['callout']}] {author_text}. {row.get('title') or entry['paper_id']}. {row.get('journal') or ''} ({row.get('year') or 'n.d.'}).")
    draft_path = stage / "first_draft.md"
    draft_path.write_text(draft_path.read_text(encoding="utf-8", errors="ignore").rstrip() + "\n\n" + "\n".join(reference_lines) + "\n", encoding="utf-8")
    run_project_script(review_root / "skills" / "review-draft-merge-polish" / "scripts" / "insert_figures_into_draft.py", review_root, project_id)
    (stage / "remaining_issues.md").write_text("# Remaining Issues\n\nConfirm the scientific interpretation, scope boundaries, and every redrawn figure before approving the first draft.\n", encoding="utf-8")
    write_stage_handoff(project / "05_final_audit" / "final_handoff.json", "draft", [stage / "first_draft.md", stage / "citations.json"])
    return {"citation_count": len(citations), "first_draft": str(stage / "first_draft.md")}


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
    if not conclusion.exists() or not report.exists() or conclusion.stat().st_mtime <= draft_path.stat().st_mtime:
        raise RuntimeError("Generate and validate a current conclusion before running the final audit.")
    if not (quality.get("validation") or {}).get("passes_validation"):
        raise RuntimeError("The generated conclusion did not pass validation. Regenerate or correct it before final audit.")
    run_project_script(review_root / "skills" / "review-final-audit-release" / "scripts" / "integrate_generated_conclusion.py", review_root, project_id)
    run_project_script(review_root / "skills" / "review-final-audit-release" / "scripts" / "final_audit_scan.py", review_root, project_id)
    return {"final_draft": str(final_stage / "final_draft.md")}


def generate_outline_chart_preview(review_root: Path, project_id: str) -> dict[str, str]:
    """Compose the approved conclusion into a first-draft preview and chart it."""
    project = review_root / "review-projects" / project_id
    draft_stage = project / "04_first_draft"
    selected_outline = project / "01_matrix_outline" / "selected_outline.md"
    draft_path = draft_stage / "first_draft.md"
    conclusion_path = draft_stage / "conclusion_generated.md"
    report_path = draft_stage / "conclusion_quality_report.json"
    try:
        selected_outline.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Choose a readable selected outline before generating the outline chart preview.") from exc
    try:
        first_draft = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Create a readable first draft before generating the outline chart preview.") from exc
    try:
        conclusion = conclusion_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("Generate a current conclusion before generating the outline chart preview.") from exc
    report = read_json_if_exists(report_path) or {}
    if conclusion_path.stat().st_mtime <= draft_path.stat().st_mtime or not (
        report.get("validation") or {}
    ).get("passes_validation"):
        raise RuntimeError("Generate and validate a current conclusion before generating the outline chart preview.")

    preview_path = draft_stage / "outline_chart_preview.md"
    integration = conclusion_integration_module(review_root)
    try:
        preview_path.write_text(integration.integrate_conclusion(first_draft, conclusion), encoding="utf-8")
    except ValueError as exc:
        raise RuntimeError(f"Cannot compose the outline chart preview: {exc}") from exc
    chart_script = review_root / "skills" / "review-outline-summary-chart" / "scripts" / "generate_review_summary_chart.py"
    run_project_script(
        chart_script,
        review_root,
        project_id,
        extra=["--scope", "both", "--input-markdown", str(preview_path)],
    )
    full_png = draft_stage / "review_summary_chart.png"
    if not full_png.is_file():
        raise RuntimeError("Outline chart preview did not produce the full PNG.")
    return {"preview_markdown": str(preview_path), "preview_full_png": str(full_png)}


def regenerate_final_draft_bundle(review_root: Path, project_id: str) -> dict[str, str]:
    """Regenerate the validated final draft, then its current full chart bundle."""
    audit = regenerate_final_audit(review_root, project_id)
    project = review_root / "review-projects" / project_id
    final_stage = project / "05_final_audit"
    chart_script = review_root / "skills" / "review-outline-summary-chart" / "scripts" / "generate_review_summary_chart.py"
    run_project_script(chart_script, review_root, project_id, extra=["--scope", "both"])
    full_png = final_stage / "review_summary_chart.png"
    if not full_png.is_file():
        raise RuntimeError("Final chart generation did not produce the full PNG.")
    return {"final_draft": str(audit["final_draft"]), "final_full_png": str(full_png)}


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


def outline_groups(review_root: Path, rows: list[dict[str, Any]], tag_key: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in rows:
        paper_id = str(row["paper_id"])
        metadata = read_json_if_exists(review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json") or {}
        tags = value_of(metadata.get("structured_tags")) or {}
        label = str(tags.get(tag_key) or "Other or unspecified").strip()
        if not label or label.casefold() == "not specified":
            label = "Other or unspecified"
        groups.setdefault(label, []).append(paper_id)
    return groups


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
    groups = outline_groups(review_root, rows, definition["tag_key"])
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
                *outline_sections(outline_groups(review_root, rows, definition["tag_key"])),
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
    matrix = {
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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def artifact_freshness(handoff_path: Path, artifacts: list[Path]) -> dict[str, Any]:
    """Determine whether artifacts were rebuilt after their most recent handoff."""
    handoff = read_json_if_exists(handoff_path) or {}
    if not handoff_path.exists():
        return {"handoff": handoff, "stale": False, "outdated_artifacts": []}
    handoff_mtime = handoff_path.stat().st_mtime
    outdated = [str(path) for path in artifacts if not path.exists() or path.stat().st_mtime <= handoff_mtime]
    return {"handoff": handoff, "stale": bool(outdated), "outdated_artifacts": outdated}


def write_stage_handoff(path: Path, source_stage: str, source_artifacts: list[Path]) -> None:
    generated_at = now_utc()
    write_json(
        path,
        {
            "source_stage": source_stage,
            "generated_at": generated_at,
            "source_artifacts": [str(artifact) for artifact in source_artifacts],
        },
    )


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
    handoff = read_json_if_exists(handoff_path) or {}
    draft_artifacts = [
        stage / "section_drafts.json",
        stage / "section_drafts.md",
        stage / "section_drafting_report.md",
        *[Path(item["path"]) for item in section_files],
    ]
    latest_draft_artifact = max(
        (path.stat().st_mtime for path in draft_artifacts if path.exists()),
        default=0,
    )
    drafts_stale = bool(handoff_path.exists() and handoff_path.stat().st_mtime > latest_draft_artifact)
    if isinstance(handoff, dict):
        handoff = {
            **handoff,
            "drafts_stale": drafts_stale,
            "has_existing_drafts": bool(latest_draft_artifact),
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
    stage = project / "03_figure_redraw"
    source_freshness = artifact_freshness(
        draft_stage / "section_handoff.json",
        [draft_stage / "section_drafts.json", draft_stage / "figure_candidates.json", draft_stage / "paper_figure_candidates.json"],
    )
    redraw_freshness = artifact_freshness(stage / "figures_handoff.json", [stage / "redrawn_figure_manifest.json"])
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "figure_candidates": read_json_if_exists(draft_stage / "figure_candidates.json"),
        "redrawn_manifest": read_json_if_exists(stage / "redrawn_figure_manifest.json"),
        "figure_redraw_report_md": read_text_if_exists(stage / "figure_redraw_report.md"),
        "freshness": {
            "source_stale": source_freshness["stale"],
            "redraw_stale": redraw_freshness["stale"],
            "stale": source_freshness["stale"] or redraw_freshness["stale"],
        },
        "upstream": {
            "section_drafts": read_json_if_exists(draft_stage / "section_drafts.json"),
            "section_drafts_md": read_text_if_exists(draft_stage / "section_drafts.md"),
            "figure_candidates": read_json_if_exists(draft_stage / "figure_candidates.json"),
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


def sync_reviewed_figure_candidates(stage: Path, candidates_data: dict[str, Any], reviews: dict[str, Any]) -> None:
    """Make Stage 7 immediately reflect every saved Stage 6 selection."""
    papers = candidates_data.get("papers") if isinstance(candidates_data, dict) else []
    if not isinstance(papers, list) or not isinstance(reviews, dict):
        return
    manifest_path = stage / "figure_candidates.json"
    manifest = read_json_if_exists(manifest_path) or []
    if isinstance(manifest, dict):
        manifest = manifest.get("figures") or manifest.get("candidates") or []
    if not isinstance(manifest, list):
        manifest = []
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
        except RuntimeError:
            # A candidate without a paragraph anchor remains available for a manual
            # review; do not make the whole Figure Review page unavailable.
            continue
        manifest = read_json_if_exists(manifest_path) or []
        if isinstance(manifest, dict):
            manifest = manifest.get("figures") or manifest.get("candidates") or []
        if not isinstance(manifest, list):
            manifest = []


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
    sync_reviewed_figure_candidates(stage, candidates_data, reviews)
    return reviews_data


def project_figure_review_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "02_section_drafting"
    source_freshness = artifact_freshness(
        stage / "section_handoff.json",
        [stage / "section_drafts.json", stage / "figure_candidates.json", stage / "paper_figure_candidates.json"],
    )
    review_freshness = artifact_freshness(project / "03_figure_redraw" / "figure_review_handoff.json", [stage / "human_figure_review.json"])
    candidates_data = read_json_if_exists(stage / "paper_figure_candidates.json") or {}
    reviews = ensure_default_figure_reviews(stage)
    review_rows = reviews.get("papers") if isinstance(reviews, dict) else {}
    papers: list[dict[str, Any]] = []
    for row in candidates_data.get("papers", []) if isinstance(candidates_data, dict) else []:
        if not isinstance(row, dict) or not row.get("paper_id"):
            continue
        paper_id = str(row["paper_id"])
        meta = read_json_if_exists(review_root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json") or {}
        item = dict(row)
        item["title"] = value_of(meta.get("title")) or paper_id
        item["human_review"] = review_rows.get(paper_id, {}) if isinstance(review_rows, dict) else {}
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
    section_stage = project / "02_section_drafting"
    source_freshness = artifact_freshness(
        section_stage / "section_handoff.json",
        [section_stage / "section_drafts.json", section_stage / "figure_candidates.json", section_stage / "paper_figure_candidates.json"],
    )
    final_freshness = artifact_freshness(stage / "final_handoff.json", [stage / "final_draft.md"])
    draft_path = draft_stage / "first_draft.md"
    conclusion_path = draft_stage / "conclusion_generated.md"
    conclusion_report = read_json_if_exists(draft_stage / "conclusion_quality_report.json") or {}
    preview_path = draft_stage / "outline_chart_preview.md"
    preview_full_png = draft_stage / "review_summary_chart.png"
    release_full_png = stage / "review_summary_chart.png"
    conclusion_current = bool(
        conclusion_path.exists()
        and draft_path.exists()
        and conclusion_path.stat().st_mtime > draft_path.stat().st_mtime
        and (conclusion_report.get("validation") or {}).get("passes_validation")
    )
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "final_draft_md": read_text_if_exists(stage / "final_draft.md"),
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
        "final_draft_docx_exists": docx_path.exists(),
        "outline_chart_preview_path": str(preview_path),
        "outline_chart_preview_exists": preview_path.exists(),
        "outline_chart_preview_full_png_path": str(preview_full_png),
        "outline_chart_preview_full_png_exists": preview_full_png.exists(),
        "release_chart_full_png_path": str(release_full_png),
        "release_chart_full_png_exists": release_full_png.exists(),
        "freshness": {"source_stale": source_freshness["stale"], "final_stale": final_freshness["stale"], "stale": source_freshness["stale"] or final_freshness["stale"]},
        "paths": {"stage_dir": str(stage)},
    }


def project_draft_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage_dir = project / "04_first_draft"
    figures_manifest = read_json_if_exists(project / "03_figure_redraw" / "redrawn_figure_manifest.json") or {}
    draft_bundle = read_json_if_exists(stage_dir / "draft_bundle.json")
    section_drafts = read_json_if_exists(project / "02_section_drafting" / "section_drafts.json")
    section_stage = project / "02_section_drafting"
    source_freshness = artifact_freshness(
        section_stage / "section_handoff.json",
        [section_stage / "section_drafts.json", section_stage / "figure_candidates.json", section_stage / "paper_figure_candidates.json"],
    )
    draft_freshness = artifact_freshness(stage_dir / "draft_handoff.json", [stage_dir / "first_draft.md"])
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
        "freshness": {"source_stale": source_freshness["stale"], "draft_stale": draft_freshness["stale"], "stale": source_freshness["stale"] or draft_freshness["stale"]},
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
