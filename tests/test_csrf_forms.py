"""Forms submitted the way a browser submits them -- token in the body.

This file exists because of a bug that reached the user. The CSRF middleware
read the form to find the token, which consumed the request stream, so the
route that ran next saw an empty body and rejected every field as missing. The
whole suite passed regardless, because the test client sends the token as a
header and the middleware therefore never touched the body.

So these tests never send the header. They scrape the token out of the rendered
page and post it in the body, exactly as a browser does, and assert the route
still received its fields.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import auth, limits, llm
from app.csrf import HEADER
from app.database import SessionLocal
from app.main import app
from app.models import ModelUsed, Post, User
from tests.conftest import purge_users

pytestmark = pytest.mark.integration

SUB = "formUser"
ORIGIN = {"origin": "http://localhost:8000"}


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture(autouse=True)
def _purge():
    s = SessionLocal()

    def purge():
        purge_users("form", s)

    purge()
    yield
    purge()
    s.close()


def _drafts(*a, **k):
    return llm.Generation(
        drafts=[llm.Draft(body=f"Draft {i} body.", hashtags=["AI"], photo_query="x",
                          score={"hook": 8}, improvement="") for i in range(3)],
        model_used=ModelUsed.gemini, attempts=1)


@pytest.fixture
def browser(monkeypatch) -> TestClient:
    """A client that behaves like a browser: no CSRF header, ever."""
    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": SUB, "name": "Form Test", "email": f"{SUB}@x.com"})
    monkeypatch.setattr(llm, "generate", _drafts)
    monkeypatch.setattr(limits, "get_settings",
                        lambda: type("S", (), {"daily_post_limit": 20})())

    c = TestClient(app, follow_redirects=False)
    st = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={st}")
    return c


def token_from(client: TestClient, path: str) -> str:
    """Read the token out of the page, as a browser would when submitting it."""
    match = re.search(r'name="_csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"no CSRF field on {path}"
    return match.group(1)


def post_form(client: TestClient, path: str, data: dict, token: str):
    """Submit with the token in the body and no header at all."""
    return client.post(path, data={**data, "_csrf": token},
                       headers={**ORIGIN, HEADER: ""})


# ---------------------------------------------------------------------------

def test_the_new_post_form_reaches_the_route_with_its_fields(browser, db):
    """The reported bug: every field arrived as null and the page 422'd."""
    token = token_from(browser, "/new")
    r = post_form(browser, "/new", {
        "topic": "a genuine topic typed by a person",
        "post_category": "learning",
        "tone": "direct",
    }, token)

    assert r.status_code == 303, r.text[:300]
    post_id = int(r.headers["location"].split("/")[2])
    post = db.get(Post, post_id)
    assert post.topic == "a genuine topic typed by a person"


def test_a_form_without_the_token_is_still_refused(browser):
    """The fix must not have turned the protection off."""
    r = browser.post("/new", data={"topic": "a genuine topic", "post_category": "learning",
                                   "tone": "direct"},
                     headers={**ORIGIN, HEADER: ""})
    assert r.status_code == 403


def test_a_form_with_the_wrong_token_is_refused(browser):
    r = post_form(browser, "/new", {"topic": "a genuine topic",
                                    "post_category": "learning", "tone": "direct"},
                  "not-the-real-token")
    assert r.status_code == 403


def test_the_onboarding_form_keeps_its_fields(browser, db):
    token = token_from(browser, "/onboarding")
    r = post_form(browser, "/onboarding", {
        "account_type": "personal",
        "role_title": "ML Engineer",
        "niche": "RAG systems",
        "default_tone": "storytelling",
        "timezone": "Asia/Kolkata",
    }, token)

    assert r.status_code == 303
    db.expire_all()
    user = db.query(User).filter_by(linkedin_person_urn=f"urn:li:person:{SUB}").one()
    assert user.role_title == "ML Engineer"
    assert user.timezone == "Asia/Kolkata"


def test_editing_a_post_keeps_its_fields(browser, db):
    token = token_from(browser, "/new")
    created = post_form(browser, "/new", {"topic": "another real topic",
                                          "post_category": "learning", "tone": "direct"}, token)
    post_id = int(created.headers["location"].split("/")[2])

    choose_token = token_from(browser, f"/posts/{post_id}/drafts")
    post_form(browser, f"/posts/{post_id}/choose", {"index": 1}, choose_token)

    edit_token = token_from(browser, f"/posts/{post_id}")
    r = post_form(browser, f"/posts/{post_id}",
                  {"content": "Words I typed myself.", "hashtags": "RAG MLOps"}, edit_token)
    assert r.status_code == 303

    db.expire_all()
    post = db.get(Post, post_id)
    assert post.content == "Words I typed myself."
    assert post.hashtags == ["RAG", "MLOps"]


def test_a_file_upload_still_reaches_the_route(browser, db):
    """Multipart goes through the same body-reading path as a plain form."""
    token = token_from(browser, "/new")
    created = post_form(browser, "/new", {"topic": "a topic with a picture",
                                          "post_category": "learning", "tone": "direct"}, token)
    post_id = int(created.headers["location"].split("/")[2])
    choose = token_from(browser, f"/posts/{post_id}/drafts")
    post_form(browser, f"/posts/{post_id}/choose", {"index": 0}, choose)

    upload_token = token_from(browser, f"/posts/{post_id}")
    r = browser.post(f"/posts/{post_id}/image/upload",
                     data={"_csrf": upload_token},
                     files={"file": ("x.jpg", b"not-an-image", "image/jpeg")},
                     headers={**ORIGIN, HEADER: ""})
    # Rejected for being a bad image, which means the route ran and saw the file
    # -- not rejected for a missing field before ever getting there.
    assert r.status_code == 303
    assert "look" in r.headers["location"].lower()
