"""FastAPI application entry point.

Phase 1: configuration, Sentry, sessions, and a health check that actually
touches the database -- a health endpoint that only returns {"ok": true} tells
you nothing you didn't already know.
"""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import admin, auth, compose, csrf, onboarding, performance, privacy
from app.auth import current_user, token_is_expiring
from app.config import get_settings
from app.database import engine, get_db
from app.models import Post, User
from app.templating import templates

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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the publishing worker with the web app, stop it on shutdown.

    Plan section 14, option 1: on a free tier the web process is the only one
    there is, so it also publishes scheduled posts. RUN_SCHEDULER=false turns
    that off when a separate `python -m worker.scheduler` process runs instead
    -- though running both is safe, because claims use SKIP LOCKED.
    """
    from worker.scheduler import loop

    if settings.run_scheduler:
        loop.start()
    try:
        yield
    finally:
        if settings.run_scheduler:
            loop.stop()


app = FastAPI(title="PostPilot", version="0.1.0", lifespan=lifespan)

# Order matters: middleware added later runs first, so CSRF is registered
# before SessionMiddleware in order to run *after* it -- it needs the session.
app.add_middleware(csrf.CSRFMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=not settings.app_url.startswith("http://localhost"),
    same_site="lax",           # must survive the LinkedIn OAuth redirect back
)


ERROR_COPY = {
    400: ("That didn't go through", "The request couldn't be verified. Starting again usually fixes it."),
    401: ("You need to sign in", "Connect your LinkedIn account to continue."),
    403: ("Not allowed", "You don't have access to that."),
    404: ("Nothing here", "That page doesn't exist — or it moved."),
    500: ("Something broke on our side", "It's been logged. Try again in a moment."),
    502: ("LinkedIn didn't answer", "That's on their end, not yours. Try again shortly."),
}


def _render_error(request: Request, status: int, detail: str | None = None):
    heading, message = ERROR_COPY.get(
        status, ("Something went wrong", "An unexpected error occurred.")
    )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"status": status, "heading": heading, "message": detail or message, "user": None},
        status_code=status,
    )


# Starlette's stock reason phrases. Ours read better, so these never reach a user.
GENERIC_DETAILS = {
    "Not Found", "Unauthorized", "Forbidden", "Bad Request",
    "Internal Server Error", "Bad Gateway", "Method Not Allowed",
}


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    """Render errors as pages. A raw JSON body in the browser reads as broken,
    and this app is server-rendered -- there is no API client to serve.

    A hand-written detail (\"Sign-in could not be verified\") is worth showing;
    a framework reason phrase (\"Not Found\") is not.
    """
    detail = exc.detail if isinstance(exc.detail, str) else None
    if detail in GENERIC_DETAILS or exc.status_code >= 500:
        detail = None
    return _render_error(request, exc.status_code, detail)


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
        # Which model the deployment is actually generating with. The same
        # reasoning as the two above: a setting that changes behaviour and is
        # set per environment, so the only way to know what production is
        # really using was to sign in and generate an image. A model id is not
        # a secret; the token that reaches it is, and that stays out of here.
        "image_model": settings.image_model,
        "image_fallback_model": settings.image_fallback_model,
    }


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(onboarding.router)
app.include_router(compose.router)
app.include_router(admin.router)
app.include_router(privacy.router)
app.include_router(performance.router)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: User | None = Depends(current_user),
    db: Session = Depends(get_db),
    error: str | None = None,
):
    counts = {"draft": 0, "scheduled": 0, "posted": 0}
    days_left = None
    awaiting = []
    if user:
        awaiting = performance.pending(db, user)
        rows = db.execute(
            select(Post.status, func.count())
            .where(Post.user_id == user.id)
            .group_by(Post.status)
        ).all()
        for status, n in rows:
            key = status.value if hasattr(status, "value") else str(status)
            if key in counts:
                counts[key] = n
        if user.token_expires_at:
            days_left = (user.token_expires_at - dt.datetime.now(dt.UTC)).days
        if auth.token_is_dead(user):
            # LinkedIn has already refused it. The stored expiry is meaningless
            # now, and showing "50 days left" would be a straight lie.
            days_left = 0

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "user": user,
            "error": error,
            "counts": counts,
            "days_left": days_left,
            "token_expiring": bool(user) and token_is_expiring(user),
            "token_rejected": bool(user) and auth.token_is_dead(user),
            "awaiting": awaiting,
            "message": request.query_params.get("m"),
        },
    )
