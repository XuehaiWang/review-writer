"""Behavior checks for the seven-stage dashboard presentation."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from review_writer_api.dashboard_executor import DashboardRequestExecutor


ROOT = Path(__file__).resolve().parents[1]
REVIEW_UI = ROOT / "view" / "assets" / "dashboard" / "review-ui.js"
I18N = ROOT / "view" / "assets" / "dashboard" / "review-i18n.js"
STAGE_PAGES = (
    "library.html",
    "discovery.html",
    "matrix.html",
    "blueprint.html",
    "sections.html",
    "figure-review.html",
    "figures.html",
    "draft.html",
    "final.html",
)


class SevenStageDashboardChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = DashboardRequestExecutor()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.review_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, path: str):
        return self.executor.dispatch(
            self.review_root,
            method="GET",
            path_and_query=path,
            headers={},
            body=b"",
        )

    def test_canonical_workspace_routes_serve_each_existing_tool(self) -> None:
        cases = (
            ("/planning?tab=matrix", "<title>Review Matrix</title>"),
            ("/planning?tab=blueprint", "<title>Review Blueprint</title>"),
            ("/images?tab=review", "<title>Figure Review</title>"),
            ("/images?tab=redraw", "<title>Review Figures</title>"),
        )
        for path, title in cases:
            with self.subTest(path=path):
                response = self.request(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(title, response.body.decode("utf-8"))

    def test_legacy_routes_redirect_without_losing_project_context(self) -> None:
        cases = (
            ("/matrix?project=demo&selection=abc", "/planning?project=demo&selection=abc&tab=matrix"),
            ("/blueprint?project=demo", "/planning?project=demo&tab=blueprint"),
            ("/figure-review?project=demo", "/images?project=demo&tab=review"),
            ("/figures?project=demo&figure=F01", "/images?project=demo&figure=F01&tab=redraw"),
        )
        for path, location in cases:
            with self.subTest(path=path):
                response = self.request(path)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers.get("Location"), location)

    def test_shared_stage_model_exposes_seven_stages_and_internal_actions(self) -> None:
        node_script = f"""
