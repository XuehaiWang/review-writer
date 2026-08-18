"""Small synchronous client used by scientific subprocesses for the internal gateway."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def gateway_configured() -> bool:
    return bool(
        str(os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
        and str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    )


def image_gateway_configured() -> bool:
    return bool(
        str(os.environ.get("REVIEW_WRITER_IMAGE_GATEWAY_URL") or "").strip()
        and str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    )


def call_model(
    prompt: str,
    *,
    label: str,
    response_format: str = "text",
    timeout_seconds: int = 330,
) -> str:
    url = str(os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
    token = str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("The internal model gateway configuration is incomplete.")
    normalized_format = str(response_format).strip().casefold()
    digest = hashlib.sha256(
        f"{normalized_format}\0{prompt}".encode("utf-8")
    ).hexdigest()
    request_key = f"{str(label)[:32]}-{digest[:48]}"
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "request_key": request_key,
                "stage": str(label)[:96],
                "prompt": prompt,
                "response_format": normalized_format,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=max(1, int(timeout_seconds)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            text = str(payload.get("output_text") or "")
            if not text:
                raise RuntimeError("The internal model gateway returned an empty response.")
            return text
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:800].replace("\n", " ")
            if exc.code not in TRANSIENT_STATUS or attempt >= 3:
                raise RuntimeError(
                    f"{label} gateway failed with HTTP {exc.code} after {attempt} attempts: {body}"
                ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt >= 3:
                raise RuntimeError(
                    f"{label} gateway transport/JSON failure after {attempt} attempts: {exc}"
                ) from exc
        time.sleep(min(4.0, float(2 ** (attempt - 1))))
    raise RuntimeError(f"{label} gateway failed after 3 attempts")


def call_json_model(prompt: str, *, label: str, timeout_seconds: int = 330) -> dict[str, Any]:
    raw = call_model(
        prompt,
        label=label,
        response_format="json",
        timeout_seconds=timeout_seconds,
    ).strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().casefold() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("The internal model gateway returned no JSON object.")
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("The internal model gateway JSON response must be an object.")
    return parsed


def call_image_model(
    prompt: str,
    *,
    label: str,
    images: list[tuple[str, bytes]] | None = None,
    operation: str = "edit",
    quality: str = "high",
    background: str = "opaque",
    output_format: str = "png",
    size: str = "",
    timeout_seconds: int = 660,
) -> tuple[bytes, dict[str, Any]]:
    url = str(os.environ.get("REVIEW_WRITER_IMAGE_GATEWAY_URL") or "").strip()
    token = str(os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("The internal image gateway configuration is incomplete.")
    image_values = list(images or [])
    digest_builder = hashlib.sha256()
    digest_builder.update(str(operation).encode("utf-8"))
    digest_builder.update(b"\0")
    digest_builder.update(prompt.encode("utf-8"))
    for mime_type, raw in image_values:
        digest_builder.update(b"\0")
        digest_builder.update(str(mime_type).encode("ascii", "ignore"))
        digest_builder.update(hashlib.sha256(raw).digest())
    for value in (quality, background, output_format, size):
        digest_builder.update(b"\0")
        digest_builder.update(str(value).encode("utf-8"))
    digest = digest_builder.hexdigest()
    request_key = f"{str(label)[:32]}-{digest[:48]}"
    request = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "request_key": request_key,
                "stage": str(label)[:96],
                "operation": operation,
                "prompt": prompt,
                "images": [
                    {
                        "mime_type": mime_type,
                        "data_base64": base64.b64encode(raw).decode("ascii"),
                    }
                    for mime_type, raw in image_values
                ],
                "quality": quality,
                "background": background,
                "output_format": output_format,
                "size": size,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=max(1, int(timeout_seconds)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            encoded = str(payload.get("image_base64") or "")
            image_bytes = base64.b64decode(encoded, validate=True)
            if not image_bytes:
                raise RuntimeError("The internal image gateway returned an empty image.")
            metadata = {
                key: value for key, value in payload.items() if key != "image_base64"
            }
            return image_bytes, metadata
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:800].replace("\n", " ")
            if exc.code not in TRANSIENT_STATUS or attempt >= 3:
                raise RuntimeError(
                    f"{label} image gateway failed with HTTP {exc.code} after {attempt} attempts: {body}"
                ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            if attempt >= 3:
                raise RuntimeError(
                    f"{label} image gateway transport/JSON failure after {attempt} attempts: {exc}"
                ) from exc
        time.sleep(min(4.0, float(2 ** (attempt - 1))))
    raise RuntimeError(f"{label} image gateway failed after 3 attempts")
