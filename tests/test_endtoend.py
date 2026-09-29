"""Phase 15 -- scenarios rather than units.

Each piece below is already unit tested. What is not tested is the pieces
working together for a person who lives somewhere other than where the server
does, which is where date and time bugs actually surface:

* two people in different timezones ask for "tomorrow at 09:00" and mean
  different instants
* a daily limit has to reset at each of their midnights, not at one shared one
* a LinkedIn connection expires, and everything downstream has to say so
  clearly instead of failing silently

Nothing here publishes: DRY_RUN is on for the whole suite.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app import limits, linkedin, publisher, scheduling, security
from app.database import SessionLocal
from app.models import ImageSource, Post, PostCategory, PostStatus, Tone, User
from worker import scheduler

pytestmark = pytest.mark.integration

UTC = dt.timezone.utc

# Deliberately spread: half-hour offset, daylight saving in opposite hemispheres,
# and one that never changes.
PEOPLE = {
    "kolkata": "Asia/Kolkata",        # UTC+05:30, no DST
    "newyork": "America/New_York",    # DST, northern
    "sydney":  "Australia/Sydney",    # DST, southern
    "london":  "Europe/London",
}


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
        s.rollback()
        s.execute(text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:e2e%'"))
        s.commit()

    purge()
    yield
    purge()
    s.close()


def make_user(db, key: str, *, expires_in_days: int = 40) -> User:
    u = User(
        linkedin_person_urn=f"urn:li:person:e2e_{key}",
        name=key.title(), email=f"e2e_{key}@example.com",
        timezone=PEOPLE[key],
        access_token=security.encrypt(f"token-{key}"),
        token_expires_at=dt.datetime.now(UTC) + dt.timedelta(days=expires_in_days),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_post(db, user, *, status=PostStatus.draft, at=None) -> Post:
    p = Post(user_id=user.id, topic=f"{user.name} topic",
             post_category=PostCategory.learning, tone=Tone.direct,
             content="Body (here).", hashtags=["AI"], status=status,
             scheduled_at=at, image_source=ImageSource.none)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Four people, one wall-clock time, four different instants
# ---------------------------------------------------------------------------

def test_the_same_local_time_means_different_instants(db):
    """'Tomorrow at 09:00' is not one moment. If this were wrong, three of the
    four would publish at the wrong hour and nothing would report an error."""
    wall = "2027-02-10T09:00"          # a date with no DST transition anywhere
    instants = {}
    for key in PEOPLE:
        user = make_user(db, key)
        instants[key] = scheduling.parse_local(wall, user.timezone)

    assert len({i.isoformat() for i in instants.values()}) == 4

    # Sydney is furthest ahead, so its 09:00 happens first in real time.
    order = sorted(instants, key=lambda k: instants[k])
    assert order[0] == "sydney"
    assert order[-1] == "newyork"

    # And each really is 09:00 where that person lives.
    for key, when in instants.items():
        assert scheduling.to_local(when, PEOPLE[key]).strftime("%H:%M") == "09:00"


def test_the_worker_publishes_each_at_its_own_moment(db):
    """The whole chain: four timezones, one queue, one worker."""
    users = {key: make_user(db, key) for key in PEOPLE}

    # Everyone asked for 09:00 local on the same date; two of those moments have
    # already passed, two have not.
    now = dt.datetime.now(UTC)
    posts = {}
    for key, user in users.items():
        when = scheduling.parse_local("2027-02-10T09:00", user.timezone)
        # Shift the whole set so half are due: keeps the relative offsets real.
        offset = when - scheduling.parse_local("2027-02-10T09:00", "UTC")
        posts[key] = make_post(db, user, status=PostStatus.scheduled,
                               at=now - dt.timedelta(hours=1) + offset)

    scheduler.run_once()
    db.expire_all()

    published = {k: db.get(Post, p.id).status for k, p in posts.items()}
    # Sydney and Kolkata are ahead of UTC, so their shifted times are in the past.
    assert published["sydney"] is PostStatus.dry_run
    assert published["kolkata"] is PostStatus.dry_run
    # New York is behind, so its moment has not arrived.
    assert published["newyork"] is PostStatus.scheduled


def test_a_post_scheduled_across_a_daylight_saving_change_keeps_its_wall_time(db):
    """London moves to BST on 28 March 2027. A post set for 09:00 the day after
    must still be 09:00 to the reader, not 08:00."""
    user = make_user(db, "london")
    before = scheduling.parse_local("2027-03-27T09:00", user.timezone)   # GMT
    after = scheduling.parse_local("2027-03-29T09:00", user.timezone)    # BST

    assert before.strftime("%H:%M") == "09:00"       # GMT == UTC
    assert after.strftime("%H:%M") == "08:00"        # BST is UTC+1
    for when in (before, after):
        assert scheduling.to_local(when, user.timezone).strftime("%H:%M") == "09:00"


# ---------------------------------------------------------------------------
# A daily limit for people whose days start at different times
# ---------------------------------------------------------------------------

def test_two_people_have_different_todays(db, monkeypatch):
    """At 20:00 UTC it is already tomorrow in Sydney. One shared UTC day would
    reset somebody's allowance in the middle of their working morning."""
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 2})())
    evening_utc = dt.datetime(2027, 2, 10, 20, 0, tzinfo=UTC)

    sydney = make_user(db, "sydney")
    newyork = make_user(db, "newyork")

    assert limits.local_date(sydney, now=evening_utc) == dt.date(2027, 2, 11)
    assert limits.local_date(newyork, now=evening_utc) == dt.date(2027, 2, 10)


