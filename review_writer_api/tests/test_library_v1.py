from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings, database_url_from_env
from review_writer_api.database import (
    Base,
    User,
    create_session_factory,
    database_session,
)
from review_writer_api.domain_services.library import LibraryService
from review_writer_api.scientific_runner import ScientificRunFailed
from review_writer_api.security import Principal, Role
from review_writer_api.workflow_models import LibraryArtifact, LibraryPaper
from review_writer_api.workspaces import HostedWorkspaceManager


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")


def fake_pdf(seed: bytes = b"A") -> bytes:
    return b"%PDF-1.7\n" + seed * 700 + b"\n%%EOF\n"


class LibraryV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        database_url = f"sqlite+pysqlite:///{(root / 'library.sqlite3').as_posix()}"
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
            self.first = Principal(str(first.id), frozenset({Role.USER}), first.email)
            self.second = Principal(str(second.id), frozenset({Role.USER}), second.email)
        self.current = self.first
        self.settings = ApiSettings(
            review_root=root,
            deployment_mode="hosted",
            database_url=database_url,
            public_origin="http://testserver",
            credential_encryption_key=TEST_KEY,
            hosted_workspace_root=root / "users",
        )
        self.parse_calls = 0

        def precise_ingest(user_root: Path, filename: str, staged_pdf: Path):
            self.parse_calls += 1
            if filename == "fails.pdf":
                raise RuntimeError(
                    "MinerU precise parsing failed; the PDF was not admitted to Library. "
                    "Missing MinerU API token."
                )
            paper_id = f"P{self.parse_calls:03d}"
            library = user_root / "review-library"
            uploads = library / "uploads"
            markdown = library / "markdown"
            metadata_dir = library / "metadata" / "papers"
            uploads.mkdir(parents=True, exist_ok=True)
            markdown.mkdir(parents=True, exist_ok=True)
            metadata_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = uploads / f"{paper_id}.pdf"
            pdf_path.write_bytes(staged_pdf.read_bytes())
            md_path = markdown / f"{paper_id}.md"
            md_path.write_text(f"# Copper catalysis {paper_id}\n\nallene keyword", encoding="utf-8")
            extracted_dir = user_root / ".upload-staging" / f"{paper_id}-extracted"
            extracted_dir.mkdir(parents=True, exist_ok=True)
            source_image = extracted_dir / "images" / "scheme.png"
            source_image.parent.mkdir(parents=True, exist_ok=True)
            source_image.write_bytes(b"image-bytes")
            content_list = extracted_dir / f"{paper_id}_content_list.json"
            content_list.write_text(
                json.dumps(
                    [
                        {
                            "type": "image",
                            "img_path": "images/scheme.png",
                            "image_caption": ["Scheme 1"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            metadata = {
                "paper_id": paper_id,
                "title": {"value": f"Copper catalysis {paper_id}"},
                "authors": {"value": ["Ada Lovelace"]},
                "keywords": {"value": ["allene"]},
                "structured_tags": {"value": {"reaction_type": "allenation"}},
                "source_paths": {
                    "pdf": str(pdf_path),
                    "markdown": str(md_path),
                    "content_list": str(content_list),
                    "extracted_dir": str(extracted_dir),
                },
            }
            meta_path = metadata_dir / f"{paper_id}.metadata.json"
            meta_path.write_text(json.dumps(metadata), encoding="utf-8")
            return {
                "status": "uploaded",
                "paper_id": paper_id,
                "title": f"Copper catalysis {paper_id}",
                "metadata_path": str(meta_path),
                "pdf_path": str(pdf_path),
                "markdown_path": str(md_path),
                "mineru_ready": True,
            }

        def search_provider(_context, payload):
            return {
                "candidates": [
                    {
                        "candidate_id": "crossref:1",
                        "title": payload["topic"],
                        "source": "crossref",
                    }
                ]
            }

        def download_provider(_context, payload):
            root = (
                self.settings.hosted_workspace_root
                / _context.user_id
                / ".review-writer"
                / "job-staging"
                / _context.job_id
                / "library-workspace"
                / "review-library"
            )
            pdf_path = root / "downloads" / "P900.pdf"
            markdown_path = root / "downloads" / "P900.md"
            metadata_path = root / "metadata" / "papers" / "P900.metadata.json"
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(fake_pdf(b"9"))
            markdown_path.write_text("# Downloaded native paper\n", encoding="utf-8")
            metadata_path.write_text(
                json.dumps(
                    {
                        "paper_id": "P900",
                        "title": {"value": "Downloaded native paper"},
                        "authors": {"value": ["Grace Hopper"]},
                        "keywords": {"value": ["native catalog"]},
                        "structured_tags": {"value": {"reaction_type": "download"}},
                        "source_paths": {
                            "pdf": str(pdf_path),
                            "markdown": str(markdown_path),
                        },
                    }
                ),
                encoding="utf-8",
            )
            return {
                "added_count": len(payload["candidates"]),
                "already_present_count": 0,
                "failed_count": 0,
                "results": [
                    {
                        "status": "downloaded",
                        "paper_id": "P900",
                        "path": str(pdf_path),
                        "metadata_path": str(metadata_path),
                    }
                ],
            }

        self.app = create_app(
            self.settings,
            principal_provider=lambda: self.current,
            session_factory_override=self.sessions,
            native_workflow_overrides={
                "library.precise_ingest": precise_ingest,
                "library.search": search_provider,
                "library.download": download_provider,
            },
        )

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temporary.cleanup()

    def wait_job(self, client: TestClient, job_id: str) -> dict:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/jobs/{job_id}").json()
            if payload["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return payload
            time.sleep(0.02)
        self.fail("Job did not finish.")

    def upload(self, client: TestClient, filename: str, body: bytes):
        return client.post(
            f"/api/v1/library/papers?filename={filename}",
            content=body,
            headers={"Content-Type": "application/pdf", "Origin": "http://testserver"},
        )

    def test_upload_admits_only_precisely_parsed_pdf(self) -> None:
        with TestClient(self.app) as client:
            admitted = self.upload(client, "copper.pdf", fake_pdf())
            rejected = self.upload(client, "fails.pdf", fake_pdf(b"B"))

        self.assertEqual(201, admitted.status_code)
        self.assertTrue(admitted.json()["mineru_ready"])
        self.assertEqual(502, rejected.status_code)
        self.assertEqual("MINERU_PRECISE_PARSE_FAILED", rejected.json()["error"]["code"])
        self.assertEqual(1, admitted.json()["library_count"])

    def test_upload_persists_mineru_content_and_images_before_staging_cleanup(self) -> None:
        with TestClient(self.app) as client:
            admitted = self.upload(client, "figures.pdf", fake_pdf(b"F"))
        self.assertEqual(201, admitted.status_code, admitted.text)
        paper = self.app.state.library_service.get(
            self.first, admitted.json()["paper_id"]
        )
        paths = paper.metadata["source_paths"]
        content_list = Path(paths["content_list"])
        extracted = Path(paths["extracted_dir"])
        self.assertTrue(content_list.is_file())
        self.assertTrue((extracted / "images" / "scheme.png").is_file())
        self.assertIn("mineru", paper.artifact_ids)
        self.assertIn("review-library/.artifacts/", content_list.as_posix())

    def test_duplicate_upload_is_idempotent(self) -> None:
        content = fake_pdf()
        with TestClient(self.app) as client:
            first = self.upload(client, "first.pdf", content)
            duplicate = self.upload(client, "renamed.pdf", content)

        self.assertEqual(201, first.status_code)
        self.assertEqual(200, duplicate.status_code)
        self.assertEqual("duplicate_file", duplicate.json()["status"])
        self.assertEqual(first.json()["paper_id"], duplicate.json()["paper_id"])
        self.assertEqual(1, self.parse_calls)

    def test_native_upload_runner_receives_secrets_only_in_task_environment(self) -> None:
        captured: dict = {}

        class RecordingRunner:
            def run(_self, command, **kwargs):
                captured["command"] = tuple(command)
                captured.update(kwargs)
                root = Path(command[command.index("--review-root") + 1])
                staged = Path(command[command.index("--input") + 1])
                output = Path(command[command.index("--output") + 1])
                paper_id = "P777"
                pdf = root / "review-library" / "uploads" / f"{paper_id}.pdf"
                markdown = root / "review-library" / "markdown" / f"{paper_id}.md"
                metadata = (
                    root
                    / "review-library"
                    / "metadata"
                    / "papers"
                    / f"{paper_id}.metadata.json"
                )
                for parent in (pdf.parent, markdown.parent, metadata.parent):
                    parent.mkdir(parents=True, exist_ok=True)
                pdf.write_bytes(staged.read_bytes())
                markdown.write_text("# Native runner output\n", encoding="utf-8")
                metadata.write_text(
                    json.dumps(
                        {
                            "paper_id": paper_id,
                            "title": {"value": "Native runner paper"},
                            "source_paths": {
                                "pdf": str(pdf),
                                "markdown": str(markdown),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                output.write_text(
                    json.dumps(
                        {
                            "status": "uploaded",
                            "paper_id": paper_id,
                            "metadata_path": str(metadata),
                            "pdf_path": str(pdf),
                            "markdown_path": str(markdown),
                            "mineru_ready": True,
                        }
                    ),
                    encoding="utf-8",
                )

        service = self.app.state.library_service
        service.precise_ingest = None
        service.scientific_runner = RecordingRunner()
        service.runtime_environment = lambda _principal: {
            "MINERU_API_TOKEN": "task-secret",
            "MINERU_BASE_URL": "https://mineru.example.test",
        }
        with TestClient(self.app) as client:
            response = self.upload(client, "native.pdf", fake_pdf(b"N"))

        self.assertEqual(201, response.status_code, response.text)
        self.assertRegex(response.json()["paper_id"], r"^P[0-9]+$")
        self.assertNotEqual("P777", response.json()["paper_id"])
        self.assertEqual(
            {"pdf", "markdown", "metadata"},
            set(response.json()["artifact_ids"]),
        )
        self.assertNotIn("task-secret", " ".join(captured["command"]))
        self.assertEqual(
            {"MINERU_API_TOKEN": "task-secret"}, captured["secret_env"]
        )
        self.assertEqual(
            "https://mineru.example.test", captured["env"]["MINERU_BASE_URL"]
        )
        self.assertTrue(callable(captured["cancel_requested"]))

    def test_library_mineru_environment_ignores_unrelated_provider_dns_failure(self) -> None:
        provider_service = self.app.state.provider_settings_service
        provider_service.allowed_hosts = ("mineru.net", "blocked.example")

        def public_resolver(host, port, *_args, **_kwargs):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", port),
                )
            ]

        with patch(
            "review_writer_api.credentials.socket.getaddrinfo",
            side_effect=public_resolver,
        ):
            provider_service.save_settings(
                self.first,
                "mineru",
                base_url="",
                model_name="",
                wire_api="",
                api_key="mineru-secret",
                enabled=True,
            )
            provider_service.save_settings(
                self.first,
                "text",
                base_url="https://blocked.example/v1",
                model_name="text-model",
                wire_api="responses",
                api_key="text-secret",
                enabled=True,
            )

        def task_resolver(host, port, *_args, **_kwargs):
            address = "93.184.216.34" if str(host).casefold() == "mineru.net" else "127.0.0.1"
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (address, port),
                )
            ]

        with patch(
            "review_writer_api.credentials.socket.getaddrinfo",
            side_effect=task_resolver,
        ):
            try:
                environment = self.app.state.library_service.runtime_environment(
                    self.first
                )
            except Exception as exc:
                self.fail(f"An unrelated provider blocked MinerU: {exc}")

        self.assertEqual({"MINERU_API_TOKEN": "mineru-secret"}, environment)

    def test_native_runner_failure_preserves_mineru_upload_error_contract(self) -> None:
        class FailingRunner:
            def run(self, _command, **_kwargs):
                raise ScientificRunFailed(
                    "MinerU subprocess failed.", attempts=1, retryable=False
                )

        service = self.app.state.library_service
        service.precise_ingest = None
        service.scientific_runner = FailingRunner()
        with TestClient(self.app) as client:
            response = self.upload(client, "native-failure.pdf", fake_pdf(b"F"))

        self.assertEqual(502, response.status_code)
        self.assertEqual(
            "MINERU_PRECISE_PARSE_FAILED", response.json()["error"]["code"]
        )

    def test_batch_upload_reports_real_outcomes(self) -> None:
        with TestClient(self.app) as client:
            responses = [
                self.upload(client, "one.pdf", fake_pdf(b"1")),
                self.upload(client, "one-copy.pdf", fake_pdf(b"1")),
                self.upload(client, "fails.pdf", fake_pdf(b"2")),
            ]
        outcomes = [response.json()["status"] for response in responses]
        self.assertEqual(["uploaded", "duplicate_file", "failed"], outcomes)

    def test_search_covers_title_author_keyword_and_tag(self) -> None:
        with TestClient(self.app) as client:
            self.upload(client, "copper.pdf", fake_pdf())
            for query in ("Copper", "Lovelace", "allene", "allenation"):
                response = client.get("/api/v1/library/papers", params={"q": query})
                self.assertEqual(1, response.json()["count"], query)

    def test_metadata_markdown_and_pdf_are_user_isolated(self) -> None:
        with TestClient(self.app) as client:
            paper = self.upload(client, "copper.pdf", fake_pdf()).json()
            paper_id = paper["paper_id"]
            pdf_artifact_id = paper["artifact_ids"]["pdf"]
            self.assertEqual(200, client.get(f"/api/v1/library/papers/{paper_id}/metadata").status_code)
            self.assertIn("allene", client.get(f"/api/v1/library/papers/{paper_id}/markdown").text)
            ranged = client.get(
                f"/api/v1/library/papers/{paper_id}/pdf",
                headers={"Range": "bytes=0-9"},
            )
            self.assertEqual(206, ranged.status_code)
            self.assertEqual(fake_pdf()[:10], ranged.content)
            self.assertEqual(
                fake_pdf(),
                client.get(f"/api/v1/artifacts/{pdf_artifact_id}/content").content,
            )

            self.current = self.second
            for suffix in ("metadata", "markdown", "pdf"):
                self.assertEqual(
                    404,
                    client.get(f"/api/v1/library/papers/{paper_id}/{suffix}").status_code,
                )
            self.assertEqual(
                404,
                client.get(f"/api/v1/artifacts/{pdf_artifact_id}/content").status_code,
            )

    def test_mineru_assets_are_served_by_versioned_user_scoped_route(self) -> None:
        with TestClient(self.app) as client:
            paper = self.upload(client, "asset.pdf", fake_pdf()).json()
            paper_id = paper["paper_id"]
            asset = client.get(
                f"/api/v1/library/papers/{paper_id}/asset",
                params={"path": "images/scheme.png"},
            )
            traversal = client.get(
                f"/api/v1/library/papers/{paper_id}/asset",
                params={"path": "../paper.pdf"},
            )
            non_image = client.get(
                f"/api/v1/library/papers/{paper_id}/asset",
                params={"path": f"{paper_id}_content_list.json"},
            )
            self.current = self.second
            isolated = client.get(
                f"/api/v1/library/papers/{paper_id}/asset",
                params={"path": "images/scheme.png"},
            )
        self.assertEqual(200, asset.status_code, asset.text)
        self.assertEqual(b"image-bytes", asset.content)
        self.assertEqual(404, traversal.status_code, traversal.text)
        self.assertEqual(404, non_image.status_code, non_image.text)
        self.assertEqual(404, isolated.status_code, isolated.text)

    def test_mineru_asset_rejects_lexical_extracted_directory_symlink(self) -> None:
        with TestClient(self.app) as client:
            paper = self.upload(client, "symlink-asset.pdf", fake_pdf()).json()
            paper_id = paper["paper_id"]
            record = self.app.state.library_service.get(self.first, paper_id)
            extracted = (
                self.app.state.hosted_workspace_manager.user_root(self.first.user_id)
                / "review-library"
                / ".artifacts"
                / paper_id
                / record.artifact_ids["mineru"]
                / "extracted"
            )
            original_is_symlink = Path.is_symlink

            def reports_extracted_symlink(path: Path) -> bool:
                return path == extracted or original_is_symlink(path)

            with patch.object(Path, "is_symlink", reports_extracted_symlink):
                response = client.get(
                    f"/api/v1/library/papers/{paper_id}/asset",
                    params={"path": "images/scheme.png"},
                )
        self.assertEqual(404, response.status_code, response.text)

    def test_metadata_edit_publishes_a_new_immutable_metadata_artifact(self) -> None:
        with TestClient(self.app) as client:
            uploaded = self.upload(client, "versioned.pdf", fake_pdf(b"V")).json()
            paper_id = uploaded["paper_id"]
            before = self.app.state.library_service.get(self.first, paper_id)
            metadata = client.get(
                f"/api/v1/library/papers/{paper_id}/metadata"
            ).json()
            metadata["title"] = {"value": "Human-reviewed title"}
            saved = client.put(
                f"/api/v1/library/papers/{paper_id}/metadata",
                json=metadata,
                headers={"Origin": "http://testserver"},
            )
            after = self.app.state.library_service.get(self.first, paper_id)

        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(
            before.artifact_ids["pdf"], after.artifact_ids["pdf"]
        )
        self.assertNotEqual(
            before.artifact_ids["metadata"], after.artifact_ids["metadata"]
        )
        root = self.settings.hosted_workspace_root / self.first.user_id
        for record in (before, after):
            metadata_path = root / Path(
                *record.metadata["_artifact_paths"]["metadata"].split("/")
            )
            self.assertTrue(metadata_path.is_file())
        self.assertEqual("Human-reviewed title", after.title)

    def test_literature_search_and_download_jobs_are_user_scoped_and_persist_results(self) -> None:
        with TestClient(self.app) as client:
            search = client.post(
                "/api/v1/library/search-jobs",
                json={"topic": "allenation", "limit": 10},
                headers={"Origin": "http://testserver", "Idempotency-Key": "search-1"},
            )
            self.assertEqual(202, search.status_code)
            search_job = self.wait_job(client, search.json()["id"])
            self.assertEqual("succeeded", search_job["status"])
            self.assertEqual("crossref:1", search_job["result"]["candidates"][0]["candidate_id"])

            download = client.post(
                "/api/v1/library/download-jobs",
                json={"candidates": search_job["result"]["candidates"]},
                headers={"Origin": "http://testserver", "Idempotency-Key": "download-1"},
            )
            download_job = self.wait_job(client, download.json()["id"])
            self.assertEqual(1, download_job["result"]["added_count"])
            final_paper_id = download_job["result"]["results"][0]["paper_id"]
            self.assertNotEqual("P900", final_paper_id)
            catalog = client.get("/api/v1/library/papers").json()
            self.assertEqual(
                [final_paper_id], [paper["paper_id"] for paper in catalog["items"]]
            )
            self.assertEqual("Downloaded native paper", catalog["items"][0]["title"])
            self.assertIn(
                "Downloaded native paper",
                client.get(
                    f"/api/v1/library/papers/{final_paper_id}/markdown"
                ).text,
            )
            self.assertEqual(
                fake_pdf(b"9"),
                client.get(f"/api/v1/library/papers/{final_paper_id}/pdf").content,
            )
            artifact_id = download_job["result"]["results"][0]["artifact_ids"]["pdf"]
            self.assertEqual(
                fake_pdf(b"9"),
                client.get(f"/api/v1/artifacts/{artifact_id}/content").content,
            )

            duplicate = client.post(
                "/api/v1/library/download-jobs",
                json={"candidates": search_job["result"]["candidates"]},
                headers={
                    "Origin": "http://testserver",
                    "Idempotency-Key": "download-duplicate",
                },
            )
            duplicate_job = self.wait_job(client, duplicate.json()["id"])
            self.assertEqual(0, duplicate_job["result"]["added_count"])
            self.assertEqual(1, duplicate_job["result"]["already_present_count"])
            self.assertEqual(
                final_paper_id,
                duplicate_job["result"]["results"][0]["paper_id"],
            )

            self.current = self.second
            self.assertEqual(404, client.get(f"/api/v1/jobs/{search_job['id']}").status_code)

    def test_library_job_payloads_reject_wrong_json_types(self) -> None:
        with TestClient(self.app) as client:
            invalid_topic = client.post(
                "/api/v1/library/search-jobs",
                json={"topic": ["not", "a", "string"]},
                headers={"Origin": "http://testserver"},
            )
            invalid_candidates = client.post(
                "/api/v1/library/download-jobs",
                json={"candidates": ["not-a-candidate-object"]},
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(422, invalid_topic.status_code)
        self.assertEqual(422, invalid_candidates.status_code)

    def test_delete_moves_owned_paper_to_trash(self) -> None:
        with TestClient(self.app) as client:
            paper = self.upload(client, "copper.pdf", fake_pdf()).json()
            stored = self.app.state.library_service.get(
                self.first, paper["paper_id"]
            )
            extracted_dir = Path(stored.metadata["source_paths"]["extracted_dir"])
            artifact_ids = {
                uuid.UUID(artifact_id) for artifact_id in paper["artifact_ids"].values()
            }
            metadata = (
                self.settings.hosted_workspace_root
                / self.first.user_id
                / "review-library"
                / "metadata"
                / "papers"
                / f"{paper['paper_id']}.metadata.json"
            )
            response = client.delete(
                f"/api/v1/library/papers/{paper['paper_id']}",
                headers={"Origin": "http://testserver"},
            )
            listing = client.get("/api/v1/library/papers").json()
        self.assertEqual(204, response.status_code)
        self.assertEqual(0, listing["count"])
        trash = self.settings.hosted_workspace_root / self.first.user_id / ".trash" / "library"
        trash_entry = next(trash.iterdir())
        self.assertEqual(5, len(list(trash_entry.iterdir())))
        self.assertFalse(extracted_dir.exists())
        self.assertTrue((trash_entry / "mineru-artifact" / "extracted").is_dir())
        self.assertFalse(metadata.exists())
        with self.sessions() as session:
            self.assertEqual(
                {"trashed"},
                {
                    artifact.availability
                    for artifact in session.query(LibraryArtifact)
                    if artifact.id in artifact_ids
                },
            )

    def test_deleted_pdf_can_be_uploaded_again_and_restores_the_catalog_row(self) -> None:
        content = fake_pdf(b"restore")
        with TestClient(self.app) as client:
            first = self.upload(client, "first.pdf", content).json()
            deleted = client.delete(
                f"/api/v1/library/papers/{first['paper_id']}",
                headers={"Origin": "http://testserver"},
            )
            restored = self.upload(client, "restored.pdf", content)
            listing = client.get("/api/v1/library/papers").json()

        self.assertEqual(204, deleted.status_code)
        self.assertEqual(201, restored.status_code, restored.text)
        self.assertEqual("restored", restored.json()["status"])
        self.assertEqual(first["paper_id"], restored.json()["paper_id"])
        self.assertEqual(1, listing["count"])
        self.assertNotEqual(
            first["artifact_ids"]["pdf"], restored.json()["artifact_ids"]["pdf"]
        )

    def test_delete_handles_migrated_metadata_artifact_and_compatibility_path_alias(self) -> None:
        with TestClient(self.app) as client:
            paper = self.upload(client, "legacy.pdf", fake_pdf(b"legacy")).json()
            root = self.settings.hosted_workspace_root / self.first.user_id
            compatibility = (
                root
                / "review-library"
                / "metadata"
                / "papers"
                / f"{paper['paper_id']}.metadata.json"
            )
            with database_session(self.sessions) as session:
                catalog = session.query(LibraryPaper).filter_by(
                    user_id=uuid.UUID(self.first.user_id),
                    paper_id=paper["paper_id"],
                ).one()
                metadata = dict(catalog.metadata_json)
                artifact_paths = dict(metadata["_artifact_paths"])
                artifact_paths["metadata"] = compatibility.relative_to(root).as_posix()
                metadata["_artifact_paths"] = artifact_paths
                catalog.metadata_json = metadata
                artifact = session.get(
                    LibraryArtifact, uuid.UUID(paper["artifact_ids"]["metadata"])
                )
                artifact.relative_path = compatibility.relative_to(root).as_posix()

            deleted = client.delete(
                f"/api/v1/library/papers/{paper['paper_id']}",
                headers={"Origin": "http://testserver"},
            )

        self.assertEqual(204, deleted.status_code, deleted.text)

    def test_download_reconciliation_rejects_the_entire_manifest_before_catalog_write(self) -> None:
        root = self.settings.hosted_workspace_root / self.first.user_id / "review-library"
        pdf = root / "downloads" / "P901.pdf"
        markdown = root / "downloads" / "P901.md"
        metadata = root / "metadata" / "papers" / "P901.metadata.json"
        pdf.parent.mkdir(parents=True, exist_ok=True); markdown.parent.mkdir(parents=True, exist_ok=True); metadata.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(fake_pdf(b"x")); markdown.write_text("# Valid", encoding="utf-8")
        metadata.write_text(json.dumps({"paper_id": "P901", "title": {"value": "Valid"}, "source_paths": {"pdf": str(pdf), "markdown": str(markdown)}}), encoding="utf-8")
        with self.assertRaises(Exception):
            self.app.state.library_service.reconcile_download_result(
                self.first,
                {"results": [{"status": "downloaded", "paper_id": "P901", "path": str(pdf), "metadata_path": str(metadata)}, {"status": "downloaded", "paper_id": "P902", "path": str(root / "missing.pdf"), "metadata_path": str(metadata)}]},
            )
        self.assertEqual(0, self.app.state.library_service.count(self.first))

    def test_download_retry_recovers_files_already_registered_by_acquisition(self) -> None:
        root = self.settings.hosted_workspace_root / self.first.user_id / "review-library"
        pdf = root / "downloads" / "P903.pdf"
        markdown = root / "downloads" / "P903.md"
        metadata = root / "metadata" / "papers" / "P903.metadata.json"
        for parent in (pdf.parent, markdown.parent, metadata.parent):
            parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(fake_pdf(b"R"))
        markdown.write_text("# Recovered retry\n", encoding="utf-8")
        metadata.write_text(
            json.dumps(
                {
                    "paper_id": "P903",
                    "title": {"value": "Recovered retry"},
                    "source_paths": {
                        "pdf": str(pdf),
                        "markdown": str(markdown),
                    },
                }
            ),
            encoding="utf-8",
        )

        records = self.app.state.library_service.reconcile_download_result(
            self.first,
            {
                "results": [
                    {"status": "already_in_library", "paper_id": "P903"}
                ]
            },
        )

        self.assertEqual(["P903"], [record.paper_id for record in records])
        self.assertEqual(1, self.app.state.library_service.count(self.first))


@unittest.skipUnless(
    os.environ.get("REVIEW_WRITER_RUN_POSTGRES_TESTS") == "1",
    "Set REVIEW_WRITER_RUN_POSTGRES_TESTS=1 for PostgreSQL Library tests.",
)
class PostgreSQLLibraryConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.sessions, self.engine = create_session_factory(database_url_from_env())
        with database_session(self.sessions) as session:
            user = User(
                email=f"library-concurrency-{uuid.uuid4().hex}@example.com",
                display_name="Library concurrency",
                password_hash="hash",
            )
            session.add(user)
            session.flush()
            self.principal = Principal(
                str(user.id), frozenset({Role.USER}), user.email
            )
        self.workspace_manager = HostedWorkspaceManager(
            Path(self.temporary.name) / "users"
        )
        self.service = LibraryService(self.sessions, self.workspace_manager)

    def tearDown(self) -> None:
        with database_session(self.sessions) as session:
            user = session.get(User, uuid.UUID(self.principal.user_id))
            if user is not None:
                session.delete(user)
        self.engine.dispose()
        self.temporary.cleanup()

    def _output(self, container: Path, seed: bytes) -> tuple[dict, str]:
        paper_id = "P001"
        container.mkdir(parents=True, exist_ok=False)
        pdf = container / f"{paper_id}.pdf"
        markdown = container / f"{paper_id}.md"
        metadata_path = container / f"{paper_id}.metadata.json"
        pdf.write_bytes(fake_pdf(seed))
        markdown.write_text(f"# Concurrent {seed.decode()}\n", encoding="utf-8")
        metadata = {
            "paper_id": paper_id,
            "title": {"value": f"Concurrent {seed.decode()}"},
            "source_paths": {"pdf": str(pdf), "markdown": str(markdown)},
        }
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        return {
            "status": "uploaded",
            "paper_id": paper_id,
            "metadata_path": str(metadata_path),
            "pdf_path": str(pdf),
            "markdown_path": str(markdown),
            "mineru_ready": True,
        }, self.service._digest(pdf)

    def test_two_isolated_uploads_never_share_a_paper_identity_or_artifact(self) -> None:
        root = self.workspace_manager.user_root(self.principal.user_id)
        outputs = [
            self._output(
                root
                / "review-library"
                / ".upload-staging"
                / uuid.uuid4().hex
                / "parse-workspace",
                seed,
            )
            for seed in (b"A", b"B")
        ]

        def admit(item):
            result, digest = item
            return self.service._record_parsed_result(
                self.principal, "concurrent.pdf", digest, result
            )[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            records = list(executor.map(admit, outputs))

        self.assertEqual(2, len({record.paper_id for record in records}))
        self.assertEqual(2, len({record.artifact_ids["pdf"] for record in records}))
        for record in records:
            self.assertIn("review-library/.artifacts/", record.pdf_relative_path)
            self.assertEqual(
                record.content_sha256,
                self.service._digest(
                    self.workspace_manager.user_root(self.principal.user_id)
                    / Path(*record.pdf_relative_path.split("/"))
                ),
            )

    def test_upload_and_download_outputs_cannot_claim_the_same_paper_identity(self) -> None:
        root = self.workspace_manager.user_root(self.principal.user_id)
        upload, upload_digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"U",
        )
        download, _download_digest = self._output(
            root
            / ".review-writer"
            / "job-staging"
            / uuid.uuid4().hex
            / "library-workspace",
            b"D",
        )
        download_result = {
            "results": [
                {
                    "status": "downloaded",
                    "paper_id": "P001",
                    "path": download["pdf_path"],
                    "metadata_path": download["metadata_path"],
                }
            ]
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            upload_future = executor.submit(
                self.service._record_parsed_result,
                self.principal,
                "upload.pdf",
                upload_digest,
                upload,
            )
            download_future = executor.submit(
                self.service.reconcile_download_result,
                self.principal,
                download_result,
            )
            uploaded = upload_future.result(timeout=15)[0]
            downloaded = download_future.result(timeout=15)[0]

        self.assertNotEqual(uploaded.paper_id, downloaded.paper_id)
        self.assertNotEqual(
            uploaded.artifact_ids["pdf"], downloaded.artifact_ids["pdf"]
        )

    def test_soft_deleted_digest_is_restored_with_new_artifact_versions(self) -> None:
        root = self.workspace_manager.user_root(self.principal.user_id)
        first_output, digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"R",
        )
        first, first_outcome = self.service._record_parsed_result(
            self.principal, "first.pdf", digest, first_output
        )
        self.service.delete(self.principal, first.paper_id)
        restored_output, restored_digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"R",
        )
        restored, restored_outcome = self.service._record_parsed_result(
            self.principal, "restored.pdf", restored_digest, restored_output
        )

        self.assertEqual("uploaded", first_outcome)
        self.assertEqual("restored", restored_outcome)
        self.assertEqual(first.paper_id, restored.paper_id)
        self.assertNotEqual(
            first.artifact_ids["pdf"], restored.artifact_ids["pdf"]
        )
        self.assertEqual(1, self.service.count(self.principal))
        with database_session(self.sessions) as session:
            artifacts = list(
                session.query(LibraryArtifact).filter(
                    LibraryArtifact.user_id == uuid.UUID(self.principal.user_id),
                    LibraryArtifact.kind == "pdf",
                )
            )
        self.assertEqual(
            {"trashed", "available"},
            {artifact.availability for artifact in artifacts},
        )

    def test_delete_locks_catalog_before_same_digest_upload_can_decide(self) -> None:
        root = self.workspace_manager.user_root(self.principal.user_id)
        first_output, digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"L",
        )
        first = self.service._record_parsed_result(
            self.principal, "first.pdf", digest, first_output
        )[0]
        replacement, replacement_digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"L",
        )
        entered_delete = Event()
        release_delete = Event()
        original_safe_path = self.service._safe_stored_path

        def pause_inside_delete(user_root, relative_path):
            path = original_safe_path(user_root, relative_path)
            if not entered_delete.is_set():
                entered_delete.set()
                release_delete.wait(timeout=5)
            return path

        self.service._safe_stored_path = pause_inside_delete
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            delete_future = executor.submit(
                self.service.delete, self.principal, first.paper_id
            )
            self.assertTrue(entered_delete.wait(timeout=5))
            restore_future = executor.submit(
                self.service._record_parsed_result,
                self.principal,
                "replacement.pdf",
                replacement_digest,
                replacement,
            )
            try:
                time.sleep(0.15)
                self.assertFalse(
                    restore_future.done(),
                    "same-digest upload decided while delete was moving current files",
                )
            finally:
                release_delete.set()
            delete_future.result(timeout=10)
            restored, outcome = restore_future.result(timeout=10)

        self.assertEqual("restored", outcome)
        self.assertEqual(first.paper_id, restored.paper_id)
        self.assertTrue(self.service.file(self.principal, restored.paper_id, "pdf").is_file())

    def test_delete_locks_catalog_before_same_digest_download_can_reconcile(self) -> None:
        root = self.workspace_manager.user_root(self.principal.user_id)
        first_output, digest = self._output(
            root
            / "review-library"
            / ".upload-staging"
            / uuid.uuid4().hex
            / "parse-workspace",
            b"Q",
        )
        first = self.service._record_parsed_result(
            self.principal, "first.pdf", digest, first_output
        )[0]
        replacement, _replacement_digest = self._output(
            root
            / ".review-writer"
            / "job-staging"
            / uuid.uuid4().hex
            / "library-workspace",
            b"Q",
        )
        result = {
            "results": [
                {
                    "status": "downloaded",
                    "paper_id": "P001",
                    "path": replacement["pdf_path"],
                    "metadata_path": replacement["metadata_path"],
                }
            ]
        }
        entered_delete = Event()
        release_delete = Event()
        original_safe_path = self.service._safe_stored_path

        def pause_inside_delete(user_root, relative_path):
            path = original_safe_path(user_root, relative_path)
            if not entered_delete.is_set():
                entered_delete.set()
                release_delete.wait(timeout=5)
            return path

        self.service._safe_stored_path = pause_inside_delete
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            delete_future = executor.submit(
                self.service.delete, self.principal, first.paper_id
            )
            self.assertTrue(entered_delete.wait(timeout=5))
            restore_future = executor.submit(
                self.service.reconcile_download_result, self.principal, result
            )
            try:
                time.sleep(0.15)
                self.assertFalse(
                    restore_future.done(),
                    "download reconciliation decided while delete was moving current files",
                )
            finally:
                release_delete.set()
            delete_future.result(timeout=10)
            restored = restore_future.result(timeout=10)[0]

        self.assertEqual(first.paper_id, restored.paper_id)
        self.assertEqual("restored", result["results"][0]["catalog_outcome"])
        self.assertTrue(self.service.file(self.principal, restored.paper_id, "pdf").is_file())


if __name__ == "__main__":
    unittest.main()
