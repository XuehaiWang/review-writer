from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from review_writer_api.app import create_app
from review_writer_api.config import ApiSettings
from review_writer_api.database import Base, User
from review_writer_api.security import Principal, Role


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
            metadata = {
                "paper_id": paper_id,
                "title": {"value": f"Copper catalysis {paper_id}"},
                "authors": {"value": ["Ada Lovelace"]},
                "keywords": {"value": ["allene"]},
                "structured_tags": {"value": {"reaction_type": "allenation"}},
                "source_paths": {
                    "pdf": str(pdf_path),
                    "markdown": str(md_path),
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
            root = self.settings.hosted_workspace_root / _context.user_id / "review-library"
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
            self.assertEqual(200, client.get(f"/api/v1/library/papers/{paper_id}/metadata").status_code)
            self.assertIn("allene", client.get(f"/api/v1/library/papers/{paper_id}/markdown").text)
            ranged = client.get(
                f"/api/v1/library/papers/{paper_id}/pdf",
                headers={"Range": "bytes=0-9"},
            )
            self.assertEqual(206, ranged.status_code)
            self.assertEqual(fake_pdf()[:10], ranged.content)

            self.current = self.second
            for suffix in ("metadata", "markdown", "pdf"):
                self.assertEqual(
                    404,
                    client.get(f"/api/v1/library/papers/{paper_id}/{suffix}").status_code,
                )

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
            catalog = client.get("/api/v1/library/papers").json()
            self.assertEqual(["P900"], [paper["paper_id"] for paper in catalog["items"]])
            self.assertEqual("Downloaded native paper", catalog["items"][0]["title"])
            self.assertIn(
                "Downloaded native paper",
                client.get("/api/v1/library/papers/P900/markdown").text,
            )
            self.assertEqual(
                fake_pdf(b"9"),
                client.get("/api/v1/library/papers/P900/pdf").content,
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
            response = client.delete(
                f"/api/v1/library/papers/{paper['paper_id']}",
                headers={"Origin": "http://testserver"},
            )
            listing = client.get("/api/v1/library/papers").json()
        self.assertEqual(204, response.status_code)
        self.assertEqual(0, listing["count"])
        trash = self.settings.hosted_workspace_root / self.first.user_id / ".trash" / "library"
        self.assertTrue(any(trash.iterdir()))


if __name__ == "__main__":
    unittest.main()
