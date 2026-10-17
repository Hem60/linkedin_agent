"""Leaving -- plan.md section 13 (F4) and the privacy policy.

Three routes:

    GET  /privacy          what is stored and why
    GET  /account/export   everything we hold about you, as JSON
    POST /account/delete   erase it

Deletion has to reach three places, and the easy mistake is to remember only
the first:

* rows in the database -- posts, images, usage, the account itself
* image **files** in the storage bucket, which no foreign key cascade touches
* the LinkedIn access token, which is the one piece that lets anything be
  published as this person

It is a real delete, not a flag. A soft delete would leave the encrypted token
sitting in a row marked "gone", which is not what anyone means by "delete my
account".
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.auth import require_user
from app.database import get_db
from app.models import Post, User
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["privacy"])


@router.get("/privacy")
def privacy(request: Request):
    """Public: LinkedIn expects a reachable policy for apps other people use."""
    return templates.TemplateResponse(request, "privacy.html", {"user": None})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_data(db: Session, user: User) -> dict:
    """Everything held about this user, in one structure.

    The access token is deliberately excluded. It is ours to hold on their
    behalf, not data about them, and a plaintext LinkedIn token sitting in a
    downloaded file is a hazard to the person we are trying to help.
    """
    posts = db.scalars(
        select(Post).where(Post.user_id == user.id).order_by(Post.created_at)
    ).all()

    return {
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "account": {
            "name": user.name,
            "email": user.email,
            "linkedin_person_urn": user.linkedin_person_urn,
            "account_type": user.account_type.value,
            "role_title": user.role_title,
            "niche": user.niche,
            "company_name": user.company_name,
            "products": user.products,
            "target_markets": user.target_markets,
            "buyer_types": user.buyer_types,
            "company_page_url": user.company_page_url,
            "default_tone": user.default_tone.value,
            "timezone": user.timezone,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "linkedin_connection_expires_at": (
                user.token_expires_at.isoformat() if user.token_expires_at else None
            ),
        },
        "posts": [
            {
                "id": p.id,
                "topic": p.topic,
                "type": p.post_category.value,
                "tone": p.tone.value,
                "content": p.content,
                "hashtags": p.hashtags or [],
                "status": p.status.value,
                "quality_score": p.quality_score,
                "written_by": p.model_used.value if p.model_used else None,
                "edited_by_you": p.edited_by_user,
                "images": [i.url for i in p.images],
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "scheduled_at": p.scheduled_at.isoformat() if p.scheduled_at else None,
                "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                "linkedin_post_id": p.linkedin_post_id,
            }
            for p in posts
        ],
        "note": (
            "Your LinkedIn access token is not included: it is a credential held "
            "on your behalf, not information about you."
        ),
    }


@router.get("/account/export")
def export(user: User = Depends(require_user), db: Session = Depends(get_db)) -> Response:
    payload = json.dumps(export_data(db, user), indent=2, ensure_ascii=False)
    # The filename's date, fixed to UTC so two people exporting the same
    # moment get the same name regardless of where the server runs.
    stamp = dt.datetime.now(dt.UTC).date().isoformat()
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="postpilot-{stamp}.json"'},
    )


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

@router.get("/account")
def account(request: Request, user: User = Depends(require_user),
            db: Session = Depends(get_db)):
    post_count = len(db.scalars(select(Post).where(Post.user_id == user.id)).all())
    return templates.TemplateResponse(
        request, "account.html",
        {"user": user, "post_count": post_count,
         "message": request.query_params.get("m")},
    )


@router.post("/account/delete")
def delete_account(request: Request, confirm: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Erase the account. Irreversible, and typed confirmation is required."""
    if confirm.strip().lower() != "delete":
        return RedirectResponse(
            "/account?m=Type+DELETE+to+confirm.", status_code=303)

    user_id, name = user.id, user.name

    # Files first. If this half fails we still have the rows, so the files can
    # be found and removed later; doing it the other way round would leave
    # orphans nothing points at.
    removed = _delete_images(db, user)

    db.delete(user)          # cascades to posts, images, usage, performance
    db.commit()
    request.session.clear()

    log.info("deleted account %s (%s), %d image file(s)", user_id, name, removed)
    return RedirectResponse("/?deleted=1", status_code=303)


def _delete_images(db: Session, user: User) -> int:
    """Remove this user's image files from the bucket.

    No cascade reaches storage, so without this every deleted account leaves
    its pictures behind. Deletion is idempotent (see storage.delete), and one
    failure must not stop the rest -- a partially deleted account is worse than
    a leftover file.
    """
    removed = 0
    for post in db.scalars(select(Post).where(Post.user_id == user.id)):
        for image in post.images:
            path = storage.path_from_url(image.url)
            if not path:
                continue
            try:
                storage.delete(path)
                removed += 1
            except Exception:
                log.exception("could not delete %s while deleting account %s",
                              path, user.id)
    return removed
