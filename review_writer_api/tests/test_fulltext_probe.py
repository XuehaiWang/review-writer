from unittest.mock import MagicMock, patch

import pytest

from review_writer_api import fulltext_probe as p


def test_pdf_header_required():
    with patch.object(p, "public_get", return_value=(200, b"<html>login</html>", "https://example.org/a")):
        assert p.probe({"pdf_url": "https://example.org/a"})["state"] == "not_found"
    with patch.object(p, "public_get", return_value=(206, b"%PDF-1.7\n", "https://example.org/a")):
        assert p.probe({"pdf_url": "https://example.org/a"}) == {"state": "available", "pdf_url": "https://example.org/a"}


@pytest.mark.parametrize("status,state", [(403, "restricted"), (401, "restricted"), (429, "unknown"), (503, "unknown"), (404, "not_found")])
def test_status_is_not_inferred_as_paywall(status, state):
    with patch.object(p, "public_get", return_value=(status, b"", "https://example.org/a")):
        assert p.probe({"pdf_url": "https://example.org/a"})["state"] == state


def test_timeout_is_unknown():
    with patch.object(p, "public_get", side_effect=TimeoutError):
        assert p.probe({"doi": "10.1234/test"})["state"] == "unknown"


def test_doi_open_location_is_probed_not_trusted():
    with patch.object(p, "public_get", side_effect=[
        (200, b'{"best_oa_location":{"is_oa":true,"pdf_url":"https://example.org/paper"}}', ""),
        (200, b"%PDF-1.6", "https://example.org/paper"),
    ]) as get:
        assert p.probe({"doi": "10.1234/test"})["state"] == "available"
        assert get.call_count == 2


@pytest.mark.parametrize("url", ["file:///secret", "https://u:p@example.org", "http://example.org:22/x"])
def test_unsafe_schemes_credentials_ports_rejected(url):
    with pytest.raises(ValueError):
        p.public_get(url)


def test_redirect_to_private_network_is_blocked():
    response = MagicMock(status=302)
    response.getheader.return_value = "http://127.0.0.1/private"
    connection = MagicMock()
    connection.getresponse.return_value = response
    with patch.object(p.socket, "getaddrinfo", side_effect=[
        [(2, 1, 6, "", ("93.184.216.34", 443))],
        [(2, 1, 6, "", ("127.0.0.1", 80))],
    ]), patch.object(p.http.client, "HTTPSConnection", return_value=connection):
        with pytest.raises(ValueError, match="Non-public"):
            p.public_get("https://example.org/a")
        connection.close.assert_called_once()


def test_transparent_proxy_address_requires_public_dns_verification():
    response = MagicMock(status=206)
    response.read.return_value = b"%PDF-1.7"
    connection = MagicMock()
    connection.getresponse.return_value = response
    fake_addresses = [(2, 1, 6, "", ("198.18.0.11", 443))]
    with patch.object(p.socket, "getaddrinfo", return_value=fake_addresses), patch.object(
        p, "_verify_proxy_hostname"
    ) as verify, patch.object(p.http.client, "HTTPSConnection", return_value=connection):
        assert p.public_get("https://example.org/paper.pdf")[0:2] == (206, b"%PDF-1.7")
        verify.assert_called_once_with("example.org")


def test_transparent_proxy_cannot_bypass_failed_public_dns_verification():
    fake_addresses = [(2, 1, 6, "", ("198.18.0.11", 443))]
    with patch.object(p.socket, "getaddrinfo", return_value=fake_addresses), patch.object(
        p, "_verify_proxy_hostname", side_effect=ValueError("Public DNS verification failed")
    ), patch.object(p.http.client, "HTTPSConnection") as connection:
        with pytest.raises(ValueError, match="Public DNS verification failed"):
            p.public_get("https://example.org/paper.pdf")
        connection.assert_not_called()


def test_cache_deduplicates_and_isolates_changed_links():
    with p._lock:
        p._cache.clear()
    with patch.object(p, "probe", return_value={"state": "available", "pdf_url": "https://example.org/a"}) as probe:
        row = {"pdf_url": "https://example.org/a"}
        first = p.cached_check(row)
        assert "checked_at" in first
        first["state"] = "corrupted"
        assert p.cached_check(row)["state"] == "available"
        assert probe.call_count == 1
        p.cached_check({"pdf_url": "https://example.org/b"})
        assert probe.call_count == 2


def test_service_only_checks_current_owned_candidates():
    from review_writer_api.domain_services.discovery import DiscoveryService
    from review_writer_api.errors import WorkflowNotFound

    service = DiscoveryService.__new__(DiscoveryService)
    row = {"candidate_id": "C1", "pdf_url": "https://example.org/a"}
    service._read_current = MagicMock(return_value=({"results": [{"web_results": [row]}]}, None))
    with patch.object(p, "cached_check", return_value={"state": "available"}) as check:
        assert service.check_fulltext("principal", "project", "C1")["state"] == "available"
        check.assert_called_once_with(row)
        with pytest.raises(WorkflowNotFound):
            service.check_fulltext("principal", "project", "https://attacker.invalid/")
        assert check.call_count == 1
        assert service._read_current.call_args.args[:2] == ("principal", "project")
