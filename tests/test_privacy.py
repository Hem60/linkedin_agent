"""Security and privacy -- plan.md section 13.

Three things, each of which is only worth having if it actually holds:

* CSRF: another site must not be able to act as a signed-in user.
* Deletion: "delete my account" must reach the database, the storage bucket
  and the LinkedIn token, and must not be a flag.
* Ownership: every route that takes an id must refuse someone else's.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth, security, storage
from app.database import SessionLocal
from app.main import app
from app.models import (
    ImageSource,
    Post,
    PostCategory,
    PostImage,
    PostStatus,
    Tone,
    User,
)
from app.privacy import export_data

SUB = "privacyUserA"
OTHER = "privacyUserB"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge(request):
    if "integration" not in request.keywords:
        yield
        return
    s = SessionLocal()

    def purge():
        s.rollback()
        s.execute(text("DELETE FROM users WHERE linkedin_person_urn LIKE 'urn:li:person:privacy%'"))
        s.commit()

    purge()
    yield
    purge()
    s.close()


def _client(monkeypatch, sub=SUB) -> TestClient:
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "linkedin-token", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": sub, "name": "Privacy Test", "email": f"{sub}@x.com"})
    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def _user(db, sub=SUB) -> User:
    return db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{sub}").one()


def _post(db, user, *, with_image=False) -> Post:
    p = Post(user_id=user.id, topic="a topic", post_category=PostCategory.learning,
             tone=Tone.direct, content="Body (here).", hashtags=["AI"],
             status=PostStatus.draft,
             image_source=ImageSource.pexels if with_image else ImageSource.none)
    db.add(p)
    db.commit()
    if with_image:
        db.add(PostImage(post_id=p.id, source=ImageSource.pexels, position=0,
                         url="https://x.supabase.co/storage/v1/object/public/"
                             "post-images/users/1/posts/abc.jpg"))
        db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_post_without_a_token_is_refused(monkeypatch, db):
    """The attack this exists for: a hidden form on another site, submitted by
    a browser that is already signed in here."""
    client = _client(monkeypatch)
    post = _post(db, _user(db))

    raw = TestClient(app, follow_redirects=False)
    raw.cookies.update(client.cookies)          # same session, no token supplied
    from app.csrf import HEADER
    r = raw.post(f"/posts/{post.id}/delete", headers={HEADER: ""})

    assert r.status_code == 403
    assert "couldn't be verified" in r.text
    db.expunge_all()
    assert db.get(Post, post.id) is not None, "the post must survive"


@pytest.mark.integration
def test_a_wrong_token_is_refused(monkeypatch, db):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    from app.csrf import HEADER
    r = client.post(f"/posts/{post.id}/delete", headers={HEADER: "not-the-token"})
    assert r.status_code == 403
    db.expunge_all()
    assert db.get(Post, post.id) is not None


@pytest.mark.integration
def test_a_request_claiming_another_origin_is_refused(monkeypatch, db):
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    r = client.post(f"/posts/{post.id}/delete", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
    db.expunge_all()
    assert db.get(Post, post.id) is not None


@pytest.mark.integration
def test_the_real_form_works(monkeypatch, db):
    """The protection is worthless if it also blocks the app's own forms."""
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    assert client.post(f"/posts/{post.id}/delete").status_code == 303
    db.expunge_all()
    assert db.get(Post, post.id) is None


@pytest.mark.integration
def test_every_form_carries_a_token(monkeypatch, db):
    """A form missing the field is a page that stops working -- exactly the
    kind of thing that is easy to forget when adding one."""
    client = _client(monkeypatch)
    post = _post(db, _user(db))
    client.post("/onboarding", data={"account_type": "personal", "timezone": "UTC",
                                     "default_tone": "direct"})

    import re
    for path in ("/new", f"/posts/{post.id}", "/account", "/posts"):
        html = client.get(path).text
        forms = re.findall(r"<form\b[^>]*method=\"post\"[^>]*>(.*?)</form>", html, re.DOTALL | re.IGNORECASE)
        for body in forms:
            assert 'name="_csrf"' in body, f"a form on {path} has no CSRF field"


def test_safe_methods_are_not_blocked():
    """A GET changes nothing, so demanding a token would only break links."""
    anon = TestClient(app)
    assert anon.get("/privacy").status_code == 200


