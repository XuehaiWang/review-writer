"""Versioned text connections in the existing encrypted system configuration store.

Jobs reference a connection ID and immutable revision, never a plaintext key.
The current revision is only used for new submissions and administrator tests.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .credentials import ProviderSettingsError, validate_provider_base_url, _secret_hint
from .database import database_session, ServerProviderAuditEvent, utc_now
from .security import Permission
from .workflow_models import WorkflowSystemState

CONNECTIONS_KEY = "text_service_connections"


def connection_in_session(session, connection_id="default", revision=0):
    state = session.get(WorkflowSystemState, CONNECTIONS_KEY)
    if state is None and connection_id == "default":
        return None  # compatibility with direct/legacy repository callers
    for item in (state.value_json.get("items", []) if state else []):
        if item["id"] == connection_id:
            if not revision:
                return dict(item["versions"][-1])
            for version in item["versions"]:
                if version["revision"] == revision:
                    return dict(version)
    raise ProviderSettingsError("The selected model's service connection is unavailable.")


def _version(service, runtime, name, revision):
    return dict(revision=revision, name=name, base_url=runtime.base_url,
                wire_api=runtime.wire_api, enabled=runtime.enabled,
                encrypted_secret=base64.b64encode(service.cipher.encrypt("server-global", "text", runtime.api_key)).decode() if runtime.api_key else "",
                api_key_hint=_secret_hint(runtime.api_key) if runtime.api_key else "",
                source=runtime.source, max_concurrency=4, updated_at=utc_now().isoformat())


def ensure_default(service):
    with database_session(service.session_factory) as session:
        if session.get(WorkflowSystemState, CONNECTIONS_KEY) is not None:
            return
    runtime = service._legacy_runtime_config("text")
    version = _version(service, runtime, "默认连接", 1)
    # Preserve legacy admission when no credentials have been configured yet;
    # the gateway still refuses to call without a key.
    if runtime.source == "environment" and not runtime.api_key:
        version["enabled"] = True
    try:
        with database_session(service.session_factory) as session:
            session.add(WorkflowSystemState(key=CONNECTIONS_KEY,
                value_json={"revision": 1, "items": [{"id": "default", "versions": [version]}]}))
    except IntegrityError:
        # Another API/worker process initialized the same default connection.
        with database_session(service.session_factory) as session:
            if session.get(WorkflowSystemState, CONNECTIONS_KEY) is None:
                raise


def _commit_state(session, old, new):
    new["revision"] = old["revision"] + 1
    changed = session.execute(update(WorkflowSystemState).where(
        WorkflowSystemState.key == CONNECTIONS_KEY,
        WorkflowSystemState.value_json["revision"].as_integer() == old["revision"],
    ).values(value_json=new))
    if changed.rowcount != 1:
        raise ProviderSettingsError("Service connections changed. Refresh before saving.")


def mirror_legacy_default(service, session, runtime):
    """Keep the old default-provider endpoints compatible, in one transaction."""
    state = session.scalar(select(WorkflowSystemState).where(WorkflowSystemState.key == CONNECTIONS_KEY).with_for_update())
    if state is None:
        return
    old = state.value_json
    data = deepcopy(old)
    item = next(item for item in data["items"] if item["id"] == "default")
    current = item["versions"][-1]
    item["versions"].append({**_version(service, runtime, current["name"], current["revision"] + 1),
                             "max_concurrency": current.get("max_concurrency", 4)})
    _commit_state(session, old, data)


def runtime_for_connection(service, connection_id="default", revision=0):
    from .server_providers import ServerProviderRuntime
    with database_session(service.session_factory) as session:
        data = connection_in_session(session, connection_id, revision)
    if data is None:
        return service._legacy_runtime_config("text")
    secret = service.cipher.decrypt("server-global", "text", base64.b64decode(data["encrypted_secret"])) if data["encrypted_secret"] else ""
    return ServerProviderRuntime("text", data["base_url"], "", data["wire_api"], secret,
                                 bool(data["enabled"] and secret), data["source"], data["api_key_hint"])


def list_connections(service, principal):
    principal.require(Permission.PROVIDER_MANAGE)
    with database_session(service.session_factory) as session:
        state = session.get(WorkflowSystemState, CONNECTIONS_KEY)
        return {"items": [dict(id=item["id"], **{"max_concurrency": 4, **{k: v for k, v in item["versions"][-1].items() if k != "encrypted_secret"}},
                               api_key_configured=bool(item["versions"][-1]["encrypted_secret"]))
                          for item in state.value_json["items"]]}


def save_connection(service, principal, connection_id, data):
    principal.require(Permission.PROVIDER_MANAGE)
    name = str(data.get("name") or "").strip()
    wire = str(data.get("wire_api") or "")
    capacity = data.get("max_concurrency")
    if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 32):
        raise ProviderSettingsError("Connection concurrency must be between 1 and 32.")
    if not name or len(name) > 100:
        raise ProviderSettingsError("Provide a connection name (1–100 characters).")
    if wire not in {"responses", "chat-completions"}:
        raise ProviderSettingsError("Unsupported text protocol.")
    url = validate_provider_base_url(str(data.get("base_url") or ""),
        allow_private_urls=service.settings.allow_private_provider_urls,
        allowed_hosts=service.settings.allowed_provider_hosts,
        trusted_proxy_networks=service.settings.trusted_proxy_networks)
    with database_session(service.session_factory) as session:
        state = session.scalar(select(WorkflowSystemState).where(WorkflowSystemState.key == CONNECTIONS_KEY).with_for_update())
        old = state.value_json
        updated = deepcopy(old)
        if connection_id is None:
            connection_id = uuid.uuid4().hex
            item = {"id": connection_id, "versions": []}
            updated["items"].append(item)
            previous = {}
        else:
            item = next((i for i in updated["items"] if i["id"] == connection_id), None)
            if item is None:
                raise ProviderSettingsError("Service connection not found.")
            previous = item["versions"][-1]
        if int(data.get("revision", 0)) != previous.get("revision", 0):
            raise ProviderSettingsError("This connection changed. Refresh before saving.")
        if capacity is None:
            capacity = previous.get("max_concurrency", 4)
        if any(i["id"] != connection_id and i["versions"][-1]["name"].casefold() == name.casefold() for i in updated["items"]):
            raise ProviderSettingsError("Connection names must be unique.")
        key = str(data.get("api_key") or "").strip()
        encrypted = base64.b64encode(service.cipher.encrypt("server-global", "text", key)).decode() if key else previous.get("encrypted_secret", "")
        enabled = bool(data.get("enabled", True))
        if enabled and not encrypted:
            raise ProviderSettingsError("An API key is required before enabling a connection.")
        item["versions"].append(dict(revision=previous.get("revision", 0) + 1, name=name,
            base_url=url, wire_api=wire, enabled=enabled, encrypted_secret=encrypted,
            api_key_hint=_secret_hint(key) if key else previous.get("api_key_hint", ""),
            source="database", max_concurrency=capacity, updated_at=utc_now().isoformat()))
        _commit_state(session, old, updated)
        session.add(ServerProviderAuditEvent(actor_user_id=uuid.UUID(principal.user_id), provider_kind="text",
            action="connection_update", summary=f"Text connection {connection_id}; revision={item['versions'][-1]['revision']}; enabled={enabled}"))
    return next(item for item in list_connections(service, principal)["items"] if item["id"] == connection_id)
