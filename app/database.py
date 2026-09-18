"""Database engine, session factory and declarative base.

Every query in this project filters by user_id (plan section 13). Nothing here
enforces that -- it is enforced in the query helpers added in phase 2 -- but the
session is deliberately short-lived per request so a leaked session cannot carry
one user's context into another's.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all models (defined in phase 2)."""


_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,      # survive a Postgres connection dropped while idle
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
