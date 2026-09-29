"""Daily limits, email and the admin view -- plan.md F27, F24, F5.

The limit tests that matter are about races and calendars: two requests at once
must not both slip through, and "today" must mean the user's today.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, limits, llm, notify
from app.database import SessionLocal
from app.main import app
from app.models import ModelUsed, Post, PostStatus, User
from app.templating import is_admin

UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Email: offline, because it must work before a key exists
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, text_=""):
        self.status_code = status
        self.text = text_


@pytest.fixture
def email_on(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda: type(
        "S", (), {"resend_api_key": "re_key", "email_from": "PostPilot <x@y.dev>"})())


def _user(email="hem@example.com", name="Hem Patel") -> User:
    u = User(linkedin_person_urn="urn:li:person:x", name=name, email=email)
    u.timezone = "Asia/Kolkata"
    return u


def test_email_is_off_without_a_key(monkeypatch):
    """The app must be fully usable before the key exists."""
    monkeypatch.setattr(notify, "get_settings", lambda: type(
        "S", (), {"resend_api_key": "", "email_from": ""})())
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: pytest.fail("must not send"))
    assert notify.send("a@b.com", "s", "b") is False


def test_a_send_failure_never_raises(email_on, monkeypatch):
    """A publish that succeeded and then failed to send a receipt is still a
    successful publish."""
    def boom(*a, **k):
        raise notify.httpx.ConnectError("no network")
    monkeypatch.setattr(notify.httpx, "post", boom)
    assert notify.send("a@b.com", "s", "b") is False


def test_a_rejected_send_is_reported_not_raised(email_on, monkeypatch):
    """Resend returns 403 for unverified domains on the free tier."""
    monkeypatch.setattr(notify.httpx, "post",
                        lambda *a, **k: FakeResponse(403, "domain not verified"))
    assert notify.send("a@b.com", "s", "b") is False


def test_a_successful_send_reports_true(email_on, monkeypatch):
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: FakeResponse(200))
    assert notify.send("a@b.com", "s", "b") is True


def test_the_failure_email_says_nothing_was_posted(email_on, monkeypatch):
    sent = {}
    monkeypatch.setattr(notify.httpx, "post",
                        lambda url, **k: sent.update(k["json"]) or FakeResponse(200))
    post = Post(id=7, topic="my topic", error_message="LinkedIn is unavailable (503).")
    notify.post_failed(_user(), post, "http://localhost:8000")

    body = sent["text"]
    assert "my topic" in body
    assert "Nothing was posted to LinkedIn" in body
    assert "503" in body
    assert "/posts/7" in body


def test_the_expiry_email_explains_why_it_recurs(email_on, monkeypatch):
    sent = {}
    monkeypatch.setattr(notify.httpx, "post",
                        lambda url, **k: sent.update(k["json"]) or FakeResponse(200))
    notify.token_expiring(_user(), 3, "http://localhost:8000")
    assert "in 3 days" in sent["text"]
    assert "60 days" in sent["text"]
    assert "/auth/login" in sent["text"]


def test_addresses_are_masked_in_logs():
    """Logs should not become a list of everyone's email address."""
    masked = notify._mask("hem.patel@example.com")
    assert masked == "he***@example.com"
    assert "patel" not in masked


# ---------------------------------------------------------------------------
# Who counts as an admin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("admins,email,expected", [
    ("boss@x.com", "boss@x.com", True),
    ("boss@x.com", "BOSS@X.com", True),          # case is not identity
    ("boss@x.com,other@x.com", "other@x.com", True),
    ("boss@x.com", "someone@x.com", False),
    ("", "boss@x.com", False),                    # unset means nobody
])
def test_admin_membership(monkeypatch, admins, email, expected):
    from app import templating
    monkeypatch.setattr(templating, "get_settings", lambda: type(
        "S", (), {"admin_email_list": [e.strip().lower() for e in admins.split(",") if e.strip()]})())
    assert is_admin(_user(email=email)) is expected


def test_nobody_is_an_admin_when_signed_out():
    assert is_admin(None) is False


# ---------------------------------------------------------------------------
# Limits (database)
# ---------------------------------------------------------------------------

