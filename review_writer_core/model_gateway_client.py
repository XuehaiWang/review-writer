"""Small synchronous client used by scientific subprocesses for the internal gateway."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import ssl
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from contextlib import contextmanager
from pathlib import Path

from .provider_errors import normalize_provider_error, provider_error_message


# Recovery only reads the original request. Allow the gateway's three provider
# attempts (up to 300 seconds each) to finish without starting another paid call.
MODEL_RESULT_RECOVERY_SECONDS = 900


class ModelResultUnknown(RuntimeError):
    """The original request may have been sent; automatic replay is unsafe."""


@contextmanager
def _model_wait_marker():
    directory = str(os.environ.get("REVIEW_WRITER_MODEL_WAIT_DIR") or "").strip()
    if not directory or not Path(directory).is_dir():
        yield
        return
    marker = Path(directory) / f"{uuid.uuid4().hex}.waiting"
    try:
        marker.touch(exist_ok=False)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass


def _recover_model_result(url: str, token: str, request_key: str, *, label: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/" + urllib.parse.quote(request_key, safe=""),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    deadline = time.monotonic() + MODEL_RESULT_RECOVERY_SECONDS
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            with urllib.request.urlopen(
                request, context=ssl.create_default_context(), timeout=min(30.0, remaining)
            ) as response:
                snapshot = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504, 524}:
                raise _gateway_http_error(exc) from exc
            exc.close()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        else:
            status = snapshot.get("status") if isinstance(snapshot, dict) else None
            if status == "succeeded" and isinstance(snapshot.get("result"), dict):
                return snapshot["result"]
            if status == "failed":
                failure = snapshot.get("error") or {}
                raise GatewayRequestError(
                    provider_error_message(failure) if failure else "文本模型原请求执行失败，请稍后重试。",
                    status_code=502, code="MODEL_REQUEST_FAILED", details=failure,
                )
            if status != "running":
                raise RuntimeError(f"{label} gateway returned an invalid recovery status.")
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    raise ModelResultUnknown(
        f"{label} gateway result recovery timed out after {MODEL_RESULT_RECOVERY_SECONDS} seconds; "
        "the original request was not resubmitted."
    )


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


class DeferredModelCall(BaseException):
    """The durable model job owns this request; the scientific job may yield."""

    def __init__(self, model_job_id: str, *, model_job_ids=None):
        super().__init__(model_job_id)
        self.model_job_id = model_job_id
        self.model_job_ids = list(dict.fromkeys([model_job_id, *(model_job_ids or [])]))


def _delegated_text_result(
    url: str, token: str, *, request_key: str, stage: str,
    prompt: str, response_format: str,
) -> str:
    base = url.rsplit("/", 1)[0] + "/model-delegations"
    body = json.dumps({
        "request_key": request_key, "stage": stage,
        "prompt": prompt, "response_format": response_format,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json",
    })
    with _model_wait_marker():
        try:
            with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=30) as response:
                snapshot = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 500, 502, 503, 504, 524}:
                raise _gateway_http_error(exc) from exc
            exc.close()
            snapshot = _recover_delegated_submission(base, token, request_key)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # A lost submission response is not permission to create another
            # paid request. Query the original key; never repeat the POST.
            snapshot = _recover_delegated_submission(base, token, request_key)
    if not isinstance(snapshot, dict):
        raise RuntimeError("The model delegation returned an invalid response.")
    status = str(snapshot.get("status") or "")
    if status == "succeeded":
        output = str(snapshot.get("output_text") or "")
        if not output:
            raise RuntimeError("The delegated model returned an empty response.")
        return output
    if status in {"queued", "running"}:
        model_job_id = str(snapshot.get("model_job_id") or "")
        if not model_job_id:
            raise RuntimeError("The model delegation did not identify its durable job.")
        raise DeferredModelCall(model_job_id)
    raise RuntimeError(str(snapshot.get("error") or "The delegated model request failed."))


def _recover_delegated_submission(base: str, token: str, request_key: str) -> dict[str, Any]:
    lookup = urllib.request.Request(
        base.rstrip("/") + "/" + urllib.parse.quote(request_key, safe=""),
        method="GET", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(lookup, context=ssl.create_default_context(), timeout=5) as response:
                value = json.loads(response.read().decode("utf-8"))
            if isinstance(value, dict):
                return value
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 408, 429, 500, 502, 503, 504, 524}:
                raise _gateway_http_error(exc) from exc
            exc.close()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    raise ModelResultUnknown("Model delegation outcome is uncertain; no paid request was submitted again.")


_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(.*?)```",
    flags=re.IGNORECASE | re.DOTALL,
)
_LEADING_THINK_BLOCK_RE = re.compile(
    r"^\s*(?:<think\b[^>]*>.*?</think\s*>\s*)+",
    flags=re.IGNORECASE | re.DOTALL,
)
_JSON_SIMPLE_ESCAPES = frozenset('"\\/bfnrt')
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _escape_invalid_json_string_backslashes(text: str) -> str:
    """Escape model-authored LaTeX slashes that are invalid in JSON strings.

    This is deliberately a narrow fallback. Structural JSON remains untouched;
    only a backslash inside a quoted string is doubled when it does not begin a
    valid JSON escape. Odd slash runs such as ``\\\\\\mathrm`` are repaired
    without changing already-valid pairs.
    """

    output: list[str] = []
    in_string = False
    cursor = 0
    while cursor < len(text):
        character = text[cursor]
        if character == '"':
            in_string = not in_string
            output.append(character)
            cursor += 1
            continue
        if character != "\\" or not in_string:
            output.append(character)
            cursor += 1
            continue

        following = text[cursor + 1] if cursor + 1 < len(text) else ""
        if following in _JSON_SIMPLE_ESCAPES:
            output.extend((character, following))
            cursor += 2
            continue
        if (
            following == "u"
            and cursor + 5 < len(text)
            and all(value in _HEX_DIGITS for value in text[cursor + 2 : cursor + 6])
        ):
            output.append(text[cursor : cursor + 6])
            cursor += 6
            continue

        output.append("\\\\")
        cursor += 1
    return "".join(output)


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

    cleaned = _LEADING_THINK_BLOCK_RE.sub(
        "", str(text or "").lstrip("\ufeff")
    ).strip()
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
            repaired = _escape_invalid_json_string_backslashes(source)
            try:
                decoded = [json.loads(repaired)]
            except json.JSONDecodeError:
                decoded = _decoded_json_values(source)
                if repaired != source:
                    decoded.extend(_decoded_json_values(repaired))
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
        error = payload.get("error") or payload.get("detail")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            details = error.get("details") if isinstance(error.get("details"), dict) else {}
        elif isinstance(error, str):
            message = error
    failure = normalize_provider_error(exc.code, payload)
    if code == "INSUFFICIENT_CREDIT" or exc.code == 402:
        public = "余额不足，无法使用智能服务。请在“API 设置”中查看余额，或联系管理员添加额度。"
    elif failure.get("provider_status") in {408, 504, 524} or failure["category"] in {"quota_exhausted", "context_limit", "rate_limited", "model_unavailable"}:
        public = provider_error_message(failure)
    elif exc.code in {408, 409, 425, 429, 500, 502, 503, 504, 524}:
        public = "图像服务暂时不可用，请稍后重试。" if image else "文本模型服务暂时不可用，请稍后重试。"
    elif exc.code in {401, 403}:
        public = "任务授权已失效，请重新启动任务。"
    else:
        public = "图像服务请求失败，请稍后重试或联系管理员。" if image else "文本模型请求失败，请稍后重试或联系管理员。"
    return GatewayRequestError(
        public,
        status_code=exc.code,
        code=code or failure["code"],
        details={**details, **failure, "provider_message": message[:500]},
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
    recover_on_timeout: bool = True,
    gateway_url: str = "",
    task_token: str = "",
) -> str:
    url = str(gateway_url or os.environ.get("REVIEW_WRITER_MODEL_GATEWAY_URL") or "").strip()
    token = str(task_token or os.environ.get("REVIEW_WRITER_TASK_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("The internal model gateway configuration is incomplete.")
    normalized_format = str(response_format).strip().casefold()
    digest = hashlib.sha256(
        f"{normalized_format}\0{prompt}".encode("utf-8")
    ).hexdigest()
    request_key = f"{str(label)[:32]}-{digest[:48]}"
    if os.environ.get("REVIEW_WRITER_DELEGATE_MODEL_CALLS") == "1" and not gateway_url:
        return _delegated_text_result(
            url, token, request_key=request_key, stage=str(label)[:96],
            prompt=prompt, response_format=normalized_format,
        )
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
    # Provider retries belong to the gateway. An uncertain connection failure
    # only triggers read-only recovery, never another generation POST.
    with _model_wait_marker():
        try:
            with urllib.request.urlopen(
                request,
                context=ssl.create_default_context(),
                timeout=max(1, int(timeout_seconds)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error = _gateway_http_error(exc)
            if not recover_on_timeout:
                exc.close()
                raise error from exc
            if exc.code not in {408, 500, 502, 503, 504, 524}:
                raise error from exc
            # A proxy error does not say whether generation finished. Inspect the
            # original request once; a terminal failed status stops polling immediately.
            exc.close()
            try:
                payload = _recover_model_result(url, token, request_key, label=label)
            except GatewayRequestError as recovery_error:
                if recovery_error.code == "MODEL_REQUEST_FAILED" and recovery_error.details:
                    raise
                if recovery_error.code == "MODEL_REQUEST_FAILED" or recovery_error.status_code == 404:
                    raise error from exc
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if not recover_on_timeout:
                raise
            payload = _recover_model_result(url, token, request_key, label=label)
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
    recover_on_timeout: bool = True,
) -> dict[str, Any]:
    raw = call_model(
        prompt,
        label=label,
        response_format="json",
        timeout_seconds=timeout_seconds,
        **({"recover_on_timeout": False} if not recover_on_timeout else {}),
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
