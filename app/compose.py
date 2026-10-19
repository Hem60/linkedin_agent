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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import (
    limits,
    llm,
    performance,
    photos,
    publisher,
    sanitize,
    scheduling,
    storage,
    uploads,
)
from app.auth import require_user, token_is_expiring
from app.config import get_settings
from app.database import get_db
from app.models import (
    AccountType,
    ImageSource,
    Post,
    PostCategory,
    PostImage,
    PostStatus,
    Tone,
    User,
)
from app.prompts import POST_TYPES, TONES
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter(tags=["compose"])

EDITABLE = {PostStatus.draft, PostStatus.failed}


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
def new_form(request: Request, user: User = Depends(require_user),
             db: Session = Depends(get_db)):
    return _render(
        request, "new.html", user=user, types=POST_TYPES, tones=list(TONES),
        form={"tone": user.default_tone.value,
              "post_category": ("business" if user.account_type is AccountType.business
                                else "learning")},
        error=None, usage=limits.current(db, user),
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
                       tones=list(TONES), form=form, error=message,
                       usage=limits.current(db, user))

    if len(topic) < 4:
        return fail("Give the topic a few more words so the drafts have something to work with.")
    try:
        category = PostCategory(post_category)
        tone_ = Tone(tone)
    except ValueError:
        return fail("Pick a post type and a tone.")

    # Consume the allowance before spending a model request, not after: the
    # point is to protect a shared quota, and a request already made cannot be
    # unmade (plan section 12).
    try:
        limits.take_post(db, user)
    except limits.LimitReached as exc:
        return fail(str(exc))

    try:
        gen = llm.generate(user, topic[:500], category, tone_, extra_answer[:300])
    except llm.GenerationError as exc:
        log.warning("generation failed for user %s: %s", user.id, exc)
        # Nothing was billed, so nothing should be charged.
        limits.refund_post(db, user)
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
    tags = d.get("hashtags") or []
    # The model sometimes ends the body with its own copy of the hashtag line,
    # which would then be published twice. Drop it before the editor shows it.
    post.content = sanitize.strip_duplicate_tag_line(
        sanitize.strip_markdown(d["body"]), tags)
    post.hashtags = tags
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

    return _render(
        request, "editor.html", user=user, post=post, prep=prep,
        editable=post.status in EDITABLE,
        photo_query=q if q is not None else post.photo_query,
        dry_run=get_settings().dry_run,
        token_expiring=token_is_expiring(user),
        message=request.query_params.get("m"),
        can_rate=(post.status is PostStatus.posted),
        reported=performance.reported(db, user).get(post.id),
        is_brand=(post.post_category is PostCategory.business),
        company_page_url=user.company_page_url,
        copy_text=prep.preview,
        scheduled_for=scheduling.display(post.scheduled_at, user.timezone),
        schedule_value=(scheduling.input_value(post.scheduled_at, user.timezone)
                        or scheduling.suggest(user.timezone)),
        schedule_min=scheduling.input_value(
            scheduling.now_utc() + scheduling.MIN_LEAD, user.timezone),
        tz_name=user.timezone,
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

@router.post("/posts/{post_id}/image/generate")
def generate_image(post_id: int, q: str = Form(""),
                   user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Generate an image for this post and attach it.

    One image rather than a row of candidates: the old Pexels picker existed
    because a search returns many photos and only the reader can say which
    fits. Generation returns an image of this post, and another press costs a
    second and a fortieth of a day's free allowance, so choosing again is
    cheaper than choosing between.

    Nothing is fetched from a URL the browser supplied, so the SSRF check the
    Pexels route needed has no counterpart here -- the bytes come straight
    back from the model.
    """
    post = _own_post(db, user, post_id)
    if post.status not in EDITABLE:
        raise HTTPException(400, "Only drafts can be edited.")

    try:
        raw = photos.generate(q or post.photo_query or "")
        prepared = uploads.prepare_upload(raw)
        _attach(db, post, prepared, ImageSource.generated, user)
    except (photos.ImageGenError, uploads.ImageRejected,
            storage.StorageError, RuntimeError) as exc:
        return RedirectResponse(
            f"/posts/{post.id}?m={urllib.parse.quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/posts/{post.id}?m=image#preview", status_code=303)


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
    return RedirectResponse(f"/posts/{post.id}?m=image#preview", status_code=303)


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


# ---------------------------------------------------------------------------
# Scheduling (F19)
# ---------------------------------------------------------------------------
#
# Both transitions are conditional UPDATEs rather than "load, change, save".
# The worker may claim this row at any moment. A plain ORM save issued just
# after the claim would overwrite `publishing` with `scheduled`, and the post
# would go out now and again at the new time. The WHERE clause makes the
# database refuse instead.

_SCHEDULE = text("""
    UPDATE posts
    SET status = 'scheduled', scheduled_at = :at, retry_count = 0, error_message = NULL
    WHERE id = :id AND user_id = :uid AND status IN ('draft', 'failed', 'scheduled')
    RETURNING id
""")

_UNSCHEDULE = text("""
    UPDATE posts SET status = 'draft', scheduled_at = NULL
    WHERE id = :id AND user_id = :uid AND status = 'scheduled'
    RETURNING id
""")


def _back(post_id: int, message: str):
    return RedirectResponse(f"/posts/{post_id}?m={urllib.parse.quote(message)}", status_code=303)


@router.post("/posts/{post_id}/schedule")
def schedule(post_id: int, when: str = Form(""),
             user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Schedule a draft, or move an already-scheduled post to a new time."""
    post = _own_post(db, user, post_id)
    if not post.content.strip():
        return _back(post.id, "There's nothing to schedule yet.")
    if sanitize.prepare(post.content, post.hashtags or []).too_long:
        return _back(post.id, "That's over LinkedIn's 3,000 characters.")

    try:
        at = scheduling.parse_local(when, user.timezone)
        scheduling.validate(at, user)
    except scheduling.ScheduleError as exc:
        return _back(post.id, str(exc))

    changed = db.execute(_SCHEDULE, {"at": at, "id": post.id, "uid": user.id}).first()
    db.commit()
    if changed is None:
        return _back(post.id, "That post is being published right now and can't be rescheduled.")
    return RedirectResponse(f"/posts/{post.id}?m=scheduled", status_code=303)


@router.post("/posts/{post_id}/unschedule")
def unschedule(post_id: int, user: User = Depends(require_user),
               db: Session = Depends(get_db)):
    """Cancel a schedule and return the post to draft."""
    post = _own_post(db, user, post_id)
    changed = db.execute(_UNSCHEDULE, {"id": post.id, "uid": user.id}).first()
    db.commit()
    if changed is None:
        return _back(post.id, "Too late to cancel — it's already being published.")
    return RedirectResponse(f"/posts/{post.id}?m=unscheduled", status_code=303)


# ---------------------------------------------------------------------------
# Company pages (F20)
# ---------------------------------------------------------------------------
#
# LinkedIn will not let this app publish to a company page: that needs
# Community Management access, which is a separate application on a separate
# developer app (plan section 2). So the app does everything up to the last
# step and hands over the finished text.
#
# What gets handed over is the *rendered* text, not the escaped commentary.
# The user is pasting into LinkedIn's own composer, which takes plain text;
# `little` escaping exists only for the API, and pasting it would publish
# visible backslashes.

@router.post("/posts/{post_id}/copied")
def mark_copied(post_id: int, user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """Record that the user took this to their company page.

    Deliberately not a status change. The app never sees the post reach
    LinkedIn, so calling it `posted` would be a claim we cannot support -- and
    `posted` rows are expected to carry a LinkedIn id.
    """
    post = _own_post(db, user, post_id)
    post.copied_at = scheduling.now_utc()
    db.commit()
    return RedirectResponse(f"/posts/{post.id}?m=copied", status_code=303)


@router.post("/posts/{post_id}/delete")
def delete(post_id: int, user: User = Depends(require_user),
           db: Session = Depends(get_db)):
    post = _own_post(db, user, post_id)
    if post.status in {PostStatus.publishing, PostStatus.posted}:
        raise HTTPException(400, "Published posts are removed from LinkedIn, not here.")
    if post.status is PostStatus.scheduled:
        return _back(post.id, "Cancel the schedule first, then delete.")
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
    when = {p.id: scheduling.display(p.scheduled_at, user.timezone)
            for p in posts if p.status is PostStatus.scheduled}
    return _render(request, "posts.html", user=user, posts=posts, when=when,
                   results=performance.reported(db, user))