def test_an_allowance_resets_at_the_users_own_midnight(db, monkeypatch):
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 2})())
    user = make_user(db, "kolkata")

    # 18:00 UTC is 23:30 in Kolkata: late on their day.
    late = dt.datetime(2027, 2, 10, 18, 0, tzinfo=UTC)
    limits.take_post(db, user, now=late)
    limits.take_post(db, user, now=late)
    with pytest.raises(limits.LimitReached):
        limits.take_post(db, user, now=late)

    # 19:00 UTC is 00:30 the next day there: a new allowance.
    just_after = dt.datetime(2027, 2, 10, 19, 0, tzinfo=UTC)
    assert limits.take_post(db, user, now=just_after).posts == 1


def test_one_persons_usage_does_not_touch_anothers(db, monkeypatch):
    monkeypatch.setattr(limits, "get_settings", lambda: type("S", (), {"daily_post_limit": 1})())
    a, b = make_user(db, "kolkata"), make_user(db, "london")

    limits.take_post(db, a)
    with pytest.raises(limits.LimitReached):
        limits.take_post(db, a)

    limits.take_post(db, b)          # b's own allowance is untouched
    assert limits.current(db, b).posts == 1


# ---------------------------------------------------------------------------
# When the LinkedIn connection lapses
# ---------------------------------------------------------------------------

def test_an_expired_connection_fails_the_publish_with_a_readable_reason(db, monkeypatch):
    """LinkedIn gives standard apps 60 days and no refresh token. What matters
    is that the user is told, not left wondering why nothing posted."""
    class S:
        dry_run = False
        linkedin_api_version = "202609"
        app_url = "http://localhost:8000"

    monkeypatch.setattr(publisher, "get_settings", lambda: S())
    monkeypatch.setattr(publisher.notify, "post_failed", lambda *a, **k: False)
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: pytest.fail("must not call LinkedIn with a dead token"))

    user = make_user(db, "kolkata", expires_in_days=-1)
    post = make_post(db, user)

    with pytest.raises(publisher.PublishError, match="expired"):
        publisher.publish(db, publisher.claim_one(db, post.id))

    db.expire_all()
    failed = db.get(Post, post.id)
    assert failed.status is PostStatus.failed
    assert "Reconnect" in failed.error_message
    assert failed.scheduled_at is None, "an expired token will not fix itself on a retry"


def test_a_scheduled_post_cannot_outlive_the_connection(db):
    user = make_user(db, "kolkata", expires_in_days=5)
    with pytest.raises(scheduling.ScheduleError, match="expires on"):
        scheduling.validate(dt.datetime.now(UTC) + dt.timedelta(days=6), user)


def test_the_expiry_warning_goes_out_once_per_person(db, monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(scheduler.notify, "token_expiring",
                        lambda u, d, url: warned.append(u.name) or True)

    make_user(db, "kolkata", expires_in_days=2)     # lapsing
    make_user(db, "london", expires_in_days=40)     # healthy

    scheduler.warn_expiring_tokens(db)
    scheduler.warn_expiring_tokens(db)              # a minute later

    assert warned == ["Kolkata"]


def test_a_lapsed_connection_does_not_stop_other_people_publishing(db, monkeypatch):
    """One broken account must not stall the queue for everyone else."""
    monkeypatch.setattr(publisher.notify, "post_failed", lambda *a, **k: False)

    class S:
        dry_run = False
        linkedin_api_version = "202609"
        app_url = "http://localhost:8000"

    monkeypatch.setattr(publisher, "get_settings", lambda: S())
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: linkedin.PublishedPost(urn="urn:li:share:ok"))

    past = dt.datetime.now(UTC) - dt.timedelta(minutes=5)
    broken = make_post(db, make_user(db, "kolkata", expires_in_days=-1),
                       status=PostStatus.scheduled, at=past)
    fine = make_post(db, make_user(db, "london"), status=PostStatus.scheduled, at=past)

    result = scheduler.run_once()
    assert result.claimed == 2
    assert result.published == 1
    assert result.failed == 1

    db.expire_all()
    assert db.get(Post, broken.id).status is PostStatus.failed
    assert db.get(Post, fine.id).status is PostStatus.posted
