"""Sign-in flow -- plan.md sections 6 and 13.

LinkedIn is never called: `exchange_code_for_token` and `fetch_userinfo` are
replaced, so these test our logic rather than the network.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, security
from app.database import SessionLocal
from app.main import app

pytestmark = pytest.mark.integration

FAKE_TOKEN = "AQV8-fake-access-token"
PERSON_SUB = "testSub_PnM"
PERSON_URN = f"urn:li:person:{PERSON_SUB}"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _cleanup(db):
    def purge():
        db.rollback()
        db.execute(
            text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:testSub%'")
        )
        db.commit()

    purge()
    yield
    purge()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        auth, "exchange_code_for_token",
        lambda code: {"access_token": FAKE_TOKEN, "expires_in": 5_184_000},
    )
    monkeypatch.setattr(
        auth, "fetch_userinfo",
        lambda token: {"sub": PERSON_SUB, "name": "Test Person", "email": "t@example.com"},
    )
    return TestClient(app, follow_redirects=False)


def _sign_in(client) -> None:
    """Walk the real flow: /auth/login to seed state, then the callback."""
    login = client.get("/auth/login")
    assert login.status_code == 302
    state = login.headers["location"].split("state=")[1].split("&")[0]
    cb = client.get(f"/auth/callback?code=abc&state={state}")
    assert cb.status_code == 302, cb.text


# ---------------------------------------------------------------------------
# The state parameter
# ---------------------------------------------------------------------------

def test_login_redirects_to_linkedin_with_state_and_scopes(client):
    r = client.get("/auth/login")
    loc = r.headers["location"]
    assert loc.startswith(auth.AUTHORIZE_URL)
    assert "state=" in loc
    assert "w_member_social" in loc
    assert "response_type=code" in loc


def test_callback_without_state_is_rejected(client):
    assert client.get("/auth/callback?code=abc").status_code == 400


def test_callback_with_a_forged_state_is_rejected(client):
    client.get("/auth/login")
    r = client.get("/auth/callback?code=abc&state=attacker-chosen-value")
    assert r.status_code == 400


def test_state_is_single_use(client, db, monkeypatch):
    """A replayed callback must not re-run the exchange.

    Browsers do deliver this callback twice in practice, so the second attempt
    redirects an already-signed-in user rather than erroring -- but it must not
    call LinkedIn again.
    """
    login = client.get("/auth/login")
    state = login.headers["location"].split("state=")[1].split("&")[0]
    assert client.get(f"/auth/callback?code=abc&state={state}").status_code == 302

    calls: list[str] = []
    monkeypatch.setattr(
        auth, "exchange_code_for_token",
        lambda code: calls.append(code) or {"access_token": FAKE_TOKEN, "expires_in": 1},
    )
    replay = client.get(f"/auth/callback?code=abc&state={state}")
    assert replay.status_code == 302
    assert replay.headers["location"] == "/"
    assert calls == [], "a replayed callback must not exchange the code again"


def test_a_forged_callback_cannot_sign_in_a_stranger(client):
    """No session and no matching state means no sign-in, full stop."""
    r = client.get("/auth/callback?code=abc&state=attacker-value")
    assert r.status_code == 400


def test_declined_sign_in_returns_home_without_an_error_page(client):
    r = client.get("/auth/callback?error=user_cancelled_login")
    assert r.status_code == 302
    assert r.headers["location"] == "/?error=declined"


# ---------------------------------------------------------------------------
# What gets stored
# ---------------------------------------------------------------------------

def test_sign_in_creates_the_user(client, db):
    _sign_in(client)
    user = db.scalar(text("SELECT id FROM users WHERE linkedin_person_urn = :u").bindparams(u=PERSON_URN))
    assert user is not None


def test_access_token_is_encrypted_at_rest(client, db):
    _sign_in(client)
    stored = db.execute(
        text("SELECT access_token FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).scalar_one()
    assert stored is not None
    assert FAKE_TOKEN not in stored, "raw token found in the database"
    assert security.decrypt(stored) == FAKE_TOKEN


def test_token_expiry_is_recorded_about_sixty_days_out(client, db):
    _sign_in(client)
    expires = db.execute(
        text("SELECT token_expires_at FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).scalar_one()
    days = (expires - dt.datetime.now(dt.UTC)).days
    assert 58 <= days <= 61, f"expected ~60 days, got {days}"


def test_signing_in_twice_does_not_duplicate_the_user(client, db):
    _sign_in(client)
    _sign_in(client)
    count = db.execute(
        text("SELECT count(*) FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).scalar_one()
    assert count == 1


def test_first_sign_in_goes_to_onboarding_and_later_ones_go_home(client, db):
    login = client.get("/auth/login")
    state = login.headers["location"].split("state=")[1].split("&")[0]
    first = client.get(f"/auth/callback?code=abc&state={state}")
    assert first.headers["location"] == "/onboarding"

    db.execute(
        text("UPDATE users SET onboarded_at = now() WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    )
    db.commit()

    login = client.get("/auth/login")
    state = login.headers["location"].split("state=")[1].split("&")[0]
    second = client.get(f"/auth/callback?code=abc&state={state}")
    assert second.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Session behaviour
# ---------------------------------------------------------------------------

def test_onboarding_requires_sign_in(client):
    assert TestClient(app).get("/onboarding").status_code == 401


def test_logout_clears_the_session(client):
    _sign_in(client)
    assert client.get("/onboarding").status_code == 200
    client.get("/auth/logout")
    assert client.get("/onboarding").status_code == 401


def test_soft_deleted_user_is_treated_as_signed_out(client, db):
    _sign_in(client)
    db.execute(
        text("UPDATE users SET deleted_at = now() WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    )
    db.commit()
    assert client.get("/onboarding").status_code == 401


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

def test_onboarding_saves_a_personal_profile(client, db):
    _sign_in(client)
    r = client.post(
        "/onboarding",
        data={
            "account_type": "personal",
            "role_title": "ML Engineer",
            "niche": "RAG systems",
            "default_tone": "storytelling",
            "timezone": "Asia/Kolkata",
        },
    )
    assert r.status_code == 303
    row = db.execute(
        text("SELECT role_title, niche, default_tone, timezone, onboarded_at, account_type"
             " FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).one()
    assert row.role_title == "ML Engineer"
    assert row.niche == "RAG systems"
    assert row.default_tone == "storytelling"
    assert row.timezone == "Asia/Kolkata"
    assert row.onboarded_at is not None
    assert row.account_type == "personal"


def test_onboarding_saves_business_context(client, db):
    _sign_in(client)
    client.post(
        "/onboarding",
        data={
            "account_type": "business",
            "role_title": "Founder",
            "niche": "industrial valves",
            "default_tone": "direct",
            "timezone": "UTC",
            "company_name": "Acme Valves",
            "products": "ball valves",
            "target_markets": "UAE, Germany",
            "buyer_types": "importers",
        },
    )
    row = db.execute(
        text("SELECT account_type, company_name, buyer_types FROM users"
             " WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).one()
    assert row.account_type == "business"
    assert row.company_name == "Acme Valves"
    assert row.buyer_types == "importers"


def test_switching_back_to_personal_clears_company_details(client, db):
    """Stale company context must not leak into a later personal post."""
    _sign_in(client)
    client.post("/onboarding", data={
        "account_type": "business", "company_name": "Acme Valves",
        "products": "ball valves", "timezone": "UTC", "default_tone": "direct",
    })
    client.post("/onboarding", data={
        "account_type": "personal", "role_title": "Engineer",
        "timezone": "UTC", "default_tone": "direct",
    })
    row = db.execute(
        text("SELECT company_name, products FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).one()
    assert row.company_name == ""
    assert row.products == ""


def test_an_unknown_timezone_falls_back_to_utc(client, db):
    """A bad zone here would only surface at publish time, far from the cause."""
    _sign_in(client)
    client.post("/onboarding", data={
        "account_type": "personal", "timezone": "Mars/Olympus_Mons",
        "default_tone": "direct",
    })
    tz = db.execute(
        text("SELECT timezone FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).scalar_one()
    assert tz == "UTC"


def test_an_unknown_tone_falls_back_to_professional(client, db):
    _sign_in(client)
    client.post("/onboarding", data={
        "account_type": "personal", "timezone": "UTC", "default_tone": "shouty",
    })
    tone = db.execute(
        text("SELECT default_tone FROM users WHERE linkedin_person_urn = :u"),
        {"u": PERSON_URN},
    ).scalar_one()
    assert tone == "professional"
