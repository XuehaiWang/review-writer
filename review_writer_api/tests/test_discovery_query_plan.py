from __future__ import annotations

import importlib.util
import os
import tempfile
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
    def test_positional_enyne_name_matches_conjugated_enyne_class(self) -> None:
        self.assertEqual(
            1.0,
            discover.match_score(
                "conjugated enynes",
                "Hydroboration of but-1-en-3-ynes affords axially chiral allenylboranes.",
            ),
        )

    def test_named_propargylic_leaving_group_matches_alcohol_derivative_class(self) -> None:
        self.assertEqual(
            1.0,
            discover.match_score(
                "propargylic alcohol derivatives",
                "Carbonylation of enantioenriched propargylic mesylates",
            ),
        )

    def test_equivalent_chiral_product_synonyms_share_one_discovery_group(self) -> None:
        keyword_set = discover.build_keyword_set(
            "axial-chiral allene synthesis",
            [],
            agent_keywords=[
                {
                    "keyword": "axial-chiral allenes",
                    "category": "product",
                    "source": "agent",
                },
                {
                    "keyword": "axially chiral allenes",
                    "category": "product",
                    "source": "agent",
                },
                {
                    "keyword": "atropisomeric allenes",
                    "category": "product",
                    "source": "agent",
                },
            ],
            classification_rules=EMPTY_RULES,
        )

        products = [
            item
            for item in keyword_set["merged_keywords"]
            if item["category"] == "product"
        ]
        self.assertEqual(1, len(products))
        self.assertEqual(
            [
                "axial-chiral allenes",
                "axially chiral allenes",
                "atropisomeric allenes",
            ],
            products[0]["query_aliases"],
        )

    def test_resolved_concept_normalized_alias_is_canonicalized(self) -> None:
        topic = "Syntheses of axial-chiral allenes categorized by the substrates"
        plan = {
            "schema_version": 1,
            "topic": topic,
            "resolved_concepts": [
                {
                    "surface": "axial-chiral allenes",
                    "normalized": "axially chiral allenes",
                    "confidence": 0.96,
                    "reason": "Common normalized scientific phrasing.",
                }
            ],
            "unresolved_concepts": [],
            "keywords": [
                {
                    "keyword": "axially chiral allenes",
                    "category": "product",
                    "source": "user",
                    "reason": "Core product family.",
                }
            ],
            "filters": {},
            "group_by": ["substrate"],
        }

        validated = discover.validate_query_plan(plan, topic)

        self.assertEqual(
            "axially chiral allenes",
            validated["resolved_concepts"][0]["expanded_name"],
        )
        self.assertNotIn("normalized", validated["resolved_concepts"][0])

    def test_empty_optional_year_filters_are_removed(self) -> None:
        topic = "Syntheses of axial-chiral allenes categorized by substrate"
        for empty_value in (None, "", "   "):
            with self.subTest(empty_value=empty_value):
                plan = discover.deterministic_query_plan(
                    topic,
                    [],
                    EMPTY_RULES,
                )
                plan["filters"] = {
                    "year_from": empty_value,
                    "year_to": empty_value,
                }

                validated = discover.validate_query_plan(plan, topic)

                self.assertEqual({}, validated["filters"])

    def test_non_empty_non_integer_year_filter_is_rejected(self) -> None:
        topic = "Syntheses of axial-chiral allenes"
        plan = discover.deterministic_query_plan(topic, [], EMPTY_RULES)
        plan["filters"] = {"year_from": "2020"}

        with self.assertRaisesRegex(
            discover.QueryPlanError,
            "filters.year_from must be an integer",
        ):
            discover.validate_query_plan(plan, topic)

    def test_explicit_substrate_grouping_is_detected_from_original_prompt(self) -> None:
        topic = (
            'Please write a review on the topic "syntheses of the axial-chiral allenes", '
            "categorized by the substrates (propargylic alcohols, their derivatives, "
            "terminal alkynes, conjugated enynes, etc.) of methods."
        )

        intent = discover.parse_topic_intent(topic)
        phrases = discover.topic_phrase_candidates(topic)

        self.assertEqual(["substrate"], intent["group_by"])
        self.assertIn("syntheses axial-chiral allenes", phrases)
        self.assertIn("propargylic alcohols", phrases)
        self.assertIn("propargylic alcohol derivatives", phrases)
        self.assertIn("terminal alkynes", phrases)
        self.assertIn("conjugated enynes", phrases)
        self.assertNotIn("their derivatives", phrases)
        self.assertNotIn("methods", phrases)

    def test_keyword_prioritization_removes_implicit_review_document_scope(self) -> None:
        topic = "Please write a review of chiral allenes categorized by the substrates"
        keywords = [
            {
                "keyword": "chiral allenes",
                "category": "product",
                "source": "user",
                "reason": "core topic",
            },
            {
                "keyword": "review article",
                "category": "document_scope",
                "source": "agent",
                "reason": "writing instruction",
            },
        ]
        keywords.extend(
            {
                "keyword": f"substrate family {index}",
                "category": "substrate",
                "source": "agent",
                "reason": "facet",
            }
            for index in range(20)
        )
        compact = discover.prioritize_query_plan_keywords(
            {"keywords": keywords, "group_by": ["substrate"]}, topic
        )

        self.assertLessEqual(len(compact["keywords"]), 16)
        self.assertNotIn("review article", [item["keyword"] for item in compact["keywords"]])
        self.assertIn("chiral allenes", [item["keyword"] for item in compact["keywords"]])

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
        self.assertIn("primary_evidence", gold["matched_fields"])

    def test_anchor_constrained_retrieval_keeps_relevant_facet_and_excludes_unrelated_method(self) -> None:
        papers = {
            "P001": {
                "paper_id": "P001",
                "title": {
                    "value": "Catalytic Asymmetric Synthesis of Optically Active Allenes from Terminal Alkynes"
                },
                "structured_tags": {"value": {}},
                "source_paths": {},
            },
            "P999": {
                "paper_id": "P999",
                "title": {"value": "Computational X-ray Diffraction Methods for Crystal Analysis"},
                "structured_tags": {"value": {}},
                "source_paths": {},
            },
        }
        grouped, _stats = discover.local_search_by_keyword(
            papers,
            [
                {"keyword": "axially chiral allenes", "category": "product", "keep": True},
                {"keyword": "terminal alkynes", "category": "substrate", "keep": True},
                {"keyword": "computational methods", "category": "catalyst_or_method", "keep": True},
            ],
            "axially chiral allene synthesis",
            EMPTY_RULES,
            anchor_keywords=["axially chiral allenes"],
        )
        by_keyword = {
            group["keyword"]: [row["paper_id"] for row in group["local_results"]]
            for group in grouped
        }

        self.assertIn("P001", by_keyword["terminal alkynes"])
        self.assertNotIn("P999", by_keyword["computational methods"])

    def test_body_only_mentions_cannot_establish_topic_or_substrate_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "body.md"
            markdown.write_text(
                "Related work mentions axially chiral allenes and terminal alkynes, "
                "but this article studies generic palladium cyclization.",
                encoding="utf-8",
            )
            papers = {
                "P001": {
                    "paper_id": "P001",
                    "title": {"value": "A Generic Palladium Cyclization Study"},
                    "abstract": {
                        "value": "Cyclization pathways and catalyst stability are examined."
                    },
                    "structured_tags": {"value": {}},
                    "source_paths": {"markdown": str(markdown)},
                },
                "P002": {
                    "paper_id": "P002",
                    "title": {
                        "value": "Asymmetric Synthesis of Chiral Allenes from Terminal Alkynes"
                    },
                    "abstract": {
                        "value": "Axially chiral allenes are prepared directly from terminal alkynes."
                    },
                    "structured_tags": {"value": {}},
                    "source_paths": {},
                },
            }

            grouped, _stats = discover.local_search_by_keyword(
                papers,
                [
                    {
                        "keyword": "terminal alkynes",
                        "category": "substrate",
                        "keep": True,
                    }
                ],
                "axially chiral allene synthesis",
                EMPTY_RULES,
                anchor_keywords=["axially chiral allenes"],
            )

        self.assertEqual(
            ["P002"],
            [row["paper_id"] for row in grouped[0]["local_results"]],
        )

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

    def test_auto_planner_keeps_model_plan_when_provider_uses_normalized_alias(self) -> None:
        topic = (
            'Please write a review on the topic "syntheses of the axial-chiral allenes", '
            "categorized by the substrates (propargylic alcohols, their derivatives, "
            "terminal alkynes, conjugated enynes, etc.) of methods."
        )
        provider_plan = {
            "schema_version": 1,
            "topic": topic,
            "resolved_concepts": [
                {
                    "surface": "axial-chiral allenes",
                    "normalized": "axially chiral allenes",
                    "confidence": 0.96,
                    "reason": "Normalized scientific terminology.",
                }
            ],
            "unresolved_concepts": [],
            "keywords": [
                {
                    "keyword": keyword,
                    "category": category,
                    "source": "agent",
                    "reason": "Provider expansion.",
                }
                for keyword, category in [
                    ("axially chiral allenes", "product"),
                    ("chiral allenes", "product"),
                    ("propargylic alcohols", "substrate"),
                    ("propargylic alcohol derivatives", "substrate"),
                    ("terminal alkynes", "substrate"),
                    ("conjugated enynes", "substrate"),
                    ("review article", "document_scope"),
                ]
            ],
            "filters": {},
            "group_by": ["substrate"],
        }

        with patch.object(discover, "llm_query_plan", return_value=provider_plan):
            plan = discover.build_auto_query_plan(topic, [], EMPTY_RULES)

        self.assertEqual("dashboard_llm", plan["planner"])
        self.assertNotIn("planner_notice", plan)
        self.assertEqual(["substrate"], plan["group_by"])
        self.assertEqual(
            "axially chiral allenes",
            plan["resolved_concepts"][0]["expanded_name"],
        )
        self.assertNotIn("review article", [item["keyword"] for item in plan["keywords"]])

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
            "title": {"value": "A catalytic platform"},
            "structured_tags": {
                "value": {"catalyst_or_method": "photoredox catalysis"},
                "human_checked": True,
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
        self.assertGreater(scored["direct_raw_score"], 0)
        self.assertIn("catalyst_or_method", scored["matched_fields"])

        meta["structured_tags"]["human_checked"] = False
        ignored = discover.score_local_paper(
            meta,
            "photoredox",
            "unclassified",
            [],
            rules,
        )
        self.assertEqual(0, ignored["direct_raw_score"])
        self.assertNotIn("catalyst_or_method", ignored["matched_fields"])

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
                    },
                    "human_checked": True,
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
        self.assertTrue(first["base_tags_verified"])
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

    def test_unverified_base_tags_are_hidden_from_project_assessment(self) -> None:
        papers = {
            "P001": {
                "paper_id": "P001",
                "structured_tags": {
                    "value": {"catalyst_or_method": "copper catalysis"},
                    "human_checked": False,
                },
            }
        }
        grouped = [
            {
                "keyword": "gold catalysis",
                "category": "catalyst_or_method",
                "local_results": [
                    {
                        "paper_id": "P001",
                        "score": 0.9,
                        "matched_fields": ["primary_evidence"],
                        "reason": "title evidence",
                    }
                ],
            }
        ]

        discover.attach_project_tag_assessments(
            grouped,
            papers,
            topic="Gold-catalyzed allene synthesis",
            query_plan_source="dashboard_llm",
            taxonomy={"profile": "allene"},
        )

        row = grouped[0]["local_results"][0]
        self.assertFalse(row["base_tags_verified"])
        self.assertEqual("not specified", row["base_tags"]["catalyst_or_method"])
        self.assertEqual(
            ["gold catalysis"],
            row["project_tag_assessment"]["suggested_tags"]["catalyst_or_method"],
        )


if __name__ == "__main__":
    unittest.main()
