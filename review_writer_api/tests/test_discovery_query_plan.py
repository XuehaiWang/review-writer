from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "review-topic-paper-discovery" / "scripts" / "discover.py"
SPEC = importlib.util.spec_from_file_location("review_topic_paper_discovery_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
discover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(discover)


EMPTY_RULES = {key: {} for key in discover.STRUCTURED_TAG_KEYS}
ALLENE_RULES = discover.load_classification_rules(ROOT, "allene")


class DiscoveryQueryPlanTests(unittest.TestCase):
    def test_metal_aliases_are_canonicalized_and_deduplicated(self) -> None:
        keyword_set = discover.build_keyword_set(
            "Palladium catalysis",
            ["Pd"],
            agent_keywords=[
                {
                    "keyword": "palladium catalysis",
                    "category": "catalyst_or_method",
                    "source": "agent",
                }
            ],
            classification_rules=ALLENE_RULES,
        )

        self.assertEqual(1, len(keyword_set["merged_keywords"]))
        self.assertEqual(
            "palladium catalysis",
            keyword_set["merged_keywords"][0]["keyword"],
        )

    def test_provider_instruction_fragments_are_removed_without_losing_valid_terms(self) -> None:
        plan = {
            "schema_version": 1,
            "topic": "Allene synthesis grouped by catalyst metal center",
            "resolved_concepts": [],
            "unresolved_concepts": [],
            "keywords": [
                {
                    "keyword": "gold catalysis",
                    "category": "catalyst_or_method",
                    "source": "agent",
                    "reason": "taxonomy match",
                },
                {
                    "keyword": "categorized metal center Cu",
                    "category": "catalyst_or_method",
                    "source": "agent",
                    "reason": "instruction echo",
                },
                {
                    "keyword": "etc.",
                    "category": "unclassified",
                    "source": "agent",
                    "reason": "list filler",
                },
            ],
            "filters": {},
            "group_by": ["catalyst_or_method"],
        }

        validated = discover.validate_query_plan(plan, plan["topic"])

        self.assertEqual(["gold catalysis"], [item["keyword"] for item in validated["keywords"]])

    def test_old_false_copper_tag_is_checked_against_source_evidence(self) -> None:
        meta = {
            "paper_id": "P900",
            "title": {"value": "Gold-catalyzed synthesis of axially chiral allenes"},
            "structured_tags": {
                "value": {"catalyst_or_method": "copper catalysis"}
            },
            "source_paths": {},
        }

        copper = discover.score_local_paper(
            meta,
            "copper catalysis",
            "catalyst_or_method",
            [],
            ALLENE_RULES,
        )
        gold = discover.score_local_paper(
            meta,
            "gold catalysis",
            "catalyst_or_method",
            [],
            ALLENE_RULES,
        )

        self.assertEqual(0, copper["direct_raw_score"])
        self.assertGreaterEqual(gold["direct_raw_score"], 1.4)
        self.assertIn("source_text", gold["matched_fields"])

    def test_unknown_keyword_uses_unclassified_route(self) -> None:
        self.assertEqual(
            "unclassified",
            discover.classify_keyword("electrochemical activation platform", EMPTY_RULES),
        )

    def test_deterministic_plan_splits_multiple_topic_themes(self) -> None:
        plan = discover.deterministic_query_plan(
            "photoredox catalysis, electrochemical activation; enzyme-mediated synthesis",
            [],
            EMPTY_RULES,
        )
        by_keyword = {item["keyword"]: item["category"] for item in plan["keywords"]}
        self.assertIn("photoredox catalysis", by_keyword)
        self.assertIn("electrochemical activation", by_keyword)
        self.assertIn("enzyme-mediated synthesis", by_keyword)
        self.assertEqual("unclassified", by_keyword["electrochemical activation"])
        self.assertNotIn(
            "photoredox catalysis electrochemical activation enzyme-mediated synthesis",
            by_keyword,
        )

    def test_auto_planner_falls_back_without_failing_discovery(self) -> None:
        with patch.object(discover, "llm_query_plan", side_effect=TimeoutError("offline")):
            plan = discover.build_auto_query_plan(
                "photoredox catalysis, electrochemical activation",
                [],
                EMPTY_RULES,
            )
        self.assertEqual("dashboard_deterministic", plan["planner"])
        self.assertIn("TimeoutError", plan["planner_notice"])

    def test_llm_query_plan_uses_internal_gateway_without_provider_key(self) -> None:
        gateway_plan = {
            "schema_version": 1,
            "topic": "ignored",
            "resolved_concepts": [],
            "unresolved_concepts": [],
            "keywords": [],
            "filters": {},
            "group_by": [],
        }
        with (
            patch.dict(
                os.environ,
                {
                    "REVIEW_WRITER_MODEL_GATEWAY_URL": "http://127.0.0.1/gateway",
                    "REVIEW_WRITER_TASK_TOKEN": "task-token",
                    "REVIEW_WRITING_API_KEY": "",
                    "OPENAI_API_KEY": "",
                },
                clear=False,
            ),
            patch.object(discover, "call_gateway_json", return_value=gateway_plan) as gateway,
        ):
            result = discover.llm_query_plan("electrochemical catalysis", [])

        self.assertEqual(gateway_plan, result)
        self.assertEqual("discovery-query-plan", gateway.call_args.kwargs["label"])
        self.assertIn("electrochemical catalysis", gateway.call_args.args[0])

    def test_unclassified_keyword_searches_across_structured_fields(self) -> None:
        rules = {
            **EMPTY_RULES,
            "catalyst_or_method": {"photoredox catalysis": ["photoredox"]},
        }
        meta = {
            "paper_id": "P001",
            "title": {"value": "A photoredox platform"},
            "structured_tags": {
                "catalyst_or_method": "photoredox catalysis",
            },
            "source_paths": {},
        }
        scored = discover.score_local_paper(
            meta,
            "photoredox",
            "unclassified",
            [],
            rules,
        )
        self.assertGreaterEqual(scored["direct_raw_score"], 1.4)
        self.assertIn("catalyst_or_method", scored["matched_fields"])

    def test_unclassified_is_not_valid_for_group_by(self) -> None:
        plan = discover.deterministic_query_plan(
            "electrochemical activation",
            [],
            EMPTY_RULES,
        )
        plan["group_by"] = ["unclassified"]
        with self.assertRaises(discover.QueryPlanError):
            discover.validate_query_plan(plan, plan["topic"])

    def test_project_tag_assessment_preserves_base_tags_and_syncs_duplicate_hits(self) -> None:
        papers = {
            "P001": {
                "paper_id": "P001",
                "structured_tags": {
                    "value": {
                        "catalyst_or_method": "copper catalysis",
                        "reaction_type": "allenation",
                    }
                },
            }
        }
        grouped = [
            {
                "keyword": "copper catalysis",
                "category": "catalyst_or_method",
                "local_results": [
                    {
                        "paper_id": "P001",
                        "score": 0.91,
                        "matched_fields": ["catalyst_or_method"],
                        "reason": "structured tag match",
                    }
                ],
            },
            {
                "keyword": "allenation",
                "category": "reaction_type",
                "local_results": [
                    {
                        "paper_id": "P001",
                        "score": 0.83,
                        "matched_fields": ["reaction_type"],
                        "reason": "structured tag match",
                    }
                ],
            },
        ]

        discover.attach_project_tag_assessments(
            grouped,
            papers,
            topic="Copper-catalyzed allenation",
            query_plan_source="dashboard_llm",
            taxonomy={"profile": "default"},
        )

        first = grouped[0]["local_results"][0]
        duplicate = grouped[1]["local_results"][0]
        self.assertEqual("copper catalysis", first["base_tags"]["catalyst_or_method"])
        self.assertEqual("allenation", first["base_tags"]["reaction_type"])
        self.assertEqual(
            {
                "catalyst_or_method": ["copper catalysis"],
                "reaction_type": ["allenation"],
            },
            first["project_tag_assessment"]["suggested_tags"],
        )
        self.assertEqual(first["project_tag_assessment"], duplicate["project_tag_assessment"])
        self.assertFalse(first["project_tag_assessment"]["review_required"])
        self.assertEqual(
            "automatic", first["project_tag_assessment"]["application_mode"]
        )
        self.assertEqual({}, first["confirmed_project_tags"])
        self.assertEqual("pending", first["tag_review_status"])


if __name__ == "__main__":
    unittest.main()
