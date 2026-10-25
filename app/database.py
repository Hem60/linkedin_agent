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

from sqlalchemy import create_engine
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

# psycopg3 prepares a statement after its fifth execution. A pooler in
# transaction mode hands each transaction whichever server connection is free,
# so a statement prepared on one is not there on the next. Harmless on a
# session pooler, required on a transaction one.
_connect_args: dict[str, object] = {"prepare_threshold": None}

if _settings.db_schema:
    _schema = validated_schema(_settings.db_schema)
    # The schema goes in the startup packet, not a SET SESSION afterwards.
    #
    # This used to be a `SET SESSION search_path` on connect. On a transaction
    # pooler that is unsafe in a way that does not announce itself: session
    # state belongs to whichever server connection served it, so search_path
    # can quietly revert to `public` -- and a test suite pointed at `public`
    # claims real users' scheduled posts and marks them published through a
    # fake LinkedIn. A startup parameter travels with every server connection
    # the pooler opens, so there is no window where it is not set.
    #
    # Verified under contention before relying on it: six concurrent clients,
    # three transactions each, search_path held on all eighteen.
    _connect_args["options"] = f"-c search_path={_schema},public"

engine = create_engine(
    _settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,      # survive a Postgres connection dropped while idle
    # Supabase's session-mode pooler hands each client its own server
    # connection and reclaims the ones sitting idle. pool_pre_ping checks a
    # connection at checkout, which does not help when the pooler drops one
    # that is already in use: that surfaces as "server closed the connection
    # unexpectedly" in the middle of a statement. Recycling well inside the
    # pooler's own idle window means we retire connections before it does.
    #
    # 240 and not less. Dropping it to 60 was tried and was far worse: the
    # suite went from one connection error to about a hundred and fifty. Each
    # recycle opens a new client connection, and on a session-mode pooler each
    # of those holds a server connection of its own, so recycling four times
    # as often churns through the pooler's slots rather than staying ahead of
    # its idle sweep. Tightening this is the wrong lever; the right one is the
    # transaction-mode pooler on port 6543.
    pool_recycle=240,
    pool_size=5,
    max_overflow=5,
    future=True,
)



SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency. One session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
