"""Alembic environment for the hosted PostgreSQL database."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from review_writer_api.config import database_url_from_env
from review_writer_api.database import Base
import review_writer_api.workflow_models  # noqa: F401 - registers workflow metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = database_url_from_env()
if not database_url:
    raise RuntimeError("Set the PostgreSQL connection environment variables before running Alembic.")
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