@pytest.mark.parametrize("origin,ok", [
    ("http://localhost:8000", True),
    ("http://localhost:8000/posts/1", True),
    ("https://evil.example.com", False),
    ("http://localhost:9999", False),
    (None, True),        # absent: some browsers omit it same-origin
])
def test_origin_matching(monkeypatch, origin, ok):
    from app import csrf as csrf_mod

    class Req:
        headers = {"origin": origin} if origin else {}

    monkeypatch.setattr(csrf_mod, "get_settings",
                        lambda: type("S", (), {"app_url": "http://localhost:8000"})())
    assert csrf_mod._origin_ok(Req()) is ok


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_the_export_contains_the_account_and_its_posts(monkeypatch, db):
    client = _client(monkeypatch)
    user = _user(db)
    _post(db, user)

    r = client.get("/account/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]

    data = json.loads(r.text)
    assert data["account"]["email"] == f"{SUB}@x.com"
    assert data["posts"][0]["topic"] == "a topic"
    assert data["posts"][0]["content"] == "Body (here)."


@pytest.mark.integration
def test_the_export_never_contains_the_linkedin_token(monkeypatch, db):
    """A plaintext token in a downloaded file is a hazard to the person we are
    trying to help."""
    client = _client(monkeypatch)
    user = _user(db)
    raw = security.decrypt(user.access_token)

    body = client.get("/account/export").text
    assert raw not in body
    assert user.access_token not in body
    assert "access_token" not in body


@pytest.mark.integration
def test_the_export_holds_only_your_own_posts(monkeypatch, db):
    _client(monkeypatch, OTHER)
    other_post = _post(db, _user(db, OTHER))
    _client(monkeypatch, SUB)

    data = export_data(db, _user(db, SUB))
    assert other_post.id not in [p["id"] for p in data["posts"]]


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_deleting_removes_the_account_and_everything_under_it(monkeypatch, db):
    client = _client(monkeypatch)
    user = _user(db)
    user_id = user.id
    post = _post(db, user, with_image=True)

    assert client.post("/account/delete", data={"confirm": "DELETE"}).status_code == 303

    db.expunge_all()
    assert db.get(User, user_id) is None, "the account must be gone, not flagged"
    assert db.get(Post, post.id) is None
    assert db.execute(
        text("SELECT count(*) FROM post_images WHERE post_id = :p"), {"p": post.id}
    ).scalar() == 0


@pytest.mark.integration
def test_deleting_removes_the_image_files_too(monkeypatch, db, mocker=None):
    """No cascade reaches the bucket. Without this, every deleted account
    leaves its pictures behind."""
    deleted: list[str] = []
    import app.privacy as privacy_mod
    original = privacy_mod.storage.delete
    privacy_mod.storage.delete = lambda path: deleted.append(path)
    try:
        client = _client(monkeypatch)
        _post(db, _user(db), with_image=True)
        client.post("/account/delete", data={"confirm": "DELETE"})
    finally:
        privacy_mod.storage.delete = original

    assert deleted == ["users/1/posts/abc.jpg"]


@pytest.mark.integration
def test_deletion_needs_the_word_typed(monkeypatch, db):
    client = _client(monkeypatch)
    user_id = _user(db).id

    r = client.post("/account/delete", data={"confirm": "yes"})
    assert r.status_code == 303 and "Type" in r.headers["location"]
    db.expunge_all()
    assert db.get(User, user_id) is not None


@pytest.mark.integration
def test_a_failing_bucket_does_not_abandon_the_deletion(monkeypatch, db):
    """A partially deleted account is worse than a leftover file."""
    import app.privacy as privacy_mod
    original = privacy_mod.storage.delete

    def boom(path):
        raise storage.StorageError("bucket unavailable")

    privacy_mod.storage.delete = boom
    try:
        client = _client(monkeypatch)
        user_id = _user(db).id
        _post(db, _user(db), with_image=True)
        assert client.post("/account/delete", data={"confirm": "DELETE"}).status_code == 303
    finally:
        privacy_mod.storage.delete = original

    db.expunge_all()
    assert db.get(User, user_id) is None


@pytest.mark.integration
def test_deleting_ends_the_session(monkeypatch, db):
    client = _client(monkeypatch)
    client.post("/account/delete", data={"confirm": "DELETE"})
    assert client.get("/account").status_code == 401


@pytest.mark.integration
def test_deleting_one_account_leaves_the_other_alone(monkeypatch, db):
    _client(monkeypatch, OTHER)
    other_id = _user(db, OTHER).id
    other_post = _post(db, _user(db, OTHER))

    client = _client(monkeypatch, SUB)
    _post(db, _user(db, SUB))
    client.post("/account/delete", data={"confirm": "DELETE"})

    db.expunge_all()
    assert db.get(User, other_id) is not None
    assert db.get(Post, other_post.id) is not None


# ---------------------------------------------------------------------------
# Ownership, audited rather than assumed
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_no_route_taking_a_post_id_serves_another_users_post(monkeypatch, db):
    """Checked route by route while building, but nothing verified the set.
    This walks every registered /posts/{post_id} route."""
    _client(monkeypatch, OTHER)
    victim = _post(db, _user(db, OTHER))
    client = _client(monkeypatch, SUB)

    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if "{post_id}" not in path:
            continue
        url = path.replace("{post_id}", str(victim.id))
        for method in sorted(set(getattr(route, "methods", []) or []) - {"HEAD", "OPTIONS"}):
            # Every form field any of these routes requires: a missing one is
            # rejected by validation before ownership is ever consulted.
            # Every input any of these routes requires. A missing one is
            # rejected by validation (422) before ownership is consulted, which
            # would hide whether the ownership check exists at all.
            r = client.request(method, url, data={
                "index": 0, "when": "2030-01-01T09:00", "content": "x",
                "hashtags": "", "confirm": "DELETE",
                "url": "https://images.pexels.com/photos/1/large.jpg",
            }, files={"file": ("x.jpg", b"not-an-image", "image/jpeg")})
            assert r.status_code == 404, f"{method} {path} returned {r.status_code}"
            checked += 1

    assert checked >= 8, f"expected to check several routes, checked {checked}"
    db.expunge_all()
    assert db.get(Post, victim.id) is not None
    assert db.get(Post, victim.id).content == "Body (here)."
