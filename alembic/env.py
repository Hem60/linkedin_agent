"""Alembic environment.

The database URL comes from the app's settings, so there is exactly one place
credentials live (.env) and alembic.ini stays safe to commit.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.config import get_settings
from app.database import Base, validated_schema

# Importing the models module registers every table on Base.metadata so that
# `alembic revision --autogenerate` can see them. Added in phase 2.
try:
    from app import models  # noqa: F401
except ImportError:  # phase 1: models.py does not exist yet
    pass

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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
    schema = get_settings().db_schema
    with connectable.connect() as connection:
        if schema:
            # Same isolation as app/database.py: create the schema, put it first
            # on the search path, and keep Alembic's own bookkeeping table there
            # too -- otherwise the test schema's migration history would be
            # written into the real public.alembic_version.
            schema = validated_schema(schema)
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            connection.execute(text(f"SET SESSION search_path TO {schema}, public"))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,        # catch column type changes
            compare_server_default=True,
            version_table_schema=schema or None,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
