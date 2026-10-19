"""Database engine, session factory and declarative base.

Every query in this project filters by user_id (plan section 13). Nothing here
enforces that -- it is enforced in the query helpers added in phase 2 -- but the
session is deliberately short-lived per request so a leaked session cannot carry
one user's context into another's.

Schema isolation: when DB_SCHEMA is set, every connection's search_path puts
that schema first, so unqualified table names -- which is all of them -- resolve
there. The test suite uses this to run against `test_postpilot` and never touch
the real tables in `public`. `public` stays on the path because the pg_trgm
extension's functions and operator classes live there.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Base(DeclarativeBase):
    """Declarative base for all models (defined in phase 2)."""


def validated_schema(name: str) -> str:
    """The schema name is interpolated into SQL, so it must be a plain
    identifier. It comes from our own config, but config is not a reason to
    skip the check."""
    if not SCHEMA_NAME.match(name):
        raise ValueError(f"DB_SCHEMA must be a lowercase identifier, got {name!r}")
    return name


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,      # survive a Postgres connection dropped while idle
    # Supabase's session-mode pooler hands each client its own server
    # connection and reclaims the ones sitting idle. pool_pre_ping checks a
    # connection at checkout, which does not help when the pooler drops one
    # that is already in use: that surfaces as "server closed the connection
    # unexpectedly" in the middle of a statement. Recycling well inside the
    # pooler's own idle window means we retire connections before it does.
    pool_recycle=240,
    pool_size=5,
    max_overflow=5,
    future=True,
)

if _settings.db_schema:
    _schema = validated_schema(_settings.db_schema)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record) -> None:
        # Autocommit for this one statement: a SET issued inside a transaction
        # is undone if that transaction rolls back, and SQLAlchemy rolls back
        # every connection on return to the pool. Without this the schema would
        # silently revert to `public` after the first request.
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        with dbapi_connection.cursor() as cur:
            cur.execute(f"SET SESSION search_path TO {_schema}, public")
        dbapi_connection.autocommit = previous


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. One session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
