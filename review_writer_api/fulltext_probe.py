"""Bounded pre-download checks. A PDF prefix is evidence of access, not of parsing.

Connections are pinned to validated public IPs, including every redirect. No
cookies, credentials, environment proxies, full downloads or model calls.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from review_writer_core.paper_sources.normalize import normalize_doi

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fulltext-check")
_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_inflight = {}
_PROXY_FAKE_IP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)


def _verify_proxy_hostname(hostname: str) -> None:
    """Verify a transparent-proxy hostname through HTTPS DNS before using it."""
    verified = []
    for record_type in ("A", "AAAA"):
        url = "https://cloudflare-dns.com/dns-query?" + urlencode({"name": hostname, "type": record_type})
        request = Request(url, headers={"Accept": "application/dns-json"})
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read(128 * 1024 + 1))
        for answer in payload.get("Answer") or []:
            if isinstance(answer, dict) and answer.get("type") in (1, 28):
                verified.append(ipaddress.ip_address(str(answer.get("data") or "")))
    if not verified or any(not address.is_global for address in verified):
        raise ValueError("Public DNS verification failed")


def public_get(url: str, limit: int = 1024) -> tuple[int, bytes, str]:
    for _ in range(4):
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.port not in {None, 80, 443}):
            raise ValueError("Unsupported public URL")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        resolved = [ipaddress.ip_address(a[4][0].split("%", 1)[0]) for a in addresses]
        if not resolved:
            raise ValueError("Non-public destination")
        if any(not address.is_global for address in resolved):
            if all(any(address in network for network in _PROXY_FAKE_IP_NETWORKS) for address in resolved):
                _verify_proxy_hostname(parts.hostname)
            else:
                raise ValueError("Non-public destination")
        address = addresses[0][4][0]
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = cls(parts.hostname, port, timeout=4)
        # Keep the original hostname for TLS verification/Host, pin only TCP.
        connection._create_connection = lambda *args, **kwargs: socket.create_connection((address, port), timeout=4)
        try:
            connection.request("GET", parts.path + ("?" + parts.query if parts.query else "") or "/",
                               headers={"Range": f"bytes=0-{limit - 1}", "Accept-Encoding": "identity",
                                        "User-Agent": "ReviewWriter-FulltextCheck/1.0"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    return response.status, b"", url
                url = urljoin(url, location)
                continue
            return response.status, response.read(limit), url
        finally:
            connection.close()
    raise ValueError("Too many redirects")


def probe(row: dict) -> dict:
    urls = list(dict.fromkeys(str(row.get(k) or "").strip() for k in ("pdf_url", "crossref_pdf_url") if row.get(k)))
    uncertain = False
    restricted = False

    def check(url):
        nonlocal uncertain, restricted
        try:
            status, prefix, resolved = public_get(url)
            if status in {200, 206} and prefix.lstrip().startswith(b"%PDF-"):
                return {"state": "available", "pdf_url": resolved}
            restricted |= status in {401, 403}
            uncertain |= status not in {200, 206, 401, 403, 404, 410}
        except (OSError, ValueError, http.client.HTTPException):
            uncertain = True
        return None

    for url in urls[:2]:
        if result := check(url):
            return result
    doi = normalize_doi(row.get("doi"))
    if doi:
        try:
            status, body, _ = public_get("https://api.openalex.org/works/https://doi.org/" + quote(doi, safe=""), 256 * 1024)
            if status in {200, 206}:
                record = json.loads(body)
                locations = [record.get("best_oa_location") or {}, *(record.get("locations") or [])]
                alternatives = list(dict.fromkeys(p.get("pdf_url") for p in locations if isinstance(p, dict) and p.get("is_oa") and p.get("pdf_url")))
                for url in [u for u in alternatives if u not in urls][:2]:
                    if result := check(url):
                        return result
            elif status != 404:
                uncertain = True
        except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
            uncertain = True
    if uncertain:
        return {"state": "unknown"}
    if restricted:
        return {"state": "restricted"}
    return {"state": "not_found"}


def cached_check(row: dict) -> dict:
    key = hashlib.sha256(json.dumps([row.get(k) for k in ("doi", "pdf_url", "crossref_pdf_url")], sort_keys=True).encode()).hexdigest()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1])
        future = _inflight.get(key)
        if future is None:
            # Never allow page refreshes or many users to grow an unbounded queue.
            if len(_inflight) >= 16:
                return {"state": "unknown"}
            future = _pool.submit(probe, dict(row))
            _inflight[key] = future
    try:
        result = future.result()
        result = {**result, "checked_at": int(time.time())}
        with _lock:
            _cache[key] = (time.monotonic() + (120 if result["state"] == "unknown" else 1800), result)
            _cache.move_to_end(key)
            while len(_cache) > 2048:
                _cache.popitem(last=False)
        return dict(result)
    finally:
        with _lock:
            _inflight.pop(key, None)
