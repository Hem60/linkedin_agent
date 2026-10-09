"""Scheduling -- plan.md F19, phase 10.

Three layers, each with its own way to go wrong:

* timezone arithmetic, which is wrong silently: an hour off, on two nights a
  year, for users in daylight-saving zones only
* the reschedule race, which is wrong catastrophically: a post published twice
* the worker, which must keep going when one post fails and must never retry a
  publish that may already have reached LinkedIn
"""

from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, publisher, scheduling, security
from app.database import SessionLocal
from app.main import app
from app.models import (
    ImageSource,
    Post,
    PostCategory,
    PostStatus,
    Tone,
    User,
)
from worker import scheduler

UTC = dt.timezone.utc


def _user(tz="Asia/Kolkata", expires_in_days=50) -> User:
    u = User(linkedin_person_urn="urn:li:person:x", name="T")
    u.timezone = tz
    u.token_expires_at = dt.datetime.now(UTC) + dt.timedelta(days=expires_in_days)
    return u


# ---------------------------------------------------------------------------
# Timezone arithmetic (no database)
# ---------------------------------------------------------------------------

def test_india_time_converts_to_utc():
    # IST is UTC+05:30, no daylight saving.
    assert scheduling.parse_local("2026-09-23T09:30", "Asia/Kolkata") == \
        dt.datetime(2026, 9, 23, 4, 0, tzinfo=UTC)


def test_utc_passes_straight_through():
    assert scheduling.parse_local("2026-09-23T09:30", "UTC") == \
        dt.datetime(2026, 9, 23, 9, 30, tzinfo=UTC)


def test_a_time_that_does_not_exist_is_refused():
    """New York, 8 March 2026: clocks jump 01:59 -> 03:00. Accepting 02:30 would
    silently publish at 03:30 instead."""
    with pytest.raises(scheduling.ScheduleError, match="doesn't exist"):
        scheduling.parse_local("2026-03-08T02:30", "America/New_York")


def test_a_time_that_happens_twice_takes_the_first():
    """New York, 1 November 2026: 01:30 happens in EDT and again in EST. The
    first occurrence is what a person means."""
    assert scheduling.parse_local("2026-11-01T01:30", "America/New_York") == \
        dt.datetime(2026, 11, 1, 5, 30, tzinfo=UTC)      # EDT, UTC-4


def test_london_summer_and_winter_both_convert():
    assert scheduling.parse_local("2026-07-01T09:00", "Europe/London").hour == 8    # BST
    assert scheduling.parse_local("2026-12-01T09:00", "Europe/London").hour == 9    # GMT


@pytest.mark.parametrize("bad", ["", "tomorrow", "2026-13-01T09:00", "2026-09-23 09:30"])
def test_malformed_input_is_refused(bad):
    with pytest.raises(scheduling.ScheduleError, match="Pick a date"):
        scheduling.parse_local(bad, "UTC")


def test_an_unknown_zone_falls_back_to_utc():
    assert scheduling.parse_local("2026-09-23T09:30", "Mars/Olympus") == \
        dt.datetime(2026, 9, 23, 9, 30, tzinfo=UTC)


def test_display_shows_the_users_own_clock():
    shown = scheduling.display(dt.datetime(2026, 9, 23, 4, 0, tzinfo=UTC), "Asia/Kolkata")
    assert "09:30" in shown and "IST" in shown


def test_the_input_prefill_round_trips():
    when = dt.datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
    value = scheduling.input_value(when, "Asia/Kolkata")
    assert value == "2026-09-23T09:30"
    assert scheduling.parse_local(value, "Asia/Kolkata") == when


def test_the_suggestion_is_tomorrow_at_nine_local():
    now = dt.datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert scheduling.suggest("Asia/Kolkata", now=now) == "2026-09-22T09:00"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_the_past_is_refused():
    with pytest.raises(scheduling.ScheduleError, match="couple of minutes"):
        scheduling.validate(NOW - dt.timedelta(hours=1), _user(), now=NOW)


def test_too_soon_is_refused():
    """Otherwise 'schedule' is just a slower 'publish now'."""
    with pytest.raises(scheduling.ScheduleError):
        scheduling.validate(NOW + dt.timedelta(seconds=30), _user(), now=NOW)


def test_a_normal_time_is_accepted():
    scheduling.validate(NOW + dt.timedelta(hours=3), _user(), now=NOW)


def test_a_time_after_the_linkedin_token_dies_is_refused():
    """No refresh token (plan section 2): the publish would fail days later,
    long after the user stopped thinking about it."""
    u = _user()
    u.token_expires_at = NOW + dt.timedelta(days=5)
    with pytest.raises(scheduling.ScheduleError, match="expires on"):
        scheduling.validate(NOW + dt.timedelta(days=6), u, now=NOW)


