#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import shutil
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class DashboardHandler(BaseHTTPRequestHandler):
    review_root: Path
    library_app_path: Path
    discovery_app_path: Path
    matrix_app_path: Path
    blueprint_app_path: Path
    sections_app_path: Path
    figures_app_path: Path
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
        if parsed.path.startswith("/api/discovery/"):
            project_id = unquote(parsed.path.rsplit("/", 1)[-1])
            query = parse_qs(parsed.query)
            self.handle_discovery_put(project_id, confirm=bool(query.get("confirm")))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/project/") and parsed.path.endswith("/export-docx"):
            project_id = unquote(parsed.path.split("/")[3])
            self.handle_project_export_docx(project_id)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_project_export_docx(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        md_path = project / "07_final_audit" / "final_draft.md"
        if not md_path.exists():
            self.send_error(HTTPStatus.BAD_REQUEST, "final_draft.md not found")
            return
        docx_stage = project / "09_docx_export"
        docx_stage.mkdir(parents=True, exist_ok=True)
        docx_path = docx_stage / "final_draft.docx"
        script = self.review_root / "skills" / "review-export-docx" / "scripts" / "md2docx.py"
        if not script.exists():
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "md2docx.py not found")
            return
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--input", str(md_path), "--output", str(docx_path)],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            self.send_error(HTTPStatus.GATEWAY_TIMEOUT, "docx export timeout")
            return
        if result.returncode != 0 or not docx_path.exists():
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-20:]
            self.send_json({
                "ok": False,
                "returncode": result.returncode,
                "error": "\n".join(tail) or "md2docx.py failed",
            })
            return
        self.send_json({
            "ok": True,
            "path": str(docx_path),
            "size": docx_path.stat().st_size,
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

        # Preserve any excluded_candidates the agent already recorded (per
        # review-online-paper-discovery/SKILL.md's "Agent relevance check"
        # step) -- re-aggregating from raw per-keyword results would otherwise
        # silently discard that work on every dashboard save.
        candidates_path = path.parent / "online_search_candidates.json"
        prior = read_json_if_exists(candidates_path) or {}
        prior_excluded = prior.get("excluded_candidates") if isinstance(prior.get("excluded_candidates"), list) else []
        excluded_keys = {_result_dedupe_key(c) for c in prior_excluded if isinstance(c, dict)}

        aggregated = aggregate_candidates_from_groups(data.get("results", []))
        if excluded_keys:
            aggregated["candidates"] = [
                c for c in aggregated["candidates"] if _result_dedupe_key(c) not in excluded_keys
            ]
            aggregated["borderline_candidates"] = [
                c for c in aggregated["borderline_candidates"] if _result_dedupe_key(c) not in excluded_keys
            ]
        aggregated["excluded_candidates"] = prior_excluded
        aggregated["project_id"] = project_id
        aggregated["human_confirmed"] = bool(confirm)
        candidates_path.write_text(
            json.dumps(aggregated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (path.parent / "online_search_human_check_state.json").write_text(
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
        self.send_json({"ok": True, "confirmed": confirm})

    def handle_project_draft_get(self, project_id: str) -> None:
        project = self.review_root / "review-projects" / project_id
        if not project.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "project not found")
            return
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
            "final": project_final_payload,
        }
        builder = payloads.get(stage)
        if not builder:
            self.send_error(HTTPStatus.NOT_FOUND, "unknown stage")
            return
        self.send_json(builder(self.review_root, project_id))

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
        stage_dir = project / "05_first_draft"
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

    def discovery_path(self, project_id: str) -> Path:
        return self.review_root / "review-projects" / project_id / "00_discovery" / "online_search_results_by_keyword.json"

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

    def send_json(self, data: object) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
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


# Mirrors discover.py's ONLINE_CANDIDATE_SCORE/ONLINE_BORDERLINE_SCORE and
# aggregate_candidates() -- kept in sync by hand since this dashboard doesn't
# import discover.py (it may be serving a different --base-url's data).
ONLINE_CANDIDATE_SCORE = 0.35
ONLINE_BORDERLINE_SCORE = 0.15


def _result_dedupe_key(row: dict) -> str:
    doi = (row.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    url = (row.get("url") or "").strip().lower()
    if url:
        return "url:" + url
    title = " ".join((row.get("title") or "").strip().lower().split())
    return "title:" + title


def aggregate_candidates_from_groups(groups: list[dict]) -> dict:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for group in groups:
        if group.get("keep") is False:
            continue
        keyword = group.get("keyword")
        for row in group.get("web_results", []):
            if row.get("keep") is False:
                continue
            key = _result_dedupe_key(row)
            if not key:
                continue
            if key not in merged:
                merged[key] = {**row, "matched_keywords": [keyword]}
                order.append(key)
                continue
            existing = merged[key]
            if keyword not in existing["matched_keywords"]:
                existing["matched_keywords"].append(keyword)
            if (row.get("score") or 0) > (existing.get("score") or 0):
                matched_keywords = existing["matched_keywords"]
                merged[key] = {**row, "matched_keywords": matched_keywords}
    rows = [merged[key] for key in order]
    candidates = [r for r in rows if (r.get("score") or 0) >= ONLINE_CANDIDATE_SCORE]
    borderline = [
        r for r in rows if ONLINE_BORDERLINE_SCORE <= (r.get("score") or 0) < ONLINE_CANDIDATE_SCORE
    ]
    candidates.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    borderline.sort(key=lambda r: (r.get("score") or 0, r.get("year") or 0), reverse=True)
    return {
        "keywords": [{"keyword": g.get("keyword")} for g in groups],
        "candidates": candidates,
        "borderline_candidates": borderline,
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


def infer_project_topic(project: Path) -> str:
    discovery = read_json_if_exists(project / "00_discovery" / "online_search_results_by_keyword.json")
    if isinstance(discovery, dict) and discovery.get("topic"):
        return str(discovery.get("topic"))
    topic_input = project / "00_discovery" / "online_search_topic.md"
    if topic_input.exists():
        for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
    bundle = read_json_if_exists(project / "05_first_draft" / "draft_bundle.json")
    if isinstance(bundle, dict) and bundle.get("topic"):
        return str(bundle.get("topic"))
    return ""


def list_review_projects(review_root: Path) -> list[dict[str, Any]]:
    base = review_root / "review-projects"
    projects: list[dict[str, Any]] = []
    if not base.exists():
        return projects
    for project in sorted(p for p in base.iterdir() if p.is_dir()):
        discovery_state = read_json_if_exists(project / "00_discovery" / "online_search_human_check_state.json") or {}
        projects.append(
            {
                "project_id": project.name,
                "topic": infer_project_topic(project),
                "has_discovery": (project / "00_discovery" / "online_search_results_by_keyword.json").exists(),
                "discovery_status": discovery_state.get("status") or "pending",
                "has_matrix_outline": (project / "01_matrix_outline" / "literature_matrix.json").exists(),
                "has_blueprint": (project / "02_section_blueprint" / "section_blueprint.json").exists(),
                "has_section_drafting": (project / "03_section_drafting" / "section_drafts.md").exists(),
                "has_figure_redraw": (project / "04_figure_redraw" / "redrawn_figure_manifest.json").exists(),
                "has_first_draft": (project / "05_first_draft" / "first_draft.md").exists(),
                "has_final_audit": (project / "07_final_audit" / "final_draft.md").exists(),
            }
        )
    return projects


def project_summary(review_root: Path, project_id: str) -> dict[str, Any] | None:
    return next((p for p in list_review_projects(review_root) if p["project_id"] == project_id), None)


def project_matrix_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "01_matrix_outline"
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "paper_reading_notes": read_json_if_exists(stage / "paper_reading_notes.json"),
        "literature_matrix": read_json_if_exists(stage / "literature_matrix.json"),
        "literature_matrix_csv": read_text_if_exists(stage / "literature_matrix.csv"),
        "outline_options_md": read_text_if_exists(stage / "outline_options.md"),
        "selected_outline_md": read_text_if_exists(stage / "selected_outline.md"),
        "matrix_outline_report_md": read_text_if_exists(stage / "matrix_outline_report.md"),
        "paths": {"stage_dir": str(stage)},
    }


def project_blueprint_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    outline_stage = project / "01_matrix_outline"
    blueprint_stage = project / "02_section_blueprint"
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "section_blueprint": read_json_if_exists(blueprint_stage / "section_blueprint.json"),
        "section_writing_plan_md": read_text_if_exists(blueprint_stage / "section_writing_plan.md"),
        "selected_outline_md": read_text_if_exists(outline_stage / "selected_outline.md"),
        "paths": {"stage_dir": str(blueprint_stage), "outline_stage_dir": str(outline_stage)},
    }


def project_sections_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "03_section_drafting"
    section_files = []
    sections_dir = stage / "sections"
    if sections_dir.exists():
        for path in sorted(sections_dir.glob("*.md")):
            section_files.append({"name": path.name, "path": str(path), "content": read_text_if_exists(path)})
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
        "paths": {"stage_dir": str(stage), "sections_dir": str(sections_dir)},
    }


def project_figures_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    draft_stage = project / "03_section_drafting"
    stage = project / "04_figure_redraw"
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "figure_candidates": read_json_if_exists(draft_stage / "figure_candidates.json"),
        "redrawn_manifest": read_json_if_exists(stage / "redrawn_figure_manifest.json"),
        "figure_redraw_report_md": read_text_if_exists(stage / "figure_redraw_report.md"),
        "paths": {"stage_dir": str(stage), "draft_stage_dir": str(draft_stage)},
    }


def project_final_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage = project / "07_final_audit"
    docx_path = project / "09_docx_export" / "final_draft.docx"
    return {
        "project_id": project_id,
        "topic": infer_project_topic(project),
        "summary": project_summary(review_root, project_id),
        "final_draft_md": read_text_if_exists(stage / "final_draft.md"),
        "content_audit_report_md": read_text_if_exists(stage / "content_audit_report.md"),
        "format_audit_report_md": read_text_if_exists(stage / "format_audit_report.md"),
        "format_scan_md": read_text_if_exists(stage / "format_scan.md"),
        "final_remaining_issues_md": read_text_if_exists(stage / "final_remaining_issues.md"),
        "release_report_md": read_text_if_exists(stage / "release_report.md"),
        "final_draft_docx_path": str(docx_path),
        "final_draft_docx_exists": docx_path.exists(),
        "paths": {"stage_dir": str(stage)},
    }


def project_draft_payload(review_root: Path, project_id: str) -> dict[str, Any]:
    project = review_root / "review-projects" / project_id
    stage_dir = project / "05_first_draft"
    figures_manifest = read_json_if_exists(project / "04_figure_redraw" / "redrawn_figure_manifest.json") or {}
    draft_bundle = read_json_if_exists(stage_dir / "draft_bundle.json")
    section_drafts = read_json_if_exists(project / "03_section_drafting" / "section_drafts.json")
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
    draft_path = dashboard / "draft.html"
    final_path = dashboard / "final.html"
    paths = [library_path, discovery_path, matrix_path, blueprint_path, sections_path, figures_path, draft_path, final_path]
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
        draft_app_path,
        final_app_path,
    ) = dashboard_assets(view_root)
    if not (review_root / "review-library" / "metadata" / "papers").exists():
        print("ERROR: metadata files not found. Run labkag-review-skill's ingest workflow first.", file=sys.stderr)
        return 2
    DashboardHandler.review_root = review_root
    DashboardHandler.library_app_path = library_app_path
    DashboardHandler.discovery_app_path = discovery_app_path
    DashboardHandler.matrix_app_path = matrix_app_path
    DashboardHandler.blueprint_app_path = blueprint_app_path
    DashboardHandler.sections_app_path = sections_app_path
    DashboardHandler.figures_app_path = figures_app_path
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
    default_review_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Serve local review metadata dashboard.")
    parser.add_argument("--review-root", default=str(default_review_root))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
