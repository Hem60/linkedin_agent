"""Publishing -- plan.md F22.

Against a real database, because the guarantee being tested is a database one:
a post must never be published twice, and a claimed post must never be left
stuck in `publishing`.

LinkedIn itself is replaced. These test the decisions, not the network.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app import linkedin, publisher
from app.database import SessionLocal
from app.models import (
    ImageSource,
    Post,
    PostCategory,
    PostImage,
    PostStatus,
    Tone,
    User,
)

pytestmark = pytest.mark.integration

URN_PREFIX = "urn:li:person:pubtest"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def user(db):
    from app import security

    u = User(
        linkedin_person_urn=f"{URN_PREFIX}{dt.datetime.now().timestamp()}",
        name="Pub Test",
        email="pub@example.com",
        access_token=security.encrypt("fake-linkedin-token"),
        token_expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=30),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    yield u
    db.rollback()
    db.execute(text("DELETE FROM users WHERE id = :i"), {"i": u.id})
    db.commit()


def make_post(db, user, *, status=PostStatus.draft, scheduled_at=None,
              content="We shipped v2 (finally).", hashtags=("RAG", "MLOps")):
    p = Post(
        user_id=user.id,
        topic="a topic",
        post_category=PostCategory.learning,
        tone=Tone.direct,
        content=content,
        hashtags=list(hashtags),
        status=status,
        scheduled_at=scheduled_at,
        image_source=ImageSource.none,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    """No test here should send mail. Failure notifications are covered in
    test_limits.py; here they would only add network calls."""
    monkeypatch.setattr(publisher.notify, "post_failed", lambda *a, **k: False)


@pytest.fixture
def no_dry_run(monkeypatch):
    """Most tests want the real publishing path, not the dry-run short circuit."""
    class S:
        dry_run = False
        linkedin_api_version = "202609"
        app_url = "http://localhost:8000"
    monkeypatch.setattr(publisher, "get_settings", lambda: S())


@pytest.fixture
def fake_linkedin(monkeypatch):
    calls: list[dict] = []

    def create_post(token, author, commentary, *, image_urn=None, image_title=""):
        calls.append({"token": token, "author": author, "commentary": commentary,
                      "image_urn": image_urn})
        return linkedin.PublishedPost(urn="urn:li:share:12345")

    monkeypatch.setattr(linkedin, "create_post", create_post)
    return calls


# ---------------------------------------------------------------------------
# Claiming: the F22 guarantee
# ---------------------------------------------------------------------------

def test_a_due_post_is_claimed(db, user):
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    p = make_post(db, user, status=PostStatus.scheduled, scheduled_at=past)
    claimed = publisher.claim_due(db)
    assert p.id in [c.id for c in claimed]
    db.refresh(p)
    assert p.status is PostStatus.publishing


def test_a_future_post_is_not_claimed(db, user):
    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
    make_post(db, user, status=PostStatus.scheduled, scheduled_at=future)
    assert publisher.claim_due(db) == []


def test_a_claimed_post_cannot_be_claimed_again(db, user):
    """The whole point: a restart mid-publish must not post twice."""
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    make_post(db, user, status=PostStatus.scheduled, scheduled_at=past)
    assert len(publisher.claim_due(db)) == 1
    assert publisher.claim_due(db) == []


def test_claiming_commits_before_returning(db, user):
    """The claim must be durable before any network call. A separate session
    must be able to see it."""
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    p = make_post(db, user, status=PostStatus.scheduled, scheduled_at=past)
    publisher.claim_due(db)

    other = SessionLocal()
    try:
        assert other.get(Post, p.id).status is PostStatus.publishing
    finally:
        other.close()


def test_two_workers_racing_publish_exactly_once(db, user, monkeypatch):
    """Phase 8's acceptance test.

    Two independent sessions, as two worker processes would be, both claim and
    publish at the same moment. LinkedIn has no idempotency key, so the only
    thing standing between a restart and a duplicate post under someone's real
    name is the claim.
    """
    import threading

    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    post = make_post(db, user, status=PostStatus.scheduled, scheduled_at=past)

    published: list[int] = []
    lock = threading.Lock()

    class S:
        dry_run = False
        linkedin_api_version = "202609"
        app_url = "http://localhost:8000"

    monkeypatch.setattr(publisher, "get_settings", lambda: S())

    def create_post(token, author, commentary, *, image_urn=None, image_title=""):
        with lock:
            published.append(post.id)
        return linkedin.PublishedPost(urn=f"urn:li:share:{len(published)}")

    monkeypatch.setattr(linkedin, "create_post", create_post)

    barrier = threading.Barrier(2)

    def worker():
        session = SessionLocal()
        try:
            barrier.wait(timeout=10)          # start together
            for claimed in publisher.claim_due(session):
                publisher.publish(session, claimed)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(published) == 1, f"post published {len(published)} times"
    db.expire_all()
    assert db.get(Post, post.id).status is PostStatus.posted


def test_claim_one_takes_a_draft(db, user):
    p = make_post(db, user, status=PostStatus.draft)
    assert publisher.claim_one(db, p.id).id == p.id


def test_claim_one_refuses_a_post_already_being_published(db, user):
    """A double-clicked Publish button must not produce two posts."""
    p = make_post(db, user, status=PostStatus.draft)
    assert publisher.claim_one(db, p.id) is not None
    assert publisher.claim_one(db, p.id) is None


def test_claim_one_refuses_an_already_posted_post(db, user):
    p = make_post(db, user, status=PostStatus.draft)
    p.status = PostStatus.posted
    p.linkedin_post_id = "urn:li:share:1"
    db.commit()
    assert publisher.claim_one(db, p.id) is None


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def test_publishing_records_the_linkedin_id(db, user, no_dry_run, fake_linkedin):
    p = make_post(db, user)
    published = publisher.publish(db, publisher.claim_one(db, p.id))
    assert published.status is PostStatus.posted
    assert published.linkedin_post_id == "urn:li:share:12345"
    assert published.posted_at is not None


def test_the_commentary_sent_is_escaped(db, user, no_dry_run, fake_linkedin):
    """The single most important assertion here: raw text publishes a post
    truncated at the first reserved character, behind a 201."""
    make_post(db, user, content="We shipped v2 (finally).")
    p = db.query(Post).filter(Post.user_id == user.id).first()
    publisher.publish(db, publisher.claim_one(db, p.id))

    sent = fake_linkedin[0]["commentary"]
    assert r"v2 \(finally\)" in sent
    assert "{hashtag|\\#|RAG}" in sent


def test_publishing_refuses_an_unclaimed_post(db, user, no_dry_run, fake_linkedin):
    p = make_post(db, user, status=PostStatus.draft)
    with pytest.raises(publisher.PublishError, match="claim it first"):
        publisher.publish(db, p)
    assert not fake_linkedin


def test_dry_run_builds_everything_but_calls_nothing(db, user, monkeypatch):
    class S:
        dry_run = True
        linkedin_api_version = "202609"
    monkeypatch.setattr(publisher, "get_settings", lambda: S())
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: pytest.fail("dry run must not publish"))

    p = make_post(db, user)
    out = publisher.publish(db, publisher.claim_one(db, p.id))
    assert out.status is PostStatus.dry_run
    assert out.linkedin_post_id is None


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def _fails(message, *, retryable, status=500):
    def call(*a, **k):
        raise linkedin.LinkedInError(message, status=status, retryable=retryable)
    return call


def test_a_retryable_failure_reschedules_with_backoff(db, user, no_dry_run, monkeypatch):
    monkeypatch.setattr(linkedin, "create_post", _fails("503", retryable=True))
    p = make_post(db, user)
    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, p.id))

    db.expire_all()
    p = db.get(Post, p.id)
    assert p.status is PostStatus.scheduled
    assert p.retry_count == 1
    assert p.scheduled_at > dt.datetime.now(dt.UTC)


def test_a_permanent_failure_does_not_retry(db, user, no_dry_run, monkeypatch):
    """An expired token will fail identically forever; retrying wastes days."""
    monkeypatch.setattr(linkedin, "create_post",
                        _fails("expired", retryable=False, status=401))
    p = make_post(db, user)
    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, p.id))

    db.expire_all()
    p = db.get(Post, p.id)
    assert p.status is PostStatus.failed
    assert "expired" in p.error_message


def test_retries_stop_after_the_limit(db, user, no_dry_run, monkeypatch):
    monkeypatch.setattr(linkedin, "create_post", _fails("503", retryable=True))
    p = make_post(db, user)
    p.retry_count = publisher.MAX_RETRIES - 1
    db.commit()

    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, p.id))

    db.expire_all()
    assert db.get(Post, p.id).status is PostStatus.failed


def test_a_failed_post_is_never_left_stuck_in_publishing(db, user, no_dry_run, monkeypatch):
    """A row stuck in `publishing` is invisible to both the worker and the
    user -- it would simply never publish and never report why."""
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    p = make_post(db, user)
    with pytest.raises(publisher.PublishError):
        publisher.publish(db, publisher.claim_one(db, p.id))

    db.expire_all()
    assert db.get(Post, p.id).status is not PostStatus.publishing


def test_a_broken_notifier_does_not_hide_the_real_failure(db, user, no_dry_run, monkeypatch):
    """The failure email is sent from inside the caller's `except`. Anything
    raised there replaces the real publish error with a misleading one -- which
    is exactly what happened when reading a setting was left unguarded."""
    monkeypatch.setattr(linkedin, "create_post",
                        _fails("LinkedIn is unavailable", retryable=False, status=503))
    monkeypatch.setattr(publisher.notify, "post_failed",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mail server down")))

    p = make_post(db, user)
    with pytest.raises(publisher.PublishError, match="LinkedIn is unavailable"):
        publisher.publish(db, publisher.claim_one(db, p.id))

    db.expire_all()
    post = db.get(Post, p.id)
    assert post.status is PostStatus.failed
    assert "LinkedIn is unavailable" in post.error_message
    assert "mail server" not in post.error_message


def test_an_expired_token_fails_without_calling_linkedin(db, user, no_dry_run, monkeypatch):
    monkeypatch.setattr(linkedin, "create_post",
                        lambda *a, **k: pytest.fail("should not reach LinkedIn"))
    user.token_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    db.commit()

    p = make_post(db, user)
    with pytest.raises(publisher.PublishError, match="expired"):
        publisher.publish(db, publisher.claim_one(db, p.id))


def test_publish_now_refuses_a_second_concurrent_attempt(db, user, no_dry_run, fake_linkedin):
    p = make_post(db, user)
    publisher.claim_one(db, p.id)          # someone else got there first
    with pytest.raises(publisher.PublishError, match="already being published"):
        publisher.publish_now(db, p.id)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def test_an_image_is_uploaded_and_attached(db, user, no_dry_run, fake_linkedin, monkeypatch):
    monkeypatch.setattr(publisher.storage, "fetch", lambda url, **k: b"bytes")
    monkeypatch.setattr(linkedin, "upload_image",
                        lambda *a, **k: "urn:li:image:ABC")

    p = make_post(db, user)
    p.image_source = ImageSource.pexels
    db.add(PostImage(post_id=p.id, url="https://x/y.jpg",
                     source=ImageSource.pexels, position=0))
    db.commit()

    publisher.publish(db, publisher.claim_one(db, p.id))
    assert fake_linkedin[0]["image_urn"] == "urn:li:image:ABC"


def test_a_retry_does_not_re_upload_the_image(db, user, no_dry_run, fake_linkedin, monkeypatch):
    """The URN is cached on the row; re-uploading would waste a call and could
    leave orphaned images on LinkedIn."""
    monkeypatch.setattr(publisher.storage, "fetch",
                        lambda url, **k: pytest.fail("should not re-download"))
    monkeypatch.setattr(linkedin, "upload_image",
                        lambda *a, **k: pytest.fail("should not re-upload"))

    p = make_post(db, user)
    p.image_source = ImageSource.pexels
    db.add(PostImage(post_id=p.id, url="https://x/y.jpg", source=ImageSource.pexels,
                     position=0, linkedin_image_urn="urn:li:image:CACHED"))
    db.commit()

    publisher.publish(db, publisher.claim_one(db, p.id))
    assert fake_linkedin[0]["image_urn"] == "urn:li:image:CACHED"


def test_a_text_only_post_sends_no_image(db, user, no_dry_run, fake_linkedin):
    p = make_post(db, user)
    publisher.publish(db, publisher.claim_one(db, p.id))
    assert fake_linkedin[0]["image_urn"] is None
