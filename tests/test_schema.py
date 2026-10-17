"""Integration tests against a real Postgres.

These verify guarantees the application relies on but does not implement itself:
database constraints, the trigram extension, and row-level locking. They need
DATABASE_URL to point at a real Postgres and are skipped otherwise.

The concurrency test is the important one. F22 -- not publishing the same post
twice -- rests entirely on `SELECT ... FOR UPDATE SKIP LOCKED` behaving
correctly through whatever sits between the app and Postgres. A connection
pooler in transaction mode can break that, so it is proven here rather than
assumed.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def user_id(db):
    """A throwaway user, removed afterwards along with everything cascading."""
    uid = db.execute(
        text(
            "INSERT INTO users (linkedin_person_urn, name, email, role_title, niche,"
            " account_type, company_name, products, target_markets, buyer_types,"
            " default_tone, timezone)"
            " VALUES (:urn, 'Test', 't@example.com', '', '', 'personal', '', '', '',"
            " '', 'professional', 'UTC') RETURNING id"
        ),
        {"urn": f"urn:li:person:test{dt.datetime.now().timestamp()}"},
    ).scalar_one()
    db.commit()
    yield uid
    # A test that asserted on a constraint violation leaves the transaction in a
    # failed state; Postgres rejects everything after that until it is rolled back.
    db.rollback()
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})
    db.commit()


def _insert_post(db, user_id: int, *, status: str, scheduled_at, topic="test topic"):
    return db.execute(
        text(
            "INSERT INTO posts (user_id, topic, post_category, tone, content,"
            " edited_by_user, account_type, image_source, status, scheduled_at,"
            " retry_count)"
            " VALUES (:uid, :topic, 'learning', 'professional', 'body', false,"
            " 'personal', 'none', :status, :at, 0) RETURNING id"
        ),
        {"uid": user_id, "topic": topic, "status": status, "at": scheduled_at},
    ).scalar_one()


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

def test_posted_row_must_carry_a_linkedin_id(db, user_id):
    """A post marked posted with no LinkedIn id means we lost track of it."""
    with pytest.raises(IntegrityError):
        _insert_post(db, user_id, status="posted", scheduled_at=None)
        db.flush()


def test_scheduled_row_must_carry_a_time(db, user_id):
    with pytest.raises(IntegrityError):
        _insert_post(db, user_id, status="scheduled", scheduled_at=None)
        db.flush()


def test_usage_cannot_have_two_rows_for_one_user_and_day(db, user_id):
    """The composite primary key is what makes the daily limit unraceable."""
    today = dt.date.today()
    db.execute(
        text("INSERT INTO usage (user_id, date, posts_generated, rewrites)"
             " VALUES (:u, :d, 1, 0)"),
        {"u": user_id, "d": today},
    )
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(
            text("INSERT INTO usage (user_id, date, posts_generated, rewrites)"
                 " VALUES (:u, :d, 1, 0)"),
            {"u": user_id, "d": today},
        )
        db.flush()


# ---------------------------------------------------------------------------
# Duplicate-topic check (F13)
# ---------------------------------------------------------------------------

def test_trigram_similarity_is_available_and_discriminates(db):
    near = db.execute(
        text("SELECT similarity('lessons from my first ML project',"
             " 'lessons from my first machine learning project')")
    ).scalar_one()
    far = db.execute(
        text("SELECT similarity('lessons from my first ML project',"
             " 'why I switched to a standing desk')")
    ).scalar_one()
    assert near > far
    assert far < 0.3, "unrelated topics must not trip the duplicate warning"


# ---------------------------------------------------------------------------
# F22: the publish lock
# ---------------------------------------------------------------------------

CLAIM = text(
    """
    UPDATE posts SET status = 'publishing'
    WHERE id IN (
        SELECT id FROM posts
        WHERE status = 'scheduled' AND scheduled_at <= now()
        ORDER BY scheduled_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    RETURNING id
    """
)


def test_two_workers_never_claim_the_same_post(db, user_id):
    """The whole of F22 in one test.

    Two connections run the claim query concurrently, with the first holding its
    transaction open. Without SKIP LOCKED the second would block and then claim
    the same row once the first committed -- publishing the post twice under a
    real person's name.
    """
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    ids = {_insert_post(db, user_id, status="scheduled", scheduled_at=past) for _ in range(2)}
    db.commit()

    conn_a = engine.connect()
    conn_b = engine.connect()
    try:
        tx_a = conn_a.begin()
        claimed_a = {r[0] for r in conn_a.execute(CLAIM, {"limit": 1})}

        # A has not committed. B must skip A's locked row, not wait for it.
        tx_b = conn_b.begin()
        claimed_b = {r[0] for r in conn_b.execute(CLAIM, {"limit": 5})}

        assert claimed_a, "first worker claimed nothing"
        assert not (claimed_a & claimed_b), (
            f"both workers claimed {claimed_a & claimed_b} -- SKIP LOCKED is not "
            "working through this connection path; a restart would double-post"
        )
        assert claimed_a | claimed_b >= ids

        tx_a.commit()
        tx_b.commit()
    finally:
        conn_a.close()
        conn_b.close()
        db.execute(text("DELETE FROM posts WHERE user_id = :u"), {"u": user_id})
        db.commit()


def test_a_claimed_post_is_no_longer_claimable(db, user_id):
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    _insert_post(db, user_id, status="scheduled", scheduled_at=past)
    db.commit()
    try:
        first = {r[0] for r in db.execute(CLAIM, {"limit": 10})}
        db.commit()
        second = {r[0] for r in db.execute(CLAIM, {"limit": 10})}
        db.commit()
        assert first
        assert not second, "a post in 'publishing' must not be picked up again"
    finally:
        db.execute(text("DELETE FROM posts WHERE user_id = :u"), {"u": user_id})
        db.commit()


def test_future_posts_are_not_claimed(db, user_id):
    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    _insert_post(db, user_id, status="scheduled", scheduled_at=future)
    db.commit()
    try:
        assert not {r[0] for r in db.execute(CLAIM, {"limit": 10})}
        db.commit()
    finally:
        db.execute(text("DELETE FROM posts WHERE user_id = :u"), {"u": user_id})
        db.commit()
