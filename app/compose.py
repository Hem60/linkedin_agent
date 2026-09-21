"""Writing and publishing a post -- plan.md phase 9.

    /new                        topic, type, tone, one optional question
    /posts/{id}/drafts          the three drafts, side by side
    /posts/{id}                 edit text and hashtags, choose an image, publish
    /posts                      history

Every route that takes a post id goes through `_own_post`, which returns 404
rather than 403 for someone else's post. Plan section 13: nothing is shared
between accounts, and a 403 would confirm that the id exists.
"""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm, photos, publisher, sanitize, storage, uploads
from app.auth import require_user, token_is_expiring
from app.config import get_settings
from app.database import get_db
from app.models import (
    ImageSource,
    Post,
    PostCategory,
    PostImage,
    PostStatus,
    Tone,
    User,
)
from app.prompts import POST_TYPES, TONES

log = logging.getLogger(__name__)
router = APIRouter(tags=["compose"])
templates = Jinja2Templates(directory="app/templates")

EDITABLE = {PostStatus.draft, PostStatus.failed}
PEXELS_IMAGE_HOSTS = {"images.pexels.com"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _own_post(db: Session, user: User, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None or post.user_id != user.id:
        raise HTTPException(404)
    return post


def _parse_tags(raw: str) -> list[str]:
    """Accept '#RAG, MLOps  #AI' in any mix of commas, spaces and hashes."""
    tags = []
    for part in raw.replace(",", " ").split():
        tag = sanitize.normalise_tag(part)
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags[:10]


def _render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, ctx)


def _clear_images(db: Session, post: Post) -> None:
    """Remove a post's images from the bucket and the table. The row cascade
    does not reach the bucket, so the file is deleted explicitly."""
    for img in list(post.images):
        path = storage.path_from_url(img.url)
        if path:
            storage.delete(path)
        db.delete(img)
    db.flush()


def _attach(db: Session, post: Post, prepared: uploads.PreparedImage,
            source: ImageSource, user: User) -> None:
    _clear_images(db, post)
    stored = storage.put(
        prepared.data, storage.object_path(user.id, prepared.extension),
        prepared.content_type,
    )
    db.add(PostImage(post_id=post.id, url=stored.url, source=source, position=0))
    post.image_source = source
    db.commit()


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

@router.get("/new")
def new_form(request: Request, user: User = Depends(require_user)):
    return _render(
        request, "new.html", user=user, types=POST_TYPES, tones=list(TONES),
        form={"tone": user.default_tone.value}, error=None,
    )


@router.post("/new")
def new_submit(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    topic: str = Form(...),
    post_category: str = Form(...),
    tone: str = Form(...),
    extra_answer: str = Form(""),
):
    form = {"topic": topic, "post_category": post_category, "tone": tone,
            "extra_answer": extra_answer}
    topic = topic.strip()

    def fail(message: str):
        return _render(request, "new.html", user=user, types=POST_TYPES,
                       tones=list(TONES), form=form, error=message)

    if len(topic) < 4:
        return fail("Give the topic a few more words so the drafts have something to work with.")
    try:
        category = PostCategory(post_category)
        tone_ = Tone(tone)
    except ValueError:
        return fail("Pick a post type and a tone.")

    try:
        gen = llm.generate(user, topic[:500], category, tone_, extra_answer[:300])
    except llm.GenerationError as exc:
        log.warning("generation failed for user %s: %s", user.id, exc)
        return fail("Both writing models are busy right now. Try again in a minute.")

    drafts = [
        {"body": d.body, "hashtags": d.hashtags, "photo_query": d.photo_query,
         "score": d.score, "improvement": d.improvement, "overall": d.overall}
        for d in gen.drafts
    ]
    post = Post(
        user_id=user.id, topic=topic[:500], post_category=category, tone=tone_,
        account_type=user.account_type, model_used=gen.model_used,
        drafts=drafts, status=PostStatus.draft, image_source=ImageSource.none,
    )
    db.add(post)
    db.commit()
    return RedirectResponse(f"/posts/{post.id}/drafts", status_code=303)


@router.get("/posts/{post_id}/drafts")
def show_drafts(post_id: int, request: Request,
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if not post.drafts:
        return RedirectResponse(f"/posts/{post.id}", status_code=303)

    cards = []
    for i, d in enumerate(post.drafts):
        prep = sanitize.prepare(d["body"], d.get("hashtags") or [])
        cards.append({**d, "index": i, "preview": prep.preview, "length": prep.length})
    best = max(range(len(cards)), key=lambda i: cards[i].get("overall", 0))
    return _render(request, "drafts.html", user=user, post=post, cards=cards, best=best)


@router.post("/posts/{post_id}/choose")
def choose_draft(post_id: int, index: int = Form(...),
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE or not post.drafts or not 0 <= index < len(post.drafts):
        raise HTTPException(400, "That draft can't be chosen.")

    d = post.drafts[index]
    post.content = sanitize.strip_markdown(d["body"])
    post.hashtags = d.get("hashtags") or []
    post.photo_query = (d.get("photo_query") or "")[:200]
    post.quality_score = d.get("score")
    post.chosen_draft = index
    post.edited_by_user = False
    db.commit()
    return RedirectResponse(f"/posts/{post.id}", status_code=303)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

@router.get("/posts/{post_id}")
def editor(post_id: int, request: Request, q: str | None = None,
           user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.chosen_draft is None and post.drafts and not post.content:
        return RedirectResponse(f"/posts/{post.id}/drafts", status_code=303)

    prep = sanitize.prepare(post.content, post.hashtags or [])

    candidates, photo_error = [], None
    if q is not None and post.status in EDITABLE:
        try:
            candidates = photos.search(q or post.photo_query)
        except (photos.PhotoSearchError, RuntimeError) as exc:
            photo_error = str(exc)

    return _render(
        request, "editor.html", user=user, post=post, prep=prep,
        editable=post.status in EDITABLE, candidates=candidates,
        photo_query=q if q is not None else post.photo_query,
        photo_error=photo_error, dry_run=get_settings().dry_run,
        token_expiring=token_is_expiring(user),
        message=request.query_params.get("m"),
    )


@router.post("/posts/{post_id}")
def save(post_id: int, content: str = Form(""), hashtags: str = Form(""),
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(400, "Only drafts can be edited.")

    cleaned = sanitize.strip_markdown(content)
    tags = _parse_tags(hashtags)
    if cleaned != post.content or tags != (post.hashtags or []):
        post.edited_by_user = True
    post.content = cleaned
    post.hashtags = tags
    db.commit()
    return RedirectResponse(f"/posts/{post.id}?m=saved", status_code=303)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

@router.post("/posts/{post_id}/image/pexels")
def pick_pexels(post_id: int, url: str = Form(...),
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(400, "Only drafts can be edited.")

    # The URL comes from the browser, so it is untrusted. Without this check the
    # server would fetch any address a user supplies -- an SSRF hole pointing at
    # whatever the server can reach.
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in PEXELS_IMAGE_HOSTS:
        raise HTTPException(400, "That image address isn't from Pexels.")

    try:
        prepared = uploads.prepare_upload(storage.fetch(url))
        _attach(db, post, prepared, ImageSource.pexels, user)
    except (uploads.ImageRejected, storage.StorageError) as exc:
        return RedirectResponse(
            f"/posts/{post.id}?m={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/posts/{post.id}?m=image", status_code=303)


@router.post("/posts/{post_id}/image/upload")
async def upload_image(post_id: int, file: UploadFile = File(...),
                       user: User = Depends(require_user), db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(400, "Only drafts can be edited.")

    # Read one byte past the limit: enough to know it is too big without
    # buffering an arbitrarily large upload.
    raw = await file.read(uploads.MAX_BYTES + 1)
    try:
        prepared = uploads.prepare_upload(raw, file.filename or "")
        _attach(db, post, prepared, ImageSource.upload, user)
    except (uploads.ImageRejected, storage.StorageError) as exc:
        return RedirectResponse(
            f"/posts/{post.id}?m={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/posts/{post.id}?m=image", status_code=303)


@router.post("/posts/{post_id}/image/remove")
def remove_image(post_id: int, user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(400, "Only drafts can be edited.")
    _clear_images(db, post)
    post.image_source = ImageSource.none
    db.commit()
    return RedirectResponse(f"/posts/{post.id}", status_code=303)


# ---------------------------------------------------------------------------
# Publish, delete, history
# ---------------------------------------------------------------------------

@router.post("/posts/{post_id}/publish")
def publish(post_id: int, user: User = Depends(require_user),
            db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if not post.content.strip():
        raise HTTPException(400, "There's nothing to publish yet.")
    if sanitize.prepare(post.content, post.hashtags or []).too_long:
        return RedirectResponse(
            f"/posts/{post.id}?m=" + urllib.parse.quote("That's over LinkedIn's 3,000 characters."),
            status_code=303)

    if post.status is PostStatus.failed:
        # A failed post the user has looked at and chosen to retry.
        post.status = PostStatus.draft
        post.retry_count = 0
        db.commit()

    try:
        publisher.publish_now(db, post.id)
    except publisher.PublishError as exc:
        return RedirectResponse(
            f"/posts/{post.id}?m={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/posts/{post.id}?m=published", status_code=303)


@router.post("/posts/{post_id}/delete")
def delete(post_id: int, user: User = Depends(require_user),
           db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status in {PostStatus.publishing, PostStatus.posted}:
        raise HTTPException(400, "Published posts are removed from LinkedIn, not here.")
    _clear_images(db, post)
    db.delete(post)
    db.commit()
    return RedirectResponse("/posts", status_code=303)


@router.get("/posts")
def history(request: Request, user: User = Depends(require_user),
            db: Session = Depends(get_db)):
    posts = db.scalars(
        select(Post).where(Post.user_id == user.id).order_by(Post.created_at.desc()).limit(100)
    ).all()
    return _render(request, "posts.html", user=user, posts=posts)
