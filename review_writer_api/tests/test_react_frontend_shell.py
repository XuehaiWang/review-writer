from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReactFrontendShellTests(unittest.TestCase):
    def test_react_router_owns_every_canonical_workflow_url(self) -> None:
        source = (ROOT / "frontend" / "src" / "app" / "App.tsx").read_text(
            encoding="utf-8"
        )
        for path in (
            "/",
            "/library",
            "/discovery",
            "/planning",
            "/sections",
            "/images",
            "/draft",
            "/final",
            "/settings",
        ):
            with self.subTest(path=path):
                self.assertIn(f'path="{path}"', source)

    def test_fastapi_serves_spa_without_a_legacy_figure_editor_bridge(self) -> None:
        source = (ROOT / "review_writer_api" / "app.py").read_text(encoding="utf-8")
        self.assertIn('app.mount(\n            "/assets/react"', source)
        self.assertIn("if react_spa_available:", source)
        retired_route = '@app.get("/legacy/' + 'figures"'
        self.assertNotIn(retired_route, source)
        self.assertNotIn('return dashboard_response("/figures")', source)
        editor = (
            ROOT / "frontend" / "src" / "features" / "images" / "SvgKetcherEditor.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('/assets/ketcher/standalone/index.html', editor)
        self.assertIn('/manual-edit', editor)

    def test_container_builds_and_tests_frontend_before_python_runtime(self) -> None:
        source = (ROOT / "Dockerfile.api").read_text(encoding="utf-8")
        self.assertIn("FROM node:24-alpine AS frontend-builder", source)
        self.assertIn("RUN npm test && npm run build", source)
        self.assertIn("COPY --from=frontend-builder /frontend/dist ./frontend/dist", source)

    def test_lan_safe_idempotency_keys_do_not_require_secure_context(self) -> None:
        client = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("export function newIdempotencyKey", client)
        offenders: list[str] = []
        for path in (ROOT / "frontend" / "src" / "features").rglob("*.tsx"):
            if "crypto.randomUUID()" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_stage_transitions_stay_inside_the_spa(self) -> None:
        offenders: list[str] = []
        for path in (ROOT / "frontend" / "src").rglob("*.tsx"):
            source = path.read_text(encoding="utf-8")
            if "window.location.assign(" in source:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_react_shell_preserves_project_deletion_and_figure_retry(self) -> None:
        projects = (
            ROOT / "frontend" / "src" / "features" / "projects" / "ProjectsPage.tsx"
        ).read_text(encoding="utf-8")
        selector = (
            ROOT / "frontend" / "src" / "components" / "ProjectSelector.tsx"
        ).read_text(encoding="utf-8")
        figures = (
            ROOT / "frontend" / "src" / "features" / "images" / "ImagesPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('method: "DELETE"', projects)
        self.assertIn('method: "DELETE"', selector)
        self.assertIn('/api/v1/jobs/${encodeURIComponent(retryJobId)}/retry', figures)
        self.assertIn('retry_of_job_id: retryJobId', figures)

    def test_react_shell_reuses_i18n_without_duplicate_legacy_controls(self) -> None:
        index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        translator = (
            ROOT / "view" / "assets" / "dashboard" / "review-i18n.js"
        ).read_text(encoding="utf-8")
        self.assertIn('data-review-writer-react="true"', index)
        self.assertIn('dataset.reviewWriterReact !== "true"', translator)

    def test_blueprint_generation_uses_the_blueprint_stage_revision(self) -> None:
        planning = (
            ROOT
            / "frontend"
            / "src"
            / "features"
            / "planning"
            / "PlanningPage.tsx"
        ).read_text(encoding="utf-8")
        request = planning.split("const generateBlueprint", 1)[1].split(
            "const confirmBlueprint", 1
        )[0]
        self.assertIn("revision: planning.data!.blueprint_revision", request)
        self.assertNotIn("revision: planning.data!.matrix_revision", request)

    def test_source_selection_syncs_redraw_without_manual_confirmation(self) -> None:
        images = (
            ROOT
            / "frontend"
            / "src"
            / "features"
            / "images"
            / "ImagesPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn('/figures/review/confirm', images)
        self.assertIn('/figures/review/sync', images)
        self.assertIn('["figures", projectId]', images)
        self.assertIn("selection_complete", images)

    def test_svg_editor_locks_the_clicked_figure_during_background_refresh(self) -> None:
        images = (
            ROOT / "frontend" / "src" / "features" / "images" / "ImagesPage.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn('const [editorFigureId, setEditorFigureId] = useState("")', images)
        self.assertIn("figureId={editorFigureId}", images)
        self.assertNotIn("editorOpen && selected?.figure_id", images)


if __name__ == "__main__":
    unittest.main()
