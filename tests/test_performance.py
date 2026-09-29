"""Self-reported performance -- plan.md F26.

LinkedIn will not give an app the likes and comments on a member's own posts
(r_member_social is closed), so the person supplies them. The tests care about
what that implies: only ask about posts that really went out, take whatever the
user is willing to give, and never lose it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, performance
from app.database import SessionLocal
from app.main import app
from app.models import ImageSource, Post, PostCategory, PostStatus, Tone, User

UTC = dt.timezone.utc
SUB = "perfUserA"
OTHER = "perfUserB"


# ---------------------------------------------------------------------------
# Parsing what people type (offline)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("12", 12),
    ("  12 ", 12),
    ("1,204", 1204),          # people paste the number LinkedIn shows
    ("0", 0),
    ("", None),
    ("   ", None),
    ("lots", None),
    ("-5", 0),                # a negative count is a typo, not a value
])
def test_counts_are_read_forgivingly(given, expected):
    assert performance._clean_count(given) == expected


def test_an_absurd_number_is_capped():
    assert performance._clean_count("99999999999") == 10_000_000


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

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
        s.execute(text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:perf%'"))
        s.commit()

    purge()
    yield
    purge()
    s.close()


def _client(monkeypatch, sub=SUB) -> TestClient:
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": sub, "name": "Perf Test", "email": f"{sub}@x.com"})
    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def _user(db, sub=SUB) -> User:
    return db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{sub}").one()


def _post(db, user, *, status=PostStatus.posted, days_ago=5) -> Post:
    when = dt.datetime.now(UTC) - dt.timedelta(days=days_ago)
    p = Post(user_id=user.id, topic="a published topic", post_category=PostCategory.learning,
             tone=Tone.direct, content="Body.", hashtags=["AI"], status=status,
             image_source=ImageSource.none,
             posted_at=when if status in (PostStatus.posted, PostStatus.dry_run) else None,
             linkedin_post_id="urn:li:share:1" if status is PostStatus.posted else None)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Who gets asked
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_an_old_published_post_is_asked_about(db, monkeypatch):
    _client(monkeypatch)
    post = _post(db, _user(db), days_ago=5)
    assert [p.id for p in performance.pending(db, _user(db))] == [post.id]


@pytest.mark.integration
def test_a_post_published_today_is_left_alone(db, monkeypatch):
    """Nothing to report yet, and asking immediately trains people to ignore it."""
    _client(monkeypatch)
    _post(db, _user(db), days_ago=0)
    assert performance.pending(db, _user(db)) == []


@pytest.mark.integration
def test_a_dry_run_is_never_asked_about(db, monkeypatch):
    """It never reached LinkedIn, so there is nothing to have gone well."""
    _client(monkeypatch)
    _post(db, _user(db), status=PostStatus.dry_run, days_ago=5)
    assert performance.pending(db, _user(db)) == []


@pytest.mark.integration
def test_a_draft_is_never_asked_about(db, monkeypatch):
    _client(monkeypatch)
    _post(db, _user(db), status=PostStatus.draft, days_ago=5)
    assert performance.pending(db, _user(db)) == []


@pytest.mark.integration
def test_answering_stops_the_asking(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance", data={"likes": "12", "comments": "3"})
    assert performance.pending(db, _user(db)) == []


@pytest.mark.integration
def test_only_your_own_posts_are_asked_about(db, monkeypatch):
    _client(monkeypatch, OTHER)
    _post(db, _user(db, OTHER))
    _client(monkeypatch, SUB)
    assert performance.pending(db, _user(db, SUB)) == []


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_numbers_are_stored(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    r = client.post(f"/posts/{post.id}/performance", data={"likes": "42", "comments": "7"})
    assert r.status_code == 303

    row = performance.reported(db, _user(db))[post.id]
    assert row.likes == 42 and row.comments == 7


@pytest.mark.integration
def test_a_thumbs_up_counts_as_an_answer(db, monkeypatch):
    """Most people will not count comments. A weak signal offered willingly
    beats a precise one nobody gives."""
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance", data={"sentiment": "up"})

    row = performance.reported(db, _user(db))[post.id]
    assert row.sentiment == "up"
    assert row.likes is None
    assert performance.pending(db, _user(db)) == []


@pytest.mark.integration
def test_an_answer_can_be_corrected(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance", data={"likes": "4"})
    client.post(f"/posts/{post.id}/performance", data={"likes": "40", "comments": "2"})

    row = performance.reported(db, _user(db))[post.id]
    assert row.likes == 40 and row.comments == 2
    assert db.execute(
        text("SELECT count(*) FROM post_performance WHERE post_id = :p"), {"p": post.id}
    ).scalar() == 1


@pytest.mark.integration
def test_rubbish_input_is_ignored_rather_than_stored(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance",
                data={"likes": "several", "comments": "", "sentiment": "sideways"})

    row = performance.reported(db, _user(db))[post.id]
    assert row.likes is None and row.comments is None and row.sentiment is None


@pytest.mark.integration
def test_an_unpublished_post_cannot_be_rated(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db), status=PostStatus.draft, days_ago=5)
    assert client.post(f"/posts/{post.id}/performance",
                       data={"likes": "5"}).status_code == 400


@pytest.mark.integration
def test_someone_elses_post_cannot_be_rated(db, monkeypatch):
    _client(monkeypatch, OTHER)
    victim = _post(db, _user(db, OTHER))
    client = _client(monkeypatch, SUB)

    assert client.post(f"/posts/{victim.id}/performance",
                       data={"likes": "999"}).status_code == 404
    assert performance.reported(db, _user(db, OTHER)) == {}


# ---------------------------------------------------------------------------
# Where it appears
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_dashboard_asks(db, monkeypatch):
    client = _client(monkeypatch)
    _post(db, _user(db))
    page = client.get("/").text
    assert "How did this post do?" in page
    assert "Went well" in page


@pytest.mark.integration
def test_the_dashboard_stops_asking_once_answered(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance", data={"sentiment": "down"})
    assert "How did this post do?" not in client.get("/").text


@pytest.mark.integration
def test_the_published_post_page_asks_too(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    assert "How did it do?" in client.get(f"/posts/{post.id}").text


@pytest.mark.integration
def test_history_shows_what_was_reported(db, monkeypatch):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post(f"/posts/{post.id}/performance", data={"likes": "42", "comments": "7"})

    page = client.get("/posts").text
    assert "42" in page and "likes" in page
