"""Database-backed text model catalog; legacy tier IDs remain API-compatible."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import re
import uuid


@dataclass(frozen=True)
class ModelTier:
    id: str
    model: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    enabled: bool = True
    wire_api: str = ""
    connection_id: str = "default"
    connection_revision: int = 0
    channels: tuple[dict, ...] = ()


MODEL_TIERS: tuple[ModelTier, ...] = (
    ModelTier(
        id="sol",
        model="gpt-5.6-sol",
        label_zh="gpt-5.6-sol",
        label_en="gpt-5.6-sol",
        description_zh="",
        description_en="",
        input_usd_per_million=Decimal("5.00"),
        cached_input_usd_per_million=Decimal("0.50"),
        output_usd_per_million=Decimal("30.00"),
    ),
    ModelTier(
        id="terra",
        model="gpt-5.6-terra",
        label_zh="gpt-5.6-terra",
        label_en="gpt-5.6-terra",
        description_zh="",
        description_en="",
        input_usd_per_million=Decimal("2.00"),
        cached_input_usd_per_million=Decimal("0.20"),
        output_usd_per_million=Decimal("12.00"),
    ),
    ModelTier(
        id="luna",
        model="gpt-5.6-luna",
        label_zh="gpt-5.6-luna",
        label_en="gpt-5.6-luna",
        description_zh="",
        description_en="",
        input_usd_per_million=Decimal("0.20"),
        cached_input_usd_per_million=Decimal("0.02"),
        output_usd_per_million=Decimal("1.20"),
    ),
)

DEFAULT_MODEL_TIER = "terra"


CATALOG_KEY = "text_model_catalog"
SNAPSHOT_KEY = "_text_model_snapshot"
PRICE_FIELDS = ("input_usd_per_million", "cached_input_usd_per_million", "output_usd_per_million")


def model_dict(model: ModelTier) -> dict:
    data = asdict(model)
    data["channels"] = list(data["channels"])
    for key in PRICE_FIELDS:
        data[key] = format(data[key], "f")
    return data


def model_from_dict(data: dict) -> ModelTier:
    data = dict(data)
    for key in PRICE_FIELDS:
        data[key] = Decimal(str(data[key]))
    data["channels"] = tuple(data.get("channels") or ())
    return ModelTier(**data)


def model_channels(model: ModelTier) -> list[dict]:
    """Normalize historical single-connection jobs at the boundary."""
    return [{"connection_revision": 0, "wire_api": "", **c} for c in model.channels] if model.channels else [{
        "connection_id": model.connection_id, "connection_revision": model.connection_revision,
        "model": model.model, "wire_api": model.wire_api,
    }]


def routed_model(model: ModelTier, channel: dict) -> ModelTier:
    return replace(model, model=channel["model"], connection_id=channel["connection_id"],
                   connection_revision=int(channel.get("connection_revision", 0)),
                   wire_api=channel.get("wire_api", ""), channels=())


def available_channels(session, model: ModelTier) -> list[dict]:
    from .text_connections import connection_in_session
    result = []
    for channel in model_channels(model):
        connection = connection_in_session(session, channel["connection_id"])
        if connection is None or (connection["enabled"] and connection["encrypted_secret"]):
            result.append(channel)
    return result


def catalog_in_session(session=None) -> dict:
    if session is not None:
        from .workflow_models import WorkflowSystemState
        row = session.get(WorkflowSystemState, CATALOG_KEY)
        if row is not None:
            return dict(row.value_json)
    # Legacy IDs remain stable; they are model names, not quality tiers.
    return {"revision": 0, "default_tier": DEFAULT_MODEL_TIER, "items": [
        model_dict(item) for item in MODEL_TIERS]}


def read_catalog(session_factory=None) -> dict:
    if session_factory is None:
        return catalog_in_session()
    from .database import database_session
    with database_session(session_factory) as session:
        return catalog_in_session(session)


def resolve_from_catalog(value: str | None, catalog: dict, *, require_enabled=False) -> ModelTier:
    key = str(value or catalog["default_tier"]).strip()
    for item in catalog["items"]:
        if item["id"] == key:
            model = model_from_dict(item)
            if require_enabled and not model.enabled:
                raise ValueError("The selected text model is disabled. Choose an enabled model in API Settings.")
            return model
    raise ValueError("The selected text model is unavailable. Choose a model in API Settings.")


def resolve_model_tier(value: str | None, session_factory=None, *, require_enabled=False) -> ModelTier:
    model = resolve_from_catalog(value, read_catalog(session_factory), require_enabled=require_enabled)
    if require_enabled and session_factory is not None:
        from .database import database_session
        with database_session(session_factory) as session:
            if not available_channels(session, model):
                raise ValueError("The selected model's service connection is disabled.")
    return model


def public_catalog(session_factory=None):
    data = read_catalog(session_factory)
    if session_factory is None:
        return data
    from .database import database_session
    with database_session(session_factory) as session:
        items = []
        for item in data["items"]:
            items.append({**item, "enabled": bool(item.get("enabled", True)
                and available_channels(session, model_from_dict(item)))})
        return {**data, "items": items}


def save_catalog(session_factory, principal, data: dict) -> dict:
    from .database import database_session, ServerProviderAuditEvent
    from .workflow_models import WorkflowSystemState
    from .security import Permission
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    principal.require(Permission.PROVIDER_MANAGE)
    items = data.get("items") or []
    if not 1 <= len(items) <= 100:
        raise ValueError("Provide between 1 and 100 models.")
    models = []
    for item in items:
        model = model_from_dict(item)
        model = replace(model, connection_revision=0)
        channels = model_channels(model)
        if not 1 <= len(channels) <= 16:
            raise ValueError("Configure between 1 and 16 channels per model.")
        if len({c.get("connection_id") for c in channels}) != len(channels):
            raise ValueError("Each service connection can appear only once per model.")
        for channel in channels:
            name = channel.get("model", "")
            if not isinstance(name, str) or not name.strip() or len(name) > 255 or any(ord(c) < 32 for c in name):
                raise ValueError("Provide the exact provider model name for each channel.")
            if channel.get("wire_api", "") not in {"", "responses", "chat-completions"}:
                raise ValueError("Unsupported text protocol.")
            if not isinstance(channel.get("connection_id"), str) or not channel["connection_id"]:
                raise ValueError("Choose a service connection for each channel.")
        # Store a single canonical channel list; old fields describe the first
        # channel only for compatibility with existing catalog consumers.
        channels = [{"connection_id": c["connection_id"], "model": c["model"].strip(),
                     "wire_api": c.get("wire_api", "")} for c in channels]
        model = replace(model, channels=tuple(channels), connection_id=channels[0]["connection_id"],
                        model=channels[0]["model"], wire_api=channels[0]["wire_api"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", model.id):
            raise ValueError("Model ID must use 1–32 lowercase letters, digits, hyphens or underscores.")
        if any(not getattr(model, k).is_finite() or getattr(model, k) < 0 or getattr(model, k) > 1000000 for k in PRICE_FIELDS):
            raise ValueError("Prices must be finite, non-negative amounts below 1000000.")
        models.append(model_dict(model))
    ids = {item["id"] for item in models}
    if len(ids) != len(models):
        raise ValueError("Model IDs must be unique.")
    proposed = {"items": models, "default_tier": data["default_tier"]}
    resolve_from_catalog(data["default_tier"], proposed, require_enabled=True)
    try:
        with database_session(session_factory) as session:
            row = session.scalar(select(WorkflowSystemState).where(WorkflowSystemState.key == CATALOG_KEY).with_for_update())
            old = dict(row.value_json) if row else catalog_in_session()
            for item in models:
                if item["enabled"] and not available_channels(session, model_from_dict(item)):
                    raise ValueError("An enabled model must use an enabled service connection.")
            if int(data.get("revision", -1)) != old["revision"]:
                raise ValueError("The model catalog changed. Refresh before saving.")
            if not {item["id"] for item in old["items"]}.issubset(ids):
                raise ValueError("Disable existing models instead of deleting them; historical selections must remain visible.")
            proposed["revision"] = old["revision"] + 1
            if row:
                row.value_json = proposed
            else:
                session.add(WorkflowSystemState(key=CATALOG_KEY, value_json=proposed))
            session.add(ServerProviderAuditEvent(actor_user_id=uuid.UUID(principal.user_id),
                provider_kind="text", action="model_catalog_update",
                summary=f"Model catalog revision {proposed['revision']}; default={proposed['default_tier']}; count={len(models)}"))
    except IntegrityError as exc:
        raise ValueError("The model catalog changed. Refresh before saving.") from exc
    return proposed


def snapshot_for_retry(session, previous: dict, *, default_wire: str) -> dict:
    """Refresh transport routes on explicit retry, retaining identity and prices.

    Running jobs and individual request retries keep their pinned snapshots/routes.
    """
    current = snapshot_for_job(session, previous["id"], default_wire=default_wire)
    return {**previous, **{key: current[key] for key in (
        "channels", "connection_id", "connection_revision", "model", "wire_api",
    )}}


def snapshot_for_job(session, model_id: str | None, *, default_wire: str) -> dict:
    from .database import ServerProviderCredential
    from sqlalchemy import select
    catalog = catalog_in_session(session)
    model = resolve_from_catalog(model_id, catalog, require_enabled=True)
    from .text_connections import connection_in_session
    credential = session.scalar(select(ServerProviderCredential).where(ServerProviderCredential.provider_kind == "text"))
    channels = []
    for channel in available_channels(session, model):
        connection = connection_in_session(session, channel["connection_id"])
        channels.append({**channel, "connection_revision": connection["revision"] if connection else 0,
            "wire_api": channel.get("wire_api") or (connection["wire_api"] if connection else
                (credential.wire_api if credential else "")) or default_wire})
    if not channels:
        raise ValueError("The selected model's service connection is disabled.")
    return model_dict(replace(routed_model(model, channels[0]), channels=tuple(channels)))
