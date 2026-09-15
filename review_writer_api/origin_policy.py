"""Explicit browser origins permitted to submit authenticated API requests."""

from urllib.parse import urlsplit


def normalize_browser_origin(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char.isspace() or char in "\\*?#" for char in value)
    ):
        raise ValueError("Browser origins must be explicit http(s) origins without paths or credentials.")
    port = parsed.port  # Reject malformed and out-of-range ports.
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port is None or port == {"http": 80, "https": 443}[parsed.scheme] else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def parse_browser_origins(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(normalize_browser_origin(item) for item in value.split(",") if item.strip()))


def browser_origin_allowed(value: str, allowed: frozenset[str]) -> bool:
    try:
        return normalize_browser_origin(value) in allowed
    except ValueError:
        return False