# ---------------------------------------------------------------------------
# Routes (database)
# ---------------------------------------------------------------------------

SUB = "scheduleUserA"
OTHER = "scheduleUserB"


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
    purge = lambda: (s.rollback(), s.execute(text(
        "DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:schedule%'")), s.commit())
    purge()
    yield
    purge()
    s.close()


def _client(monkeypatch, sub) -> TestClient:
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": sub, "name": "Sched Test", "email": f"{sub}@x.com"})
    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def _draft(db, sub) -> int:
    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{sub}").one()
    user.timezone = "Asia/Kolkata"
    p = Post(user_id=user.id, topic="t", post_category=PostCategory.learning,
             tone=Tone.direct, content="Hello (world).", hashtags=["AI"],
             status=PostStatus.draft, image_source=ImageSource.none, chosen_draft=0)
    db.add(p)
    db.commit()
    return p.id


def _loc(response) -> str:
    """The redirect target, decoded -- messages travel URL-encoded in ?m=."""
    return urllib.parse.unquote(response.headers["location"])


def _future(hours=5) -> str:
    local = dt.datetime.now(UTC).astimezone(scheduling.zone("Asia/Kolkata"))
    return (local + dt.timedelta(hours=hours)).strftime(scheduling.INPUT_FORMAT)


@pytest.fixture
def client(monkeypatch):
    return _client(monkeypatch, SUB)


@pytest.mark.integration
def test_scheduling_stores_utc(client, db):
    pid = _draft(db, SUB)
    when = _future()
    r = client.post(f"/posts/{pid}/schedule", data={"when": when})
    assert r.status_code == 303 and "m=scheduled" in r.headers["location"]

    db.expire_all()
    post = db.get(Post, pid)
    assert post.status is PostStatus.scheduled
    assert post.scheduled_at == scheduling.parse_local(when, "Asia/Kolkata")
    assert post.scheduled_at.utcoffset() == dt.timedelta(0)


@pytest.mark.integration
def test_a_scheduled_post_shows_its_time_in_the_users_zone(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    page = client.get(f"/posts/{pid}").text
    assert "Scheduled" in page and "IST" in page
    assert "Cancel schedule" in page


@pytest.mark.integration
def test_rescheduling_moves_the_time(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future(5)})
    client.post(f"/posts/{pid}/schedule", data={"when": _future(9)})
    db.expire_all()
    assert db.get(Post, pid).scheduled_at == scheduling.parse_local(_future(9), "Asia/Kolkata")