const model = require({json.dumps(str(REVIEW_UI))});
const result = {{
  stages: model.stages.map(stage => [stage.id, stage.href]),
  legacy: ['/matrix','/blueprint','/figure-review','/figures'].map(path => model.currentId(path)),
  planningMatrix: model.stageActionSpec('planning', 'matrix', 'demo'),
  planningBlueprint: model.stageActionSpec('planning', 'blueprint', 'demo'),
  imageReview: model.stageActionSpec('images', 'review', 'demo'),
  imageRedraw: model.stageActionSpec('images', 'redraw', 'demo'),
  planningTabs: model.workspaceTabs('planning').map(tab => tab.href),
  imageTabs: model.workspaceTabs('images').map(tab => tab.href),
  planningPlacement: model.workspaceStepPlacement('planning'),
  imagePlacement: model.workspaceStepPlacement('images')
}};
process.stdout.write(JSON.stringify(result));
"""
        completed = subprocess.run(
            ["node", "-e", node_script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["stages"],
            [
                ["library", "/library"],
                ["discovery", "/discovery"],
                ["planning", "/planning?tab=matrix"],
                ["sections", "/sections"],
                ["images", "/images?tab=review"],
                ["draft", "/draft"],
                ["final", "/final"],
            ],
        )
        self.assertEqual(result["legacy"], ["planning", "planning", "images", "images"])
        self.assertEqual(result["planningMatrix"]["backendStage"], "matrix")
        self.assertEqual(result["planningMatrix"]["nextPath"], "/planning?tab=blueprint&project=demo")
        self.assertEqual(result["planningBlueprint"]["backendStage"], "blueprint")
        self.assertEqual(result["planningBlueprint"]["nextPath"], "/sections?project=demo")
        self.assertEqual(result["imageReview"]["backendStage"], "figure-review")
        self.assertEqual(result["imageReview"]["nextPath"], "/images?tab=redraw&project=demo")
        self.assertEqual(result["imageRedraw"]["backendStage"], "figures")
        self.assertEqual(result["imageRedraw"]["nextPath"], "/draft?project=demo")
        self.assertEqual(result["planningTabs"], ["/planning?tab=matrix", "/planning?tab=blueprint"])
        self.assertEqual(result["imageTabs"], ["/images?tab=review", "/images?tab=redraw"])
        self.assertEqual(result["planningPlacement"], "middle-header")
        self.assertEqual(result["imagePlacement"], "middle-header")

    def test_planning_and_image_workspaces_separate_flow_steps_from_view_tabs(self) -> None:
        script = REVIEW_UI.read_text(encoding="utf-8")
        css = (REVIEW_UI.parent / "review-ui.css").read_text(encoding="utf-8")
        matrix_page = (REVIEW_UI.parent / "matrix.html").read_text(encoding="utf-8")
        blueprint_page = (REVIEW_UI.parent / "blueprint.html").read_text(encoding="utf-8")
        figure_page = (REVIEW_UI.parent / "figures.html").read_text(encoding="utf-8")
        review_page = (REVIEW_UI.parent / "figure-review.html").read_text(encoding="utf-8")
        self.assertIn('workspaceStepPlacement(stageId) === "middle-header"', script)
        self.assertIn('heading.classList.add("rw-workspace-flow-head")', script)
        self.assertIn('viewTabs?.classList.add("rw-workspace-view-tabs")', script)
        self.assertNotIn('toolbar = document.createElement("div")', script)
        self.assertIn(".workspace-step-strip-head", css)
        self.assertIn(".rw-workspace-view-tabs .tab::before", css)
        self.assertRegex(
            css,
            r"\.rw-workspace-view-tabs \.tab::before\s*\{[^}]*content:\s*none",
        )
        self.assertIn('data-tab="paper">Paper</button>', matrix_page)
        self.assertIn('data-tab="outline">Outline</button>', matrix_page)
        self.assertIn('data-tab="section">Section</button>', blueprint_page)
        self.assertIn('data-tab="plan">Writing Plan</button>', blueprint_page)
        self.assertIn('data-tab="outline">Selected Outline</button>', blueprint_page)
        self.assertIn('data-tab="figure">Image Preview</button>', figure_page)
        self.assertIn('data-tab="report">Redraw Report</button>', figure_page)
        self.assertNotIn('<div class="tabs">', review_page)
        self.assertNotIn("body.page-images .app {\n  height: calc(100vh - 172px)", css)

    def test_figure_to_draft_action_uses_live_readiness_without_hidden_ai_generation(self) -> None:
        script = REVIEW_UI.read_text(encoding="utf-8")
        figure_page = (REVIEW_UI.parent / "figures.html").read_text(encoding="utf-8")
        i18n = I18N.read_text(encoding="utf-8")

        self.assertIn("window.reviewFigureStageReadiness", figure_page)
        self.assertIn("review-stage-readiness-change", figure_page)
        self.assertIn("review-stage-readiness-change", script)
        self.assertIn("syncStageActionReadiness", script)
        self.assertIn("button.disabled = !readiness.ready", script)
        self.assertIn("usableCount", figure_page)
        self.assertIn("remainingCount", figure_page)
        self.assertIn("generationActive", figure_page)
        self.assertIn('endpoint: `/api/project/${encoded}/run/figures`', script)
        self.assertIn("selected manuscript figures are usable", i18n)
        self.assertIn("Figure generation is still running", i18n)
        self.assertIn("No manuscript figure is selected", i18n)

    def test_every_new_navigation_label_has_a_chinese_translation(self) -> None:
        source = I18N.read_text(encoding="utf-8")
        mappings = {
            "Analysis & Planning": "文献分析与写作规划",
            "Image Processing": "图像处理",
            "Literature Matrix": "文献矩阵",
            "Outline & Blueprint": "大纲与章节蓝图",
            "Source Figure Review": "候选源图审核",
            "AI Redraw & Manual Edit": "AI 重绘与人工编辑",
            "Image Preview": "图像预览",
            "Redraw Report": "重绘报告",
            "Confirm Matrix and Continue to Outline & Blueprint": "确认文献矩阵并继续大纲与章节蓝图",
            "Generate Writing Tasks and Enter Sections": "生成写作任务并进入分节写作",
            "Confirm Source Figures and Continue to AI Redraw": "确认候选源图并继续 AI 重绘",
            "Confirm Images and Enter Draft": "确认图像并进入初稿",
            "Used by Outline & Blueprint": "用于大纲与章节蓝图",
            "Saved. Outline & Blueprint will use this edited outline.": "已保存，大纲与章节蓝图将使用此编辑版本。",
            "Planning handoff time is unavailable.": "未记录写作规划交接时间。",
            "The planning blueprint changed. Regenerate section drafts from the current writing requirements.": "写作规划中的章节蓝图已更新，请根据当前写作要求重新生成分节草稿。",
            "Final assembles and audits the human-approved Draft.": "最终生成会组装并审计已由人工确认的初稿。",
            "Approved Draft": "已确认初稿",
        }
        for english, chinese in mappings.items():
            with self.subTest(label=english):
                self.assertIn(f'"{english}": "{chinese}"', source)
        self.assertIn(r"/^Transferred from Analysis & Planning at (.+)$/", source)

    def test_visible_workspace_copy_uses_seven_stage_names_not_old_numbers(self) -> None:
        dashboard = ROOT / "view" / "assets" / "dashboard"
        for page_name in (
            "matrix.html",
            "sections.html",
            "figure-review.html",
            "figures.html",
            "draft.html",
            "final.html",
        ):
            source = (dashboard / page_name).read_text(encoding="utf-8")
            with self.subTest(page=page_name):
                for old_name in ("Stage 4", "Stage 6", "Stage 7", "Stage 8", "Stage 9"):
                    self.assertNotIn(old_name, source)

    def test_stage_pages_do_not_ship_a_second_legacy_navigation(self) -> None:
        dashboard = ROOT / "view" / "assets" / "dashboard"
        legacy_paths = (
            "/library",
            "/discovery",
            "/matrix",
            "/blueprint",
            "/sections",
            "/figure-review",
            "/figures",
            "/draft",
            "/final",
        )
        for page_name in STAGE_PAGES:
            source = (dashboard / page_name).read_text(encoding="utf-8")
            nav_match = re.search(
                r'<div class="nav-right">(?P<body>.*?)</div>',
                source,
                flags=re.DOTALL,
            )
            with self.subTest(page=page_name):
                self.assertIsNotNone(nav_match)
                nav_body = nav_match.group("body")
                for path in legacy_paths:
                    self.assertNotIn(f'href="{path}"', nav_body)

    def test_stage_pages_use_one_cache_version_for_shared_ui_assets(self) -> None:
        dashboard = ROOT / "view" / "assets" / "dashboard"
        pattern = re.compile(
            r'review-(?:ui\.css|i18n\.js|ui\.js)\?v=([0-9A-Za-z_-]+)'
        )
        versions: set[str] = set()
        for page_name in STAGE_PAGES:
            source = (dashboard / page_name).read_text(encoding="utf-8")
            page_versions = pattern.findall(source)
            with self.subTest(page=page_name):
                self.assertEqual(len(page_versions), 3)
                self.assertEqual(len(set(page_versions)), 1)
            versions.update(page_versions)
        self.assertEqual(len(versions), 1)


if __name__ == "__main__":
    unittest.main()
