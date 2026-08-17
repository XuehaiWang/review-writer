"""Canonical dashboard page and asset locations, independent of HTTP servers."""

from __future__ import annotations

from pathlib import Path


DASHBOARD_PAGE_FILES = {
    "/library": "library.html",
    "/discovery": "discovery.html",
    "/matrix": "matrix.html",
    "/blueprint": "blueprint.html",
    "/sections": "sections.html",
    "/figure-review": "figure-review.html",
    "/draft": "draft.html",
    "/final": "final.html",
    "/settings": "settings.html",
}


def dashboard_page_paths(view_root: Path) -> dict[str, Path]:
    dashboard = Path(view_root) / "assets" / "dashboard"
    paths = {route: dashboard / filename for route, filename in DASHBOARD_PAGE_FILES.items()}
    missing = [path for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Dashboard assets not found under {dashboard}")
    return paths


def dashboard_assets(view_root: Path) -> tuple[Path, ...]:
    """Return the historical handler order while keeping one filename registry."""
    paths = dashboard_page_paths(view_root)
    route_order = (
        "/library",
        "/discovery",
        "/matrix",
        "/blueprint",
        "/sections",
        "/figure-review",
        "/draft",
        "/final",
        "/settings",
    )
    return tuple(paths[route] for route in route_order)
