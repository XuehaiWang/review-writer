#!/usr/bin/env python3
"""Focused checks for the online literature acquisition boundary."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "review-literature-acquisition" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from literature_acquisition import (  # noqa: E402
    acquire_candidate,
    normalize_doi,
    register_download,
    resolve_crossref,
    resolve_europe_pmc,
    resolve_pdf_sources,
    resolve_semantic_scholar,
    search_crossref,
    validate_pdf_file,
    validate_public_url,
)


def check_normalization_and_ranking() -> None:
    def fake_request(_url: str, **_kwargs):
        return {
            "message": {
                "items": [
                    {
                        "DOI": "https://doi.org/10.1000/EXAMPLE",
                        "title": ["Catalytic synthesis of axially chiral allenes"],
                        "abstract": "<jats:p>Enantioselective allene synthesis.</jats:p>",
                        "author": [{"given": "A.", "family": "Chemist"}],
                        "container-title": ["Journal of Test Chemistry"],
                        "issued": {"date-parts": [[2024, 1, 1]]},
                        "is-referenced-by-count": 12,
                    },
                    {
                        "DOI": "10.1000/unrelated",
                        "title": ["A study of urban drainage"],
                        "issued": {"date-parts": [[2024]]},
                    },
                ]
            }
        }

    rows = search_crossref(
        "enantioselective synthesis of axially chiral allenes",
        limit=2,
        request_json=fake_request,
    )
    assert rows[0]["doi"] == "10.1000/example"
    assert rows[0]["score"] > rows[1]["score"]
    assert normalize_doi("DOI: 10.1000/EXAMPLE.") == "10.1000/example"


def check_network_boundary() -> None:
    private = [(None, None, None, None, ("127.0.0.1", 443))]
    with patch("literature_acquisition.socket.getaddrinfo", return_value=private):
        try:
            validate_public_url("https://example.test/paper.pdf")
        except ValueError as exc:
            assert "non-public" in str(exc)
        else:
            raise AssertionError("Private destination was accepted.")
    public = [(None, None, None, None, ("93.184.216.34", 443))]
    with patch("literature_acquisition.socket.getaddrinfo", return_value=public):
        assert validate_public_url("https://example.test/paper.pdf").startswith("https://")
    fake_proxy = [
        (None, None, None, None, ("198.18.0.5", 443)),
        (None, None, None, None, ("fdfe:dcba:9876::19", 443)),
    ]
    with (
        patch("literature_acquisition.socket.getaddrinfo", return_value=fake_proxy),
        patch("literature_acquisition._resolve_public_doh", return_value=[] ) as doh,
    ):
        assert validate_public_url("https://example.test/paper.pdf").startswith("https://")
        doh.assert_called_once_with("example.test")


def check_provider_resolution() -> None:
    crossref = resolve_crossref(
        {
            "crossref_pdf_url": "https://publisher.example/paper.pdf",
            "license_urls": ["https://creativecommons.org/licenses/by/4.0/"],
            "landing_url": "https://doi.org/10.1000/example",
        }
    )
    assert crossref["status"] == "open_access_pdf"
    assert crossref["provider"] == "crossref"
    tdm_only = resolve_crossref(
        {
            "crossref_pdf_url": "https://publisher.example/paper.pdf",
            "license_urls": ["https://publisher.example/text-and-data-mining"],
        }
    )
    assert tdm_only["status"] == "license_not_confirmed"

    europe = resolve_europe_pmc(
        "10.1000/example",
        request_json=lambda *_args, **_kwargs: {
            "resultList": {
                "result": [
                    {
                        "doi": "10.1000/example",
                        "isOpenAccess": "Y",
                        "pmcid": "PMC123",
                        "fullTextUrlList": {
                            "fullTextUrl": [
                                {
                                    "documentStyle": "pdf",
                                    "url": "https://europepmc.org/articles/PMC123/bin/paper.pdf",
                                }
                            ]
                        },
                    }
                ]
            }
        },
    )
    assert europe["status"] == "open_access_pdf"
    assert europe["provider"] == "europe_pmc"

    semantic = resolve_semantic_scholar(
        "10.1000/example",
        request_json=lambda *_args, **_kwargs: {
            "paperId": "paper-123",
            "isOpenAccess": True,
            "openAccessPdf": {
                "url": "https://repository.example/paper.pdf",
                "license": "CCBY",
                "status": "GREEN",
            },
        },
    )
    assert semantic["status"] == "open_access_pdf"
    assert semantic["provider"] == "semantic_scholar"


def check_optional_unpaywall_and_download_fallback() -> None:
    candidate = {
        "candidate_id": "fallback-candidate",
        "doi": "10.1000/fallback",
        "title": "Fallback paper",
        "authors": [],
        "year": 2024,
        "journal": "Journal",
        "abstract": "",
    }
    with (
        patch("literature_acquisition.resolve_europe_pmc", return_value={
            "status": "open_access_pdf",
            "provider": "europe_pmc",
            "pdf_url": "https://first.example/paper.pdf",
        }),
        patch("literature_acquisition.resolve_semantic_scholar", return_value={
            "status": "open_access_pdf",
            "provider": "semantic_scholar",
            "pdf_url": "https://second.example/paper.pdf",
        }),
        patch("literature_acquisition.resolve_unpaywall") as unpaywall,
    ):
        sources, attempts = resolve_pdf_sources(candidate, email="")
    assert [row["provider"] for row in sources] == ["europe_pmc", "semantic_scholar"]
    assert any(row["provider"] == "unpaywall" and row["status"] == "skipped" for row in attempts)
    unpaywall.assert_not_called()

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "review-library" / "metadata" / "papers").mkdir(parents=True)
        download_count = {"value": 0}

        def fake_download(_url: str, output_path: Path, **_kwargs):
            download_count["value"] += 1
            if download_count["value"] == 1:
                raise ValueError("first source returned HTML")
            output_path.write_bytes(b"%PDF-1.4\n" + b"0" * 2048 + b"\n%%EOF\n")
            return {
                "path": str(output_path),
                "source_url": _url,
                "size_bytes": output_path.stat().st_size,
                "sha256": "ignored",
            }

        with (
            patch("literature_acquisition.resolve_pdf_sources", return_value=(
                [
                    {
                        "status": "open_access_pdf",
                        "provider": "europe_pmc",
                        "pdf_url": "https://first.example/paper.pdf",
                    },
                    {
                        "status": "open_access_pdf",
                        "provider": "semantic_scholar",
                        "pdf_url": "https://second.example/paper.pdf",
                    },
                ],
                [],
            )),
            patch("literature_acquisition.download_pdf", side_effect=fake_download),
        ):
            result = acquire_candidate(root, candidate, email="")
        assert result["status"] == "downloaded"
        assert result["provider"] == "semantic_scholar"
        assert download_count["value"] == 2


def check_pdf_and_registration() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "review-library" / "metadata" / "papers").mkdir(parents=True)
        source = root / "staged.pdf"
        source.write_bytes(b"%PDF-1.4\n" + b"0" * 2048 + b"\n%%EOF\n")
        validation = validate_pdf_file(source)
        assert validation["size_bytes"] > 1024
        result = register_download(
            root,
            {
                "candidate_id": "candidate-one",
                "doi": "10.1000/example",
                "title": "Example open paper",
                "authors": ["A. Chemist"],
                "year": 2024,
                "journal": "Journal of Test Chemistry",
                "abstract": "Abstract.",
            },
            {
                "source_url": "https://repository.example/paper.pdf",
                "license": "cc-by",
                "host_type": "repository",
                "version": "acceptedVersion",
            },
            source,
        )
        assert result["status"] == "downloaded"
        metadata = json.loads(
            (root / "review-library" / "metadata" / "papers" / "P001.metadata.json").read_text(
                encoding="utf-8"
            )
        )
        assert metadata["doi"]["value"] == "10.1000/example"
        assert metadata["human_review"]["status"] == "not_reviewed"
        assert Path(metadata["source_paths"]["pdf"]).is_file()
        assert (root / "review-library" / "registry" / "papers.jsonl").is_file()


def main() -> int:
    checks = [
        ("normalization/ranking", check_normalization_and_ranking),
        ("network boundary", check_network_boundary),
        ("provider resolution", check_provider_resolution),
        ("optional Unpaywall/fallback", check_optional_unpaywall_and_download_fallback),
        ("PDF/registration", check_pdf_and_registration),
    ]
    for name, check in checks:
        check()
        print(f"PASS {name}")
    print(f"PASS {len(checks)}/{len(checks)} literature acquisition checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
