"""The write-and-publish flow -- plan.md phase 9.

Models, Pexels, storage and LinkedIn are replaced. What is tested is the web
layer's own decisions: who can touch which post, what the browser is trusted
with, and that each step hands the next one what it expects.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, compose, llm, photos, publisher, storage, uploads
from app.database import SessionLocal
from app.main import app
from app.models import ModelUsed, Post, PostImage, PostStatus

pytestmark = pytest.mark.integration

SUB_A, SUB_B = "composeUserA", "composeUserB"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge(db):
    def purge():
        db.rollback()
        db.execute(text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:compose%'"))
        db.commit()
    purge()
    yield
    purge()


def signed_in(monkeypatch, sub: str) -> TestClient:
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda code: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": sub, "name": "Test Person", "email": f"{sub}@x.com"})
    c = TestClient(app, follow_redirects=False)
    state = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    assert c.get(f"/auth/callback?code=x&state={state}").status_code == 302
    return c


def fake_generation(*args, **kwargs):
    drafts = [
        llm.Draft(body=f"Draft {i} hook.\n\n**Bold** point (v{i}).",
                  hashtags=["RAG", "MLOps", "AI"], photo_query="server room",
                  score={"hook": 7 + i, "clarity": 8, "readability": 8},
                  improvement="Sharper close.")
        for i in range(3)
    ]
    return llm.Generation(drafts=drafts, model_used=ModelUsed.gemini, attempts=1)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm, "generate", fake_generation)
    return signed_in(monkeypatch, SUB_A)


def create(client) -> int:
    r = client.post("/new", data={"topic": "what I learned tuning RAG",
                                  "post_category": "learning", "tone": "direct"})
    assert r.status_code == 303, r.text[:300]
    return int(r.headers["location"].split("/")[2])


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_new_post_form_renders(client):
    r = client.get("/new")
    assert r.status_code == 200
    assert "Write three drafts" in r.text


def test_generating_creates_a_post_holding_three_drafts(client, db):
    pid = create(client)
    post = db.get(Post, pid)
    assert len(post.drafts) == 3
    assert post.status is PostStatus.draft
    assert post.chosen_draft is None


def test_drafts_page_shows_all_three_and_marks_the_best(client):
    pid = create(client)
    r = client.get(f"/posts/{pid}/drafts")
    assert r.status_code == 200
    assert r.text.count("Use this one") == 3
    assert "Highest score" in r.text


def test_choosing_a_draft_fills_the_post_and_strips_markdown(client, db):
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 2})
    db.expire_all()
    post = db.get(Post, pid)
    assert post.chosen_draft == 2
    assert post.content.startswith("Draft 2 hook.")
    assert "**" not in post.content, "markdown must be stripped before storing"
    assert post.hashtags == ["RAG", "MLOps", "AI"]


def test_editing_marks_the_post_as_edited(client, db):
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    client.post(f"/posts/{pid}", data={"content": "My own words.", "hashtags": "#One, Two three"})
    db.expire_all()
    post = db.get(Post, pid)
    assert post.content == "My own words."
    assert post.hashtags == ["One", "Two", "three"]
    assert post.edited_by_user


def test_publishing_runs_the_real_publisher(client, db, monkeypatch):
    """With dry run on, the full path runs and nothing is sent."""
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    r = client.post(f"/posts/{pid}/publish")
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Post, pid).status is PostStatus.dry_run


def test_history_lists_the_users_posts(client):
    create(client)
    r = client.get("/posts")
    assert r.status_code == 200
    assert "what I learned tuning RAG" in r.text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_a_too_short_topic_is_refused_without_calling_the_model(client, monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda *a, **k: pytest.fail("model called"))
    r = client.post("/new", data={"topic": "ai", "post_category": "learning", "tone": "direct"})
    assert r.status_code == 200
    assert "few more words" in r.text


def test_an_invalid_post_type_is_refused(client, monkeypatch):
    monkeypatch.setattr(llm, "generate", lambda *a, **k: pytest.fail("model called"))
    r = client.post("/new", data={"topic": "a real topic", "post_category": "nonsense",
                                  "tone": "direct"})
    assert "Pick a post type" in r.text


def test_a_generation_failure_is_shown_not_raised(client, monkeypatch):
    def boom(*a, **k):
        raise llm.GenerationError("both down")
    monkeypatch.setattr(llm, "generate", boom)
    r = client.post("/new", data={"topic": "a real topic", "post_category": "learning",
                                  "tone": "direct"})
    assert r.status_code == 200
    assert "busy right now" in r.text


def test_an_over_length_post_cannot_be_published(client, db, monkeypatch):
    monkeypatch.setattr(publisher, "publish_now", lambda *a, **k: pytest.fail("published"))
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    client.post(f"/posts/{pid}", data={"content": "x" * 3100, "hashtags": ""})
    r = client.post(f"/posts/{pid}/publish")
    assert "3%2C000" in r.headers["location"] or "3,000" in r.headers["location"]


def test_a_dry_run_can_be_deleted_from_its_page(client, db):
    """Dry runs are practice. Without a Delete button, every test run would
    leave a post the user can never remove from the app."""
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    client.post(f"/posts/{pid}/publish")                         # dry run
    page = client.get(f"/posts/{pid}").text
    assert f'action="/posts/{pid}/delete"' in page
    assert "This was a dry run" in page

    assert client.post(f"/posts/{pid}/delete").status_code == 303
    db.expire_all()
    assert db.get(Post, pid) is None


def test_a_published_post_can_no_longer_be_edited(client, db):
    pid = create(client)
    client.post(f"/posts/{pid}/choose", data={"index": 0})
    client.post(f"/posts/{pid}/publish")                         # dry run
    r = client.post(f"/posts/{pid}", data={"content": "changed", "hashtags": ""})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Ownership -- plan section 13
# ---------------------------------------------------------------------------

@pytest.fixture
def someone_elses_post(monkeypatch, db) -> int:
    monkeypatch.setattr(llm, "generate", fake_generation)
    other = signed_in(monkeypatch, SUB_B)
    pid = create(other)
    other.post(f"/posts/{pid}/choose", data={"index": 0})
    return pid


ROUTES_TAKING_A_POST = [
    ("get", "/posts/{id}", None),
    ("get", "/posts/{id}/drafts", None),
    ("post", "/posts/{id}", {"content": "hijacked", "hashtags": ""}),
    ("post", "/posts/{id}/choose", {"index": 1}),
    ("post", "/posts/{id}/publish", None),
    ("post", "/posts/{id}/delete", None),
    ("post", "/posts/{id}/image/remove", None),
]


def test_another_users_post_is_invisible(client, someone_elses_post, db):
    """404, not 403: a 403 would confirm the id exists.

    One test walking the routes rather than a parametrised case each. Every
    case needs two sign-ins and a generated post to arrange, and none of them
    changes anything -- the whole point is that they all 404 and the post
    survives untouched -- so rebuilding that seven more times bought nothing
    and cost about half a minute against a remote database. The same shape as
    test_privacy.py's walk over every registered post route.
    """
    for method, path, data in ROUTES_TAKING_A_POST:
        url = path.format(id=someone_elses_post)
        r = getattr(client, method)(url, data=data) if data else getattr(client, method)(url)
        assert r.status_code == 404, f"{method.upper()} {path} returned {r.status_code}"

    db.expire_all()
    post = db.get(Post, someone_elses_post)
    assert post is not None, "the other user's post must survive"
    assert post.content != "hijacked"
    assert post.status is PostStatus.draft


def test_history_does_not_show_other_users_posts(client, someone_elses_post):
    assert f"/posts/{someone_elses_post}" not in client.get("/posts").text


def test_signed_out_requests_are_refused():
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/new").status_code == 401
    assert anon.get("/posts").status_code == 401


# ---------------------------------------------------------------------------
# What the browser is trusted with
# ---------------------------------------------------------------------------

URLS_THE_SERVER_MUST_REFUSE = [
    "http://169.254.169.254/latest/meta-data/",      # cloud metadata endpoint
    "http://localhost:8000/healthz",
    "https://evil.example.com/x.jpg",
    "http://images.pexels.com/photos/1.jpg",         # right host, wrong scheme
    "https://images.pexels.com.evil.com/x.jpg",      # look-alike host
    "file:///etc/passwd",
]


def test_the_server_will_not_fetch_arbitrary_urls(client, monkeypatch):
    """The photo URL arrives from the browser. Fetching whatever it says would
    let any signed-in user make the server request internal addresses.

    One post for all six, rather than a sign-in and a generated draft each:
    every one is refused before anything is fetched, so there is no state for
    them to share badly.
    """
    fetched: list[str] = []
    monkeypatch.setattr(storage, "fetch", lambda *a, **k: fetched.append(a[0] if a else "?"))
    pid = create(client)

    for url in URLS_THE_SERVER_MUST_REFUSE:
        r = client.post(f"/posts/{pid}/image/pexels", data={"url": url})
        assert r.status_code == 400, f"{url} returned {r.status_code}"

    assert fetched == [], f"the server fetched {fetched}"


def test_a_genuine_pexels_url_is_accepted(client, db, monkeypatch):
    monkeypatch.setattr(storage, "fetch", lambda *a, **k: b"img")
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=900, height=600, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))
    pid = create(client)
    r = client.post(f"/posts/{pid}/image/pexels",
                    data={"url": "https://images.pexels.com/photos/1/large.jpg"})
    assert r.status_code == 303
    db.expire_all()
    assert len(db.get(Post, pid).images) == 1


def test_an_uploaded_non_image_is_refused_with_a_message(client, db):
    pid = create(client)
    r = client.post(f"/posts/{pid}/image/upload",
                    files={"file": ("evil.jpg", b"#!/bin/sh\nrm -rf /", "image/jpeg")})
    assert r.status_code == 303
    assert "look" in r.headers["location"].lower()
    db.expire_all()
    assert not db.get(Post, pid).images


def test_replacing_an_image_removes_the_old_file(client, db, monkeypatch):
    """The row cascade does not reach the bucket; the old file must be deleted
    explicitly or it is orphaned forever."""
    deleted: list[str] = []
    monkeypatch.setattr(storage, "delete", lambda path: deleted.append(path))
    monkeypatch.setattr(storage, "path_from_url", lambda url: url.rsplit("public/", 1)[-1])
    monkeypatch.setattr(storage, "fetch", lambda *a, **k: b"img")
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=900, height=600, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    for _ in range(2):
        client.post(f"/posts/{pid}/image/pexels",
                    data={"url": "https://images.pexels.com/photos/1/large.jpg"})
    assert len(deleted) == 1
    db.expire_all()
    assert len(db.get(Post, pid).images) == 1
