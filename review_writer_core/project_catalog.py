"""Read-only project catalog shared by local and hosted web frontends.

This module intentionally contains no HTTP or process-global state. Keeping
the filesystem contract here lets the workflow dashboard and versioned API
return the same project summaries during endpoint migration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .project_config import load_project_config
from .workspace import WorkspaceConfigurationError, validate_project_id


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def infer_project_topic(review_root: Path, project_id: str) -> str:
    """Return the best available topic without mutating project artifacts."""
    root = Path(review_root).resolve()
    project = root / "review-projects" / project_id
    project_config = load_project_config(root, project_id)
    if project_config.get("topic"):
        return str(project_config["topic"])

    discovery = _read_json(project / "00_discovery" / "combined_results_by_keyword.json")
    if discovery.get("topic"):
        return str(discovery["topic"])

    topic_input = project / "00_discovery" / "topic_input.md"
    if topic_input.is_file():
        for line in topic_input.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()

    bundle = _read_json(project / "04_first_draft" / "draft_bundle.json")
    return str(bundle.get("topic") or "")


def list_review_projects(review_root: Path) -> list[dict[str, Any]]:
    """List filesystem projects using the stable dashboard contract."""
    root = Path(review_root).resolve()
    base = root / "review-projects"
    if not base.is_dir():
        return []

    projects: list[dict[str, Any]] = []
    resolved_base = base.resolve()
    for project in sorted(path for path in base.iterdir() if path.is_dir()):
        try:
            validate_project_id(project.name)
        except WorkspaceConfigurationError:
            continue
        resolved_project = project.resolve()
        if resolved_project.parent != resolved_base:
            continue
        project_config = load_project_config(root, project.name)
        discovery_state = _read_json(project / "00_discovery" / "human_check_state.json")
        projects.append(
            {
                "project_id": project.name,
                "topic": infer_project_topic(root, project.name),
                "taxonomy_profile": str(
                    project_config.get("taxonomy_profile") or "chemistry_general"
                ),
                "has_discovery": (project / "00_discovery" / "combined_results_by_keyword.json").is_file(),
                "discovery_status": discovery_state.get("status") or "pending",
                "has_matrix_outline": (project / "01_matrix_outline" / "literature_matrix.json").is_file(),
                "has_blueprint": (project / "01_matrix_outline" / "section_blueprint.json").is_file(),
                "has_section_drafting": (project / "02_section_drafting" / "section_drafts.md").is_file(),
                "has_figure_redraw": (project / "03_figure_redraw" / "redrawn_figure_manifest.json").is_file(),
                "has_first_draft": (project / "04_first_draft" / "first_draft.md").is_file(),
                "has_final_audit": (project / "05_final_audit" / "final_draft.md").is_file(),
            }
        )
    return projects


def project_summary(review_root: Path, project_id: str) -> dict[str, Any] | None:
    return next(
        (project for project in list_review_projects(review_root) if project["project_id"] == project_id),
        None,
    )
