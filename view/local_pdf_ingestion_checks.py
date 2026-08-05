#!/usr/bin/env python3
"""Focused checks for first-stage local PDF upload and downstream reuse."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ingestion = load_module("local_pdf_ingestion_checks_target", ROOT / "view" / "local_pdf_ingestion.py")


def check_filename_boundary() -> None:
    assert ingestion.sanitize_pdf_filename(r"C:\fake\paper?.pdf") == "paper_.pdf"
    try:
        ingestion.sanitize_pdf_filename("notes.txt")
    except ValueError as exc:
        assert "Only .pdf" in str(exc)
    else:
        raise AssertionError("A non-PDF filename passed validation.")


def check_ingestion_and_downstream_reuse() -> None:
    sample = ROOT / "examples" / "reference-reviews" / "allenation-of-terminal-alkynes-with-aldehydes-and-ketones.pdf"
    assert sample.is_file(), sample
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        result = ingestion.ingest_local_pdf(review_root, sample.name, sample)
        assert result["status"] == "uploaded"
        assert result["paper_id"] == "P001"
        assert result["page_count"] > 0
        assert result["extracted_text_chars"] > 1000

        metadata_path = review_root / "review-library" / "metadata" / "papers" / "P001.metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["paper_id"] == "P001"
        assert metadata["title"]["value"]
        assert metadata["source_file"]["original_upload_name"] == sample.name
        assert metadata["extraction"]["mode"] == "local_pdf_upload+pypdf"
        assert Path(metadata["source_paths"]["pdf"]).is_file()
        assert Path(metadata["source_paths"]["markdown"]).stat().st_size > 1000

        registry = review_root / "review-library" / "registry" / "papers.jsonl"
        registry_rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()]
        assert registry_rows[0]["paper_id"] == "P001"
        assert registry_rows[0]["parse_status"] == "done"

        discover = load_module(
            "local_pdf_discovery_checks_target",
            ROOT / "skills" / "review-topic-paper-discovery" / "scripts" / "discover.py",
        )
        papers = discover.load_metadata(review_root)
        assert "P001" in papers
        assert len(discover.markdown_signal(papers["P001"])) > 1000
        grouped, _ = discover.local_search_by_keyword(
            papers,
            [{"keyword": "allenes", "category": "product"}],
            "allene synthesis",
            discover.load_classification_rules(review_root),
        )
        assert any(row["paper_id"] == "P001" for row in grouped[0]["local_results"])

        section_writer = load_module(
            "local_pdf_section_checks_target",
            ROOT
            / "skills"
            / "review-section-drafting-figure-picking"
            / "scripts"
            / "generate_section_drafts.py",
        )
        evidence = section_writer.paper_evidence(
            review_root,
            {"P001": {"title": metadata["title"]["value"]}},
            "P001",
        )
        assert len(evidence["evidence"]) > 1000

        duplicate = ingestion.ingest_local_pdf(review_root, "duplicate.pdf", sample)
        assert duplicate["status"] == "duplicate_file"
        assert duplicate["paper_id"] == "P001"
        assert len(list(metadata_path.parent.glob("P*.metadata.json"))) == 1


def check_dashboard_wiring() -> None:
    html = (ROOT / "view" / "assets" / "dashboard" / "library.html").read_text(encoding="utf-8")
    server = (ROOT / "view" / "serve_review_dashboard.py").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-workflow.txt").read_text(encoding="utf-8")
    assert 'id="localPdfInput"' in html and "multiple" in html
    assert "/api/library/upload-pdf?filename=" in html
    assert 'parsed.path == "/api/library/upload-pdf"' in server
    assert "pypdf" in requirements.casefold()


def check_http_upload_boundary() -> None:
    sample = ROOT / "examples" / "reference-reviews" / "allenation-of-terminal-alkynes-with-aldehydes-and-ketones.pdf"
    dashboard = load_module("local_pdf_dashboard_checks_target", ROOT / "view" / "serve_review_dashboard.py")
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        (review_root / "review-library" / "metadata" / "papers").mkdir(parents=True)
        dashboard.DashboardHandler.review_root = review_root
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/library/upload-pdf?filename=sample.pdf",
                data=sample.read_bytes(),
                method="POST",
                headers={"Content-Type": "application/pdf"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                assert response.status == 201
                payload = json.loads(response.read().decode("utf-8"))
            assert payload["ok"] is True and payload["paper_id"] == "P001"
            assert (review_root / "review-library" / "uploads" / "P001.md").is_file()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> int:
    checks = [
        ("filename boundary", check_filename_boundary),
        ("ingestion/downstream reuse", check_ingestion_and_downstream_reuse),
        ("dashboard wiring", check_dashboard_wiring),
        ("HTTP upload boundary", check_http_upload_boundary),
    ]
    for name, check in checks:
        check()
        print(f"PASS {name}")
    print(f"PASS {len(checks)}/{len(checks)} local PDF upload checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
