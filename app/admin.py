"""Admin usage view -- plan.md F27, section 12.

"An admin view shows total usage per day so you can adjust limits." Exactly
that, and nothing more: no editing, no impersonation, no reading anyone's post
text. It answers one question -- is the shared free quota under strain, and who
is using it.

Access is by email address against ADMIN_EMAILS. Non-admins get 404 rather than
403, matching the rest of the app: a 403 tells you the page exists.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_user
from app.config import get_settings
from app.templating import templates
from app.database import get_db
from app.models import User

router = APIRouter(tags=["admin"])

DAYS = 14


def require_admin(user: User = Depends(require_user)) -> User:
    admins = get_settings().admin_email_list
    if not admins or (user.email or "").lower() not in admins:
        raise HTTPException(404)
    return user


_DAILY = text("""
    SELECT date,
           sum(posts_generated) AS generated,
           count(*)             AS active_users
    FROM usage
    WHERE date >= :since
    GROUP BY date
    ORDER BY date DESC
""")

_BY_USER = text("""
    SELECT u.id, u.name, u.email, u.timezone,
           coalesce(sum(g.posts_generated), 0) AS generated
    FROM users u
    LEFT JOIN usage g ON g.user_id = u.id AND g.date >= :since
    WHERE u.deleted_at IS NULL
    GROUP BY u.id, u.name, u.email, u.timezone
    ORDER BY generated DESC, u.id
    LIMIT 100
""")

_OUTCOMES = text("""
    SELECT status, count(*) AS n
    FROM posts
    WHERE created_at >= :since_ts
    GROUP BY status
""")


@router.get("/admin")
def dashboard(request: Request, user: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    since = dt.date.today() - dt.timedelta(days=DAYS)
    since_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)

    daily = db.execute(_DAILY, {"since": since}).mappings().all()
    by_user = db.execute(_BY_USER, {"since": since}).mappings().all()
    outcomes = {r["status"]: r["n"] for r in db.execute(
        _OUTCOMES, {"since_ts": since_ts}).mappings()}

    settings = get_settings()
    busiest = max((r["generated"] for r in daily), default=0)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "days": DAYS,
            "daily": daily,
            "by_user": by_user,
            "outcomes": outcomes,
            "busiest": busiest,
            "post_limit": settings.daily_post_limit,
            "dry_run": settings.dry_run,
            "email_on": bool(settings.resend_api_key and settings.email_from),
            "total_users": len(by_user),
        },
    )
