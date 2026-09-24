"""No provider billing: exercise translation contracts with deterministic fixtures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_writer_api.tests.test_discovery_query_plan import discover, EMPTY_RULES


class DiscoveryTranslationTests(unittest.TestCase):
    def setUp(self):
        discover.configure_discovery_normalization(None)

    def test_chinese_request_retains_original_and_years(self):
        topic = "2020-2024年轴手性联烯的不对称合成"
        with patch.object(discover, "resolve_query_ambiguities", return_value={
            "search_topic": "Asymmetric synthesis of axially chiral allenes from 2020 to 2024",
            "keywords": ["copper catalysis", "Pd"],
        }) as provider:
            plan = discover.build_auto_query_plan(topic, ["铜催化", "Pd"], EMPTY_RULES)
        provider.assert_called_once_with(topic, ["铜催化", "Pd"], translation=True)
        self.assertEqual(topic, plan["topic"])
        self.assertEqual(topic, plan["organization_intent"])
        self.assertEqual(discover.parse_topic_intent(topic)["filters"], plan["filters"])
        self.assertEqual(["copper catalysis", "Pd"], plan["search_keywords"])
        self.assertTrue(plan["semantic_queries"])
        self.assertFalse(any("铜" in item["query"] for item in plan["semantic_queries"]))

    def test_english_input_does_not_require_translation(self):
        with patch.object(discover, "resolve_query_ambiguities") as provider:
            plan = discover.build_auto_query_plan("graph neural networks", [], EMPTY_RULES)
        provider.assert_not_called()
        self.assertNotIn("search_topic", plan)

    def test_bad_translation_stops_instead_of_low_recall_fallback(self):
        for response in ({"search_topic": "仍为中文", "keywords": []},
                         {"search_topic": "Allene synthesis", "keywords": ["extra"]},
                         {"search_topic": "", "keywords": []}):
            with self.subTest(response=response), patch.object(discover, "resolve_query_ambiguities", return_value=response):
                with self.assertRaisesRegex(discover.QueryPlanError, "QUERY_TRANSLATION_FAILED"):
                    discover.build_auto_query_plan("联烯合成", [], EMPTY_RULES)

    def test_provider_failure_and_credit(self):
        for error, expected in ((RuntimeError("timeout"), "QUERY_TRANSLATION_FAILED"),
                                (RuntimeError("INSUFFICIENT_CREDIT"), "INSUFFICIENT_CREDIT")):
            with patch.object(discover, "resolve_query_ambiguities", side_effect=error):
                with self.assertRaisesRegex(Exception, expected):
                    discover.build_auto_query_plan("联烯合成", [], EMPTY_RULES)

    def test_translation_cannot_add_year_filters(self):
        with patch.object(discover, "resolve_query_ambiguities", return_value={
            "search_topic": "Allene synthesis since 2020", "keywords": [],
        }):
            with self.assertRaisesRegex(discover.QueryPlanError, "QUERY_TRANSLATION_FAILED"):
                discover.build_auto_query_plan("联烯合成", [], EMPTY_RULES)

    def test_cache_preserves_translation_without_provider_call(self):
        with patch.object(discover, "resolve_query_ambiguities", return_value={
            "search_topic": "Allene synthesis", "keywords": [],
        }):
            plan = discover.build_auto_query_plan("联烯合成", [], EMPTY_RULES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            discover.write_cached_query_plan(path, fingerprint="sample", query_plan=plan)
            with patch.object(discover, "resolve_query_ambiguities") as provider:
                cached = discover.load_cached_query_plan(path, fingerprint="sample", topic="联烯合成")
            provider.assert_not_called()
            self.assertEqual(plan["search_topic"], cached["search_topic"])
            self.assertIsNone(discover.load_cached_query_plan(path, fingerprint="changed", topic="联烯合成"))

    def test_mixed_input_does_not_rewrite_english_keyword(self):
        with patch.object(discover, "resolve_query_ambiguities", return_value={
            "search_topic": "Allene synthesis", "keywords": ["Cu"],
        }):
            with self.assertRaisesRegex(discover.QueryPlanError, "QUERY_TRANSLATION_FAILED"):
                discover.build_auto_query_plan("联烯合成", ["Pd"], EMPTY_RULES)

    def test_run_uses_english_online_and_both_languages_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def local_search(papers, keywords, topic, *args, **kwargs):
                return ([{"keyword": item["keyword"], "category": item["category"],
                          "keep": True, "local_results": []} for item in keywords],
                        {"before_filter": 0, "after_filter": 0})
            def online_search(request, **kwargs):
                kwargs["status_callback"]("crossref", "completed", 0, "")
                return {"completion_state": "complete", "source_statuses": {"crossref": {"status": "completed", "count": 0}}, "candidates": []}
            with (
                patch.object(discover.sys, "argv", ["discover.py", "--review-root", str(root),
                    "--project-id", "sample", "--topic", "联烯合成", "--keywords", "铜催化",
                    "--output-project-dir", str(root / "staging"), "--auto-query-plan",
                    "--taxonomy-profile", "general_academic", "--web-search", "--web-delay", "0"]),
                patch.object(discover, "_load_dotenv_if_present"),
                patch.object(discover, "load_metadata", return_value={}),
                patch.object(discover, "resolve_query_ambiguities", return_value={
                    "search_topic": "Allene synthesis", "keywords": ["copper catalysis"]}),
                patch.object(discover, "local_search_by_keyword", side_effect=local_search) as local,
                patch.object(discover, "search_paper_sources", side_effect=online_search) as online,
            ):
                self.assertEqual(0, discover.run(discover.parse_args()))
            self.assertEqual(["Allene synthesis", "联烯合成"], [call.args[2] for call in local.call_args_list])
            self.assertTrue(online.called)
            for call in online.call_args_list:
                self.assertNotRegex(call.args[0].query, r"[\u3400-\u9fff]")
                self.assertNotRegex(call.args[0].topic, r"[\u3400-\u9fff]")
