"""The write-and-publish flow -- plan.md phase 9.

Models, Pexels, storage and LinkedIn are replaced. What is tested is the web
layer's own decisions: who can touch which post, what the browser is trusted
with, and that each step hands the next one what it expects.
"""

from __future__ import annotations

import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app import auth, imagegen, llm, publisher, storage, uploads
from app.database import SessionLocal
from app.main import app
from app.models import ImageSource, ModelUsed, Post, PostStatus
from tests.conftest import purge_users

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
        purge_users("compose", db)
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


def _stub_generation(monkeypatch, raw=b"img"):
    monkeypatch.setattr(imagegen, "generate", lambda subject, styled=True: raw)
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1024, height=576, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))


def test_a_generated_image_is_attached_to_the_post(client, db, monkeypatch):
    _stub_generation(monkeypatch)
    pid = create(client)
    r = client.post(f"/posts/{pid}/image/generate", data={"q": "a quiet workshop"})
    assert r.status_code == 303
    db.expire_all()
    post = db.get(Post, pid)
    assert len(post.images) == 1
    assert post.images[0].source is ImageSource.generated


def test_generating_uses_the_drafts_own_photo_query_when_nothing_is_typed(
        client, db, monkeypatch):
    """The model already suggested a subject with the draft; making someone
retype it to get a picture is asking for work already done."""
    seen: list[str] = []
    monkeypatch.setattr(imagegen, "generate",
                        lambda subject, styled=True: seen.append(subject) or b"img")
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1024, height=576, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))
    pid = create(client)
    db.expire_all()
    query = db.get(Post, pid).photo_query

    client.post(f"/posts/{pid}/image/generate", data={"q": ""})
    assert seen == [query]


def test_a_typed_prompt_reaches_the_model_unstyled(client, db, monkeypatch):
    """The box is the user's, not the app's: what they type is what is drawn."""
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(imagegen, "generate",
                        lambda subject, styled=True: seen.append((subject, styled)) or b"img")
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1024, height=576, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    client.post(f"/posts/{pid}/image/generate", data={"q": "a red tractor in a field"})
    assert seen == [("a red tractor in a field", False)]


def test_generating_never_fetches_a_browser_supplied_url(client, monkeypatch):
    """The Pexels route takes a URL and fetches it, which is why it checks the
    host. Generation takes a prompt and gets bytes back, so there is no address
    to abuse -- asserted, so that reintroducing a fetch here cannot pass
    unnoticed."""
    fetched: list[str] = []
    monkeypatch.setattr(storage, "fetch", lambda *a, **k: fetched.append(a[0] if a else "?"))
    _stub_generation(monkeypatch)
    pid = create(client)

    for hostile in ("http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
        assert client.post(f"/posts/{pid}/image/generate",
                           data={"q": hostile}).status_code == 303
    assert fetched == [], f"the server fetched {fetched}"


def test_a_spent_allowance_is_shown_and_points_at_the_photo_search(
        client, db, monkeypatch):
    """Seven or so images a day, then the page must still offer a way to get a
picture rather than looking broken."""
    def spent(subject, styled=True):
        raise imagegen.ImageGenError(
            "Today's image allowance is used up. Search for a photo instead.")
    monkeypatch.setattr(imagegen, "generate", spent)
    pid = create(client)
    r = client.post(f"/posts/{pid}/image/generate", data={"q": "anything"})
    assert r.status_code == 303
    # The message rides in the query string, so it arrives percent-encoded.
    assert "search for a photo" in urllib.parse.unquote(r.headers["location"]).lower()
    db.expire_all()
    assert not db.get(Post, pid).images


def test_a_branded_card_is_drawn_and_attached(client, db, monkeypatch):
    """The words on the card are the words that were typed -- which is the
    whole reason it is drawn rather than generated."""
    from app import imagegen, postcard

    monkeypatch.setattr(imagegen, "generate", lambda subject, styled=True: b"img")
    seen: list = []
    real_render = postcard.render
    monkeypatch.setattr(
        postcard, "render",
        lambda card, photo=None: seen.append(card) or real_render(card, None))
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1080, height=1350, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    r = client.post(f"/posts/{pid}/image/card", data={
        "headline": "WHY COMPARTMENTS\n|MATTER",
        "eyebrow": "PRODUCT SPOTLIGHT",
        "features": "Keeps food separate | No mixing\nCatering ready | For volume",
    })
    assert r.status_code == 303
    assert seen[0].headline == "WHY COMPARTMENTS\n|MATTER"
    assert [f.label for f in seen[0].features] == ["Keeps food separate", "Catering ready"]
    assert seen[0].features[0].detail == "No mixing"

    db.expire_all()
    post = db.get(Post, pid)
    assert len(post.images) == 1
    assert post.images[0].source is ImageSource.card


def test_the_card_picture_is_of_what_the_card_says(client, db, monkeypatch):
    """A card about exporting peanuts came back showing a code editor, because
    the route reused whatever was already attached -- a picture generated from
    a photo_query written for an entirely different post."""
    from app import imagegen, postcard

    asked: list[str] = []
    monkeypatch.setattr(imagegen, "generate",
                        lambda subject, styled=True: asked.append(subject) or b"img")
    monkeypatch.setattr(storage, "fetch",
                        lambda *a, **k: pytest.fail("reused a stale picture"))
    real_render = postcard.render
    monkeypatch.setattr(postcard, "render", lambda card, photo=None: real_render(card, None))
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1080, height=1350, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    client.post(f"/posts/{pid}/image/card", data={"headline": "PREMIUM PEANUTS"})
    assert asked == ["PREMIUM PEANUTS"]

    asked.clear()
    client.post(f"/posts/{pid}/image/card", data={
        "headline": "PREMIUM PEANUTS", "photo_subject": "raw peanuts in a sack"})
    assert asked == ["raw peanuts in a sack"], "a typed subject should win"


def test_the_headline_is_never_the_instruction_that_made_the_draft(client, db, monkeypatch):
    """post.topic is what someone typed to get a draft -- "write the post for
    import peanut" -- and it was going onto the card as its headline."""
    from app import imagegen, postcard

    monkeypatch.setattr(imagegen, "generate", lambda subject, styled=True: b"img")
    seen: list = []
    real_render = postcard.render
    monkeypatch.setattr(
        postcard, "render",
        lambda card, photo=None: seen.append(card) or real_render(card, None))
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1080, height=1350, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    db.expire_all()
    topic = db.get(Post, pid).topic

    client.post(f"/posts/{pid}/image/card", data={"headline": "PREMIUM PEANUTS"})
    assert seen[0].headline == "PREMIUM PEANUTS"
    assert seen[0].headline != topic


def test_a_card_is_still_drawn_when_no_picture_can_be_had(client, db, monkeypatch):
    """A spent allowance should cost the photograph, not the card."""
    from app import imagegen

    def spent(subject, styled=True):
        raise imagegen.ImageGenError("Today's image allowance is used up.")

    monkeypatch.setattr(imagegen, "generate", spent)
    monkeypatch.setattr(uploads, "prepare_upload", lambda raw, name="": uploads.PreparedImage(
        data=b"x", content_type="image/jpeg", width=1080, height=1350, format="JPEG"))
    monkeypatch.setattr(storage, "put", lambda data, path, ct: storage.StoredImage(
        path=path, url=f"https://store/public/{path}", content_type=ct, bytes=1))

    pid = create(client)
    r = client.post(f"/posts/{pid}/image/card", data={"headline": "STILL A CARD"})
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
