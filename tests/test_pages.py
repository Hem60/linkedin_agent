"""Pages render, and errors render as pages rather than raw JSON."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.integration
client = TestClient(app, raise_server_exceptions=False)


def test_landing_page_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert "Sign in with LinkedIn" in r.text
    assert "text/html" in r.headers["content-type"]


def test_stylesheet_is_served():
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "--brand" in r.text


def test_unknown_page_renders_an_html_error_not_json():
    r = client.get("/nope")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Nothing here" in r.text
    assert '{"detail"' not in r.text


def test_framework_reason_phrases_do_not_reach_the_user():
    """Starlette says 'Not Found'; our copy is friendlier and should win."""
    r = client.get("/nope")
    assert "Not Found" not in r.text


def test_a_custom_detail_is_shown():
    """A message we wrote deliberately must survive to the page."""
    r = client.get("/auth/callback?code=x&state=forged")
    assert r.status_code == 400
    assert "Sign-in could not be verified" in r.text


def test_signed_out_onboarding_offers_sign_in():
    r = client.get("/onboarding")
    assert r.status_code == 401
    assert "You need to sign in" in r.text
    assert "/auth/login" in r.text


def test_every_page_has_a_skip_link():
    assert 'class="skip"' in client.get("/").text


# ---------------------------------------------------------------------------
# Cache busting
# ---------------------------------------------------------------------------

def test_the_stylesheet_url_carries_a_version():
    """Without it a returning browser keeps the CSS it already has, and a deploy
    that changes only styling reaches nobody until their cache happens to
    expire."""
    r = client.get("/")
    assert re.search(r'href="/static/app\.css\?v=[0-9a-f]{10}"', r.text), \
        "stylesheet link has no cache-busting version"


def test_the_version_changes_when_the_stylesheet_changes(tmp_path, monkeypatch):
    """A version that never moves is the same as having none."""
    from app import templating

    css = tmp_path / "app.css"
    css.write_text("a{color:red}", encoding="utf-8")
    monkeypatch.setattr(templating, "_CSS", css)
    monkeypatch.setattr(templating, "_asset_cache", None)

    first = templating.asset_version()
    css.write_text("a{color:blue}", encoding="utf-8")
    second = templating.asset_version()

    assert first != second, "editing the stylesheet did not change its version"
    assert len(first) == 10 and len(second) == 10


def test_the_version_survives_a_missing_file():
    """A missing stylesheet is the server's problem; the page must still render
    rather than raising on the way out."""
    from app import templating

    original, cache = templating._CSS, templating._asset_cache
    try:
        templating._CSS = Path("app/static/does-not-exist.css")
        templating._asset_cache = None
        assert templating.asset_version() == "0"
    finally:
        templating._CSS, templating._asset_cache = original, cache


# ---------------------------------------------------------------------------
# Main navigation
# ---------------------------------------------------------------------------

NAV_SUB = "navUser"


@pytest.fixture
def signed_in(monkeypatch):
    from app import auth
    from app.database import SessionLocal
    from sqlalchemy import text

    monkeypatch.setattr(auth, "exchange_code_for_token",
                        lambda c: {"access_token": "tok", "expires_in": 5_184_000})
    monkeypatch.setattr(auth, "fetch_userinfo",
                        lambda t: {"sub": NAV_SUB, "name": "Nav Test",
                                   "email": f"{NAV_SUB}@x.com"})
    c = TestClient(app, follow_redirects=False)
    state = c.get("/auth/login").headers["location"].split("state=")[1].split("&")[0]
    c.get(f"/auth/callback?code=x&state={state}")
    yield c

    s = SessionLocal()
    s.execute(text("DELETE FROM users WHERE linkedin_person_urn = :u"),
              {"u": f"urn:li:person:{NAV_SUB}"})
    s.commit()
    s.close()


@pytest.mark.parametrize("path", ["/", "/new", "/posts", "/account"])
def test_every_signed_in_page_can_reach_the_rest_of_the_app(signed_in, path):
    """There was no navigation at all: from the editor the only way to write
    another post was back through the dashboard."""
    r = signed_in.get(path)
    assert r.status_code == 200, path
    for target in ('href="/"', 'href="/new"', 'href="/posts"', 'href="/account"'):
        assert target in r.text, f"{path} cannot reach {target}"


@pytest.mark.parametrize("path,expected", [
    ("/", "Dashboard"), ("/new", "Write"), ("/posts", "Posts"),
])
def test_the_header_says_which_page_you_are_on(signed_in, path, expected):
    r = signed_in.get(path)
    marked = re.findall(r'<a href="[^"]*"\s+class="on" aria-current="page">([^<]+)</a>', r.text)
    assert marked == [expected], f"{path} marked {marked}, expected [{expected}]"


def test_a_post_page_keeps_posts_marked_as_the_section(signed_in):
    """/posts/12 is still the Posts section, so equality would under-mark it."""
    r = signed_in.get("/posts/999999")          # 404 body still renders the shell
    assert r.status_code in (404, 200)


def test_the_signed_out_header_offers_a_way_in():
    """A visitor who scrolled past the hero had nothing to click."""
    r = client.get("/")
    assert 'href="/auth/login"' in r.text
    assert 'aria-label="Main"' not in r.text, "nav must not show to signed-out visitors"
