"""Small synchronous client used by scientific subprocesses for the internal gateway."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any


class GatewayRequestError(RuntimeError):
    """Public-safe gateway failure with machine-readable private context."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "")
        self.details = dict(details or {})


_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)


def _decoded_json_values(text: str) -> list[Any]:
    """Return complete top-level JSON values found in a model response.

    OpenAI-compatible relays occasionally append prose or a second JSON object
    even when JSON output was requested.  ``json.loads`` rejects that response
    with ``Extra data``.  Decode each complete value independently instead of
    joining the first opening brace to the last closing brace.
    """

    decoder = json.JSONDecoder()
    values: list[Any] = []
    cursor = 0
    while cursor < len(text):
        object_start = text.find("{", cursor)
        array_start = text.find("[", cursor)
        starts = [start for start in (object_start, array_start) if start >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        values.append(value)
        cursor = max(end, start + 1)
    return values


def parse_json_object_text(
    text: str,
    *,
    required_list: str = "",
    context: str = "Model",
) -> dict[str, Any]:
    """Extract one usable JSON object from a structured model response.

    Exact JSON remains the preferred path.  The tolerant path accepts Markdown
    fences, harmless leading/trailing prose, and multiple complete top-level
    JSON values.  When a required list key is supplied, only an object carrying
    that contract can be selected, preventing a provider diagnostic object from
    being mistaken for the requested scientific result.
    """

    cleaned = str(text or "").lstrip("\ufeff").strip()
    if not cleaned:
        raise RuntimeError(f"{context} returned an empty JSON response.")

    sources = [match.group(1).strip() for match in _FENCED_JSON_RE.finditer(cleaned)]
    sources.append(cleaned)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source in sources:
        if not source:
            continue
        decoded: list[Any]
        try:
            decoded = [json.loads(source)]
        except json.JSONDecodeError:
            decoded = _decoded_json_values(source)
        for value in decoded:
            if not isinstance(value, dict):
                continue
            fingerprint = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append(value)
            if not required_list or isinstance(value.get(required_list), list):
                return value

    if candidates and required_list:
        raise RuntimeError(
            f"{context} returned JSON object(s), but none contains the required "
            f"`{required_list}` list."
        )
    raise RuntimeError(f"{context} returned no complete JSON object.")


def _gateway_http_error(exc: urllib.error.HTTPError, *, image: bool = False) -> GatewayRequestError:
    raw_body = exc.read().decode("utf-8", "replace")[:4000]
    code = ""
    message = ""
    details: dict[str, Any] = {}
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            details = error.get("details") if isinstance(error.get("details"), dict) else {}
        elif isinstance(payload.get("detail"), str):
            message = str(payload["detail"])
    if code == "INSUFFICIENT_CREDIT" or exc.code == 402:
        public = "余额不足，无法使用智能服务。请在“API 设置”中查看余额，或联系管理员添加额度。"
    elif exc.code in {408, 409, 425, 429, 500, 502, 503, 504}:
        public = "图像服务暂时不可用，请稍后重试。" if image else "文本模型服务暂时不可用，请稍后重试。"
    elif exc.code in {401, 403}:
        public = "任务授权已失效，请重新启动任务。"
    else:
        public = "图像服务请求失败，请稍后重试或联系管理员。" if image else "文本模型请求失败，请稍后重试或联系管理员。"
    return GatewayRequestError(
        public,
        status_code=exc.code,
        code=code,
        details={**details, "provider_message": message[:500]},
    )


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
    # The API model gateway owns provider retries, backoff, request joining and
    # idempotency. Retrying again in every scientific subprocess multiplies a
    # single transient failure into as many as nine paid provider calls.
    try:
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(),
            timeout=max(1, int(timeout_seconds)),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _gateway_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} gateway transport/JSON failure: {exc}") from exc
    text = str(payload.get("output_text") or "")
    if not text:
        raise RuntimeError("The internal model gateway returned an empty response.")
    return text


def call_json_model(
    prompt: str,
    *,
    label: str,
    timeout_seconds: int = 330,
    required_list: str = "",
) -> dict[str, Any]:
    raw = call_model(
        prompt,
        label=label,
        response_format="json",
        timeout_seconds=timeout_seconds,
    ).strip()
    return parse_json_object_text(
        raw,
        required_list=required_list,
        context="The internal model gateway",
    )


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
    try:
        with urllib.request.urlopen(
            request,
            context=ssl.create_default_context(),
            timeout=max(1, int(timeout_seconds)),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _gateway_http_error(exc, image=True) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{label} image gateway transport/JSON failure: {exc}") from exc
    encoded = str(payload.get("image_base64") or "")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError(f"{label} image gateway returned invalid image data: {exc}") from exc
    if not image_bytes:
        raise RuntimeError("The internal image gateway returned an empty image.")
    metadata = {key: value for key, value in payload.items() if key != "image_base64"}
    return image_bytes, metadata