SUB = "limitsUserA"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge(request):
    if "integration" not in request.keywords:
        yield
        return
    s = SessionLocal()

    def purge():
        s.rollback()
        s.execute(text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:limits%'"))
        s.commit()

    purge()
    yield
    purge()
    s.close()


def _signed_in(monkeypatch, sub=SUB) -> TestClient:
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": sub, "name": "Limits Test", "email": f"{sub}@x.com"})
    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def _db_user(db, sub=SUB) -> User:
    return db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{sub}").one()


def _limit(monkeypatch, n):
    monkeypatch.setattr(limits, "get_settings", lambda: type(
        "S", (), {"daily_post_limit": n})())


@pytest.mark.integration
def test_taking_posts_counts_up(db, monkeypatch):
    client = _signed_in(monkeypatch)
    user = _db_user(db)
    _limit(monkeypatch, 3)

    assert limits.take_post(db, user).posts == 1
    assert limits.take_post(db, user).posts == 2
    assert limits.current(db, user).posts_left == 1


@pytest.mark.integration
def test_the_limit_is_enforced(db, monkeypatch):
    _signed_in(monkeypatch)
    user = _db_user(db)
    _limit(monkeypatch, 2)

    limits.take_post(db, user)
    limits.take_post(db, user)
    with pytest.raises(limits.LimitReached) as exc:
        limits.take_post(db, user)
    assert "resets in" in str(exc.value)


@pytest.mark.integration
def test_a_refund_gives_the_post_back(db, monkeypatch):
    """Nothing was billed when every model was down, so nothing is charged."""
    _signed_in(monkeypatch)
    user = _db_user(db)
    _limit(monkeypatch, 3)

    limits.take_post(db, user)
    limits.refund_post(db, user)
    assert limits.current(db, user).posts == 0


@pytest.mark.integration
def test_a_refund_cannot_go_negative(db, monkeypatch):
    _signed_in(monkeypatch)
    user = _db_user(db)
    _limit(monkeypatch, 3)
    limits.refund_post(db, user)
    limits.refund_post(db, user)
    assert limits.current(db, user).posts == 0


@pytest.mark.integration
def test_zero_means_unlimited(db, monkeypatch):
    """For a private deployment where the quota is nobody else's problem."""
    _signed_in(monkeypatch)
    user = _db_user(db)
    _limit(monkeypatch, 0)
    for _ in range(10):
        limits.take_post(db, user)


@pytest.mark.integration
def test_the_day_is_the_users_day_not_utc(db, monkeypatch):
    """23:00 UTC is already tomorrow in India. A UTC-based limit would reset in
    the middle of their morning."""
    _signed_in(monkeypatch)
    user = _db_user(db)
    user.timezone = "Asia/Kolkata"        # sign-in defaults to UTC; onboarding sets this
    late_utc = dt.datetime(2026, 9, 21, 23, 0, tzinfo=UTC)
    assert limits.local_date(user, now=late_utc) == dt.date(2026, 9, 22)

    user.timezone = "America/Los_Angeles"
    assert limits.local_date(user, now=late_utc) == dt.date(2026, 9, 21)


@pytest.mark.integration
def test_two_requests_at_once_cannot_both_pass_the_last_slot(db, monkeypatch):
    """The race the single-statement check exists for: read-decide-write would
    let both see 2 and both write 3."""
    _signed_in(monkeypatch)
    user_id = _db_user(db).id
    _limit(monkeypatch, 3)

    limits.take_post(db, _db_user(db))
    limits.take_post(db, _db_user(db))          # one slot left

    taken, refused = [], []
    barrier = threading.Barrier(2)

    def attempt():
        s = SessionLocal()
        try:
            barrier.wait(timeout=10)
            limits.take_post(s, s.get(User, user_id))
            taken.append(1)
        except limits.LimitReached:
            refused.append(1)
        finally:
            s.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(taken) == 1, f"{len(taken)} requests took the last slot"
    assert len(refused) == 1
    assert limits.current(db, _db_user(db)).posts == 3


# ---------------------------------------------------------------------------
# The limit as the user meets it
# ---------------------------------------------------------------------------

def _fake_generation(*a, **k):
    drafts = [llm.Draft(body=f"Draft {i}.", hashtags=["AI"], photo_query="x",
                        score={"hook": 8}, improvement="")
              for i in range(3)]
    return llm.Generation(drafts=drafts, model_used=ModelUsed.gemini, attempts=1)


@pytest.mark.integration
def test_the_form_shows_what_is_left(db, monkeypatch):
    client = _signed_in(monkeypatch)
    assert "left today" in client.get("/new").text


@pytest.mark.integration
def test_generating_beyond_the_limit_is_refused_before_the_model_is_called(db, monkeypatch):
    client = _signed_in(monkeypatch)
    user = _db_user(db)
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 1})())
    monkeypatch.setattr(llm, "generate", _fake_generation)

    first = client.post("/new", data={"topic": "a good topic", "post_category": "learning",
                                      "tone": "direct"})
    assert first.status_code == 303

    monkeypatch.setattr(llm, "generate",
                        lambda *a, **k: pytest.fail("model called past the limit"))
    second = client.post("/new", data={"topic": "another topic", "post_category": "learning",
                                       "tone": "direct"})
    assert second.status_code == 200
    assert "used all 1 posts" in second.text


