#!/usr/bin/env python3
"""Topic search and lawful open-access PDF acquisition for review-writer."""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _review_runtime.paths import resolve_review_root


CROSSREF_API = "https://api.crossref.org/works"
EUROPE_PMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
CLOUDFLARE_DOH_API = "https://cloudflare-dns.com/dns-query"
DEFAULT_TIMEOUT = 25
DEFAULT_MAX_BYTES = 80 * 1024 * 1024
_REGISTER_LOCK = threading.RLock()
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+\-]{1,}", re.I)
_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_dotenv_if_present(review_root: Path) -> None:
    path = Path(review_root) / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("'\""))


def normalize_doi(value: object) -> str:
    doi = _DOI_PREFIX_RE.sub("", str(value or "").strip()).strip().rstrip(".,;")
    return doi.casefold()


def _plain_text(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: object) -> set[str]:
    stop = {
        "and", "the", "for", "from", "with", "into", "using", "via", "of", "in", "on",
        "a", "an", "to", "by", "review", "paper", "article", "study", "synthesis",
    }
    return {token.casefold() for token in _TOKEN_RE.findall(_plain_text(value)) if token.casefold() not in stop}


def _first_year(item: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                pass
    return None


def _authors(item: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(str(author.get(key) or "").strip() for key in ("given", "family")).strip()
        if name:
            names.append(name)
    return names


def _direct_pdf_link(item: dict[str, Any]) -> str:
    for entry in item.get("link") or []:
        if not isinstance(entry, dict):
            continue
        if "pdf" in str(entry.get("content-type") or "").casefold() and entry.get("URL"):
            return str(entry["URL"])
    return ""


def _candidate_score(topic: str, title: str, abstract: str, year: int | None, cited: int) -> float:
    wanted = _tokens(topic)
    title_tokens = _tokens(title)
    abstract_tokens = _tokens(abstract)
    if not wanted:
        return 0.0
    title_overlap = len(wanted & title_tokens) / len(wanted)
    body_overlap = len(wanted & abstract_tokens) / len(wanted)
    score = 0.72 * title_overlap + 0.18 * body_overlap
    if title_tokens and wanted.issubset(title_tokens | abstract_tokens):
        score += 0.08
    if year and year >= datetime.now().year - 5:
        score += 0.02
    score += min(max(cited, 0), 500) / 500 * 0.04
    return round(min(score, 1.0), 4)


def _json_request(url: str, *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
        raw = response.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise RuntimeError("Provider response exceeded 5 MB.")
        return json.loads(raw.decode("utf-8"))


def search_crossref(
    topic: str,
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 20,
    mailto: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    request_json: Callable[..., dict[str, Any]] = _json_request,
) -> list[dict[str, Any]]:
    topic = _plain_text(topic)
    if len(topic) < 3:
        raise ValueError("Enter a more specific literature topic.")
    if year_from and year_to and year_from > year_to:
        raise ValueError("The starting year cannot be later than the ending year.")
    limit = max(1, min(int(limit), 50))
    params: dict[str, str] = {
        "query.bibliographic": topic,
        "rows": str(limit),
        "filter": "type:journal-article",
        "select": (
            "DOI,title,author,published-print,published-online,published,issued,created,"
            "container-title,URL,link,is-referenced-by-count,type,license,abstract"
        ),
    }
    filters = [params["filter"]]
    if year_from:
        filters.append(f"from-pub-date:{int(year_from)}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{int(year_to)}-12-31")
    params["filter"] = ",".join(filters)
    if mailto:
        params["mailto"] = mailto
    url = CROSSREF_API + "?" + urllib.parse.urlencode(params)
    identity = mailto or "contact-not-configured"
    data = request_json(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"review-writer-literature-acquisition/1.0 (mailto:{identity})",
        },
        timeout=timeout,
    )
    results: list[dict[str, Any]] = []
    for item in (data.get("message") or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        doi = normalize_doi(item.get("DOI"))
        title = _plain_text(" ".join(item.get("title") or [])) or "(untitled)"
        abstract = _plain_text(item.get("abstract"))
        journal = _plain_text(" ".join(item.get("container-title") or []))
        year = _first_year(item)
        cited = int(item.get("is-referenced-by-count") or 0)
        candidate_key = doi or f"{title}|{year or ''}"
        results.append(
            {
                "candidate_id": hashlib.sha256(candidate_key.casefold().encode("utf-8")).hexdigest()[:16],
                "doi": doi,
                "title": title,
                "authors": _authors(item),
                "year": year,
                "journal": journal,
                "abstract": abstract[:2000],
                "landing_url": str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
                "crossref_pdf_url": _direct_pdf_link(item),
                "license_urls": [
                    str(entry.get("URL"))
                    for entry in item.get("license") or []
                    if isinstance(entry, dict) and entry.get("URL")
                ],
                "citation_count": cited,
                "score": _candidate_score(topic, title, abstract, year, cited),
                "source": "crossref",
                "download_status": "not_started",
            }
        )
    deduped: dict[str, dict[str, Any]] = {}
    for row in results:
        key = f"doi:{row['doi']}" if row["doi"] else f"title:{row['title'].casefold()}:{row['year']}"
        current = deduped.get(key)
        if current is None or row["score"] > current["score"]:
            deduped[key] = row
    return sorted(
        deduped.values(),
        key=lambda row: (row["score"], row.get("citation_count") or 0, row.get("year") or 0),
        reverse=True,
    )


def resolve_unpaywall(
    doi: str,
    email: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    request_json: Callable[..., dict[str, Any]] = _json_request,
) -> dict[str, Any]:
    doi = normalize_doi(doi)
    email = str(email or "").strip()
    if not doi:
        return {"status": "no_doi", "error": "The selected candidate has no DOI."}
    if not email:
        return {
            "status": "configuration_required",
            "error": "Enter an email for Unpaywall identification or set UNPAYWALL_EMAIL.",
        }
    url = f"{UNPAYWALL_API}/{urllib.parse.quote(doi, safe='')}?{urllib.parse.urlencode({'email': email})}"
    try:
        data = request_json(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"review-writer-literature-acquisition/1.0 (mailto:{email})",
            },
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "not_found", "error": "Unpaywall has no record for this DOI."}
        raise
    location = data.get("best_oa_location") or {}
    pdf_url = str(location.get("url_for_pdf") or "").strip()
    if not pdf_url:
        return {"status": "no_open_access_pdf", "is_oa": bool(data.get("is_oa"))}
    return {
        "status": "open_access_pdf",
        "provider": "unpaywall",
        "pdf_url": pdf_url,
        "landing_url": str(location.get("url") or ""),
        "license": str(location.get("license") or ""),
        "host_type": str(location.get("host_type") or ""),
        "version": str(location.get("version") or ""),
        "is_best": bool(location.get("is_best")),
    }


def resolve_crossref(candidate: dict[str, Any]) -> dict[str, Any]:
    """Use a Crossref PDF link only when Crossref also supplied license metadata."""
    pdf_url = str(candidate.get("crossref_pdf_url") or "").strip()
    license_urls = [
        str(value).strip()
        for value in candidate.get("license_urls") or []
        if str(value).strip()
    ]
    if not pdf_url:
        return {"status": "no_open_access_pdf", "provider": "crossref"}
    open_license_urls = [
        value
        for value in license_urls
        if any(
            marker in value.casefold()
            for marker in (
                "creativecommons.org/licenses/",
                "creativecommons.org/publicdomain/",
                "nationalarchives.gov.uk/doc/open-government-licence/",
            )
        )
    ]
    if not open_license_urls:
        return {
            "status": "license_not_confirmed",
            "provider": "crossref",
            "error": "Crossref returned a PDF link without a recognized open-access license.",
        }
    return {
        "status": "open_access_pdf",
        "provider": "crossref",
        "pdf_url": pdf_url,
        "landing_url": str(candidate.get("landing_url") or ""),
        "license": open_license_urls[0],
        "license_urls": license_urls,
        "host_type": "publisher_or_repository",
        "version": "",
    }


def resolve_europe_pmc(
    doi: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    request_json: Callable[..., dict[str, Any]] = _json_request,
) -> dict[str, Any]:
    doi = normalize_doi(doi)
    if not doi:
        return {"status": "no_doi", "provider": "europe_pmc"}
    params = urllib.parse.urlencode(
        {
            "query": f'DOI:"{doi}"',
            "format": "json",
            "resultType": "core",
            "pageSize": "5",
        }
    )
    data = request_json(
        f"{EUROPE_PMC_API}?{params}",
        headers={
            "Accept": "application/json",
            "User-Agent": "review-writer-literature-acquisition/1.0",
        },
        timeout=timeout,
    )
    matches = (data.get("resultList") or {}).get("result") or []
    row = next(
        (
            item
            for item in matches
            if isinstance(item, dict) and normalize_doi(item.get("doi")) == doi
        ),
        None,
    )
    if not row:
        return {"status": "not_found", "provider": "europe_pmc"}
    if str(row.get("isOpenAccess") or "").casefold() not in {"y", "yes", "true", "1"}:
        return {"status": "no_open_access_pdf", "provider": "europe_pmc"}
    urls = ((row.get("fullTextUrlList") or {}).get("fullTextUrl") or [])
    pdf_url = ""
    for entry in urls:
        if not isinstance(entry, dict):
            continue
        style = str(entry.get("documentStyle") or entry.get("documentType") or "").casefold()
        url = str(entry.get("url") or "").strip()
        if url and ("pdf" in style or url.casefold().split("?", 1)[0].endswith(".pdf")):
            pdf_url = url
            break
    pmcid = str(row.get("pmcid") or "").strip()
    if not pdf_url and pmcid:
        pdf_url = f"https://europepmc.org/articles/{urllib.parse.quote(pmcid, safe='')}?pdf=render"
    if not pdf_url:
        return {"status": "no_open_access_pdf", "provider": "europe_pmc"}
    return {
        "status": "open_access_pdf",
        "provider": "europe_pmc",
        "pdf_url": pdf_url,
        "landing_url": (
            f"https://europepmc.org/article/MED/{urllib.parse.quote(str(row.get('pmid')), safe='')}"
            if row.get("pmid")
            else ""
        ),
        "license": str(row.get("license") or "Europe PMC open-access subset"),
        "host_type": "repository",
        "version": "publishedVersion",
        "pmcid": pmcid,
    }


def resolve_semantic_scholar(
    doi: str,
    *,
    api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    request_json: Callable[..., dict[str, Any]] = _json_request,
) -> dict[str, Any]:
    doi = normalize_doi(doi)
    if not doi:
        return {"status": "no_doi", "provider": "semantic_scholar"}
    paper_id = urllib.parse.quote(f"DOI:{doi}", safe=":")
    fields = urllib.parse.urlencode({"fields": "title,externalIds,isOpenAccess,openAccessPdf"})
    headers = {
        "Accept": "application/json",
        "User-Agent": "review-writer-literature-acquisition/1.0",
    }
    if api_key:
        headers["x-api-key"] = api_key
    try:
        data = request_json(
            f"{SEMANTIC_SCHOLAR_API}/{paper_id}?{fields}",
            headers=headers,
            timeout=timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "not_found", "provider": "semantic_scholar"}
        raise
    location = data.get("openAccessPdf") or {}
    pdf_url = str(location.get("url") or "").strip()
    if not bool(data.get("isOpenAccess")) or not pdf_url:
        return {"status": "no_open_access_pdf", "provider": "semantic_scholar"}
    return {
        "status": "open_access_pdf",
        "provider": "semantic_scholar",
        "pdf_url": pdf_url,
        "landing_url": f"https://www.semanticscholar.org/paper/{data.get('paperId')}" if data.get("paperId") else "",
        "license": str(location.get("license") or ""),
        "host_type": "publisher_or_repository",
        "version": str(location.get("status") or ""),
    }


def resolve_pdf_sources(
    candidate: dict[str, Any],
    *,
    email: str = "",
    semantic_scholar_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return lawful OA candidates in fallback order without letting one provider abort the chain."""
    sources: list[dict[str, Any]] = []
    attempts: list[dict[str, str]] = []

    crossref = resolve_crossref(candidate)
    attempts.append(
        {
            "provider": "crossref",
            "status": str(crossref.get("status") or ""),
            "error": str(crossref.get("error") or ""),
        }
    )
    if crossref.get("status") == "open_access_pdf":
        sources.append(crossref)

    doi = normalize_doi(candidate.get("doi"))
    if not doi:
        return sources, attempts

    resolvers: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("europe_pmc", lambda: resolve_europe_pmc(doi, timeout=timeout)),
        (
            "semantic_scholar",
            lambda: resolve_semantic_scholar(
                doi,
                api_key=semantic_scholar_api_key,
                timeout=timeout,
            ),
        ),
    ]
    if email:
        resolvers.append(("unpaywall", lambda: resolve_unpaywall(doi, email, timeout=timeout)))
    for provider, resolver in resolvers:
        try:
            result = resolver()
        except Exception as exc:
            attempts.append(
                {
                    "provider": provider,
                    "status": "provider_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        attempts.append(
            {
                "provider": provider,
                "status": str(result.get("status") or ""),
                "error": str(result.get("error") or ""),
            }
        )
        if result.get("status") == "open_access_pdf":
            sources.append(result)
    if not email:
        attempts.append(
            {
                "provider": "unpaywall",
                "status": "skipped",
                "error": "No optional Unpaywall email was supplied.",
            }
        )
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for source in sources:
        url = str(source.get("pdf_url") or "").strip()
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(source)
    return deduped, attempts


def _is_proxy_fake_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in _PROXY_FAKE_IP_NETWORKS)


def _resolve_public_doh(
    hostname: str,
    *,
    timeout: int = 10,
    request_json: Callable[..., dict[str, Any]] = _json_request,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    verified: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for record_type in ("A", "AAAA"):
        url = CLOUDFLARE_DOH_API + "?" + urllib.parse.urlencode(
            {"name": hostname, "type": record_type}
        )
        data = request_json(
            url,
            headers={
                "Accept": "application/dns-json",
                "User-Agent": "review-writer-literature-acquisition/1.0",
            },
            timeout=timeout,
        )
        for answer in data.get("Answer") or []:
            if not isinstance(answer, dict) or int(answer.get("type") or 0) not in {1, 28}:
                continue
            try:
                verified.append(ipaddress.ip_address(str(answer.get("data") or "")))
            except ValueError:
                continue
    if not verified or any(not address.is_global for address in verified):
        raise ValueError("Public DNS verification did not return exclusively public addresses.")
    return verified


def validate_public_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("Only HTTP(S) download URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Credential-bearing URLs are not allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("The download URL has no host.")
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve download host: {exc}") from exc
    resolved = [
        ipaddress.ip_address(address.split("%", 1)[0])
        for address in {entry[4][0] for entry in addresses}
    ]
    if any(not address.is_global for address in resolved):
        if resolved and all(_is_proxy_fake_ip(address) for address in resolved):
            _resolve_public_doh(hostname)
        else:
            raise ValueError("The download URL resolves to a non-public address.")
    return urllib.parse.urlunsplit(parsed)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_pdf_file(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1024:
        raise ValueError("Downloaded body is too small to be a journal PDF.")
    with path.open("rb") as stream:
        head = stream.read(1024)
    stripped = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if not stripped.startswith(b"%PDF-"):
        if b"<html" in head.casefold() or b"<!doctype html" in head.casefold():
            raise ValueError("The OA URL returned an HTML page instead of a PDF.")
        raise ValueError("The downloaded body does not have a PDF signature.")
    return {"size_bytes": size, "sha256": sha256_file(path)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_pdf(
    url: str,
    output_path: Path,
    *,
    timeout: int = 60,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    url = validate_public_url(url)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf,application/octet-stream;q=0.9",
            "User-Agent": "review-writer-literature-acquisition/1.0",
        },
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".pdf.part", prefix="acquire-", dir=output_path.parent, delete=False
        ) as target:
            temporary = Path(target.name)
            with opener.open(request, timeout=timeout) as response:
                final_url = validate_public_url(response.geturl())
                announced = response.headers.get("Content-Length")
                if announced and int(announced) > max_bytes:
                    raise ValueError(f"PDF exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"PDF exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                    target.write(chunk)
        validation = validate_pdf_file(temporary)
        temporary.replace(output_path)
        return {"path": str(output_path), "source_url": final_url, **validation}
    except Exception:
        if temporary and temporary.exists():
            temporary.unlink()
        raise


def _field(value: Any, source: str, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "confidence": confidence,
        "human_checked": False,
    }


def _existing_metadata(review_root: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    directory = Path(review_root) / "review-library" / "metadata" / "papers"
    for path in sorted(directory.glob("P*.metadata.json")):
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue


def find_existing_by_doi(review_root: Path, doi: str) -> dict[str, Any] | None:
    wanted = normalize_doi(doi)
    if not wanted:
        return None
    for _, metadata in _existing_metadata(review_root):
        raw = metadata.get("doi")
        value = raw.get("value") if isinstance(raw, dict) else raw
        if normalize_doi(value) == wanted:
            return metadata
    return None


def _next_paper_id(review_root: Path) -> str:
    used = []
    for path, metadata in _existing_metadata(review_root):
        match = re.fullmatch(r"P(\d+)", str(metadata.get("paper_id") or path.name.split(".", 1)[0]))
        if match:
            used.append(int(match.group(1)))
    return f"P{max(used, default=0) + 1:03d}"


def _rebuild_registry(review_root: Path) -> None:
    root = Path(review_root)
    registry = root / "review-library" / "registry" / "papers.jsonl"
    rows: list[dict[str, Any]] = []
    for path, meta in _existing_metadata(root):
        def value_of(key: str) -> Any:
            value = meta.get(key)
            return value.get("value") if isinstance(value, dict) and "value" in value else value
        source_paths = meta.get("source_paths") or {}
        rows.append(
            {
                "paper_id": meta.get("paper_id"),
                "slug": meta.get("slug"),
                "title": value_of("title"),
                "authors": value_of("authors"),
                "year": value_of("year"),
                "journal": value_of("journal"),
                "doi": value_of("doi"),
                "source_pdf": source_paths.get("pdf"),
                "markdown_path": source_paths.get("markdown"),
                "content_list_path": source_paths.get("content_list"),
                "metadata_path": str(path),
                "parse_status": "pending" if meta.get("extraction", {}).get("mode") == "online_acquisition" else "done",
                "human_review_status": (meta.get("human_review") or {}).get("status"),
                "needs_human_check": (meta.get("quality") or {}).get("needs_human_check"),
            }
        )
    registry.parent.mkdir(parents=True, exist_ok=True)
    temporary = registry.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(registry)


def register_download(
    review_root: Path,
    candidate: dict[str, Any],
    acquisition: dict[str, Any],
    downloaded_path: Path,
) -> dict[str, Any]:
    root = Path(review_root).resolve()
    with _REGISTER_LOCK:
        existing = find_existing_by_doi(root, candidate.get("doi") or "")
        if existing:
            downloaded_path.unlink(missing_ok=True)
            return {
                "status": "already_in_library",
                "paper_id": existing.get("paper_id"),
                "doi": normalize_doi(candidate.get("doi")),
            }
        digest = sha256_file(downloaded_path)
        for _, metadata in _existing_metadata(root):
            if str((metadata.get("source_file") or {}).get("sha256") or "") == digest:
                downloaded_path.unlink(missing_ok=True)
                return {
                    "status": "duplicate_file",
                    "paper_id": metadata.get("paper_id"),
                    "sha256": digest,
                }
        paper_id = _next_paper_id(root)
        library_dir = root / "review-library" / "downloads"
        library_dir.mkdir(parents=True, exist_ok=True)
        final_pdf = library_dir / f"{paper_id}.pdf"
        final_md = library_dir / f"{paper_id}.md"
        downloaded_path.replace(final_pdf)
        title = _plain_text(candidate.get("title")) or "(untitled)"
        markdown = [
            f"# {title}",
            "",
            f"DOI: {normalize_doi(candidate.get('doi')) or 'not available'}",
            "",
            "This open-access PDF was downloaded and registered automatically. "
            "Run the normal PDF parsing and metadata audit before using it in a review.",
        ]
        final_md.write_text("\n".join(markdown) + "\n", encoding="utf-8")
        provenance = {
            "provider": acquisition.get("provider") or "unknown",
            "source_url": acquisition.get("source_url") or acquisition.get("pdf_url"),
            "oa_landing_url": acquisition.get("landing_url"),
            "license": acquisition.get("license"),
            "host_type": acquisition.get("host_type"),
            "version": acquisition.get("version"),
            "provider_attempts": acquisition.get("provider_attempts") or [],
            "acquired_at": utc_now(),
        }
        metadata = {
            "paper_id": paper_id,
            "slug": f"online-{normalize_doi(candidate.get('doi')).replace('/', '-') or digest[:16]}",
            "title": _field(title, "crossref", 0.95),
            "authors": _field(list(candidate.get("authors") or []), "crossref", 0.9),
            "year": _field(candidate.get("year"), "crossref", 0.9),
            "journal": _field(candidate.get("journal") or None, "crossref", 0.9),
            "doi": _field(normalize_doi(candidate.get("doi")) or None, "crossref", 0.98),
            "abstract": _field(candidate.get("abstract") or "", "crossref", 0.8),
            "structured_tags": _field(
                {
                    "product": "not specified",
                    "substrate": "not specified",
                    "catalyst_or_method": "not specified",
                    "organometallic_partner": "not specified",
                    "ligand_or_chiral_source": "not specified",
                    "leaving_group": "not specified",
                    "reaction_type": "not specified",
                    "document_scope": "journal article",
                },
                "pending_metadata_audit",
                0.0,
            ),
            "source_paths": {
                "pdf": str(final_pdf),
                "markdown": str(final_md),
                "content_list": None,
                "extracted_dir": None,
            },
            "source_file": {
                "pdf_name": final_pdf.name,
                "relative_pdf_path": str(final_pdf.relative_to(root)),
                "sha256": digest,
            },
            "acquisition": provenance,
            "extraction": {
                "mode": "online_acquisition",
                "model": None,
                "created_at": utc_now(),
                "inputs": {"crossref_candidate_id": candidate.get("candidate_id")},
                "notes": ["PDF parsing has not been run."],
            },
            "human_review": {
                "status": "not_reviewed",
                "reviewed_at": None,
                "reviewer": None,
                "notes": [],
            },
            "quality": {
                "missing_fields": [],
                "warnings": ["online_pdf_requires_parsing", "structured_tags_pending"],
                "overall_confidence": 0.55,
                "needs_human_check": True,
            },
        }
        meta_path = root / "review-library" / "metadata" / "papers" / f"{paper_id}.metadata.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = meta_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(meta_path)
        _rebuild_registry(root)
        return {
            "status": "downloaded",
            "paper_id": paper_id,
            "path": str(final_pdf),
            "sha256": digest,
            "metadata_path": str(meta_path),
        }


def acquire_candidate(
    review_root: Path,
    candidate: dict[str, Any],
    *,
    email: str = "",
    timeout: int = 60,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    root = Path(review_root).resolve()
    load_dotenv_if_present(root)
    existing = find_existing_by_doi(root, candidate.get("doi") or "")
    if existing:
        return {
            "candidate_id": candidate.get("candidate_id"),
            "status": "already_in_library",
            "paper_id": existing.get("paper_id"),
        }
    configured_email = (
        email
        or os.environ.get("UNPAYWALL_EMAIL")
        or os.environ.get("CROSSREF_MAILTO")
        or ""
    )
    sources, provider_attempts = resolve_pdf_sources(
        candidate,
        email=configured_email,
        semantic_scholar_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "",
        timeout=min(timeout, DEFAULT_TIMEOUT),
    )
    if not sources:
        return {
            "candidate_id": candidate.get("candidate_id"),
            "status": "no_open_access_pdf",
            "error": "No downloadable OA PDF was found through the configured providers.",
            "provider_attempts": provider_attempts,
        }
    staging = root / "review-library" / "downloads" / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    candidate_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(candidate.get("candidate_id") or "candidate"))
    temporary = staging / f"{candidate_id}.pdf"
    download_failures: list[dict[str, str]] = []
    for resolved in sources:
        try:
            download = download_pdf(resolved["pdf_url"], temporary, timeout=timeout, max_bytes=max_bytes)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            download_failures.append(
                {
                    "provider": str(resolved.get("provider") or "unknown"),
                    "status": "download_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        full_attempts = [*provider_attempts, *download_failures]
        return {
            "candidate_id": candidate.get("candidate_id"),
            **resolved,
            **register_download(
                root,
                candidate,
                {**resolved, **download, "provider_attempts": full_attempts},
                temporary,
            ),
        }
    return {
        "candidate_id": candidate.get("candidate_id"),
        "status": "all_sources_failed",
        "error": "OA locations were found, but none returned a valid downloadable PDF.",
        "provider_attempts": [*provider_attempts, *download_failures],
    }


def _read_candidate(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Candidate JSON must be an object.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search")
    search.add_argument("--review-root", type=Path, default=None)
    search.add_argument("--topic", required=True)
    search.add_argument("--year-from", type=int)
    search.add_argument("--year-to", type=int)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--mailto", default="")
    download = subparsers.add_parser("download")
    download.add_argument("--review-root", type=Path, default=None)
    download.add_argument("--candidate-json", type=Path, required=True)
    download.add_argument("--email", default="")
    args = parser.parse_args()
    args.review_root = resolve_review_root(args.review_root, anchor=Path(__file__))
    if args.command == "search":
        load_dotenv_if_present(args.review_root)
        rows = search_crossref(
            args.topic,
            year_from=args.year_from,
            year_to=args.year_to,
            limit=args.limit,
            mailto=args.mailto or os.environ.get("CROSSREF_MAILTO") or "",
        )
        print(json.dumps({"ok": True, "candidates": rows}, ensure_ascii=False, indent=2))
        return 0
    result = acquire_candidate(args.review_root, _read_candidate(args.candidate_json), email=args.email)
    print(json.dumps({"ok": result.get("status") in {"downloaded", "already_in_library"}, **result}, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"downloaded", "already_in_library"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
