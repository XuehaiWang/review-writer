from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from review_writer_core.stages.sections.rule_packs import (
    RulePackConfigurationError,
    load_rule_pack_text,
    resolve_rule_pack,
    validate_blueprint_rule_pack,
)


ROOT = Path(__file__).resolve().parents[1]


class RulePackTests(unittest.TestCase):
    def test_short_policies_remain_complete_and_release_budget_to_longer_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            references = root / "skills/review-section-blueprint/references"
            folder = references / "general"
            folder.mkdir(parents=True)
            files = ["short.md", "long.md", "last.md"]
            (references / "rule_packs.json").write_text(json.dumps({"rule_packs": {
                "general": {"path": "references/general", "files": files},
            }}), encoding="utf-8")
            for name in files:
                (folder / name).write_text("short rule", encoding="utf-8")
            selected = resolve_rule_pack(root)
            blueprint = {"rule_pack": "general", "rule_pack_sha256": selected["sha256"]}
            complete = "\n\n".join(f"<!-- rule source: {name} -->\nshort rule" for name in files)
            self.assertEqual(complete, load_rule_pack_text(root, blueprint, char_budget=len(complete)))
            for name in files[1:]:
                (folder / name).write_text("long rule " * 500, encoding="utf-8")
            selected = resolve_rule_pack(root)
            blueprint["rule_pack_sha256"] = selected["sha256"]
            bounded = load_rule_pack_text(root, blueprint, char_budget=300)
            self.assertLessEqual(len(bounded), 300)
            self.assertIn("<!-- rule source: short.md -->\nshort rule", bounded)
            self.assertEqual(2, bounded.count("[Excerpt truncated.]"))

    def test_every_registered_policy_is_present_within_the_prompt_budget(self) -> None:
        for name in ("general", "allenation"):
            with self.subTest(name=name):
                selected = resolve_rule_pack(ROOT, name=name)
                blueprint = {"rule_pack": name, "rule_pack_sha256": selected["sha256"]}
                text = load_rule_pack_text(ROOT, blueprint)
                self.assertLessEqual(len(text), 14_000)
                for filename in selected["files"]:
                    self.assertIn(f"<!-- rule source: {filename} -->", text)
                if name == "allenation":
                    self.assertIn("Organic Review Style", text)
                    self.assertIn("[Excerpt truncated.]", text)
                with self.assertRaises(RulePackConfigurationError):
                    load_rule_pack_text(ROOT, blueprint, char_budget=20)

    def test_malformed_catalog_and_escaping_paths_fail_with_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "skills/review-section-blueprint/references/rule_packs.json"
            manifest.parent.mkdir(parents=True)
            for content in ([1], "unexpected", 1, None, {"rule_packs": {"general": {
                "path": "../../../outside", "files": ["rules.md"],
            }}}, {"rule_packs": {"general": {
                "path": "references/general", "files": ["../../outside.md"],
            }}}):
                with self.subTest(content=content):
                    manifest.write_text(json.dumps(content), encoding="utf-8")
                    with self.assertRaises(RulePackConfigurationError):
                        resolve_rule_pack(root)

    def test_only_strong_allene_signals_select_specialized_pack(self) -> None:
        self.assertEqual(
            "allenation",
            resolve_rule_pack(ROOT, topic="Recent allene synthesis")["name"],
        )
        self.assertEqual(
            "allenation",
            resolve_rule_pack(ROOT, topic="联烯合成方法")["name"],
        )
        for topic in (
            "Propargylic substitution methods",
            "Axial chirality in biaryls",
            "SN2' reactions",
            "Suzuki coupling",
            "Indole synthesis",
            "Clinical evidence review",
        ):
            with self.subTest(topic=topic):
                self.assertEqual("general", resolve_rule_pack(ROOT, topic=topic)["name"])

    def test_sections_validate_the_blueprint_locked_version(self) -> None:
        selected = resolve_rule_pack(ROOT, topic="allenation methods")
        validated = validate_blueprint_rule_pack(
            ROOT,
            {
                "rule_pack": selected["name"],
                "rule_pack_sha256": selected["sha256"],
            },
        )
        self.assertEqual(selected["sha256"], validated["sha256"])

        with self.assertRaises(RulePackConfigurationError):
            validate_blueprint_rule_pack(
                ROOT,
                {"rule_pack": selected["name"], "rule_pack_sha256": "stale"},
            )


if __name__ == "__main__":
    unittest.main()
