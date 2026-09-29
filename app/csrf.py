"""Cross-site request forgery protection -- plan.md section 13.

Without this, another site can make a signed-in user's browser submit to this
app. The dangerous ones here are not hypothetical: a hidden form could publish
a post under someone's real name, or delete their account.

Two independent checks, because each fails differently:

* **A token** tied to the session, required on every unsafe request. An
  attacker cannot read it -- it lives in a cookie they cannot see and a page
  they cannot fetch.
* **The Origin header**, when the browser sends one. This catches a request
  that somehow carried a token but came from elsewhere, and costs nothing.

SameSite=Lax on the session cookie is a third layer already in place: browsers
will not send the cookie on a cross-site POST at all. It is not relied on
alone, because it is the browser's promise rather than ours, and older ones
break it.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

log = logging.getLogger(__name__)

FIELD = "_csrf"
HEADER = "x-csrf-token"
SESSION_KEY = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def token_for(request: Request) -> str:
    """The session's token, created on first use."""
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def field(request: Request) -> Markup:
    """The hidden input every form needs. Used from templates as {{ csrf() }}."""
    return Markup(f'<input type="hidden" name="{FIELD}" value="{token_for(request)}">')


def _origin_ok(request: Request) -> bool:
    """Compare the browser's stated origin with where this app lives.

    Absent is allowed: some browsers omit Origin on same-origin form posts, and
    the token is the real check. Present-but-wrong is refused.
    """
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated:
        return True
    expected = urlparse(get_settings().app_url)
    actual = urlparse(stated)
    return (actual.scheme, actual.hostname, actual.port) == \
           (expected.scheme, expected.hostname, expected.port)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Refuse unsafe requests that cannot prove they came from our own pages."""

    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS:
            # Mint the token on any page view, so it exists before a form needs
            # it -- including on pages that render no form and on the first
            # request of a brand-new session.
            token_for(request)
            return await call_next(request)

        if not _origin_ok(request):
            log.warning("blocked %s %s: origin %r",
                        request.method, request.url.path,
                        request.headers.get("origin") or request.headers.get("referer"))
            return _refused(request)

        expected = request.session.get(SESSION_KEY)
        # An empty header counts as no header: only a real value should stop
        # us looking in the body, where a browser actually puts the token.
        supplied = request.headers.get(HEADER) or None

        if supplied is None and "form" in (request.headers.get("content-type") or ""):
            # Reading the body here would normally consume the request stream,
            # leaving the route with an empty form and every field "missing".
            # BaseHTTPMiddleware guards against exactly that: once .body() has
            # been awaited it replays the cached bytes downstream. So read the
            # body first, and only then parse it.
            await request.body()
            form = await request.form()          # parses the cached body
            supplied = form.get(FIELD)

        if not expected or not supplied or not secrets.compare_digest(str(supplied), expected):
            log.warning("blocked %s %s: bad or missing CSRF token",
                        request.method, request.url.path)
            return _refused(request)

        return await call_next(request)


def _refused(request: Request) -> HTMLResponse:
    """A plain page, not a JSON body: this reaches a person in a browser.

    403 rather than 404 here, unlike ownership checks -- nothing is being
    revealed about what exists, and "your session expired, try again" is the
    honest and usually correct explanation.
    """
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>Session expired</title>"
        "<body style='font:16px/1.6 system-ui;max-width:32em;margin:15vh auto;padding:0 20px'>"
        "<h1 style='font-size:20px'>That request couldn't be verified</h1>"
        "<p>Your session may have expired, or the request came from another site. "
        "Nothing was changed.</p>"
        "<p><a href='/'>Back to PostPilot</a></p>",
        status_code=403,
    )
