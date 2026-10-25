"""A LinkedIn token that dies before its expiry date.

Found on a real account. The database said "50 days left" and the dashboard
drew a full green meter, while LinkedIn had already been answering 401 for
days. Nothing in the app noticed, because every check we had asked the stored
expiry date -- and a token does not only die of old age. Revoking the app or
changing the password kills it immediately.

Left alone, the member sees a healthy connection while each scheduled post
fails on its own, one at a time, with the explanation buried on the post.

So: a 401 is recorded on the account, the dashboard tells the truth, the next
publish gives up without spending another request, and reconnecting clears it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import auth, linkedin, publisher, security
from app.database import SessionLocal
from app.models import ImageSource, Post, PostCategory, PostStatus, Tone, User
from tests.conftest import purge_users

pytestmark = pytest.mark.integration

UTC = dt.UTC
SUB = "revokeUser"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge():
    s = SessionLocal()

    def purge():
        purge_users("revoke", s)

    purge()
    yield
    purge()
    s.close()


@pytest.fixture
def live(monkeypatch):
    """Not a dry run: these tests are about what the API answers."""
    class S:
        dry_run = False
        linkedin_api_version = "202609"
        app_url = "http://localhost:8000"

    monkeypatch.setattr(publisher, "get_settings", lambda: S())
    monkeypatch.setattr(publisher.notify, "post_failed", lambda *a, **k: False)


def make_user(db, *, rejected=None) -> User:
    u = User(
        linkedin_person_urn=f"urn:li:person:{SUB}",
        name="Revoke Test", email=f"{SUB}@x.com", timezone="UTC",
        access_token=security.encrypt("a-token-linkedin-no-longer-likes"),
        token_expires_at=dt.datetime.now(UTC) + dt.timedelta(days=50),
        token_rejected_at=rejected,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_post(db, user) -> Post:
    p = Post(user_id=user.id, topic="a topic", post_category=PostCategory.learning,
             tone=Tone.direct, content="Body (here).", hashtags=["AI"],
             status=PostStatus.draft, image_source=ImageSource.none)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def refuse(*a, **k):
    raise linkedin.LinkedInError(
        "Your LinkedIn connection has expired. Reconnect to publish again.",
        status=401, retryable=False)


# ---------------------------------------------------------------------------
# A 401 is recorded on the account, not only on the post
# ---------------------------------------------------------------------------

def test_a_refused_publish_marks_the_connection_dead(db, live, monkeypatch):
    monkeypatch.setattr(linkedin, "create_post", refuse)
    user = make_user(db)
    post = make_post(db, user)
    assert user.token_rejected_at is None

    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    assert db.get(User, user.id).token_rejected_at is not None


def test_the_post_itself_still_fails_readably(db, live, monkeypatch):
    """Marking the account must not cost the member the explanation."""
    monkeypatch.setattr(linkedin, "create_post", refuse)
    user = make_user(db)
    post = make_post(db, user)

    with pytest.raises(publisher.PublishError, match="Reconnect"):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    failed = db.get(Post, post.id)
    assert failed.status is PostStatus.failed
    assert "Reconnect" in failed.error_message


def test_an_ordinary_failure_does_not_mark_the_connection_dead(db, live, monkeypatch):
    """Only a 401 means the token. A 500 means LinkedIn is having a bad day."""
    def unavailable(*a, **k):
        raise linkedin.LinkedInError("LinkedIn is unavailable right now (503).",
                                     status=503, retryable=True)

    monkeypatch.setattr(linkedin, "create_post", unavailable)
    user = make_user(db)
    post = make_post(db, user)

    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    assert db.get(User, user.id).token_rejected_at is None


def test_the_first_rejection_time_is_kept(db, live, monkeypatch):
    """When it died matters more than when we last retried it."""
    monkeypatch.setattr(linkedin, "create_post", refuse)
    first = dt.datetime(2026, 1, 1, tzinfo=UTC)
    user = make_user(db, rejected=first)
    post = make_post(db, user)

    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    assert db.get(User, user.id).token_rejected_at == first


# ---------------------------------------------------------------------------
# Once known dead, stop asking
# ---------------------------------------------------------------------------

def test_a_known_dead_token_is_not_spent_on_another_request(db, live, monkeypatch):
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: pytest.fail("must not call LinkedIn again"))
    user = make_user(db, rejected=dt.datetime.now(UTC))
    post = make_post(db, user)

    with pytest.raises(publisher.PublishError, match="refused"):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    assert db.get(Post, post.id).status is PostStatus.failed


# ---------------------------------------------------------------------------
# What the member is told
# ---------------------------------------------------------------------------

def test_a_dead_token_counts_as_expiring_so_the_warning_path_fires(db):
    """Otherwise the only warning we have is driven by a date that still looks
    healthy for another fifty days."""
    healthy = make_user(db)
    assert not auth.token_is_expiring(healthy)
    assert not auth.token_is_dead(healthy)

    healthy.token_rejected_at = dt.datetime.now(UTC)
    db.commit()
    assert auth.token_is_dead(healthy)
    assert auth.token_is_expiring(healthy)


def test_the_dashboard_stops_claiming_the_connection_is_healthy(db, monkeypatch):
    """The visible half of the bug: a full green meter and "50 days left" for
    a connection LinkedIn had already refused."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": SUB, "name": "Revoke Test", "email": f"{SUB}@x.com"})
    client = TestClient(app, follow_redirects=False)
    state = client.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    client.get(f"/auth/callback?code=x&state={state}")

    healthy = client.get("/").text
    assert "days left" in healthy

    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{SUB}").one()
    user.token_rejected_at = dt.datetime.now(UTC)
    db.commit()

    dead = client.get("/").text
    assert "days left" not in dead, "still advertising a healthy connection"
    assert "Refused by LinkedIn" in dead
    assert "Reconnect" in dead


def test_reconnecting_clears_the_rejection(db):
    user = make_user(db, rejected=dt.datetime.now(UTC))

    auth.upsert_user(
        db,
        person_urn=user.linkedin_person_urn,
        name="Revoke Test", email=f"{SUB}@x.com",
        access_token="a-brand-new-token",
        token_expires_at=dt.datetime.now(UTC) + dt.timedelta(days=60),
    )

    db.expire_all()
    assert db.get(User, user.id).token_rejected_at is None
    assert not auth.token_is_dead(db.get(User, user.id))
