#!/usr/bin/env python3
"""Focused checks for first-stage local PDF upload and downstream reuse."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
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


def fake_mineru_runner(review_root: Path, pdf_path: Path, slug: str) -> dict[str, object]:
    output = Path(review_root) / "mineru-outputs"
    extracted = output / "extracted" / slug
    markdown = output / "markdown" / f"{slug}.md"
    manifest = output / "manifests" / f"{slug}.json"
    extracted.mkdir(parents=True, exist_ok=True)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    reader = ingestion._pdf_reader(pdf_path)
    page_texts, _ = ingestion._extract_pages(reader)
    title = ingestion._title_from_text("\n".join(page_texts), pdf_path.name)
    markdown_text = ingestion._markdown_document(title, page_texts)
    markdown.write_text(markdown_text, encoding="utf-8")
    full_md = extracted / "full.md"
    full_md.write_text(markdown_text, encoding="utf-8")
    blocks = [
        {"type": "text", "text": text, "page_idx": index}
        for index, text in enumerate(page_texts)
        if text
    ]
    content_list = extracted / f"{slug}_content_list.json"
    content_list.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    manifest.write_text(json.dumps({"completed": [{"slug": slug}]}), encoding="utf-8")
    return {
        "slug": slug,
        "markdown_copy": markdown,
        "extracted_dir": extracted,
        "content_list": content_list,
        "full_md": full_md,
        "manifest_path": manifest,
        "content_block_count": len(blocks),
    }


def check_filename_boundary() -> None:
    assert ingestion.sanitize_pdf_filename(r"C:\fake\paper?.pdf") == "paper_.pdf"
    try:
        ingestion.sanitize_pdf_filename("notes.txt")
    except ValueError as exc:
        assert "Only .pdf" in str(exc)
    else:
        raise AssertionError("A non-PDF filename passed validation.")


def check_mineru_inherits_task_scoped_environment() -> None:
    sentinel = "task-scoped-mineru-token"
    original = os.environ.get("MINERU_API_TOKEN")
    original_run = ingestion.subprocess.run
    captured: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        pdf_path = review_root / "paper.pdf"
        pdf_path.write_bytes(b"%PDF-1.7\n%%EOF")
        (review_root / ".env").write_text(
            "MINERU_API_TOKEN=workspace-file-token\n",
            encoding="utf-8",
        )

        def fake_run(command, **kwargs):
            captured.update(kwargs["env"])
            artifacts = ingestion._mineru_artifact_paths(review_root, "paper")
            artifacts["markdown"].parent.mkdir(parents=True, exist_ok=True)
            artifacts["extracted_dir"].mkdir(parents=True, exist_ok=True)
            artifacts["manifest"].parent.mkdir(parents=True, exist_ok=True)
            artifacts["markdown"].write_text("# Parsed\n", encoding="utf-8")
            (artifacts["extracted_dir"] / "full.md").write_text(
                "# Parsed\n", encoding="utf-8"
            )
            (artifacts["extracted_dir"] / "paper_content_list.json").write_text(
                "[]", encoding="utf-8"
            )
            artifacts["manifest"].write_text("{}", encoding="utf-8")
            return ingestion.subprocess.CompletedProcess(command, 0, "", "")

        os.environ["MINERU_API_TOKEN"] = sentinel
        ingestion.subprocess.run = fake_run
        try:
            ingestion._run_mineru_parser(review_root, pdf_path, "paper")
        finally:
            ingestion.subprocess.run = original_run
            if original is None:
                os.environ.pop("MINERU_API_TOKEN", None)
            else:
                os.environ["MINERU_API_TOKEN"] = original

    assert captured["MINERU_API_TOKEN"] == sentinel


def check_ingestion_and_downstream_reuse() -> None:
    sample = ROOT / "examples" / "reference-reviews" / "allenation-of-terminal-alkynes-with-aldehydes-and-ketones.pdf"
    assert sample.is_file(), sample
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        original_runner = ingestion._run_mineru_parser
        ingestion._run_mineru_parser = fake_mineru_runner
        try:
            result = ingestion.ingest_local_pdf(review_root, sample.name, sample)
        finally:
            ingestion._run_mineru_parser = original_runner
        assert result["status"] == "uploaded"
        assert result["paper_id"] == "P001"
        assert result["page_count"] > 0
        assert result["extracted_text_chars"] > 1000
        assert result["mineru_ready"] is True

        metadata_path = review_root / "review-library" / "metadata" / "papers" / "P001.metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["paper_id"] == "P001"
        assert metadata["title"]["value"]
        assert metadata["source_file"]["original_upload_name"] == sample.name
        assert metadata["extraction"]["mode"] == "local_pdf_upload+mineru_precise"
        assert Path(metadata["source_paths"]["pdf"]).is_file()
        assert Path(metadata["source_paths"]["markdown"]).stat().st_size > 1000
        assert Path(metadata["source_paths"]["content_list"]).is_file()
        assert Path(metadata["source_paths"]["extracted_dir"]).is_dir()

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


def check_figure_inventory_rejects_incomplete_mineru_metadata() -> None:
    inventory_module = load_module(
        "local_pdf_inventory_checks_target",
        ROOT
        / "skills"
        / "review-section-drafting-figure-picking"
        / "scripts"
        / "build_paper_figure_inventory.py",
    )
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        project = review_root / "review-projects" / "test" / "00_discovery"
        metadata_dir = review_root / "review-library" / "metadata" / "papers"
        project.mkdir(parents=True)
        metadata_dir.mkdir(parents=True)
        (project / "selected_discovery_results.json").write_text(
            json.dumps({"selected_papers": [{"paper_id": "P001", "keep": True}]}),
            encoding="utf-8",
        )
        (metadata_dir / "P001.metadata.json").write_text(
            json.dumps(
                {
                    "paper_id": "P001",
                    "title": {"value": "Incomplete upload"},
                    "source_paths": {"pdf": None, "markdown": None, "content_list": None, "extracted_dir": None},
                }
            ),
            encoding="utf-8",
        )
        inventory = inventory_module.build_inventory(review_root, "test")
        assert inventory["papers"][0]["status"] == "mineru_not_ready"
        assert inventory["papers"][0]["candidate_count"] == 0


def check_legacy_upload_is_upgraded_in_place() -> None:
    sample = ROOT / "examples" / "reference-reviews" / "allenation-of-terminal-alkynes-with-aldehydes-and-ketones.pdf"
    with tempfile.TemporaryDirectory() as raw:
        review_root = Path(raw)
        upload_dir = review_root / "review-library" / "uploads"
        metadata_dir = review_root / "review-library" / "metadata" / "papers"
        upload_dir.mkdir(parents=True)
        metadata_dir.mkdir(parents=True)
        target_pdf = upload_dir / "P001.pdf"
        target_pdf.write_bytes(sample.read_bytes())
        legacy = {
            "paper_id": "P001",
            "source_file": {"sha256": ingestion.sha256_file(sample)},
            "source_paths": {
                "pdf": str(target_pdf),
                "markdown": str(upload_dir / "P001.md"),
                "content_list": None,
                "extracted_dir": None,
            },
            "extraction": {"mode": "local_pdf_upload+pypdf"},
            "human_review": {"status": "reviewed", "reviewed_at": "2026-01-01T00:00:00Z", "reviewer": "tester", "notes": []},
        }
        (metadata_dir / "P001.metadata.json").write_text(json.dumps(legacy), encoding="utf-8")
        original_runner = ingestion._run_mineru_parser
        ingestion._run_mineru_parser = fake_mineru_runner
        try:
            result = ingestion.ingest_local_pdf(review_root, sample.name, sample)
        finally:
            ingestion._run_mineru_parser = original_runner
        assert result["status"] == "upgraded_to_mineru"
        assert result["paper_id"] == "P001"
        metadata = json.loads((metadata_dir / "P001.metadata.json").read_text(encoding="utf-8"))
        assert metadata["extraction"]["mode"] == "local_pdf_upload+mineru_precise"
        assert Path(metadata["source_paths"]["content_list"]).is_file()
        assert metadata["human_review"]["status"] == "reviewed"


def main() -> int:
    checks = [
        ("filename boundary", check_filename_boundary),
        ("task-scoped MinerU environment", check_mineru_inherits_task_scoped_environment),
        ("ingestion/downstream reuse", check_ingestion_and_downstream_reuse),
        ("incomplete MinerU inventory guard", check_figure_inventory_rejects_incomplete_mineru_metadata),
        ("legacy upload MinerU upgrade", check_legacy_upload_is_upgraded_in_place),
    ]
    for name, check in checks:
        check()
        print(f"PASS {name}")
    print(f"PASS {len(checks)}/{len(checks)} local PDF upload checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
