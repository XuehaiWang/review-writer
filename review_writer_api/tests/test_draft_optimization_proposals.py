from __future__ import annotations

import unittest
from types import SimpleNamespace
import json
import hashlib
import threading

from review_writer_api.domain_services.drafts import (
    DRAFT_DOCUMENT,
    DRAFT_OPTIMIZATIONS,
    DRAFT_OVERLAYS,
    DRAFT_QUALITY,
    SECTION_EVIDENCE,
    DraftsService,
)
from review_writer_core.draft_quality import full_draft_quality_provenance, QUALITY_INPUT_ARTIFACTS


class DraftOptimizationProposalTests(unittest.TestCase):
    @staticmethod
    def _drafts_service() -> DraftsService:
        service = object.__new__(DraftsService)
        service._write_lock = threading.RLock()
        # These tests isolate proposal transformations. Actual database snapshot
        # checks are covered by test_workflow_input_guards and test_drafts_v1.
        service.validate_task_inputs = lambda _principal, _project, payload: {
            logical_name: str(payload[field] or "")
            for field, logical_name in {
                **QUALITY_INPUT_ARTIFACTS,
                "source_draft_artifact_id": DRAFT_DOCUMENT,
                "source_quality_artifact_id": DRAFT_QUALITY,
            }.items() if field in payload
        }
        return service

    def test_evaluation_payload_reuses_only_exact_full_quality(self) -> None:
        service = self._drafts_service()
        draft_text = (
            "# Review\n\nStable paragraph [1].\n\n"
            "<!-- paragraph_id: S01-p1 -->\n"
        )
        paragraphs = [{"paragraph_id": "S01-p1"}]
        compatibility = {"source_matrix_artifact_id": "matrix-v1"}
        quality = {
            "current": True,
            "source_draft_artifact_id": "draft-v1",
            "score": 90,
            **full_draft_quality_provenance(
                draft_text,
                paragraphs,
                input_artifact_ids=compatibility,
            ),
        }
        payload = {
            "draft_artifact_id": "draft-v1",
            "quality_artifact_id": "quality-v1",
            "revision": 4,
            "first_draft_md": draft_text,
            "paragraphs": paragraphs,
            "freshness": {"upstream_stale": False},
            "quality": quality,
            "optimization_proposals": [],
        }
        service.get = lambda *_args, **_kwargs: payload  # type: ignore[method-assign]
        service.compatibility_payload = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: compatibility
        )

        exact = service.evaluation_payload(
            SimpleNamespace(), "project-1", goal=90
        )
        self.assertTrue(exact["quality_reused"])
        self.assertEqual("steady_state", exact["run_mode"])
        self.assertEqual(quality, exact["baseline_quality"])

        quality["quality_scope"] = "batch_selected_paragraphs"
        migration = service.evaluation_payload(
            SimpleNamespace(), "project-1", goal=90
        )
        self.assertFalse(migration["quality_reused"])
        self.assertEqual("migration", migration["run_mode"])
        self.assertEqual({}, migration["baseline_quality"])

    def test_quality_roots_group_repeated_reference_failures(self) -> None:
        issues = [
            {
                "issue_id": "I-1",
                "paragraph_id": "S01-p1",
                "section_id": "S01",
                "repair_route": "deterministic_reference_rebuild",
                "issue_type": "citation_reference_mapping",
                "auto_repairable": True,
            },
            {
                "issue_id": "I-2",
                "paragraph_id": "S04-p3",
                "section_id": "S04",
                "repair_route": "deterministic_reference_rebuild",
                "issue_type": "citation_reference_mapping",
                "auto_repairable": True,
            },
        ]
        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertEqual(1, len(roots))
        self.assertEqual(["I-1", "I-2"], roots[0]["issue_ids"])
        self.assertEqual(roots[0]["root_cause_id"], issues[0]["root_cause_id"])
        self.assertEqual(1, len(tasks))
        self.assertEqual("queued", tasks[0]["status"])

    def test_different_missing_claims_are_not_hidden_in_one_root(self):
        issues = [{"issue_id": f"I{index}", "paragraph_id": "S1-p1", "repair_route": "targeted_evidence_then_paragraph_rewrite",
                   "claim_ids": [f"claim{index}"], "paper_ids": ["P1"]} for index in range(2)]
        roots, _tasks = DraftsService._quality_root_causes(issues)
        self.assertEqual(2, len(roots))
        self.assertEqual("unchanged", DraftsService._repair_summary({"root_causes": roots}, roots)["repair_status"])

    def test_quality_roots_keep_coverage_expansion_as_user_decision(self) -> None:
        issues = [
            {
                "issue_id": "C-1",
                "repair_route": "manual_online_retrieval_decision",
                "issue_type": "literature_coverage_gap",
                "auto_repairable": False,
            }
        ]
        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertTrue(roots[0]["requires_user_decision"])
        self.assertEqual("requires_user_input", tasks[0]["status"])

    def test_evidence_roots_are_tracked_per_paragraph_not_hidden_by_section(self) -> None:
        issues = [
            {
                "issue_id": "E-1",
                "paragraph_id": "S02-p1",
                "section_id": "S02",
                "repair_route": "targeted_evidence_then_paragraph_rewrite",
                "issue_type": "claim_evidence_gap",
                "auto_repairable": True,
            },
            {
                "issue_id": "E-2",
                "paragraph_id": "S02-p3",
                "section_id": "S02",
                "repair_route": "targeted_evidence_then_paragraph_rewrite",
                "issue_type": "claim_evidence_gap",
                "auto_repairable": True,
            },
        ]

        roots, tasks = DraftsService._quality_root_causes(issues)

        self.assertEqual(2, len(roots))
        self.assertEqual({"S02-p1", "S02-p3"}, {root["scope"] for root in roots})
        self.assertEqual(2, len(tasks))

    def test_candidate_contains_only_reviewable_paragraph_body_changes(self) -> None:
        current = (
            "# Current title\n\n"
            "Original first paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "## Current heading\n\n"
            "Original second paragraph [2].\n\n"
            "<!-- paragraph_id: sec2-p1 -->\n"
        )
        model = (
            "# Unreviewed changed title\n\n"
            "Improved first paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "## Unreviewed changed heading\n\n"
            "Original second paragraph [2].\n\n"
            "<!-- paragraph_id: sec2-p1 -->\n"
        )

        candidate, changes = DraftsService._optimization_candidate(current, model)

        self.assertIn("# Current title", candidate)
        self.assertIn("## Current heading", candidate)
        self.assertNotIn("Unreviewed changed", candidate)
        self.assertIn("Improved first paragraph [1].", candidate)
        self.assertEqual(
            [
                {
                    "paragraph_id": "sec1-p1",
                    "original_text": "Original first paragraph [1].",
                    "candidate_text": "Improved first paragraph [1].",
                }
            ],
            changes,
        )

    def test_publish_optimization_creates_a_proposal_without_publishing_draft(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        quality = SimpleNamespace(id="quality-v1")
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]

        def read_json(_principal, _project_id, logical_name, **_kwargs):
            if logical_name == DRAFT_QUALITY:
                return {"score": 70.0}, quality
            if logical_name == DRAFT_OPTIMIZATIONS:
                return {}, None
            raise AssertionError(logical_name)

        service._read_json = read_json  # type: ignore[method-assign]
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            return (
                {DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-artifact")},
                SimpleNamespace(revision=8),
            )

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.publish_optimization(
            SimpleNamespace(),
            "project-1",
            {
                "source_draft_artifact_id": "draft-v1",
                "expected_revision": 7,
            },
            {
                "draft_text": current_text.replace(
                    "Original paragraph [1].", "Improved paragraph [1]."
                ),
                "score": 88.0,
                "feedback_status": {"rewrite_accepted": 1},
            },
        )

        files = captured["files"]
        self.assertEqual({DRAFT_OPTIMIZATIONS}, set(files))
        proposal_document = json.loads(files[DRAFT_OPTIMIZATIONS][0])
        proposal = next(iter(proposal_document["entries"].values()))
        self.assertEqual("pending", proposal["status"])
        self.assertEqual("Original paragraph [1].", proposal["changes"][0]["original_text"])
        self.assertEqual("Improved paragraph [1].", proposal["changes"][0]["candidate_text"])
        self.assertTrue(result["proposal_created"])
        self.assertFalse(result["draft_changed"])

    def test_noop_optimization_still_publishes_fresh_full_draft_quality(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nStable paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: ({}, None)  # type: ignore[method-assign]
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["quality"] = json.loads(files[DRAFT_QUALITY][0])
            return (
                {DRAFT_QUALITY: SimpleNamespace(id="quality-v2")},
                SimpleNamespace(revision=8),
            )

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.publish_optimization(
            SimpleNamespace(),
            "project-1",
            {
                "source_draft_artifact_id": "draft-v1",
                "expected_revision": 7,
            },
            {
                "draft_text": current_text,
                "score": 82.0,
                "feedback_status": {"phase": "iteration_limit"},
            },
        )

        self.assertEqual({DRAFT_QUALITY}, set(captured["files"]))
        self.assertEqual("full_draft", captured["quality"]["quality_scope"])
        self.assertTrue(captured["quality"]["evaluation_input_sha256"])
        self.assertEqual("completed", captured["quality"]["feedback_status"]["phase"])
        self.assertEqual("quality-v2", result["quality_artifact_id"])
        self.assertFalse(result["proposal_created"])

    def test_pure_support_and_prose_proposal_publish_atomically_on_new_quality(self):
        service = self._drafts_service()
        text = '# Review\n\nOriginal paragraph [1].\n\n<!-- paragraph_id: sec1-p1 -->\n'
        service._read_text = lambda *_a, **_k: (text, SimpleNamespace(id='draft-v1', metadata={}))
        service._read_json = lambda _u, _p, name, **_k: (
            ({'score': 70}, SimpleNamespace(id='quality-v1')) if name == DRAFT_QUALITY else ({}, None))
        captured = {}

        def publish(_u, _p, files, **kwargs):
            published = {}
            for name, (content, _suffix) in files.items():
                captured[name] = json.loads(content(published) if callable(content) else content)
                published[name] = SimpleNamespace(id=f'new-{name}')
            captured['guard'] = kwargs
            return published, SimpleNamespace(revision=8)

        service._publish_files = publish
        entry = {'paragraph_id': 'sec1-p1', 'source_check_status': 'verified',
                 'paragraph_text_hash': hashlib.sha256(b'Original paragraph [1].').hexdigest(),
                 'targeted_source_recheck': {'status': 'supported_without_prose_change'}, 'papers': []}
        result = service.publish_optimization(SimpleNamespace(), 'project-1',
            {'source_draft_artifact_id': 'draft-v1', 'source_quality_artifact_id': 'quality-v1', 'expected_revision': 7},
            {'draft_text': text.replace('Original', 'Improved'), 'score': 88,
             'review_source_quality': {'total_score': 80, 'paragraph_scores': [], 'source_check': {'entries': [entry]}}})
        self.assertEqual({DRAFT_QUALITY, DRAFT_OPTIMIZATIONS, 'guard'}, set(captured))
        proposal = next(iter(captured[DRAFT_OPTIMIZATIONS]['entries'].values()))
        self.assertEqual(f'new-{DRAFT_QUALITY}', proposal['source_quality_artifact_id'])
        self.assertEqual('quality-v1', captured['guard']['expected_current_artifacts'][DRAFT_QUALITY])
        self.assertEqual(7, captured['guard']['expected_revision'])
        self.assertEqual('verified', captured[DRAFT_QUALITY]['source_check']['entries'][0]['source_check_status'])
        self.assertFalse(result['draft_changed'])
        self.assertTrue(result['proposal_created'])

    def test_accepting_proposal_publishes_draft_quality_and_overlays_together(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal paragraph [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "candidate_draft_text": current_text.replace(
                "Original paragraph [1].", "Improved paragraph [1]."
            ),
            "candidate_quality": {"score": 88.0},
            "source_overlays": {"entries": {}, "legacy": "keep"},
            "rewrite_overlays": {"argument_revisions": {"C": {
                "paragraph_ids": ["sec1-p1"], "group_id": "joint", "proposition": "Bounded conclusion"}}},
            "changes": [
                {
                    "paragraph_id": "sec1-p1",
                    "original_text": "Original paragraph [1].",
                    "candidate_text": "Improved paragraph [1].",
                }
            ],
            "candidate_score": 88.0,
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            quality_content = files[DRAFT_QUALITY][0](published)
            captured["quality"] = json.loads(quality_content)
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
        )

        self.assertEqual(
            {DRAFT_OPTIMIZATIONS, DRAFT_DOCUMENT, DRAFT_QUALITY, DRAFT_OVERLAYS},
            set(captured["files"]),
        )
        self.assertEqual("draft-v2", captured["quality"]["source_draft_artifact_id"])
        self.assertEqual(
            "batch_selected_paragraphs", captured["quality"]["quality_scope"]
        )
        self.assertTrue(captured["quality"]["requires_full_draft_refresh"])
        self.assertEqual("draft-v2", result["draft_artifact_id"])
        self.assertTrue(captured["kwargs"]["invalidate_final"])
        saved_overlays = json.loads(captured["files"][DRAFT_OVERLAYS][0])
        self.assertEqual("Bounded conclusion", saved_overlays["argument_revisions"]["C"]["proposition"])
        self.assertEqual("keep", saved_overlays["legacy"])

    def test_accepting_selected_paragraphs_keeps_unselected_text_and_updates_score(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal first [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "Original second [2].\n\n"
            "<!-- paragraph_id: sec1-p2 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")

        def evaluation(paragraph_id: str, score: float) -> dict[str, object]:
            return {
                "evaluation_scope": "single_paragraph",
                "paragraph_id": paragraph_id,
                "paragraph_score": {
                    "paragraph_id": paragraph_id,
                    "score": score,
                    "severity": "pass",
                    "route": "none",
                    "failed_dimensions": [],
                    "diagnosis": "Improved.",
                },
                "local_hard_gate_failures": [],
                "local_preflight": {"issues": [], "hard_regressions": []},
            }

        changes = [
            {
                "paragraph_id": "sec1-p1",
                "original_text": "Original first [1].",
                "candidate_text": "Improved first [1].",
                "candidate_evaluation": evaluation("sec1-p1", 90),
            },
            {
                "paragraph_id": "sec1-p2",
                "original_text": "Original second [2].",
                "candidate_text": "Improved second [2].",
                "candidate_evaluation": evaluation("sec1-p2", 95),
            },
        ]
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "source_quality": {
                # Feedback-loop rubric payloads use total_score without score.
                "total_score": 70.0,
                "paragraph_scores": [
                    {"paragraph_id": "sec1-p1", "score": 60},
                    {"paragraph_id": "sec1-p2", "score": 70},
                ],
                "issues": [],
                "hard_gate_failures": [],
            },
            "source_overlays": {"entries": {}},
            "changes": changes,
            "candidate_score": 97.5,
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            captured["draft"] = files[DRAFT_DOCUMENT][0].decode("utf-8")
            captured["quality"] = json.loads(files[DRAFT_QUALITY][0](published))
            captured["overlays"] = json.loads(files[DRAFT_OVERLAYS][0])
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]

        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
            selected_paragraph_ids=["sec1-p1"],
        )

        self.assertIn("Improved first [1].", captured["draft"])
        self.assertIn("Original second [2].", captured["draft"])
        self.assertNotIn("Improved second [2].", captured["draft"])
        self.assertEqual(85.0, captured["quality"]["score"])
        self.assertEqual(["sec1-p1"], captured["quality"]["selected_paragraph_ids"])
        self.assertEqual(["sec1-p1"], list(captured["overlays"]["entries"]))
        self.assertEqual(["sec1-p1"], result["selected_paragraph_ids"])

    def test_accepting_all_scored_paragraphs_uses_incremental_scores(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal first [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n\n"
            "Original second [2].\n\n"
            "<!-- paragraph_id: sec1-p2 -->\n"
        )
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")

        def evaluation(paragraph_id: str, score: float) -> dict[str, object]:
            return {
                "evaluation_scope": "single_paragraph",
                "paragraph_id": paragraph_id,
                "paragraph_score": {
                    "paragraph_id": paragraph_id,
                    "score": score,
                    "severity": "pass",
                    "route": "pass",
                    "failed_dimensions": [],
                    "diagnosis": "Improved.",
                },
                "local_hard_gate_failures": [],
                "local_preflight": {"issues": [], "hard_regressions": []},
            }

        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "source_quality": {
                "score": 70.0,
                "total_score": 70.0,
                "paragraph_scores": [
                    {"paragraph_id": "sec1-p1", "score": 60},
                    {"paragraph_id": "sec1-p2", "score": 70},
                ],
                "issues": [],
                "hard_gate_failures": [],
            },
            # The loop-level snapshot can be lower than the individually
            # accepted candidates and must not overwrite their displayed score.
            "candidate_quality": {"score": 55.0},
            "source_overlays": {"entries": {}},
            "changes": [
                {
                    "paragraph_id": "sec1-p1",
                    "original_text": "Original first [1].",
                    "candidate_text": "Improved first [1].",
                    "candidate_evaluation": evaluation("sec1-p1", 90),
                },
                {
                    "paragraph_id": "sec1-p2",
                    "original_text": "Original second [2].",
                    "candidate_text": "Improved second [2].",
                    "candidate_evaluation": evaluation("sec1-p2", 95),
                },
            ],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}}, store_artifact
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **_kwargs):
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            captured["quality"] = json.loads(files[DRAFT_QUALITY][0](published))
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
            selected_paragraph_ids=["sec1-p1", "sec1-p2"],
        )

        self.assertEqual(97.5, captured["quality"]["score"])
        self.assertEqual(
            "batch_selected_paragraphs", captured["quality"]["quality_scope"]
        )
        self.assertEqual(
            ["sec1-p1", "sec1-p2"], captured["quality"]["selected_paragraph_ids"]
        )

    def test_discarding_proposal_keeps_the_current_draft(self) -> None:
        service = self._drafts_service()
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "candidate_draft_text": "Improved paragraph.",
            "changes": [{"paragraph_id": "sec1-p1"}],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: ("Original paragraph.\n", current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}},
            store_artifact,
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["files"] = files
            captured["kwargs"] = kwargs
            return (
                {DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2")},
                SimpleNamespace(revision=9),
            )

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="reject",
            revision=8,
        )

        self.assertEqual({DRAFT_OPTIMIZATIONS}, set(captured["files"]))
        self.assertEqual("draft-v1", result["draft_artifact_id"])
        self.assertFalse(captured["kwargs"]["invalidate_final"])

    def test_all_revalidated_improvements_can_apply_automatically(self) -> None:
        service = self._drafts_service()
        current = SimpleNamespace(id="draft-v1", metadata={})
        current_text = "Original.\n"
        candidate_text = "Improved.\n"
        source_quality = {
            "source_draft_artifact_id": "draft-v1",
            "score": 70,
            "paragraph_scores": [{
                "paragraph_id": "sec1-p2", "source_check_status": "verified",
                "source_evidence_refs": ["source-2"], "unsupported_claims": [],
            }],
            **full_draft_quality_provenance(current_text, []),
        }
        candidate_quality = {
            "score": 91,
            **full_draft_quality_provenance(candidate_text, []),
        }
        change = {
            "paragraph_id": "sec1-p1",
            "source_paragraph_score": 70,
            "candidate_paragraph_score": 91,
            "candidate_evaluation": {
                "evaluation_scope": "single_paragraph",
                "paragraph_score": {"route": "pass"},
                "local_hard_gate_failures": [],
                "local_preflight": {"hard_regressions": []},
            },
        }
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {
                "entries": {
                    "proposal-1": {
                        "status": "pending",
                        "changes": [change],
                        "candidate_draft_text": candidate_text,
                        "source_quality": source_quality,
                        "candidate_quality": candidate_quality,
                        "claim_dispositions": {"historical-claim": {
                            "paragraph_id": "sec1-p2",
                            "disposition": "downgraded_due_to_insufficient_evidence",
                        }},
                    }
                }
            },
            SimpleNamespace(id="proposal-store-v1"),
        )
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service.decide_optimization_proposal = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "proposal_id": "proposal-1",
            "draft_artifact_id": "draft-v2",
            "revision": 9,
        }

        result = service.auto_apply_optimization_proposal(
            SimpleNamespace(), "project-1", "proposal-1", revision=8
        )

        self.assertTrue(result["auto_applied"])
        self.assertTrue(result["draft_changed"])
        self.assertFalse(result["proposal_created"])

        for override in [
            {"source_check_status": "partially_supported"},
            {"source_evidence_refs": []},
            {"unsupported_claims": ["Still unsupported"]},
        ]:
            with self.subTest(override=override):
                original = dict(source_quality["paragraph_scores"][0])
                source_quality["paragraph_scores"][0].update(override)
                result = service.auto_apply_optimization_proposal(
                    SimpleNamespace(), "project-1", "proposal-1", revision=8
                )
                self.assertFalse(result["auto_applied"])
                self.assertTrue(any(
                    "unsupported_claim_was_not_downgraded_in_text" in item["reasons"]
                    for item in result["manual_review_reasons"]
                ))
                source_quality["paragraph_scores"][0] = original

        source_quality["evaluation_input_sha256"] = "stale-baseline"
        result = service.auto_apply_optimization_proposal(
            SimpleNamespace(), "project-1", "proposal-1", revision=8
        )
        self.assertFalse(result["auto_applied"])
        self.assertTrue(any(
            "full_draft_baseline_not_exact" in item["reasons"]
            for item in result["manual_review_reasons"]
        ))

    def test_accuracy_improvement_can_auto_apply_when_numeric_score_is_lower(self) -> None:
        service = self._drafts_service()
        current = SimpleNamespace(id="draft-v1", metadata={})
        current_text = "Original.\n"
        candidate_text = "More accurate.\n"
        source_quality = {
            "source_draft_artifact_id": "draft-v1",
            "score": 91,
            **full_draft_quality_provenance(current_text, []),
        }
        candidate_quality = {
            "score": 90.5,
            **full_draft_quality_provenance(candidate_text, []),
        }
        change = {
            "paragraph_id": "sec1-p1",
            "source_paragraph_score": 91,
            "candidate_paragraph_score": 90.5,
            "accuracy_improved": True,
            "candidate_evaluation": {
                "evaluation_scope": "single_paragraph",
                "paragraph_score": {"route": "pass"},
                "local_hard_gate_failures": [],
                "local_preflight": {"hard_regressions": []},
            },
        }
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {
                "entries": {
                    "proposal-1": {
                        "status": "pending",
                        "changes": [change],
                        "candidate_draft_text": candidate_text,
                        "source_quality": source_quality,
                        "candidate_quality": candidate_quality,
                    }
                }
            },
            SimpleNamespace(id="proposal-store-v1"),
        )
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service.decide_optimization_proposal = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "proposal_id": "proposal-1",
            "draft_artifact_id": "draft-v2",
            "draft_changed": True,
            "revision": 9,
        }

        result = service.auto_apply_optimization_proposal(
            SimpleNamespace(), "project-1", "proposal-1", revision=8
        )

        self.assertTrue(result["auto_applied"])

    def test_reference_only_proposal_can_publish_without_paragraph_changes(self) -> None:
        service = self._drafts_service()
        current_text = "# Review\n\nText [3].\n"
        repaired_text = "# Review\n\nText [1].\n\n## References\n[1] Correct.\n"
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "deterministic_base_draft_text": repaired_text,
            "reference_repair": {"changed": True, "status": "applied"},
            "evidence_repair": {"added_evidence_count": 0},
            "candidate_quality": {"score": 90, "hard_gate_failures": []},
            "changes": [],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}}, store_artifact
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["draft"] = files[DRAFT_DOCUMENT][0].decode("utf-8")
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                DRAFT_DOCUMENT: SimpleNamespace(id="draft-v2"),
            }
            captured["quality"] = json.loads(files[DRAFT_QUALITY][0](published))
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
            selected_paragraph_ids=[],
        )

        self.assertEqual(repaired_text, captured["draft"])
        self.assertEqual(
            "batch_selected_paragraphs", captured["quality"]["quality_scope"]
        )
        self.assertTrue(captured["quality"]["requires_full_draft_refresh"])
        self.assertTrue(result["draft_changed"])

    def test_accepting_repaired_evidence_publishes_it_before_the_new_draft(self) -> None:
        service = self._drafts_service()
        current_text = (
            "# Review\n\nOriginal [1].\n\n"
            "<!-- paragraph_id: sec1-p1 -->\n"
        )
        current = SimpleNamespace(
            id="draft-v1",
            metadata={"source_section_evidence_artifact_id": "evidence-v1"},
        )
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "source_section_evidence_artifact_id": "evidence-v1",
            "deterministic_base_draft_text": current_text,
            "candidate_evidence_package": {"schema_version": 2, "sections": []},
            "evidence_repair": {"added_evidence_count": 1},
            "reference_repair": {"changed": False},
            "candidate_quality": {"score": 90, "hard_gate_failures": []},
            "changes": [],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}}, store_artifact
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            captured["file_order"] = list(files)
            captured["expected"] = kwargs["expected_current_artifacts"]
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2"),
                SECTION_EVIDENCE: SimpleNamespace(id="evidence-v2"),
            }
            captured["draft_metadata"] = kwargs["metadata_builder"](
                DRAFT_DOCUMENT, published
            )
            published[DRAFT_DOCUMENT] = SimpleNamespace(id="draft-v2")
            published[DRAFT_QUALITY] = SimpleNamespace(id="quality-v2")
            published[DRAFT_OVERLAYS] = SimpleNamespace(id="overlays-v2")
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
        )

        self.assertLess(
            captured["file_order"].index(SECTION_EVIDENCE),
            captured["file_order"].index(DRAFT_DOCUMENT),
        )
        self.assertEqual("evidence-v1", captured["expected"][SECTION_EVIDENCE])
        self.assertEqual(
            "evidence-v2",
            captured["draft_metadata"]["source_section_evidence_artifact_id"],
        )
        self.assertEqual("evidence-v2", result["section_evidence_artifact_id"])

    def test_targeted_source_passage_repairs_section_evidence_summary(self) -> None:
        repaired, summary, dispositions = DraftsService._repair_evidence_package(
            {
                "matrix": {"rows": [{"paper_id": "P001"}]},
                "section_index": {
                    "sections": [
                        {
                            "section_id": "S01",
                            "paragraphs": [
                                {
                                    "paragraph_id": "S01-p1",
                                    "cited_paper_ids": ["P001"],
                                    "claim_realizations": [
                                        {"claim_id": "C01", "question_id": "scope"}
                                    ],
                                }
                            ],
                        }
                    ]
                },
                "section_evidence": {
                    "schema_version": 2,
                    "evidence_registry": [],
                    "sections": [
                        {
                            "section_id": "S01",
                            "status": "insufficient_evidence",
                            "hits": [],
                            "primary_paper_states": [
                                {
                                    "paper_id": "P001",
                                    "status": "unresolved",
                                    "diagnostic": "query_miss",
                                }
                            ],
                            "query_plans": [
                                {
                                    "question_id": "scope",
                                    "status": "insufficient",
                                    "coverage_policy": "evidence_bearing",
                                    "matched_papers": [],
                                    "matched_primary_papers": [],
                                }
                            ],
                            "corpus_gap_questions": ["scope"],
                        }
                    ],
                },
            },
            {
                "paragraph_scores": [
                    {
                        "paragraph_id": "S01-p1",
                        "source_check_status": "verified",
                        "source_evidence_refs": ["P001:p2:b3"],
                    }
                ],
                "source_check": {
                    "entries": [
                        {
                            "paragraph_id": "S01-p1",
                            "papers": [
                                {
                                    "paper_id": "P001",
                                    "passages": [
                                        {
                                            "ref": "P001:p2:b3",
                                            "page": 2,
                                            "text": "Direct source passage.",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

        section = repaired["sections"][0]
        hit = section["hits"][0]
        self.assertEqual(1, summary["added_evidence_count"])
        self.assertEqual({}, dispositions)
        self.assertEqual("P001:p2:b3", hit["chunk_id"])
        self.assertTrue(hit["claim_eligible"])
        self.assertEqual("ready", section["status"])
        self.assertEqual([], section["unresolved_primary_papers"])
        self.assertEqual("sufficient", section["query_plans"][0]["status"])
        self.assertEqual(
            ["P001"], section["query_plans"][0]["expected_primary_papers"]
        )
        self.assertEqual([], section["corpus_gap_questions"])

    def test_validated_targeted_binding_is_promoted_to_same_paper_matrix(self) -> None:
        job_payload = {
            "matrix": {
                "rows": [
                    {
                        "paper_id": "P001",
                        "title": "Study",
                        "scientific_facts": [],
                        "topic_classifications": [{"axis_id": "substrate"}],
                    }
                ]
            },
            "section_index": {
                "sections": [
                    {
                        "section_id": "S01",
                        "paragraphs": [
                            {
                                "paragraph_id": "S01-p1",
                                "cited_paper_ids": ["P001"],
                                "claim_realizations": [
                                    {"claim_id": "C01", "question_id": "scope"}
                                ],
                            }
                        ],
                    }
                ]
            },
            "section_evidence": {
                "schema_version": 2,
                "evidence_registry": [],
                "sections": [
                    {"section_id": "S01", "hits": [], "query_plans": []}
                ],
            },
        }
        built = {
            "paragraph_scores": [
                {
                    "paragraph_id": "S01-p1",
                    "source_check_status": "verified",
                    "source_evidence_refs": ["P001:p2:b3"],
                    "claim_fact_bindings": [
                        {
                            "paper_id": "P001",
                            "source_ref": "P001:p2:b3",
                            "support_excerpt": "The reaction afforded 3aa in 82% yield.",
                            "fact_type": "outcome",
                            "subject": "the reaction",
                            "predicate": "afforded",
                            "value": "3aa in 82% yield",
                            "normalized_value": "82%",
                            "qualifiers": {},
                            "confidence": 0.96,
                        }
                    ],
                }
            ],
            "source_check": {
                "entries": [
                    {
                        "paragraph_id": "S01-p1",
                        "papers": [
                            {
                                "paper_id": "P001",
                                "passages": [
                                    {
                                        "ref": "P001:p2:b3",
                                        "page": 2,
                                        "text": "The reaction afforded 3aa in 82% yield.",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        }

        _evidence, repair, _dispositions = DraftsService._repair_evidence_package(
            job_payload, built
        )
        candidate, applied = DraftsService._matrix_with_promoted_facts(
            job_payload["matrix"], repair
        )

        self.assertEqual(1, repair["promoted_fact_count"])
        self.assertEqual(1, len(applied))
        self.assertEqual("P001", applied[0]["paper_id"])
        facts = candidate["rows"][0]["scientific_facts"]
        self.assertEqual("P001", facts[0]["paper_id"])
        self.assertEqual("claim_targeted_fact", facts[0]["field_id"])
        self.assertEqual(
            [{"axis_id": "substrate"}],
            candidate["rows"][0]["topic_classifications"],
        )
        self.assertTrue(applied[0]["fact_id"].startswith("MF-"))

    def test_accepting_fact_repair_publishes_matrix_before_evidence_and_draft(self) -> None:
        service = self._drafts_service()
        current_text = "# Draft\n\nParagraph.\n\n<!-- paragraph_id: S01-p1 -->\n"
        current = SimpleNamespace(id="draft-v1", metadata={})
        store_artifact = SimpleNamespace(id="proposal-store-v1")
        proposal = {
            "proposal_id": "proposal-1",
            "source_draft_artifact_id": "draft-v1",
            "source_matrix_artifact_id": "matrix-v1",
            "source_section_evidence_artifact_id": "evidence-v1",
            "candidate_matrix": {"rows": [{"paper_id": "P001"}]},
            "candidate_evidence_package": {"schema_version": 2, "sections": []},
            "evidence_repair": {
                "added_evidence_count": 1,
                "matrix_fact_promotion_count": 1,
            },
            "reference_repair": {"changed": False},
            "candidate_quality": {"score": 90, "hard_gate_failures": []},
            "candidate_draft_text": current_text,
            "deterministic_base_draft_text": current_text,
            "changes": [],
            "status": "pending",
        }
        service._read_text = lambda *_args, **_kwargs: (current_text, current)  # type: ignore[method-assign]
        service._read_json = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            {"entries": {"proposal-1": proposal}}, store_artifact
        )
        captured: dict[str, object] = {}

        def publish_files(_principal, _project_id, files, **kwargs):
            order = list(files)
            captured["order"] = order
            captured["expected"] = kwargs["expected_current_artifacts"]
            published = {
                DRAFT_OPTIMIZATIONS: SimpleNamespace(id="proposal-store-v2")
            }
            for logical_name in order:
                content, _kind = files[logical_name]
                if callable(content):
                    content(published)
                published[logical_name] = SimpleNamespace(
                    id={
                        "matrix/literature_matrix.json": "matrix-v2",
                        SECTION_EVIDENCE: "evidence-v2",
                        DRAFT_DOCUMENT: "draft-v2",
                        DRAFT_QUALITY: "quality-v2",
                        DRAFT_OVERLAYS: "overlays-v2",
                    }.get(logical_name, "proposal-store-v2")
                )
            captured["draft_metadata"] = kwargs["metadata_builder"](
                DRAFT_DOCUMENT, published
            )
            return published, SimpleNamespace(revision=9)

        service._publish_files = publish_files  # type: ignore[method-assign]
        result = service.decide_optimization_proposal(
            SimpleNamespace(),
            "project-1",
            "proposal-1",
            decision="accept",
            revision=8,
        )

        order = captured["order"]
        self.assertLess(order.index("matrix/literature_matrix.json"), order.index(SECTION_EVIDENCE))
        self.assertLess(order.index(SECTION_EVIDENCE), order.index(DRAFT_DOCUMENT))
        self.assertEqual(
            "matrix-v1", captured["expected"]["matrix/literature_matrix.json"]
        )
        self.assertEqual(
            "matrix-v2", captured["draft_metadata"]["source_matrix_artifact_id"]
        )
        self.assertEqual("matrix-v2", result["matrix_artifact_id"])

if __name__ == "__main__":
    unittest.main()
