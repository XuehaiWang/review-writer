from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
import uuid
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, Project, User
from review_writer_api.errors import WorkflowConflict, WorkflowValidationError
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import (
    LibraryArtifact,
    LibraryDocumentChunk,
    LibraryDocumentIndex,
    LibraryPaper,
)
from review_writer_api.domain_services.planning import BLUEPRINT_LOGICAL_NAME


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


class SectionsV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.root = root
        database_url = f"sqlite+pysqlite:///{(root / 'sections.sqlite3').as_posix()}"
        self.engine = create_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions.begin() as session:
            first = User(email="first@example.com", display_name="First", password_hash="hash")
            second = User(email="second@example.com", display_name="Second", password_hash="hash")
            session.add_all([first, second])
            session.flush()
            project = Project(user_id=first.id, slug="sections", topic="Copper allenation")
            other = Project(user_id=second.id, slug="other", topic="Hidden")
            session.add_all([project, other])
            session.flush()
            mineru_artifact_id = uuid.uuid4()
            mineru_version = (
                root
                / "users"
                / str(first.id)
                / "review-library"
                / ".artifacts"
                / "P001"
                / "a1b"
                / "extracted"
            )
            mineru_version.mkdir(parents=True)
            self.mineru_source = mineru_version / "scheme.png"
            Image.new("RGB", (16, 8), "white").save(self.mineru_source)
            content_list = mineru_version / "content_list.json"
            content_list.write_text("[]\n", encoding="utf-8")
            papers = []
            for index, paper_id in enumerate(("P001", "P002", "P003"), start=1):
                metadata = {
                    "paper_id": paper_id,
                    "title": {"value": f"Paper {index}"},
                    "abstract": {"value": f"Grounded evidence for {paper_id}."},
                }
                if paper_id == "P001":
                    metadata["_artifact_ids"] = {"mineru": str(mineru_artifact_id)}
                    metadata["source_paths"] = {
                        "extracted_dir": str(mineru_version),
                        "content_list": str(content_list),
                    }
                papers.append(
                    LibraryPaper(
                        user_id=first.id,
                        paper_id=paper_id,
                        content_sha256=f"{index:064x}",
                        original_filename=f"{paper_id}.pdf",
                        title=f"Paper {index}",
                        authors_json=[f"Author {index}"],
                        keywords_json=["copper"],
                        tags_json={},
                        metadata_json=metadata,
                        pdf_relative_path=f"review-library/uploads/{paper_id}.pdf",
                        markdown_relative_path=f"review-library/markdown/{paper_id}.md",
                    )
                )
            session.add_all(papers)
            stat = content_list.stat()
            session.add(
                LibraryArtifact(
                    id=mineru_artifact_id,
                    user_id=first.id,
                    paper_id="P001",
                    kind="mineru",
                    relative_path=content_list.relative_to(
                        root / "users" / str(first.id)
                    ).as_posix(),
                    content_sha256="a" * 64,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    availability="available",
                )
            )
            self.project_id = str(project.id)
            self.other_project_id = str(other.id)
            self.first = Principal(str(first.id), frozenset({Role.USER}), first.email)
            self.second = Principal(str(second.id), frozenset({Role.USER}), second.email)
        self.current = self.first
        self.writer_calls = 0
        self.transient_failures = 0
        self.include_figures = False
        self.figure_source_outside_recorded_extraction = False
        self.use_chunk_evidence = False
        self.use_unknown_chunk = False

        def writer(context, payload):
            self.writer_calls += 1
            if self.transient_failures > 0:
                self.transient_failures -= 1
                raise RuntimeError("Section-writing model request failed with HTTP 503")
            sections = []
            package_sections = {
                str(item.get("section_id") or ""): item
                for item in (payload.get("evidence_package") or {}).get("sections") or []
                if isinstance(item, dict)
            }
            for task in payload["tasks"]:
                evidence_by_paper = {
                    str(hit.get("paper_id") or ""): hit
                    for hit in package_sections.get(task["section_id"], {}).get("hits") or []
                    if isinstance(hit, dict) and not hit.get("is_neighbor")
                }
                paragraphs = [
                    {
                        "paragraph_id": f"{task['section_id']}-p{index}",
                        "paper_id": paper_id,
                        "cited_paper_ids": [paper_id],
                        "text": f"Grounded synthesis for {paper_id} [{index}].",
                        **(
                            {
                                "evidence": [
                                    {
                                        "paper_id": paper_id,
                                        "chunk_ids": [
                                            "unknown-chunk"
                                            if self.use_unknown_chunk
                                            else evidence_by_paper[paper_id]["chunk_id"]
                                        ],
                                        "claim": f"Grounded claim for {paper_id}",
                                    }
                                ]
                            }
                            if self.use_chunk_evidence and paper_id in evidence_by_paper
                            else {}
                        ),
                    }
                    for index, paper_id in enumerate(task["allowed_papers"], start=1)
                ]
                markdown = "\n\n".join(
                    [f"## {task['heading']}"] + [paragraph["text"] for paragraph in paragraphs]
                ) + "\n"
                sections.append(
                    {
                        "section_id": task["section_id"],
                        "heading": task["heading"],
                        "overview": task["core_argument"],
                        "paragraphs": paragraphs,
                        "draft_md": markdown,
                    }
                )
            result = {
                "sections": sections,
                "section_drafts_md": "\n".join(section["draft_md"] for section in sections),
                "report_md": f"# Section Drafting Report\n\nGenerated {len(sections)} sections.\n",
                "attempts": 1,
            }
            if self.include_figures:
                source = self.mineru_source
                if self.figure_source_outside_recorded_extraction:
                    source = (
                        root
                        / "users"
                        / context.user_id
                        / ".review-writer"
                        / "staging"
                        / "other-project"
                        / "scheme.png"
                    )
                source.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (16, 8), "white").save(source)
                first_task = payload["tasks"][0]
                paper_id = first_task["allowed_papers"][0]
                if self.figure_source_outside_recorded_extraction:
                    payload["library_metadata"][paper_id].setdefault(
                        "source_paths", {}
                    )["extracted_dir"] = str(source.parent)
                paragraph_id = f"{first_task['section_id']}-p1"
                candidate = {
                    "paper_id": paper_id,
                    "candidate_index": 0,
                    "source_label": "Scheme 1",
                    "source_type": "image",
                    "source_image_path": str(source),
                    "source_pdf": str(self.root / "users" / context.user_id / "paper.pdf"),
                    "source_content_list": str(self.root / "users" / context.user_id / "content.json"),
                    "target_paragraph_id": paragraph_id,
                    "section_id": first_task["section_id"],
                    "section_heading": first_task["heading"],
                    "score": 8,
                    "manuscript_selected": True,
                }
                result.update(
                    {
                        "paper_figure_candidates": {
                            "project_id": self.project_id,
                            "papers": [
                                {
                                    "paper_id": paper_id,
                                    "candidates": [dict(candidate)],
                                    "selected_candidate_index": 0,
                                }
                            ],
                        },
                        "figure_candidates": [dict(candidate)],
                        "default_figure_reviews": {
                            "papers": {
                                paper_id: {
                                    "selected_candidate_index": 0,
                                    "selected_source_image_path": str(source),
                                    "reviewed_at": "2026-08-13T00:00:00+00:00",
                                }
                            }
                        },
                    }
                )
            return result

        settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url=database_url,
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=root / "users",
        )
        self.app = create_app(
            settings,
            principal_provider=lambda: self.current,
            session_factory_override=self.sessions,
            native_workflow_overrides={"sections.generate": writer},
        )
        self._seed_planning()

    def _seed_document_indexes(self) -> None:
        with self.sessions.begin() as session:
            papers = session.query(LibraryPaper).filter_by(
                user_id=uuid.UUID(self.first.user_id)
            ).all()
            for ordinal, paper in enumerate(papers):
                index = LibraryDocumentIndex(
                    library_paper_id=paper.id,
                    user_id=paper.user_id,
                    paper_id=paper.paper_id,
                    source_lineage_json={"paper_id": paper.paper_id},
                    source_lineage_hash=f"{ordinal + 10:064x}",
                    chunker_version="mineru-layout-v1",
                    status="ready",
                    is_current=True,
                    chunk_count=1,
                )
                session.add(index)
                session.flush()
                session.add(
                    LibraryDocumentChunk(
                        index_id=index.id,
                        user_id=paper.user_id,
                        paper_id=paper.paper_id,
                        chunk_id=f"chunk-{paper.paper_id}",
                        ordinal=0,
                        content=(
                            "Copper allenation catalyst evidence, comparison, scope, "
                            f"mechanism, and conclusion for {paper.paper_id}."
                        ),
                        normalized_content=(
                            "copper allenation catalyst evidence, comparison, scope, "
                            f"mechanism, and conclusion for {paper.paper_id}."
                        ).casefold(),
                        content_type="text",
                        section_path_json=["Results"],
                        page_start=80 + ordinal,
                        page_end=80 + ordinal,
                        block_start=ordinal,
                        block_end=ordinal,
                        asset_refs_json=[],
                        is_reference=False,
                    )
                )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    @staticmethod
    def headers(key: str = "sections") -> dict[str, str]:
        return {"Origin": "http://testserver", "Idempotency-Key": key}

    def _seed_planning(self) -> None:
        discovery = self.app.state.discovery_service
        repository = self.app.state.workflow_repository
        review = {
            "project_id": self.project_id,
            "topic": "Copper allenation",
            "selection_mode": "explicit",
            "results": [
                {
                    "keyword": "copper",
                    "keep": True,
                    "local_results": [
                        {
                            "paper_id": paper_id,
                            "title": paper_id,
                            "score": 10 - index,
                            "role": "core_candidate",
                            "selected_for_matrix": True,
                        }
                        for index, paper_id in enumerate(("P001", "P002", "P003"))
                    ],
                    "web_results": [],
                }
            ],
        }
        artifact, run = discovery._write_json_artifact(
            self.first,
            self.project_id,
            stage_id="discovery",
            logical_name="discovery/review.json",
            payload=review,
            make_current=False,
        )
        repository.save_discovery_atomically(
            self.first.user_id,
            self.project_id,
            artifact_id=artifact.id,
            run_id=run.id,
            expected_revision=0,
            status="review",
        )
        with TestClient(self.app) as client:
            confirmed = client.post(
                f"/api/v1/projects/{self.project_id}/discovery/confirm",
                json={"revision": 1},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed.status_code, confirmed.text)
            planning = client.get(f"/api/v1/projects/{self.project_id}/planning").json()
            outline_md = (
                "# Selected Outline\n\n"
                "## Introduction\n"
                "Section role: introduction\n"
                "Purpose: define the scope and mechanistic vocabulary.\n\n"
                "## Copper-catalyzed allenation strategies\n"
                "Section role: body\n"
                "Assigned papers: P001, P002, P003.\n"
                "Purpose: compare the evidence, mechanisms, selectivity, and limitations.\n\n"
                "## Cross-strategy comparison and conclusion\n"
                "Section role: conclusion\n"
                "Purpose: synthesize supported trends, limitations, and future directions.\n"
            )
            selected = client.put(
                f"/api/v1/projects/{self.project_id}/planning/outline",
                json={
                    "revision": planning["matrix_revision"],
                    "outline_style": "custom",
                    "outline_md": outline_md,
                    "manual": True,
                },
                headers=self.headers(),
            )
            self.assertEqual(200, selected.status_code, selected.text)
            blueprint = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(200, blueprint.status_code, blueprint.text)
            confirmed_blueprint = client.post(
                f"/api/v1/projects/{self.project_id}/planning/blueprint/confirm",
                json={"revision": blueprint.json()["blueprint_revision"]},
                headers=self.headers(),
            )
            self.assertEqual(200, confirmed_blueprint.status_code, confirmed_blueprint.text)

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return payload
            time.sleep(0.02)
        self.fail("Job did not finish.")

    def start(self, client: TestClient, key: str = "sections") -> dict:
        response = client.post(
            f"/api/v1/projects/{self.project_id}/sections/jobs",
            json={},
            headers=self.headers(key),
        )
        self.assertEqual(202, response.status_code, response.text)
        return self.wait_job(client, response.json()["id"])

    def test_payload_resolves_blueprint_papers(self) -> None:
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/sections")
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload["section_tasks"])
        self.assertEqual(
            {"P001", "P002", "P003"},
            {paper["paper_id"] for paper in payload["papers"]},
        )
        self.assertEqual(
            {"P001": "P001", "P002": "P002", "P003": "P003"},
            payload["paper_display_labels"],
        )
        self.assertEqual(len(payload["section_blueprint"]["sections"]), len(payload["section_tasks"]))
        introduction = next(
            task
            for task in payload["section_tasks"]
            if task["section_role"] == "introduction"
        )
        conclusion = next(
            task
            for task in payload["section_tasks"]
            if task["section_role"] == "conclusion"
        )
        self.assertEqual([], introduction["primary_papers"])
        self.assertEqual("framing_synthesis", introduction["writing_mode"])
        self.assertEqual([], conclusion["primary_papers"])
        self.assertEqual("cross_section_synthesis", conclusion["writing_mode"])
        primary_occurrences = [
            paper_id
            for task in payload["section_tasks"]
            for paper_id in task["primary_papers"]
        ]
        self.assertEqual(len(primary_occurrences), len(set(primary_occurrences)))

    def test_legacy_blueprint_is_normalized_before_section_generation(self) -> None:
        tasks = self.app.state.sections_service.tasks_from_blueprint(
            {
                "sections": [
                    {
                        "section_id": "S01",
                        "title": "Introduction",
                        "section_role": "body",
                        "major_papers": ["P001"],
                    },
                    {
                        "section_id": "S02",
                        "title": "Primary evidence theme",
                        "section_role": "body",
                        "major_papers": ["P001", "P002"],
                    },
                    {
                        "section_id": "S03",
                        "title": "Cross-category comparison and conclusion",
                        "section_role": "body",
                        "major_papers": ["P001"],
                    },
                ]
            }
        )
        by_id = {task["section_id"]: task for task in tasks}
        self.assertEqual("introduction", by_id["S01"]["section_role"])
        self.assertEqual([], by_id["S01"]["primary_papers"])
        self.assertEqual(["P001"], by_id["S01"]["supporting_papers"])
        self.assertEqual(["P001", "P002"], by_id["S02"]["primary_papers"])
        self.assertEqual("conclusion", by_id["S03"]["section_role"])
        self.assertEqual([], by_id["S03"]["primary_papers"])

    def test_context_papers_are_allowed_and_conclusion_is_scheduled_last(self) -> None:
        tasks = self.app.state.sections_service.tasks_from_blueprint(
            {
                "paper_assignment_policy": {
                    "mode": "single_primary_section_with_supporting_cross_references"
                },
                "sections": [
                    {
                        "section_id": "S03",
                        "title": "Conclusion",
                        "section_role": "conclusion",
                        "primary_papers": [],
                        "supporting_papers": ["P001"],
                    },
                    {
                        "section_id": "S01",
                        "title": "Introduction",
                        "section_role": "introduction",
                        "primary_papers": [],
                        "context_papers": ["P009"],
                    },
                    {
                        "section_id": "S02",
                        "title": "Primary evidence",
                        "section_role": "body",
                        "primary_papers": ["P001"],
                    },
                ],
            }
        )

        introduction = next(task for task in tasks if task["section_id"] == "S01")
        self.assertEqual(["P009"], introduction["context_papers"])
        self.assertIn("P009", introduction["allowed_papers"])
        self.assertEqual("S03", tasks[-1]["section_id"])

    def test_context_papers_are_not_double_counted_as_supporting(self) -> None:
        tasks = self.app.state.sections_service.tasks_from_blueprint(
            {
                "paper_assignment_policy": {
                    "mode": "single_primary_section_with_supporting_cross_references"
                },
                "sections": [
                    {
                        "section_id": "S01",
                        "title": "Introduction",
                        "section_role": "introduction",
                        "primary_papers": [],
                        "supporting_papers": ["P001", "P009"],
                        "context_papers": ["P009"],
                    }
                ],
            }
        )

        self.assertEqual(["P001"], tasks[0]["supporting_papers"])
        self.assertEqual(["P009"], tasks[0]["context_papers"])
        self.assertEqual(["P001", "P009"], tasks[0]["allowed_papers"])

    def test_publish_rejects_a_changed_outline_dependency(self) -> None:
        service = self.app.state.sections_service
        payload = service.generation_payload(self.first, self.project_id)
        payload["source_outline_artifact_id"] = str(uuid.uuid4())
        with self.assertRaises(WorkflowConflict):
            service.publish_generation(
                self.first,
                self.project_id,
                payload,
                {},
                attempts=1,
            )

    def test_missing_blueprint_paper_blocks_generation(self) -> None:
        service = self.app.state.planning_service
        repository = self.app.state.workflow_repository
        blueprint, _artifact = service._read_json(
            self.first, self.project_id, BLUEPRINT_LOGICAL_NAME
        )
        changed = deepcopy(blueprint)
        changed["sections"][0]["major_papers"] = ["P999"]
        published, run = service._publish_files(
            self.first,
            self.project_id,
            stage_id="blueprint",
            files={
                BLUEPRINT_LOGICAL_NAME: (
                    (json.dumps(changed, ensure_ascii=False) + "\n").encode(),
                    "json",
                )
            },
        )
        state = repository.get_stage_state(self.first.user_id, self.project_id, "blueprint")
        repository.promote_stage_artifacts_atomically(
            self.first.user_id,
            self.project_id,
            "blueprint",
            artifact_ids={BLUEPRINT_LOGICAL_NAME: published[BLUEPRINT_LOGICAL_NAME].id},
            run_id=run.id,
            expected_revision=state.revision,
            status="approved",
            invalidate_stages=("sections", "figure-review", "figures", "draft", "final"),
        )
        with TestClient(self.app) as client:
            response = client.post(
                f"/api/v1/projects/{self.project_id}/sections/jobs",
                json={},
                headers=self.headers("missing"),
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("BLUEPRINT_PAPERS_MISSING", response.json()["error"]["code"])

    def test_generation_persists_section_progress(self) -> None:
        with TestClient(self.app) as client:
            job = self.start(client, "progress")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        self.assertEqual("succeeded", job["status"])
        self.assertEqual(len(payload["section_tasks"]), job["progress_total"])
        self.assertEqual(job["progress_total"], job["progress_current"])

    def test_generation_publishes_page_addressable_evidence_package(self) -> None:
        self._seed_document_indexes()
        self.use_chunk_evidence = True
        with TestClient(self.app) as client:
            job = self.start(client, "chunk-evidence")
            payload = client.get(
                f"/api/v1/projects/{self.project_id}/sections"
            ).json()

        self.assertEqual("succeeded", job["status"], job)
        self.assertTrue(job["result"]["evidence_package_artifact_id"])
        package = payload["evidence_package"]
        self.assertTrue(package["sections"])
        self.assertTrue(package["evidence_registry"])
        self.assertTrue(
            all(
                str(item["evidence_key"]).startswith("sha256:")
                and str(item["evidence_id"]).startswith("EV-")
                for item in package["evidence_registry"]
            )
        )
        self.assertTrue(
            all(section["retrieval_mode"] == "lexical" for section in package["sections"])
        )
        self.assertTrue(
            any(
                hit["page_start"] >= 80
                for section in package["sections"]
                for hit in section["hits"]
            )
        )

    def test_coverage_fallback_does_not_make_unmatched_primary_papers_writeable(self) -> None:
        self._seed_document_indexes()
        with self.sessions.begin() as session:
            chunks = {
                chunk.paper_id: chunk
                for chunk in session.query(LibraryDocumentChunk).all()
            }
            chunks["P001"].content = "Selective allene anchor evidence."
            chunks["P001"].normalized_content = chunks["P001"].content.casefold()
            for paper_id in ("P002", "P003"):
                chunks[paper_id].content = f"Independent source passage for {paper_id}."
                chunks[paper_id].normalized_content = chunks[
                    paper_id
                ].content.casefold()

        service = self.app.state.sections_service
        paper_ids = ["P001", "P002", "P003"]
        catalog = service._catalog(self.first, paper_ids)
        package = service._evidence_package(
            self.first,
            self.project_id,
            [
                {
                    "section_id": "S02",
                    "heading": "Selective allene anchor",
                    "core_argument": "Compare the primary evidence.",
                    "must_cover_points": [],
                    "primary_papers": paper_ids,
                    "supporting_papers": [],
                    "allowed_papers": paper_ids,
                }
            ],
            catalog,
        )

        section = package["sections"][0]
        self.assertEqual("partial", section["status"])
        self.assertEqual("lexical", section["retrieval_mode"])
        self.assertEqual(3, section["primary_paper_count"])
        self.assertEqual(1, section["covered_primary_paper_count"])
        self.assertEqual(["P001"], section["writeable_primary_papers"])
        self.assertEqual(["P002", "P003"], section["unresolved_primary_papers"])
        self.assertEqual(["P002", "P003"], section["missing_primary_papers"])
        self.assertEqual(
            set(paper_ids),
            {hit["paper_id"] for hit in section["hits"]},
        )
        self.assertEqual(
            {"P002", "P003"},
            {
                hit["paper_id"]
                for hit in section["hits"]
                if hit["match_reason"] == "primary_paper_coverage_fallback"
            },
        )
        self.assertTrue(
            all(
                hit["match_type"] == "coverage_only"
                and hit["support_level"] == "coverage_only"
                and not hit["claim_eligible"]
                and not hit["counts_as_evidence"]
                for hit in section["hits"]
                if hit["paper_id"] in {"P002", "P003"}
            )
        )
        p001 = next(
            hit
            for hit in section["hits"]
            if hit["paper_id"] == "P001" and hit["claim_eligible"]
        )
        self.assertIn("per_paper_targeted", p001["retrieval_passes"])
        self.assertEqual("direct", p001["support_level"])
        self.assertIn("global_comparison", p001["retrieval_passes"])
        self.assertGreater(len(section["query_plans"]), 1)
        self.assertTrue(
            all(len(plan["websearch_query"]) < 1000 for plan in section["query_plans"])
        )

    def test_academic_validation_uses_section_scoped_evidence_role(self) -> None:
        evidence_key = "sha256:" + "a" * 64
        payload = {
            "tasks": [{"section_id": "S05", "section_role": "body"}],
        }
        built = {
            "sections": [
                {
                    "section_id": "S05",
                    "paragraphs": [
                        {
                            "paragraph_id": "S05-p1",
                            "claim_realizations": [{"claim_id": "S05-p1-C01"}],
                        }
                    ],
                }
            ]
        }
        writing_plan = {
            "planning_mode": "evidence_first",
            "sections": [
                {
                    "section_id": "S05",
                    "paragraphs": [{"paragraph_id": "S05-p1"}],
                    "claims": [
                        {
                            "claim_id": "S05-p1-C01",
                            "paragraph_id": "S05-p1",
                            "support_status": "supported",
                            "citation_group": ["P001"],
                            "evidence_refs": [{"evidence_key": evidence_key}],
                        }
                    ],
                }
            ],
        }
        synthesis_state = {"sections": [{"section_id": "S05", "components": []}]}
        evidence_package = {
            # The global registry may contain the first occurrence of a chunk,
            # where it was only a neighboring context hit in another section.
            "evidence_registry": [
                {
                    "evidence_key": evidence_key,
                    "paper_id": "P001",
                    "claim_eligible": False,
                    "match_type": "neighbor_context",
                }
            ],
            "sections": [
                {
                    "section_id": "S05",
                    "retrieval_mode": "lexical",
                    "hits": [
                        {
                            "evidence_key": evidence_key,
                            "paper_id": "P001",
                            "claim_eligible": True,
                            "match_type": "direct_match",
                        }
                    ],
                }
            ],
        }

        self.app.state.sections_service._validate_academic_bundle(
            payload, built, synthesis_state, writing_plan, evidence_package
        )

        evidence_package["evidence_registry"][0]["claim_eligible"] = True
        evidence_package["evidence_registry"][0]["match_type"] = "direct_match"
        evidence_package["sections"][0]["hits"][0]["claim_eligible"] = False
        evidence_package["sections"][0]["hits"][0]["match_type"] = "neighbor_context"
        with self.assertRaisesRegex(
            WorkflowValidationError,
            "neighbor or coverage-only context",
        ):
            self.app.state.sections_service._validate_academic_bundle(
                payload, built, synthesis_state, writing_plan, evidence_package
            )

    def test_conclusion_inherits_only_claim_eligible_body_chunks(self) -> None:
        tasks = {
            "S01": {"section_id": "S01", "section_role": "introduction"},
            "S02": {"section_id": "S02", "section_role": "body"},
            "S03": {"section_id": "S03", "section_role": "conclusion"},
        }
        evidence_sections = {
            "S01": {
                "hits": [
                    {
                        "paper_id": "P000",
                        "chunk_id": "intro-direct",
                        "claim_eligible": True,
                    }
                ]
            },
            "S02": {
                "hits": [
                    {
                        "paper_id": "P001",
                        "chunk_id": "body-direct",
                        "claim_eligible": True,
                    },
                    {
                        "paper_id": "P001",
                        "chunk_id": "body-neighbor",
                        "claim_eligible": False,
                    },
                ]
            },
            "S03": {"hits": []},
        }

        conclusion_chunks = self.app.state.sections_service._valid_retrieval_chunks(
            "S03", tasks["S03"], evidence_sections, tasks
        )
        self.assertIn(("P001", "body-direct"), conclusion_chunks)
        self.assertNotIn(("P001", "body-neighbor"), conclusion_chunks)
        self.assertNotIn(("P000", "intro-direct"), conclusion_chunks)

        introduction_chunks = self.app.state.sections_service._valid_retrieval_chunks(
            "S01", tasks["S01"], evidence_sections, tasks
        )
        self.assertEqual({("P000", "intro-direct")}, introduction_chunks)

    def test_question_query_plan_uses_or_groups_and_separate_questions(self) -> None:
        service = self.app.state.sections_service
        plans = service._evidence_queries(
            {
                "section_id": "S02",
                "section_role": "body",
                "heading": "Propargylic alcohol substrates",
                "core_argument": "Compare scope and mechanisms.",
                "must_cover_points": ["Explain catalyst effects on selectivity."],
            },
            review_topic='Review of "axial-chiral allene synthesis"',
        )

        self.assertIn("section_focus", {plan["question_id"] for plan in plans})
        self.assertIn("scope", {plan["question_id"] for plan in plans})
        self.assertIn("mechanism", {plan["question_id"] for plan in plans})
        self.assertIn("required_claim_01", {plan["question_id"] for plan in plans})
        scope = next(plan for plan in plans if plan["question_id"] == "scope")
        self.assertEqual(2, len(scope["term_groups"]))
        self.assertIn(" OR ", scope["websearch_query"])
        self.assertNotIn("Compare scope and mechanisms", scope["websearch_query"])

    def test_unknown_chunk_citation_is_rejected_before_publish(self) -> None:
        self._seed_document_indexes()
        self.use_chunk_evidence = True
        self.use_unknown_chunk = True
        with TestClient(self.app) as client:
            job = self.start(client, "unknown-chunk")

        self.assertEqual("failed", job["status"])
        self.assertEqual("WORKFLOW_VALIDATION_FAILED", job["error_code"])

    def test_transient_provider_failure_is_not_replayed_by_stage_router(self) -> None:
        self.transient_failures = 1
        with TestClient(self.app) as client:
            job = self.start(client, "retry-503")
        self.assertEqual("failed", job["status"])
        self.assertEqual(1, self.writer_calls)
        self.assertEqual("SECTION_PROVIDER_UNAVAILABLE", job["error_code"])

    def test_successful_task_publishes_section_artifact(self) -> None:
        with TestClient(self.app) as client:
            job = self.start(client, "publish")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        self.assertEqual("succeeded", job["status"])
        self.assertTrue(payload["section_files"])
        self.assertIn("Grounded synthesis", payload["section_files"][0]["content"])
        self.assertTrue(job["result"]["synthesis_state_artifact_id"])
        self.assertTrue(job["result"]["writing_plan_artifact_id"])
        self.assertEqual("legacy_adapter", payload["synthesis_state"]["planning_mode"])
        self.assertEqual(
            "legacy_derived_after_generation",
            payload["writing_plan"]["planning_mode"],
        )
        for logical_name in (
            "sections/synthesis_state.json",
            "sections/writing_plan.json",
            "sections/evidence_package.json",
            "sections/section_drafts.json",
        ):
            self.assertIsNotNone(
                self.app.state.workflow_repository.get_current_artifact(
                    self.first.user_id,
                    self.project_id,
                    logical_name,
                )
            )
        artifact_id = payload["section_files"][0]["artifact_id"]
        self.assertEqual(
            artifact_id,
            self.app.state.workflow_repository.get_current_artifact(
                self.first.user_id,
                self.project_id,
                f"sections/{payload['section_files'][0]['section_id']}.md",
            ).id,
        )
        index_artifact = self.app.state.workflow_repository.get_current_artifact(
            self.first.user_id,
            self.project_id,
            "sections/section_drafts.json",
        )
        index = json.loads(
            self.app.state.artifact_service.resolve_owned_artifact(
                self.first.user_id, index_artifact.id
            ).path.read_text(encoding="utf-8")
        )
        self.assertTrue(index["source_outline_artifact_id"])

    def test_report_uses_current_jobs(self) -> None:
        with TestClient(self.app) as client:
            first = self.start(client, "report-1")
            second = self.start(client, "report-2")
            payload = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
        job_ids = [job["id"] for job in payload["report"]["jobs"]]
        self.assertIn(first["id"], job_ids)
        self.assertIn(second["id"], job_ids)
        self.assertEqual(payload["section_tasks"].__len__(), payload["report"]["current_task_count"])

    def test_stage_run_records_dirty_and_recomputed_section_ids(self) -> None:
        with TestClient(self.app) as client:
            first = self.start(client, "dirty-first")
            second = self.start(client, "dirty-second")
        self.assertEqual("succeeded", first["status"], first)
        self.assertEqual("succeeded", second["status"], second)
        self.assertEqual(first["result"]["section_ids"], first["result"]["dirty_object_ids"])
        self.assertEqual([], second["result"]["dirty_object_ids"])
        self.assertEqual(
            second["result"]["section_ids"],
            second["result"]["recomputed_object_ids"],
        )
        latest = self.app.state.workflow_repository.get_latest_stage_run(
            self.first.user_id,
            self.project_id,
            "sections",
        )
        self.assertIsNotNone(latest)
        self.assertEqual([], latest.input_snapshot["dirty_object_ids"])
        self.assertEqual(
            second["result"]["section_ids"],
            latest.output_snapshot["recomputed_object_ids"],
        )

    def test_generation_publishes_anchored_source_images_as_project_artifacts(self) -> None:
        self.include_figures = True
        with TestClient(self.app) as client:
            job = self.start(client, "figure-candidates")
        self.assertEqual("succeeded", job["status"], job)
        repository = self.app.state.workflow_repository
        candidate_artifact = repository.get_current_artifact(
            self.first.user_id,
            self.project_id,
            "sections/paper_figure_candidates.json",
        )
        self.assertIsNotNone(candidate_artifact)
        resolved = self.app.state.artifact_service.resolve_owned_artifact(
            self.first.user_id, candidate_artifact.id
        )
        payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        candidate = payload["papers"][0]["candidates"][0]
        self.assertNotIn("source_image_path", candidate)
        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertTrue(candidate["source_image_artifact_id"])
        image = self.app.state.artifact_service.resolve_owned_artifact(
            self.first.user_id, candidate["source_image_artifact_id"]
        )
        self.assertTrue(image.path.is_file())
        defaults = repository.get_current_artifact(
            self.first.user_id,
            self.project_id,
            "sections/default_figure_reviews.json",
        )
        default_payload = json.loads(
            self.app.state.artifact_service.resolve_owned_artifact(
                self.first.user_id, defaults.id
            ).path.read_text(encoding="utf-8")
        )
        review = default_payload["papers"][candidate["paper_id"]]
        self.assertNotIn("selected_source_image_path", review)
        self.assertEqual(
            candidate["source_image_artifact_id"],
            review["selected_source_artifact_id"],
        )

    def test_generation_rejects_source_image_outside_registered_mineru_artifact(self) -> None:
        self.include_figures = True
        self.figure_source_outside_recorded_extraction = True
        with TestClient(self.app) as client:
            job = self.start(client, "wrong-paper-source")
        self.assertEqual("failed", job["status"])
        self.assertEqual("WORKFLOW_VALIDATION_FAILED", job["error_code"])

    def test_handoff_requires_current_section_outputs(self) -> None:
        with TestClient(self.app) as client:
            before = client.post(
                f"/api/v1/projects/{self.project_id}/sections/confirm",
                json={"revision": 0},
                headers=self.headers(),
            )
            self.assertEqual(409, before.status_code, before.text)
            self.start(client, "handoff")
            sections = client.get(f"/api/v1/projects/{self.project_id}/sections").json()
            after = client.post(
                f"/api/v1/projects/{self.project_id}/sections/confirm",
                json={"revision": sections["revision"]},
                headers=self.headers(),
            )
        self.assertEqual(200, after.status_code, after.text)
        self.assertEqual("images", after.json()["next_stage"])

    def test_sections_api_and_container_are_user_isolated(self) -> None:
        self.assertIs(
            self.app.state.sections_service,
            self.app.state.container.sections_service,
        )
        self.current = self.second
        with TestClient(self.app) as client:
            response = client.get(f"/api/v1/projects/{self.project_id}/sections")
        self.assertEqual(404, response.status_code, response.text)


if __name__ == "__main__":
    unittest.main()
