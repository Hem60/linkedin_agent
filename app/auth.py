"""Sign in with LinkedIn (OpenID Connect) and session handling.

Flow, per plan.md section 6:

    /auth/login     -> redirect to LinkedIn with a single-use `state`
    /auth/callback  -> verify state, exchange code, read userinfo, upsert user
    /auth/logout    -> clear the session

The access token is encrypted before it touches the database (section 13), and
`token_expires_at` is recorded so F5 can warn the user before the 60-day expiry
that LinkedIn gives standard apps -- there is no refresh token to fall back on
(section 2).
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import csrf, security
from app.config import get_settings
from app.database import get_db
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

# `openid` and `profile` give identity, `email` the address, `w_member_social`
# the ability to post. All four are self-serve (plan section 2).
SCOPES = "openid profile email w_member_social"

STATE_KEY = "oauth_state"
USER_KEY = "user_id"


# ---------------------------------------------------------------------------
# LinkedIn calls, isolated so tests can replace them
# ---------------------------------------------------------------------------

def exchange_code_for_token(code: str) -> dict:
    s = get_settings()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": s.redirect_uri,
            "client_id": s.linkedin_client_id,
            "client_secret": s.linkedin_client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if resp.status_code != 200:
        # Never log the body -- it can echo the client secret back.
        log.error("token exchange failed: %s", resp.status_code)
        raise HTTPException(502, "LinkedIn rejected the sign-in. Please try again.")
    return resp.json()


def fetch_userinfo(access_token: str) -> dict:
    resp = httpx.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        log.error("userinfo failed: %s", resp.status_code)
        raise HTTPException(502, "Could not read your LinkedIn profile.")
    return resp.json()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in user, or None. Soft-deleted accounts count as signed out."""
    uid = request.session.get(USER_KEY)
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or user.deleted_at is not None:
        request.session.clear()
        return None
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in with LinkedIn first.")
    return user


def token_is_expiring(user: User, within_days: int = 7) -> bool:
    """F5: warn before the token dies, because reconnecting is manual."""
    if user.token_expires_at is None:
        return True
    remaining = user.token_expires_at - dt.datetime.now(dt.timezone.utc)
    return remaining <= dt.timedelta(days=within_days)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/login")
def login(request: Request) -> RedirectResponse:
    s = get_settings()
    s.require("linkedin_client_id", "linkedin_client_secret")

    state = secrets.token_urlsafe(32)
    request.session[STATE_KEY] = state

    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": s.linkedin_client_id,
            "redirect_uri": s.redirect_uri,
            "state": state,
            "scope": SCOPES,
        }
    )
    return RedirectResponse(f"{AUTHORIZE_URL}?{params}", status_code=302)


@router.get("/callback")
def callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    if error:
        log.info("sign-in declined: %s", error)
        return RedirectResponse("/?error=declined", status_code=302)

    # Single-use state: pop it, so a replayed callback cannot match.
    expected = request.session.pop(STATE_KEY, None)
    if not expected or not state or not secrets.compare_digest(state, expected):
        # Browsers sometimes deliver the callback twice, and users re-open the
        # URL from history. If they are already signed in, that is harmless --
        # send them on rather than showing an error for a completed sign-in.
        # The code is deliberately NOT exchanged here, so a crafted callback URL
        # still cannot establish or change a session.
        if request.session.get(USER_KEY):
            return RedirectResponse("/", status_code=302)
        raise HTTPException(400, "Sign-in could not be verified. Please start again.")
    if not code:
        raise HTTPException(400, "LinkedIn did not return an authorization code.")

    payload = exchange_code_for_token(code)
    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(502, "LinkedIn did not return an access token.")

    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=int(payload.get("expires_in", 5_184_000))
    )
    info = fetch_userinfo(access_token)
    sub = info.get("sub")
    if not sub:
        raise HTTPException(502, "LinkedIn did not return a member id.")

    user = upsert_user(
        db,
        person_urn=f"urn:li:person:{sub}",
        name=info.get("name", ""),
        email=info.get("email", ""),
        access_token=access_token,
        token_expires_at=expires_at,
    )

    request.session.clear()          # new session id contents after privilege change
    request.session[USER_KEY] = user.id
    # Clearing the session took the CSRF token with it. Mint a fresh one rather
    # than restoring the old: rotating on a privilege change is the point, and
    # without this the first POST after signing in has nothing to check against.
    csrf.token_for(request)

    return RedirectResponse(
        "/" if user.onboarded_at else "/onboarding", status_code=302
    )


@router.post("/logout")
@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def upsert_user(
    db: Session,
    *,
    person_urn: str,
    name: str,
    email: str,
    access_token: str,
    token_expires_at: dt.datetime,
) -> User:
    """Create on first sign-in, refresh the token on every later one.

    Identity is the person URN, not the email: LinkedIn emails can change, the
    URN cannot. A returning user keeps their posts, profile and history.
    """
    user = db.scalar(select(User).where(User.linkedin_person_urn == person_urn))
    if user is None:
        user = User(linkedin_person_urn=person_urn)
        db.add(user)

    user.name = name or user.name
    user.email = email or user.email
    user.access_token = security.encrypt(access_token)
    user.token_expires_at = token_expires_at
    # Reconnecting undoes a soft delete rather than stranding the account.
    user.deleted_at = None
    # A fresh token means the old expiry warning is spent; clearing it lets the
    # next lapse warn again in two months' time.
    user.expiry_notified_at = None

    db.commit()
    db.refresh(user)
    return user