@pytest.mark.integration
def test_a_failed_generation_does_not_cost_a_post(db, monkeypatch):
    client = _signed_in(monkeypatch)
    user = _db_user(db)
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 3})())

    def down(*a, **k):
        raise llm.GenerationError("both providers down")
    monkeypatch.setattr(llm, "generate", down)

    client.post("/new", data={"topic": "a good topic", "post_category": "learning",
                              "tone": "direct"})
    assert limits.current(db, _db_user(db)).posts == 0


# ---------------------------------------------------------------------------
# Admin page
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_admin_page_is_invisible_to_ordinary_users(db, monkeypatch):
    client = _signed_in(monkeypatch)
    from app import admin
    monkeypatch.setattr(admin, "get_settings", lambda: type(
        "S", (), {"admin_email_list": ["someone.else@x.com"]})())
    assert client.get("/admin").status_code == 404


@pytest.mark.integration
def test_the_admin_page_is_invisible_when_no_admins_are_configured(db, monkeypatch):
    client = _signed_in(monkeypatch)
    from app import admin
    monkeypatch.setattr(admin, "get_settings", lambda: type(
        "S", (), {"admin_email_list": []})())
    assert client.get("/admin").status_code == 404


@pytest.mark.integration
def test_an_admin_sees_usage_but_not_post_text(db, monkeypatch):
    client = _signed_in(monkeypatch)
    user = _db_user(db)
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 3})())
    limits.take_post(db, user)

    from app import admin
    from app import templating
    fake = type("S", (), {"admin_email_list": [f"{SUB}@x.com".lower()],
                          "daily_post_limit": 3, "dry_run": True,
                          "resend_api_key": "", "email_from": ""})()
    monkeypatch.setattr(admin, "get_settings", lambda: fake)
    monkeypatch.setattr(templating, "get_settings", lambda: fake)

    page = client.get("/admin")
    assert page.status_code == 200
    assert "Usage" in page.text
    assert f"{SUB}@x.com" in page.text


@pytest.mark.integration
def test_the_admin_link_only_appears_for_admins(db, monkeypatch):
    client = _signed_in(monkeypatch)
    from app import templating
    monkeypatch.setattr(templating, "get_settings", lambda: type(
        "S", (), {"admin_email_list": []})())
    assert 'href="/admin"' not in client.get("/posts").text

    monkeypatch.setattr(templating, "get_settings", lambda: type(
        "S", (), {"admin_email_list": [f"{SUB}@x.com".lower()]})())
    assert 'href="/admin"' in client.get("/posts").text


# ---------------------------------------------------------------------------
# Token expiry warnings
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_users_near_expiry_are_warned_once(db, monkeypatch):
    from worker import scheduler

    _signed_in(monkeypatch)
    user = _db_user(db)
    user.token_expires_at = dt.datetime.now(UTC) + dt.timedelta(days=3)
    user.expiry_notified_at = None
    db.commit()

    sent = []
    monkeypatch.setattr(notify, "token_expiring",
                        lambda u, d, url: sent.append((u.id, d)) or True)

    assert scheduler.warn_expiring_tokens(db) >= 1
    assert scheduler.warn_expiring_tokens(db) == 0, "must not warn again the next minute"
    assert len(sent) == 1

    db.expire_all()
    assert _db_user(db).expiry_notified_at is not None


@pytest.mark.integration
def test_a_healthy_token_is_not_warned_about(db, monkeypatch):
    from worker import scheduler

    _signed_in(monkeypatch)
    user = _db_user(db)
    user.token_expires_at = dt.datetime.now(UTC) + dt.timedelta(days=40)
    db.commit()

    monkeypatch.setattr(notify, "token_expiring",
                        lambda *a: pytest.fail("warned about a healthy token"))
    scheduler.warn_expiring_tokens(db)


@pytest.mark.integration
def test_reconnecting_clears_the_warning(db, monkeypatch):
    """So the next lapse warns again in two months rather than staying silent."""
    _signed_in(monkeypatch)
    user = _db_user(db)
    user.expiry_notified_at = dt.datetime.now(UTC)
    db.commit()

    _signed_in(monkeypatch)          # sign in again
    db.expire_all()
    assert _db_user(db).expiry_notified_at is None
