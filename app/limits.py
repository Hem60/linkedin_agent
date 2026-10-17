"""Daily limits -- plan.md F27, section 12.

Gemini and Groq free tiers are shared across everyone using this deployment, so
one enthusiastic account must not spend the day's allowance before anyone else
wakes up.

Two details that are easy to get wrong:

* The day is the *user's* day. A limit that resets at UTC midnight resets at
  05:30 for someone in India -- in the middle of their morning, or worse, at
  09:30 in Los Angeles, splitting their working day in two.

* The check and the increment are one statement. "Read the count, decide, then
  write" lets two requests a millisecond apart both read 2 and both write 3.
  INSERT ... ON CONFLICT DO UPDATE ... WHERE does the comparison inside the
  write, so the database refuses the second one.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.scheduling import to_local

log = logging.getLogger(__name__)


class LimitReached(Exception):
    """The user has used today's allowance."""

    def __init__(self, message: str, *, used: int, allowed: int, resets_in: str):
        super().__init__(message)
        self.used = used
        self.allowed = allowed
        self.resets_in = resets_in


@dataclass(frozen=True)
class Usage:
    posts: int
    rewrites: int
    post_limit: int

    @property
    def posts_left(self) -> int:
        return max(0, self.post_limit - self.posts)

    @property
    def at_limit(self) -> bool:
        # 0 means unlimited, the same as in take_post. Without this the page
        # announces "you've used today's 0 posts" to someone who has no limit.
        if self.post_limit <= 0:
            return False
        return self.posts >= self.post_limit


def local_date(user: User, *, now: dt.datetime | None = None) -> dt.date:
    """Today, on the user's own calendar."""
    now = now or dt.datetime.now(dt.UTC)
    return to_local(now, user.timezone).date()


def _resets_in(user: User, *, now: dt.datetime | None = None) -> str:
    """How long until their midnight, in words."""
    now = now or dt.datetime.now(dt.UTC)
    local = to_local(now, user.timezone)
    midnight = (local + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    minutes = int((midnight - local).total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    return f"{hours}h" if hours else f"{mins}m"


_TAKE_POST = text("""
    INSERT INTO usage (user_id, date, posts_generated, rewrites)
    VALUES (:uid, :day, 1, 0)
    ON CONFLICT (user_id, date) DO UPDATE
        SET posts_generated = usage.posts_generated + 1
        WHERE usage.posts_generated < :allowed
    RETURNING posts_generated
""")

_READ = text("""
    SELECT posts_generated, rewrites FROM usage
    WHERE user_id = :uid AND date = :day
""")


def current(db: Session, user: User, *, now: dt.datetime | None = None) -> Usage:
    """What the user has used today. For display; does not consume anything."""
    settings = get_settings()
    row = db.execute(_READ, {"uid": user.id, "day": local_date(user, now=now)}).first()
    return Usage(
        posts=row.posts_generated if row else 0,
        rewrites=row.rewrites if row else 0,
        post_limit=settings.daily_post_limit,
    )


def take_post(db: Session, user: User, *, now: dt.datetime | None = None) -> Usage:
    """Consume one of today's posts, or raise LimitReached.

    Call before spending a model request, not after: the point is to protect
    the quota, and a request that has already been made cannot be unmade.
    """
    settings = get_settings()
    allowed = settings.daily_post_limit
    day = local_date(user, now=now)

    if allowed <= 0:                    # 0 means unlimited, for a private deploy
        return current(db, user, now=now)

    row = db.execute(
        _TAKE_POST, {"uid": user.id, "day": day, "allowed": allowed}
    ).first()
    db.commit()

    if row is None:
        used = current(db, user, now=now).posts
        raise LimitReached(
            f"You've used all {allowed} posts for today. "
            f"Your limit resets in {_resets_in(user, now=now)}.",
            used=used, allowed=allowed, resets_in=_resets_in(user, now=now),
        )

    return Usage(posts=row.posts_generated, rewrites=0, post_limit=allowed)


def refund_post(db: Session, user: User, *, now: dt.datetime | None = None) -> None:
    """Give back a consumed post when generation failed anyway.

    The allowance exists to cap model requests. If every model was down, no
    request was billed and charging the user for it is just unkind.
    """
    db.execute(
        text("""
            UPDATE usage SET posts_generated = greatest(0, posts_generated - 1)
            WHERE user_id = :uid AND date = :day
        """),
        {"uid": user.id, "day": local_date(user, now=now)},
    )
    db.commit()
