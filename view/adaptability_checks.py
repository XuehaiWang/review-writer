"""Regression checks for topic, path, and provider portability."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from review_writer_core import providers, taxonomy  # noqa: E402
from review_writer_core.project_config import (  # noqa: E402
    project_taxonomy_profile,
    save_project_config,
)
from review_writer_core.sciatlas_client import load_config as load_sciatlas_config  # noqa: E402
from review_writer_core.workspace import WorkspacePaths, discover_review_root  # noqa: E402


DISCOVER = load_module(
    "adaptability_discover",
    ROOT / "skills" / "review-topic-paper-discovery" / "scripts" / "discover.py",
)
BLUEPRINT = load_module(
    "adaptability_blueprint",
    ROOT / "skills" / "review-section-blueprint" / "scripts" / "init_section_blueprint.py",
)
SECTION_WRITER = load_module(
    "adaptability_section_writer",
    ROOT
    / "skills"
    / "review-section-drafting-figure-picking"
    / "scripts"
    / "generate_section_drafts.py",
)
OVERVIEW = load_module(
    "adaptability_overview",
    ROOT / "skills" / "review-figure-style-redraw" / "scripts" / "generate_overview_figure.py",
)
SUMMARY_CHART = load_module(
    "adaptability_summary_chart",
    ROOT
    / "skills"
    / "review-outline-summary-chart"
    / "scripts"
    / "generate_review_summary_chart.py",
)


class AdaptabilityChecks(unittest.TestCase):
    def test_unrelated_topic_uses_general_profile_and_nonempty_keyword(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REVIEW_TAXONOMY_PROFILE": "", "REVIEW_CLASSIFICATION_RULES": ""},
            clear=False,
        ):
            self.assertEqual(
                taxonomy.suggest_taxonomy_profile("Graph neural networks for drug discovery"),
                "general_academic",
            )
            keywords = DISCOVER.infer_keywords(
                "Graph neural networks for drug discovery",
                [],
            )
        self.assertTrue(keywords)
        self.assertIn("graph neural networks", keywords[0]["keyword"].casefold())
        self.assertNotIn("allene", json.dumps(keywords).casefold())

    def test_allene_topic_opts_into_specialized_profile(self) -> None:
        self.assertEqual(
            taxonomy.suggest_taxonomy_profile("Axially chiral allene synthesis"),
            "allene",
        )
        rules = DISCOVER.load_classification_rules(ROOT, "allene")
        keywords = DISCOVER.infer_keywords(
            "Axially chiral allene synthesis from propargylic alcohols",
            [],
            classification_rules=rules,
        )
        self.assertTrue(any("allene" in item["keyword"] for item in keywords))

    def test_project_profile_is_persisted_and_not_reinferred(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "review-projects").mkdir()
            saved = save_project_config(
                root,
                "demo",
                topic="Graph models",
                taxonomy_profile="chemistry_general",
            )
            self.assertEqual(saved["taxonomy_profile"], "chemistry_general")
            self.assertEqual(
                project_taxonomy_profile(root, "demo", topic="allene synthesis"),
                "chemistry_general",
            )

    def test_rule_pack_selection_and_section_writer_are_connected(self) -> None:
        skill_root = ROOT / "skills" / "review-section-blueprint"
        general_name, general_path = BLUEPRINT.select_rule_pack(
            skill_root,
            "Graph neural networks for drug discovery",
        )
        allene_name, _ = BLUEPRINT.select_rule_pack(
            skill_root,
            "Axially chiral allene synthesis",
        )
        self.assertEqual(general_name, "general")
        self.assertEqual(allene_name, "allenation")
        rules = SECTION_WRITER.load_blueprint_rule_pack(
            ROOT,
            {"rule_pack": general_name, "rule_pack_path": general_path},
        )
        self.assertIn("General scientific review style", rules)
        self.assertNotIn("ATA Introduction", rules)

    def test_section_writer_loads_application_rules_for_hosted_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            hosted_workspace = Path(raw)
            self.assertFalse((hosted_workspace / "skills").exists())
            rules = SECTION_WRITER.load_blueprint_rule_pack(
                hosted_workspace,
                {
                    "rule_pack": "allenation",
                    "rule_pack_path": "references/rule_packs/allenation",
                },
            )
        self.assertIn("Organic Review Style", rules)

    def test_blueprint_logic_is_generic_unless_specialized_pack_is_selected(self) -> None:
        self.assertEqual(
            BLUEPRINT.infer_logic("Advances in graph neural networks", "general"),
            "thematic_synthesis",
        )
        self.assertEqual(
            BLUEPRINT.infer_logic("Model prediction and benchmark performance", "general"),
            "comparative_performance",
        )
        self.assertEqual(
            BLUEPRINT.infer_logic("Propargylic carbonate methods", "allenation"),
            "precursor_class",
        )
        generic = BLUEPRINT.build_section(
            {"section_id": "sec1", "title": "Graph representations", "assigned_papers": ["P001"]},
            [
                {
                    "paper_id": "P001",
                    "method": "message-passing neural networks",
                    "main_finding": "improved molecular property prediction",
                    "limitation": "evaluation on narrow benchmark sets",
                    "review_topic_relevance": "high",
                }
            ],
            ["method", "evidence"],
            {},
            "",
            "Applications",
            "general",
        )
        serialized = json.dumps(generic).casefold()
        for leaked_term in ("allene", "propargyl", "substrate class", "activation mode"):
            self.assertNotIn(leaked_term, serialized)

    def test_rule_pack_path_cannot_escape_skill(self) -> None:
        with self.assertRaises(RuntimeError):
            SECTION_WRITER.load_blueprint_rule_pack(
                ROOT,
                {"rule_pack": "custom", "rule_pack_path": "../../../.git"},
            )

    def test_generic_overview_does_not_inject_allene_geometry(self) -> None:
        prompt = OVERVIEW._build_skeleton_description(
            {
                "review_title": "Graph neural networks for drug discovery",
                "taxonomy_profile": "chemistry_general",
                "product_keywords": [],
            }
        )
        self.assertIn("GENERAL RENDERING RULES", prompt)
        self.assertNotIn("For AXIAL CHIRALITY", prompt)
        self.assertNotIn("central carbon is sp", prompt)

        features = {
            "review_title": "Graph neural networks for drug discovery",
            "taxonomy_profile": "chemistry_general",
            "product_keywords": [],
            "substrate_keywords": [],
            "catalyst_keywords": ["computational methods"],
            "metal_categories": ["Representation", "Prediction", "Generation"],
            "group_by": ["catalyst_or_method"],
            "classification_rule": "By method",
            "time_window": "recent years",
            "has_chirality": False,
            "has_reaction_focus": False,
            "has_metal_classification": False,
        }
        for template in OVERVIEW.read_json(OVERVIEW.overview_template_catalog_path()):
            adapted = OVERVIEW.build_adapted_prompt(template, features).casefold()
            for leaked_term in ("allene", "allenyl", "propargyl"):
                self.assertNotIn(leaked_term, adapted, template["layout_type"])

    def test_workspace_and_provider_contracts_are_machine_independent(self) -> None:
        self.assertEqual(discover_review_root(ROOT / "view"), ROOT)
        paths = WorkspacePaths(ROOT)
        self.assertEqual(paths.stage("demo", "final").name, "05_final_audit")
        self.assertEqual(
            providers.openai_endpoint("https://example.test", "chat/completions"),
            "https://example.test/v1/chat/completions",
        )
        self.assertEqual(
            providers.openai_endpoint("https://example.test/v1", "/responses"),
            "https://example.test/v1/responses",
        )

    def test_sciatlas_requires_explicit_safe_remote_origin(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SCIATLAS_API_BASE_URL": "",
                "SCIATLAS_API_KEY": "",
                "SCIATLAS_ALLOW_INSECURE_HTTP": "",
            },
            clear=False,
        ):
            self.assertFalse(load_sciatlas_config().configured)

        secure = load_sciatlas_config(
            base_url="https://sciatlas-proxy.example",
            api_key="secret",
        )
        self.assertTrue(secure.configured)
        self.assertEqual(secure.base_url, "https://sciatlas-proxy.example")

        local = load_sciatlas_config(
            base_url="http://127.0.0.1:9080/",
            api_key="secret",
        )
        self.assertEqual(local.base_url, "http://127.0.0.1:9080")

        with self.assertRaisesRegex(ValueError, "cleartext"):
            load_sciatlas_config(
                base_url="http://sciatlas.example",
                api_key="secret",
            )
        legacy = load_sciatlas_config(
            base_url="http://sciatlas.example",
            api_key="secret",
            allow_insecure_http=True,
        )
        self.assertTrue(legacy.configured)

    def test_summary_chart_png_has_no_browser_or_machine_dependency(self) -> None:
        source_path = (
            ROOT
            / "skills"
            / "review-outline-summary-chart"
            / "scripts"
            / "generate_review_summary_chart.py"
        )
        source = source_path.read_text(encoding="utf-8")
        for machine_specific in (
            "C:/Windows",
            "C:/Program Files",
            "subprocess.run",
            "find_browser_executable",
        ):
            self.assertNotIn(machine_specific, source)

        section = SUMMARY_CHART.ReviewSection(
            heading="Introduction",
            level=1,
            line_number=1,
            section_type="introduction",
            cited_paper_ids=["P001", "P002"],
        )
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "review_summary_chart.png"
            manifest = SUMMARY_CHART.render_full_chart_png(
                "flowchart TD",
                output,
                sections=[section],
                review_title="Portable Review",
            )
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(manifest["renderer"], "pillow-static")
            with SUMMARY_CHART.Image.open(output) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreaterEqual(image.width, 1200)

    def test_summary_chart_font_can_be_configured_without_fixed_drive_paths(self) -> None:
        configured = ROOT / "assets" / "custom-chart-font.ttf"
        with mock.patch.dict(
            os.environ,
            {"REVIEW_WRITER_CHART_FONT_REGULAR": str(configured)},
            clear=False,
        ):
            candidates = SUMMARY_CHART._font_candidates(bold=False)
        self.assertEqual(candidates[0], configured)

    def test_known_hardcoding_regressions_are_absent(self) -> None:
        production_roots = [ROOT / "review_writer_core", ROOT / "skills", ROOT / "view"]
        offenders: list[str] = []
        provider_offenders: list[str] = []
        for base in production_roots:
            for path in base.rglob("*.py"):
                if path.name.endswith("_checks.py"):
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "parents[3]" in text:
                    offenders.append(str(path.relative_to(ROOT)))
                if any(host in text.casefold() for host in ("micuapi.ai", "xiaoleai.team")):
                    provider_offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])
        self.assertEqual(provider_offenders, [])
        section_source = (
            ROOT
            / "skills"
            / "review-section-drafting-figure-picking"
            / "scripts"
            / "generate_section_drafts.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('"rule_packs" / "allenation"', section_source)


if __name__ == "__main__":
    unittest.main()
