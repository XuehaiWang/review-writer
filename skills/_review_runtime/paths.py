from __future__ import annotations

import os
from pathlib import Path


LEGACY_REVIEW_ROOTS = {
    "/home/ps/review-writer",
    "/home/ps/review-writer/",
}


def _clean_path_value(value: object) -> str:
    return str(value or "").strip().strip("\"'")


def _is_legacy_placeholder(value: object) -> bool:
    cleaned = _clean_path_value(value).replace("\\", "/")
    return cleaned in LEGACY_REVIEW_ROOTS


def _env_path(*names: str) -> Path | None:
    for name in names:
        value = _clean_path_value(os.getenv(name))
        if value:
            return Path(value).expanduser().resolve()
    return None


def _candidate_anchors(anchor: Path | None = None) -> list[Path]:
    anchors: list[Path] = []
    if anchor is not None:
        anchors.append(anchor)
    anchors.append(Path.cwd())
    return anchors


def _looks_like_review_root(path: Path) -> bool:
    return any(
        (path / marker).exists()
        for marker in ("review-library", "review-projects", ".review-writer")
    )


def discover_review_root(anchor: Path | None = None) -> Path | None:
    for raw_anchor in _candidate_anchors(anchor):
        current = raw_anchor.resolve()
        if current.is_file():
            current = current.parent
        for path in (current, *current.parents):
            if _looks_like_review_root(path):
                return path.resolve()
    return None


def resolve_review_root(value: object = None, *, anchor: Path | None = None) -> Path:
    """Resolve the review workspace root in standalone and FounDryClaw contexts.

    Precedence:
    1. FOUNDRYCLAW_REVIEW_ROOT, REVIEW_WRITER_ROOT, REVIEW_ROOT
    2. explicit CLI/config value, unless it is the old /home/ps placeholder
    3. nearest parent containing review-library, review-projects, or .review-writer
    4. current working directory, which is the FounDryClaw Claude workdir
    """
    env = _env_path("FOUNDRYCLAW_REVIEW_ROOT", "REVIEW_WRITER_ROOT", "REVIEW_ROOT")
    if env is not None:
        return env

    cleaned = _clean_path_value(value)
    if cleaned and not _is_legacy_placeholder(cleaned):
        return Path(cleaned).expanduser().resolve()

    discovered = discover_review_root(anchor)
    if discovered is not None:
        return discovered

    return Path.cwd().resolve()


def resolve_review_library_root(
    value: object = None,
    *,
    review_root: Path | None = None,
    anchor: Path | None = None,
) -> Path:
    env = _env_path("FOUNDRYCLAW_REVIEW_LIBRARY_ROOT", "REVIEW_LIBRARY_ROOT")
    if env is not None:
        return env
    cleaned = _clean_path_value(value)
    if cleaned:
        return Path(cleaned).expanduser().resolve()
    root = review_root or resolve_review_root(anchor=anchor)
    return (root / "review-library").resolve()


def resolve_review_projects_root(
    value: object = None,
    *,
    review_root: Path | None = None,
    anchor: Path | None = None,
) -> Path:
    env = _env_path("FOUNDRYCLAW_REVIEW_PROJECTS_ROOT", "REVIEW_PROJECTS_ROOT")
    if env is not None:
        return env
    cleaned = _clean_path_value(value)
    if cleaned:
        return Path(cleaned).expanduser().resolve()
    root = review_root or resolve_review_root(anchor=anchor)
    return (root / "review-projects").resolve()


def resolve_mineru_output_root(
    value: object = None,
    *,
    review_root: Path | None = None,
    anchor: Path | None = None,
) -> Path:
    env = _env_path("FOUNDRYCLAW_MINERU_OUTPUT_ROOT", "MINERU_OUTPUT_ROOT")
    if env is not None:
        return env
    cleaned = _clean_path_value(value)
    if cleaned and not _is_legacy_placeholder(cleaned):
        return Path(cleaned).expanduser().resolve()
    root = review_root or resolve_review_root(anchor=anchor)
    return (root / "mineru-outputs").resolve()


def resolve_pdf_root(
    value: object = None,
    *,
    review_root: Path | None = None,
    anchor: Path | None = None,
) -> Path | None:
    env = _env_path("FOUNDRYCLAW_REVIEW_PDF_ROOT", "REVIEW_PDF_ROOT", "PDF_ROOT")
    if env is not None:
        return env
    cleaned = _clean_path_value(value)
    if cleaned and not _is_legacy_placeholder(cleaned):
        return Path(cleaned).expanduser().resolve()
    root = review_root or resolve_review_root(anchor=anchor)
    source_root = root / "source-paper"
    return source_root.resolve() if source_root.exists() else None


def resolve_review_skills_root(value: object = None, *, anchor: Path | None = None) -> Path:
    env = _env_path("FOUNDRYCLAW_REVIEW_SKILLS_ROOT", "REVIEW_SKILLS_ROOT")
    if env is not None:
        return env
    cleaned = _clean_path_value(value)
    if cleaned:
        return Path(cleaned).expanduser().resolve()
    if anchor is not None:
        path = anchor.resolve()
        if path.is_file():
            path = path.parent
        for parent in (path, *path.parents):
            if parent.name == "skills":
                return parent.resolve()
    return (resolve_review_root(anchor=anchor) / "skills").resolve()


def project_dir(review_root: Path, project_id: str) -> Path:
    projects_root = resolve_review_projects_root(review_root=review_root)
    candidate = (projects_root / project_id).resolve()
    candidate.relative_to(projects_root)
    return candidate
