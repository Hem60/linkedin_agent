"""How did the post do? -- plan.md F26, section 13.

LinkedIn will not tell us. Reading likes and comments on a member's own posts
needs `r_member_social`, which their own FAQ calls closed: "We're not accepting
access requests at this time." So the numbers come from the person who can see
them.

That is worse data than an API would give -- it is optional, occasional and
typed by hand. It is also the only data obtainable, and one number a week is
enough to answer "which of my posts actually worked", which is what v2's
insights need.

Two deliberate choices:

* Only genuinely published posts are asked about. A dry run never reached
  LinkedIn, so there is nothing to have done well or badly.
* A thumbs up or down counts as an answer. Most people will not go and count
  comments, and a weak signal offered willingly beats a precise one nobody
  gives.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.models import Post, PostPerformance, PostStatus, User

log = logging.getLogger(__name__)
router = APIRouter(tags=["performance"])

# Long enough for a post to have been seen, short enough to still remember
# writing it.
ASK_AFTER = dt.timedelta(days=3)


def _clean_count(raw: str) -> int | None:
    """Accept '12', ' 12 ', '' and nonsense alike; store only a real number."""
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return max(0, min(value, 10_000_000))


def pending(db: Session, user: User, *, now: dt.datetime | None = None) -> list[Post]:
    """Published posts old enough to ask about, that nobody has answered yet."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return list(db.scalars(
        select(Post)
        .outerjoin(PostPerformance, PostPerformance.post_id == Post.id)
        .where(
            Post.user_id == user.id,
            Post.status == PostStatus.posted,
            Post.posted_at.is_not(None),
            Post.posted_at <= now - ASK_AFTER,
            PostPerformance.id.is_(None),
        )
        .order_by(Post.posted_at)
    ))


def reported(db: Session, user: User) -> dict[int, PostPerformance]:
    """Answers so far, keyed by post, for showing in history."""
    rows = db.scalars(
        select(PostPerformance)
        .join(Post, Post.id == PostPerformance.post_id)
        .where(Post.user_id == user.id)
    )
    return {r.post_id: r for r in rows}


_RECORD = text("""
    INSERT INTO post_performance (post_id, likes, comments, sentiment, reported_at)
    VALUES (:post_id, :likes, :comments, :sentiment, now())
    ON CONFLICT (post_id) DO UPDATE
        SET likes = excluded.likes,
            comments = excluded.comments,
            sentiment = excluded.sentiment,
            reported_at = now()
""")


@router.post("/posts/{post_id}/performance")
def record(post_id: int, request: Request,
           likes: str = Form(""), comments: str = Form(""), sentiment: str = Form(""),
           user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None or post.user_id != user.id:
        # 404 rather than 403, as everywhere else: a 403 confirms the id exists.
        from fastapi import HTTPException
        raise HTTPException(404)

    if post.status is not PostStatus.posted:
        from fastapi import HTTPException
        raise HTTPException(400, "Only published posts can be rated.")

    mood = sentiment if sentiment in ("up", "down") else None
    db.execute(_RECORD, {
        "post_id": post.id,
        "likes": _clean_count(likes),
        "comments": _clean_count(comments),
        "sentiment": mood,
    })
    db.commit()

    back = request.headers.get("referer") or f"/posts/{post.id}"
    separator = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{separator}m=thanks", status_code=303)
