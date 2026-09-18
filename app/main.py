"""FastAPI application entry point.

Phase 1: configuration, Sentry, sessions, and a health check that actually
touches the database -- a health endpoint that only returns {"ok": true} tells
you nothing you didn't already know.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("app")

settings = get_settings()

if settings.sentry_dsn:
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=0.1,
        send_default_pii=False,   # never ship member data to Sentry
    )
    log.info("Sentry enabled")
else:
    log.info("Sentry disabled (no SENTRY_DSN)")

app = FastAPI(title="PostPilot", version="0.1.0")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=not settings.app_url.startswith("http://localhost"),
    same_site="lax",           # must survive the LinkedIn OAuth redirect back
)


@app.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness plus a real database round trip."""
    db_ok = False
    detail = ""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001 -- health check reports, never raises
        detail = f"{type(exc).__name__}: {exc}"[:200]

    return {
        "ok": db_ok,
        "database": "up" if db_ok else "down",
        "detail": detail,
        "dry_run": settings.dry_run,
        "linkedin_api_version": settings.linkedin_api_version,
    }


@app.get("/")
def index() -> dict[str, str]:
    return {"app": "PostPilot", "status": "phase 1 skeleton"}
