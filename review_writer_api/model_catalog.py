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
    for key in PRICE_FIELDS:
        data[key] = format(data[key], "f")
    return data


def model_from_dict(data: dict) -> ModelTier:
    data = dict(data)
    for key in PRICE_FIELDS:
        data[key] = Decimal(str(data[key]))
    return ModelTier(**data)


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
        from .text_connections import connection_in_session
        with database_session(session_factory) as session:
            connection = connection_in_session(session, model.connection_id)
            if connection and not connection["enabled"]:
                raise ValueError("The selected model's service connection is disabled.")
    return model


def public_catalog(session_factory=None):
    data = read_catalog(session_factory)
    if session_factory is None:
        return data
    from .database import database_session
    from .text_connections import connection_in_session
    with database_session(session_factory) as session:
        items = []
        for item in data["items"]:
            connection = connection_in_session(session, item.get("connection_id", "default"))
            items.append({**item, "enabled": bool(item.get("enabled", True) and (connection is None or (connection["enabled"] and connection["encrypted_secret"])))})
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
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", model.id):
            raise ValueError("Model ID must use 1–32 lowercase letters, digits, hyphens or underscores.")
        if not model.model.strip() or len(model.model) > 255 or any(ord(c) < 32 for c in model.model):
            raise ValueError("Provide the exact provider model name (1–255 characters).")
        if model.wire_api not in {"", "responses", "chat-completions"}:
            raise ValueError("Unsupported text protocol.")
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
            from .text_connections import connection_in_session
            for item in models:
                connection = connection_in_session(session, item["connection_id"])
                if item["enabled"] and connection and not connection["enabled"]:
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


def snapshot_for_job(session, model_id: str | None, *, default_wire: str) -> dict:
    from .database import ServerProviderCredential
    from sqlalchemy import select
    catalog = catalog_in_session(session)
    model = resolve_from_catalog(model_id, catalog, require_enabled=True)
    from .text_connections import connection_in_session
    connection = connection_in_session(session, model.connection_id)
    if connection:
        if not connection["enabled"]:
            raise ValueError("The selected model's service connection is disabled.")
        return model_dict(replace(model, connection_revision=connection["revision"],
                                  wire_api=model.wire_api or connection["wire_api"]))
    credential = session.scalar(select(ServerProviderCredential).where(ServerProviderCredential.provider_kind == "text"))
    wire = model.wire_api or (credential.wire_api if credential else "") or default_wire
    return model_dict(replace(model, wire_api=wire))