@pytest.mark.integration
def test_cancelling_returns_to_draft(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    client.post(f"/posts/{pid}/unschedule")
    db.expire_all()
    post = db.get(Post, pid)
    assert post.status is PostStatus.draft
    assert post.scheduled_at is None


@pytest.mark.integration
def test_a_bad_time_is_explained_not_stored(client, db):
    pid = _draft(db, SUB)
    r = client.post(f"/posts/{pid}/schedule", data={"when": "2020-01-01T09:00"})
    assert "couple" in _loc(r)
    db.expire_all()
    assert db.get(Post, pid).status is PostStatus.draft


@pytest.mark.integration
def test_rescheduling_cannot_resurrect_a_post_being_published(client, db):
    """The race. The worker claims the row; a reschedule arrives a moment later.
    A plain save would flip `publishing` back to `scheduled`, and the post would
    go out now and again at the new time."""
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    db.execute(text("UPDATE posts SET status='publishing', claimed_at=now() WHERE id=:i"),
               {"i": pid})
    db.commit()

    r = client.post(f"/posts/{pid}/schedule", data={"when": _future(9)})
    assert "being published" in _loc(r)
    db.expire_all()
    assert db.get(Post, pid).status is PostStatus.publishing


@pytest.mark.integration
def test_cancelling_cannot_interrupt_a_post_being_published(client, db):
    pid = _draft(db, SUB)
    db.execute(text("UPDATE posts SET status='publishing', claimed_at=now() WHERE id=:i"),
               {"i": pid})
    db.commit()
    r = client.post(f"/posts/{pid}/unschedule")
    assert "Too late" in _loc(r)
    db.expire_all()
    assert db.get(Post, pid).status is PostStatus.publishing


@pytest.mark.integration
def test_a_scheduled_post_must_be_cancelled_before_editing(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    assert client.post(f"/posts/{pid}", data={"content": "x", "hashtags": ""}).status_code == 400


@pytest.mark.integration
def test_a_scheduled_post_must_be_cancelled_before_deleting(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    r = client.post(f"/posts/{pid}/delete")
    assert "Cancel the schedule" in _loc(r)
    db.expire_all()
    assert db.get(Post, pid) is not None


@pytest.mark.integration
def test_a_scheduled_post_can_be_published_now_instead(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    client.post(f"/posts/{pid}/publish")
    db.expire_all()
    assert db.get(Post, pid).status is PostStatus.dry_run


@pytest.mark.integration
def test_scheduled_time_shows_in_history(client, db):
    pid = _draft(db, SUB)
    client.post(f"/posts/{pid}/schedule", data={"when": _future()})
    assert "IST" in client.get("/posts").text


@pytest.mark.integration
def test_someone_elses_post_cannot_be_scheduled(client, monkeypatch, db):
    """Both routes against one arrangement: neither changes anything, so the
    second sign-in and draft the parametrised version built were wasted."""
    other = _client(monkeypatch, OTHER)
    pid = _draft(db, OTHER)
    other.post(f"/posts/{pid}/schedule", data={"when": _future()})

    for path in ("/schedule", "/unschedule"):
        r = client.post(f"/posts/{pid}{path}", data={"when": _future(9)})
        assert r.status_code == 404, f"{path} returned {r.status_code}"


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_user(db):
    u = User(linkedin_person_urn="urn:li:person:scheduleWorker", name="W",
             email="w@x.com", timezone="UTC",
             access_token=security.encrypt("tok"),
             token_expires_at=dt.datetime.now(UTC) + dt.timedelta(days=30))
    db.add(u)
    db.commit()
    return u


def _post(db, user, *, status, at=None, claimed_at=None) -> int:
    p = Post(user_id=user.id, topic="w", post_category=PostCategory.learning,
             tone=Tone.direct, content="body", hashtags=[], status=status,
             scheduled_at=at, claimed_at=claimed_at, image_source=ImageSource.none)
    db.add(p)
    db.commit()
    return p.id


@pytest.mark.integration
def test_the_worker_publishes_what_is_due(db, worker_user):
    due = _post(db, worker_user, status=PostStatus.scheduled,
                at=dt.datetime.now(UTC) - dt.timedelta(minutes=1))
    later = _post(db, worker_user, status=PostStatus.scheduled,
                  at=dt.datetime.now(UTC) + dt.timedelta(hours=2))

    result = scheduler.run_once()
    assert result.published >= 1
    db.expire_all()
    assert db.get(Post, due).status is PostStatus.dry_run
    assert db.get(Post, later).status is PostStatus.scheduled


@pytest.mark.integration
def test_a_post_stuck_in_publishing_is_surfaced_not_retried(db, worker_user):
    """The crash may have happened after LinkedIn accepted it. With no way to
    check (r_member_social is closed), retrying risks a duplicate."""
    stuck = _post(db, worker_user, status=PostStatus.publishing,
                  claimed_at=dt.datetime.now(UTC) - dt.timedelta(hours=1))
    assert scheduler.recover_stuck(db) >= 1
    db.expire_all()
    post = db.get(Post, stuck)
    assert post.status is PostStatus.failed
    assert "check your profile" in post.error_message
    assert post.scheduled_at is None, "must not be put back in the queue"


@pytest.mark.integration
def test_a_post_publishing_right_now_is_left_alone(db, worker_user):
    busy = _post(db, worker_user, status=PostStatus.publishing,
                 claimed_at=dt.datetime.now(UTC) - dt.timedelta(seconds=20))
    scheduler.recover_stuck(db)
    db.expire_all()
    assert db.get(Post, busy).status is PostStatus.publishing


@pytest.mark.integration
def test_one_failing_post_does_not_stop_the_others(db, worker_user, monkeypatch):
    """A worker that dies on one bad post stops publishing everyone else's."""
    past = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    ids = [_post(db, worker_user, status=PostStatus.scheduled, at=past) for _ in range(3)]

    real = publisher.publish
    calls = {"n": 0}

    def flaky(session, post):
        calls["n"] += 1
        if calls["n"] == 1:
            raise publisher.PublishError("boom")
        return real(session, post)

    monkeypatch.setattr(publisher, "publish", flaky)
    result = scheduler.run_once()
    assert result.claimed == 3
    assert result.published == 2
    assert result.failed == 1


@pytest.mark.integration
def test_a_tick_never_raises(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(scheduler, "recover_stuck", broken)
    scheduler.run_once()          # must not raise


def test_the_claim_records_when_it_happened():
    assert "claimed_at = now()" in str(publisher._CLAIM_DUE)
    assert "claimed_at = now()" in str(publisher._CLAIM_ONE)
