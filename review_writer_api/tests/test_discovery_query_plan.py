from __future__ import annotations

import importlib.util
import os
import tempfile
import threading
import time
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
    def test_systematic_23_diene_name_matches_allene_family_only(self) -> None:
        self.assertGreater(
            discover.scientific_family_signal(
                "allenes", "Preparation of (R)-4-cyclohexyl-2,3-butadien-1-ol"
            ),
            0,
        )
        self.assertEqual(
            0,
            discover.scientific_family_signal(
                "allenes", "Stereoselective synthesis of buta-1,3-diene"
            ),
        )

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
        self.assertEqual(2, validated["semantic_query_version"])
        self.assertEqual("topic_core", validated["semantic_queries"][0]["query_id"])
        self.assertNotIn("Please write a review", validated["semantic_queries"][0]["query"])
        self.assertEqual(2, validated["classification_contract_version"])
        self.assertEqual(
            "substrate", validated["classification_contract"]["primary_axis_id"]
        )
        self.assertEqual(
            validated["classification_axes"],
            validated["classification_contract"]["axes"],
        )

    def test_partition_queries_contain_only_discriminators_and_do_not_duplicate_core(self) -> None:
        queries = discover.build_semantic_queries(
            {
                "resolved_concepts": [
                    {
                        "surface": "terminal alkyne allenation",
                        "expanded_name": "terminal alkyne allenation",
                    }
                ],
                "keywords": [
                    {
                        "keyword": "terminal alkyne allenation",
                        "category": "reaction_type",
                        "source": "user",
                    }
                ],
                "group_by": ["catalyst_or_method"],
                "classification_axes": [
                    {
                        "axis_id": "catalyst_or_method",
                        "axis_role": "primary_organization",
                        "partitions": [
                            {
                                "partition_id": "copper",
                                "label": "Copper systems",
                                "aliases": ["CuBr", "CuBr2"],
                                "positive_discriminators": ["copper acetylide"],
                            },
                            {
                                "partition_id": "zinc",
                                "label": "Zinc systems",
                                "aliases": ["ZnI2"],
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(["topic_core", "partition_01", "partition_02"], [item["query_id"] for item in queries])
        for query in queries[1:]:
            self.assertNotIn("terminal alkyne allenation", query["query"].casefold())
            self.assertEqual([query["query"].split(" ; ")], query["lexical_term_groups"])
            self.assertEqual("topic_core", query["admission_query_id"])
            self.assertTrue(query.get("axis_id"))

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
        self.assertEqual("topic_core", validated["semantic_queries"][0]["query_id"])
        self.assertTrue(
            any(
                item["kind"] == "topic_partition"
                and item["source_surface"] == "gold catalysis"
                for item in validated["semantic_queries"]
            )
        )

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

    def test_product_partitions_require_their_discriminating_modifier(self) -> None:
        papers = {
            "P001": {
                "paper_id": "P001",
                "title": {
                    "value": "Copper-Promoted Synthesis of Monosubstituted Allenes from Terminal Alkynes"
                },
                "abstract": {
                    "value": "The reaction affords monosubstituted allenes under mild conditions."
                },
                "structured_tags": {"value": {}},
                "source_paths": {},
            },
            "P002": {
                "paper_id": "P002",
                "title": {
                    "value": "Zinc-Promoted Synthesis of Trisubstituted Allenes"
                },
                "abstract": {
                    "value": "A terminal alkyne route affords trisubstituted allenes."
                },
                "structured_tags": {"value": {}},
                "source_paths": {},
            },
        }

        grouped, _stats = discover.local_search_by_keyword(
            papers,
            [
                {"keyword": "allenes", "category": "product", "keep": True},
                {
                    "keyword": "monosubstituted allenes",
                    "category": "product",
                    "keep": True,
                },
                {
                    "keyword": "trisubstituted allenes",
                    "category": "product",
                    "keep": True,
                },
            ],
            "synthesis of allenes from terminal alkynes",
            EMPTY_RULES,
            anchor_keywords=["allenes"],
        )
        by_keyword = {
            group["keyword"]: [row["paper_id"] for row in group["local_results"]]
            for group in grouped
        }

        self.assertEqual(["P001", "P002"], sorted(by_keyword["allenes"]))
        self.assertEqual(["P001"], by_keyword["monosubstituted allenes"])
        self.assertEqual(["P002"], by_keyword["trisubstituted allenes"])

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

    def test_missing_title_and_abstract_use_bounded_reaction_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "p001.md"
            markdown.write_text(
                "## Working with Hazardous Chemicals\n\nSafety notes.\n\n"
                "## Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol\n\n"
                "Juntao Ye and Shengming Ma\n\n"
                "## Procedure\n\nA propargyl ether, an aldehyde, CuBr, and zinc bromide "
                "were combined to afford the isolated product.\n\n"
                "## References\n\nUnbounded related work.\n",
                encoding="utf-8",
            )
            papers = {
                "P001": {
                    "paper_id": "P001",
                    "title": {
                        "value": "p001",
                        "source": "slug_fallback",
                        "confidence": 0.35,
                    },
                    "abstract": {"value": "", "source": "rule_not_found"},
                    "structured_tags": {"value": {}},
                    "source_paths": {"markdown": str(markdown)},
                }
            }

            grouped, _stats = discover.local_search_by_keyword(
                papers,
                [{"keyword": "allenes", "category": "product", "keep": True}],
                "synthesis of allenes from terminal alkynes",
                EMPTY_RULES,
                anchor_keywords=["allenes"],
            )

        self.assertEqual(["P001"], [row["paper_id"] for row in grouped[0]["local_results"]])
        row = grouped[0]["local_results"][0]
        self.assertEqual(
            "Preparation of (R)-4-Cyclohexyl-2,3-butadien-1-ol", row["title"]
        )
        self.assertEqual(
            "bounded_front_matter_and_reaction_facts", row["admission_mode"]
        )
        self.assertIn("bounded_fulltext_admission", row["matched_fields"])

    def test_missing_metadata_related_work_does_not_bypass_title_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            markdown = Path(temporary) / "p001.md"
            markdown.write_text(
                "## Palladium Cyclization of Dienes\n\n"
                "## Procedure\n\nThe discussion cites preparation of allenes from terminal "
                "alkynes, but the reported reaction produces a carbocycle.\n",
                encoding="utf-8",
            )
            papers = {
                "P001": {
                    "paper_id": "P001",
                    "title": {
                        "value": "p001",
                        "source": "slug_fallback",
                        "confidence": 0.35,
                    },
                    "abstract": {"value": ""},
                    "structured_tags": {"value": {}},
                    "source_paths": {"markdown": str(markdown)},
                }
            }

            grouped, _stats = discover.local_search_by_keyword(
                papers,
                [{"keyword": "allenes", "category": "product", "keep": True}],
                "synthesis of allenes from terminal alkynes",
                EMPTY_RULES,
                anchor_keywords=["allenes"],
            )

        self.assertEqual([], grouped[0]["local_results"])

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
        self.assertEqual("planner_unavailable", plan["planner_notice_code"])
        self.assertNotIn("TimeoutError", plan["planner_notice"])
        self.assertNotIn("offline", plan["planner_notice"])

    def test_auto_planner_stops_discovery_when_credit_is_insufficient(self) -> None:
        failure = discover.GatewayRequestError(
            "余额不足，无法使用智能服务。",
            status_code=402,
            code="INSUFFICIENT_CREDIT",
            details={"required_usd": "0.00321480", "available_usd": "0"},
        )
        with patch.object(discover, "llm_query_plan", side_effect=failure):
            with self.assertRaises(discover.GatewayRequestError) as raised:
                discover.build_auto_query_plan(
                    "photoredox catalysis",
                    [],
                    EMPTY_RULES,
                )
        self.assertEqual("INSUFFICIENT_CREDIT", raised.exception.code)
        self.assertNotIn("0.00321480", str(raised.exception))

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

    def test_provider_group_by_surfaces_are_normalized_without_discarding_plan(self) -> None:
        topic = (
            "Review terminal alkyne allenation organized by reaction type and "
            "catalytic/promoting system, with racemic and enantioselective modes discussed separately."
        )
        plan = discover.deterministic_query_plan(topic, [], EMPTY_RULES)
        plan["group_by"] = [
            "reaction type",
            "catalytic/promoting system",
            "stereochemical mode: racemic versus enantioselective",
        ]

        validated = discover.validate_query_plan(plan, topic)

        self.assertIn("reaction_type", validated["group_by"])
        self.assertIn("catalyst_or_method", validated["group_by"])
        self.assertNotIn(
            "stereochemical mode: racemic versus enantioselective",
            validated["group_by"],
        )

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
            "retrieval_hint_only",
            first["project_tag_assessment"]["application_mode"],
        )
        self.assertEqual("deferred_to_matrix", first["classification_status"])
        self.assertEqual([], first["provisional_screening_tags"])
        self.assertEqual({}, first["screening_classification"])
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

    def test_screening_classification_requires_contribution_and_exact_quote(self) -> None:
        axes = [
            {
                "axis_id": "method",
                "label": "Method family",
                "axis_role": "primary_organization",
                "partitions": [
                    {"partition_id": "alpha", "label": "Alpha method"}
                ],
            }
        ]
        passages = [
            {
                "evidence_key": "P001:screening:abstract:1",
                "content_type": "abstract",
                "section": "Abstract",
                "content": "Prior studies used the Alpha method. We report the Alpha method for the current workflow.",
            }
        ]
        background = discover.normalize_screening_classification(
            paper_id="P001",
            generated={
                "topic_relevance": {"status": "relevant", "confidence": 0.95},
                "assignments": [
                    {
                        "axis_id": "method",
                        "partition_id": "alpha",
                        "relation_to_paper": "background_mention",
                        "confidence": 0.98,
                        "evidence_key": "P001:screening:abstract:1",
                        "support_excerpt": "Prior studies used the Alpha method.",
                    }
                ],
            },
            axes=axes,
            passages=passages,
        )
        self.assertEqual("pending_evidence", background["classification_status"])
        self.assertEqual([], background["provisional_screening_tags"])

        contribution = discover.normalize_screening_classification(
            paper_id="P001",
            generated={
                "topic_relevance": {"status": "relevant", "confidence": 0.95},
                "assignments": [
                    {
                        "axis_id": "method",
                        "partition_id": "alpha",
                        "relation_to_paper": "primary_contribution",
                        "confidence": 0.91,
                        "evidence_key": "P001:screening:abstract:1",
                        "support_excerpt": "This quotation is not in the source.",
                    },
                    {
                        "axis_id": "method",
                        "partition_id": "alpha",
                        "relation_to_paper": "secondary_contribution",
                        "confidence": 0.88,
                        "evidence_key": "P001:screening:abstract:1",
                        "support_excerpt": "We report the Alpha method for the current workflow.",
                    },
                ],
            },
            axes=axes,
            passages=passages,
        )
        self.assertEqual(
            "evidence_backed_screening", contribution["classification_status"]
        )
        self.assertEqual(1, len(contribution["provisional_screening_tags"]))

    def test_classification_axes_are_compact_after_provider_normalization(self) -> None:
        raw_axes = []
        for axis_index in range(5):
            raw_axes.append(
                {
                    "axis_id": f"axis-{axis_index}",
                    "label": f"Axis {axis_index}",
                    "source_surface": f"Axis {axis_index}",
                    "partitions": [
                        {
                            "partition_id": f"partition-{partition_index}",
                            "label": f"Partition {partition_index}",
                            "aliases": [f"alias-{value}" for value in range(10)],
                            "positive_discriminators": [f"positive-{value}" for value in range(10)],
                            "negative_or_ambiguous_signals": [f"ambiguous-{value}" for value in range(10)],
                        }
                        for partition_index in range(12)
                    ],
                }
            )

        axes = discover.normalize_classification_axes(raw_axes, group_by=[], keywords=[])

        self.assertEqual(discover.QUERY_PLAN_MAX_AXES, len(axes))
        self.assertEqual(discover.QUERY_PLAN_MAX_PARTITIONS, len(axes[0]["partitions"]))
        self.assertLessEqual(
            len(axes[0]["partitions"][0]["aliases"]),
            discover.QUERY_PLAN_MAX_ALIASES,
        )

    def test_stereochemical_partitions_are_repaired_to_cross_cutting_axis(self) -> None:
        axes = discover.normalize_classification_axes(
            [
                {
                    "axis_id": "reaction_type",
                    "label": "Reaction type",
                    "source_surface": "organize by reaction type",
                    "source_type": "explicit_topic",
                    "axis_role": "primary_organization",
                    "partitions": [
                        {"partition_id": "ata", "label": "Terminal alkyne allenation"}
                    ],
                },
                {
                    "axis_id": "reaction_type",
                    "label": "Reaction type",
                    "source_surface": "separately discuss racemic and enantioselective ATA",
                    "source_type": "explicit_topic",
                    "axis_role": "required_independent_discussion",
                    "partitions": [
                        {"partition_id": "racemic", "label": "Racemic ATA"},
                        {
                            "partition_id": "eata",
                            "label": "Enantioselective ATA (EATA)",
                        },
                    ],
                }
            ],
            group_by=["reaction_type"],
            keywords=[],
        )

        self.assertEqual("stereochemical_regime", axes[1]["axis_id"])
        self.assertEqual(
            "required_independent_discussion", axes[1]["axis_role"]
        )

    def test_query_plan_cache_is_bound_to_topic_model_and_prompt_inputs(self) -> None:
        topic = "Alpha method synthesis"
        plan = discover.deterministic_query_plan(topic, [], EMPTY_RULES)
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "query-plan.json"
            with patch.dict(
                os.environ,
                {"REVIEW_WRITING_MODEL": "gpt-5.6-luna"},
                clear=False,
            ):
                fingerprint = discover.query_plan_cache_fingerprint(
                    topic=topic,
                    user_keywords=[],
                    taxonomy_profile="general",
                    classification_rules=EMPTY_RULES,
                )
                discover.write_cached_query_plan(
                    cache_path,
                    fingerprint=fingerprint,
                    query_plan=plan,
                )
                cached = discover.load_cached_query_plan(
                    cache_path,
                    fingerprint=fingerprint,
                    topic=topic,
                )
                changed_model_fingerprint = discover.query_plan_cache_fingerprint(
                    topic=topic,
                    user_keywords=[],
                    taxonomy_profile="general",
                    classification_rules=EMPTY_RULES,
                )

            self.assertIsNotNone(cached)
            self.assertEqual(fingerprint, changed_model_fingerprint)
            self.assertIsNone(
                discover.load_cached_query_plan(
                    cache_path,
                    fingerprint="different-fingerprint",
                    topic=topic,
                )
            )

            with patch.dict(
                os.environ,
                {"REVIEW_WRITING_MODEL": "gpt-5.6-sol"},
                clear=False,
            ):
                self.assertNotEqual(
                    fingerprint,
                    discover.query_plan_cache_fingerprint(
                        topic=topic,
                        user_keywords=[],
                        taxonomy_profile="general",
                        classification_rules=EMPTY_RULES,
                    ),
                )

    def test_screening_candidates_run_concurrently_and_reuse_stable_cache(self) -> None:
        papers = {
            f"P{index:03d}": {
                "paper_id": f"P{index:03d}",
                "title": {"value": f"Alpha method paper {index}"},
                "abstract": {"value": "We report the Alpha method for this study."},
                "source_paths": {},
            }
            for index in range(1, 7)
        }
        axes = [
            {
                "axis_id": "method",
                "label": "Method",
                "axis_role": "primary_organization",
                "partitions": [{"partition_id": "alpha", "label": "Alpha method"}],
            }
        ]
        active = 0
        maximum_active = 0
        call_count = 0
        lock = threading.Lock()

        def fake_gateway(*_args, **_kwargs):
            nonlocal active, maximum_active, call_count
            with lock:
                active += 1
                call_count += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            return {
                "topic_relevance": {"status": "relevant", "confidence": 0.9},
                "assignments": [],
                "unresolved_axes": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "screening-cache.json"
            first_progress: list[dict] = []
            with (
                patch.object(discover, "gateway_configured", return_value=True),
                patch.object(discover, "call_gateway_json", side_effect=fake_gateway),
            ):
                first = discover.classify_screening_candidates(
                    papers,
                    list(papers),
                    topic="Alpha methods",
                    classification_axes=axes,
                    taxonomy_profile={},
                    cache_path=cache_path,
                    progress_callback=first_progress.append,
                    max_workers=3,
                )

            self.assertEqual(set(papers), set(first))
            self.assertGreaterEqual(maximum_active, 2)
            self.assertEqual(6, call_count)
            self.assertEqual(6, first_progress[-1]["current"])

            second_progress: list[dict] = []
            with (
                patch.object(discover, "gateway_configured", return_value=True),
                patch.object(discover, "call_gateway_json", side_effect=fake_gateway),
            ):
                second = discover.classify_screening_candidates(
                    papers,
                    list(papers),
                    topic="Alpha methods",
                    classification_axes=axes,
                    taxonomy_profile={},
                    cache_path=cache_path,
                    progress_callback=second_progress.append,
                    max_workers=3,
                )

            self.assertEqual(first, second)
            self.assertEqual(6, call_count)
            self.assertEqual(6, second_progress[-1]["cached"])


if __name__ == "__main__":
    unittest.main()
