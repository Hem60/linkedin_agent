"""Pages render, and errors render as pages rather than raw JSON."""

from __future__ import annotations

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
