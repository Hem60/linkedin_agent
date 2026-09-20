"""Publishing -- plan.md F22 and section 15.

The only code that publishes. Everything else produces drafts.

The shape of it:

    claim (lock)  ->  escape  ->  upload image  ->  POST  ->  record

The claim is the important part. A post is moved `scheduled -> publishing`
inside `SELECT ... FOR UPDATE SKIP LOCKED`, and that transaction is committed
*before* LinkedIn is called. A second worker, or the same worker after a
restart, cannot pick up a row already in `publishing`. Without it, a restart
mid-publish posts twice under someone's real name -- and LinkedIn has no
idempotency key to save us.

Phase 2 proved SKIP LOCKED works through Supabase's pooler, which a pooler in
transaction mode would have broken silently.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import linkedin, sanitize, security, storage
from app.config import get_settings
from app.models import ImageSource, ModelUsed, Post, PostStatus, User

log = logging.getLogger(__name__)

MAX_RETRIES = 3
# Backoff between attempts. Deliberately coarse: LinkedIn outages last minutes,
# and a scheduled post is already not urgent to the second.
BACKOFF_MINUTES = (5, 20, 60)


class PublishError(Exception):
    """Publishing failed; the post row carries the reason."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

_CLAIM_DUE = text("""
    UPDATE posts SET status = 'publishing'
    WHERE id IN (
        SELECT id FROM posts
        WHERE status = 'scheduled' AND scheduled_at <= now()
        ORDER BY scheduled_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    RETURNING id
""")

_CLAIM_ONE = text("""
    UPDATE posts SET status = 'publishing'
    WHERE id = (
        SELECT id FROM posts
        WHERE id = :post_id AND status IN ('draft', 'scheduled')
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
""")


def claim_due(db: Session, limit: int = 10) -> list[Post]:
    """Take ownership of posts whose time has come.

    Commits immediately: the claim must be durable before any network call, or
    a crash between the two leaves a row that looks unclaimed and gets
    published again.
    """
    ids = [r[0] for r in db.execute(_CLAIM_DUE, {"limit": limit})]
    db.commit()
    if not ids:
        return []
    log.info("claimed %d due post(s): %s", len(ids), ids)
    # populate_existing is required, not optional: the claim is raw SQL, and
    # our sessions use expire_on_commit=False, so an instance already in the
    # identity map would still report the pre-claim status.
    return [db.get(Post, i, populate_existing=True) for i in ids]


def claim_one(db: Session, post_id: int) -> Post | None:
    """Claim a single post for immediate publishing.

    Returns None when someone else already has it -- a double-clicked Publish
    button must not produce two posts.
    """
    row = db.execute(_CLAIM_ONE, {"post_id": post_id}).first()
    db.commit()
    if row is None:
        return None
    return db.get(Post, row[0], populate_existing=True)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def publish(db: Session, post: Post) -> Post:
    """Publish a post that has already been claimed.

    The caller must have moved it to `publishing`; this refuses otherwise,
    because publishing an unclaimed row is exactly the race the claim exists to
    prevent.
    """
    if post.status is not PostStatus.publishing:
        raise PublishError(
            f"post {post.id} is {post.status.value}, not publishing -- claim it first"
        )

    settings = get_settings()
    user: User = post.user

    try:
        commentary = sanitize.build_commentary(post.content, post.hashtags or [])

        if settings.dry_run:
            # Everything above ran for real; only the API call is skipped.
            post.status = PostStatus.dry_run
            post.error_message = None
            post.posted_at = _now()
            db.commit()
            log.info("post %d dry run (%d chars)", post.id, len(commentary))
            return post

        token = _token_for(user)
        image_urn = _upload_first_image(db, post, token, user.linkedin_person_urn)

        published = linkedin.create_post(
            token,
            user.linkedin_person_urn,
            commentary,
            image_urn=image_urn,
            image_title=post.topic,
        )

        post.linkedin_post_id = published.urn
        post.status = PostStatus.posted
        post.posted_at = _now()
        post.error_message = None
        db.commit()
        log.info("post %d published: %s", post.id, published.urn)
        return post

    except linkedin.LinkedInError as exc:
        _record_failure(db, post, str(exc), retryable=exc.retryable)
        raise PublishError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 -- a claimed post must never be left stuck
        _record_failure(db, post, f"{type(exc).__name__}: {exc}", retryable=True)
        raise PublishError(str(exc)) from exc


def publish_now(db: Session, post_id: int) -> Post:
    """Claim and publish in one step, for the Publish button."""
    claimed = claim_one(db, post_id)
    if claimed is None:
        raise PublishError("That post is already being published.")
    return publish(db, claimed)


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------

def _token_for(user: User) -> str:
    if not user.access_token:
        raise linkedin.LinkedInError(
            "No LinkedIn connection. Sign in again to publish.", retryable=False
        )
    if user.token_expires_at and user.token_expires_at <= _now():
        raise linkedin.LinkedInError(
            "Your LinkedIn connection has expired. Reconnect to publish again.",
            retryable=False,
        )
    try:
        return security.decrypt(user.access_token)
    except security.DecryptionError as exc:
        raise linkedin.LinkedInError(
            "Stored LinkedIn credentials could not be read. Please reconnect.",
            retryable=False,
        ) from exc


def _upload_first_image(db: Session, post: Post, token: str, author_urn: str) -> str | None:
    """Send the post's image to LinkedIn, if it has one.

    v1 publishes a single image; the ordering column exists for v2's multi-image
    posts. The URN is cached on the row so a retry does not re-upload.
    """
    if post.image_source is ImageSource.none or not post.images:
        return None

    image = post.images[0]
    if image.linkedin_image_urn:
        return image.linkedin_image_urn

    data = storage.fetch(image.url)
    urn = linkedin.upload_image(token, author_urn, data, _content_type(image.url))
    image.linkedin_image_urn = urn
    db.commit()
    return urn


def _content_type(url: str) -> str:
    lowered = url.lower().split("?")[0]
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _record_failure(db: Session, post: Post, message: str, *, retryable: bool) -> None:
    """Never leave a claimed post in `publishing` -- it would be invisible to
    the worker and to the user, stuck forever."""
    db.rollback()
    post = db.get(Post, post.id)
    post.retry_count = (post.retry_count or 0) + 1
    post.error_message = message[:1000]

    if retryable and post.retry_count < MAX_RETRIES:
        minutes = BACKOFF_MINUTES[min(post.retry_count - 1, len(BACKOFF_MINUTES) - 1)]
        post.scheduled_at = _now() + dt.timedelta(minutes=minutes)
        post.status = PostStatus.scheduled
        log.warning("post %d failed, retry %d in %dm: %s",
                    post.id, post.retry_count, minutes, message[:160])
    else:
        post.status = PostStatus.failed
        log.error("post %d failed permanently: %s", post.id, message[:160])

    db.commit()
