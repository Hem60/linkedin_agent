"""Settings behaviour that the rest of the app relies on."""

from __future__ import annotations

import pytest

from app.config import Settings

BASE = {
    "database_url": "postgresql+psycopg://u:p@localhost:5432/db",
    "session_secret": "s" * 32,
    "encryption_key": "k" * 44,
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_redirect_uri_matches_what_linkedin_has_registered():
    s = _settings(app_url="http://localhost:8000")
    assert s.redirect_uri == "http://localhost:8000/auth/callback"


def test_redirect_uri_tolerates_a_trailing_slash_on_app_url():
    # LinkedIn matches redirect URLs exactly; a stray slash must not leak through.
    s = _settings(app_url="https://postpilot.example.com/")
    assert s.redirect_uri == "https://postpilot.example.com/auth/callback"


def test_admin_emails_parse_into_a_normalised_list():
    s = _settings(admin_emails=" Alice@Example.com , bob@example.com ")
    assert s.admin_email_list == ["alice@example.com", "bob@example.com"]


def test_admin_emails_empty_means_no_admins():
    assert _settings().admin_email_list == []


def test_require_names_every_missing_key():
    s = _settings()
    with pytest.raises(RuntimeError) as exc:
        s.require("gemini_api_key", "cloudflare_api_token")
    assert "GEMINI_API_KEY" in str(exc.value)
    assert "CLOUDFLARE_API_TOKEN" in str(exc.value)


def test_require_passes_when_configured():
    s = _settings(gemini_api_key="present")
    s.require("gemini_api_key")  # must not raise


def test_dry_run_defaults_to_true():
    # Safety default: a fresh deployment must not publish to a real profile.
    assert _settings().dry_run is True
