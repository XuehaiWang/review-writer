from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ASSET_DIR = Path(__file__).resolve().parent / "assets" / "dashboard"
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


class DashboardI18nChecks(unittest.TestCase):
    def test_library_precise_parsing_progress_translates_at_runtime(self) -> None:
        source_path = (ASSET_DIR / "review-i18n.js").as_posix()
        script = f"""
const fs = require('fs');
global.window = {{location: {{pathname: '/', search: ''}}, dispatchEvent() {{}}}};
global.localStorage = {{getItem() {{ return 'zh-CN'; }}, setItem() {{}}}};
global.document = {{readyState: 'loading', addEventListener() {{}}}};
global.CustomEvent = function() {{}};
eval(fs.readFileSync({source_path!r}, 'utf8'));
process.stdout.write(window.reviewI18n.t('Uploading and running MinerU precise parsing 2/5: paper.pdf'));
"""
        translated = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        self.assertEqual(
            "正在上传并进行 MinerU 精确解析 2/5：paper.pdf",
            translated,
        )

    def test_task7_static_labels_translate_at_runtime(self) -> None:
        source_path = (ASSET_DIR / "review-i18n.js").as_posix()
        script = f"""
const fs = require('fs');
global.window = {{location: {{pathname: '/', search: ''}}, dispatchEvent() {{}}}};
global.localStorage = {{getItem() {{ return 'zh-CN'; }}, setItem() {{}}}};
global.document = {{readyState: 'loading', addEventListener() {{}}}};
global.CustomEvent = function() {{}};
eval(fs.readFileSync({source_path!r}, 'utf8'));
process.stdout.write(JSON.stringify([
  window.reviewI18n.t('Batch upload runs MinerU precise parsing, then builds searchable metadata and full-text Markdown.'),
  window.reviewI18n.t('All categories'),
  window.reviewI18n.t('Discovery project')
]));
"""
        translated = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
        self.assertEqual(
            '["批量上传会执行 MinerU 精确解析，并生成可检索的元数据和全文 Markdown。","全部类别","检索项目"]',
            translated,
        )

    def test_every_stage_loads_i18n_before_shared_ui(self) -> None:
        for page_name in STAGE_PAGES:
            with self.subTest(page=page_name):
                html = (ASSET_DIR / page_name).read_text(encoding="utf-8")
                i18n = re.search(r'review-i18n\.js\?v=([0-9]+)', html)
                ui = re.search(r'review-ui\.js\?v=([0-9]+)', html)
                self.assertIsNotNone(i18n)
                self.assertIsNotNone(ui)
                self.assertEqual(i18n.group(1), ui.group(1))
                self.assertLess(i18n.start(), ui.start())

    def test_language_switch_is_persistent_and_content_safe(self) -> None:
        source = (ASSET_DIR / "review-i18n.js").read_text(encoding="utf-8")
        self.assertIn('const STORAGE_KEY = "review-writer-ui-language"', source)
        self.assertIn('localStorage.setItem(STORAGE_KEY, language)', source)
        self.assertIn(".markdown, .draft-view, .article-content", source)
        self.assertIn('window.dispatchEvent(new CustomEvent("review-language-change"', source)

    def test_switch_has_shared_responsive_styling(self) -> None:
        css = (ASSET_DIR / "review-ui.css").read_text(encoding="utf-8")
        self.assertIn(".rw-language-switch", css)
        self.assertIn(".rw-language-option.active", css)
        self.assertIn('html[lang="zh-CN"] body', css)

    def test_blueprint_dynamic_labels_have_chinese_mappings(self) -> None:
        source = (ASSET_DIR / "review-i18n.js").read_text(encoding="utf-8")
        for english, chinese in (
            ("Section Thesis", "章节论点"),
            ("Target Paragraphs", "目标段落数"),
            ("Core Papers", "核心论文"),
            ("Claims to Establish", "待建立论点"),
            ("Figure and Table Needs", "图表需求"),
            ("Writing Guardrails", "写作约束"),
            ("Section Transition", "章节衔接"),
            ("Supporting Papers:", "支撑论文："),
            ("Comparison Axes:", "比较维度："),
        ):
            with self.subTest(label=english):
                self.assertIn(f'"{english}": "{chinese}"', source)
        self.assertIn(r"(\d+) core papers · (\d+) review claims", source)
        self.assertIn(r"(sec\d+) · (.+)", source)
        self.assertIn(r"^Claim Type: (.+)$", source)
        self.assertIn(r"^Candidate Papers: (.+)$", source)

    def test_sections_workspace_removes_redundant_report_and_adapts_tabs(self) -> None:
        source = (ASSET_DIR / "sections.html").read_text(encoding="utf-8")
        self.assertIn('id="sectionTabs"', source)
        self.assertIn("function renderTabs()", source)
        self.assertIn("taskOnlyMode()?[['tasks','Writing Requirements']]", source)
        self.assertIn("['section','Section Draft']", source)
        self.assertIn("['drafts','Merged Preview']", source)
        self.assertIn("function renderFigureRequirements", source)
        self.assertNotIn('data-tab="report"', source)
        self.assertNotIn("section_drafting_report_md", source)

    def test_stage_actions_mount_by_stable_dom_contract_in_every_language(self) -> None:
        source = (ASSET_DIR / "review-ui.js").read_text(encoding="utf-8")
        self.assertIn("function stageActionHost(current)", source)
        for selector in (
            'library: "#libraryStageAction"',
            'planning: "#summary"',
            'images: workspaceTab === "review" ? "#savedStatus" : "#summary"',
            'draft: "#summaryBox"',
        ):
            self.assertIn(selector, source)
        library_html = (ASSET_DIR / "library.html").read_text(encoding="utf-8")
        self.assertIn('id="libraryStageAction"', library_html)
        self.assertIn("data-stage-action-host", library_html)
        self.assertIn("const reviewGate = stageActionHost(current);", source)

    def test_middle_workspace_style_is_shared_and_blueprint_has_one_stage_action(self) -> None:
        source = (ASSET_DIR / "review-ui.js").read_text(encoding="utf-8")
        css = (ASSET_DIR / "review-ui.css").read_text(encoding="utf-8")
        blueprint = (ASSET_DIR / "blueprint.html").read_text(encoding="utf-8")
        self.assertIn("function mountWorkspaceChrome()", source)
        self.assertIn('workspace.classList.add("rw-workspace-panel")', source)
        self.assertIn('heading?.classList.add("rw-workspace-head")', source)
        self.assertIn('tabs?.classList.add("rw-workspace-tabs")', source)
        self.assertIn(".head.rw-workspace-head", css)
        self.assertIn(".tabs.rw-workspace-tabs", css)
        self.assertNotIn('id="enterSections"', blueprint)
        self.assertNotIn("function enterSections()", blueprint)
        self.assertIn('stageId === "planning"', source)
        self.assertIn('backendStage: "blueprint"', source)
        self.assertIn("/section-tasks`", source)


if __name__ == "__main__":
    unittest.main()
