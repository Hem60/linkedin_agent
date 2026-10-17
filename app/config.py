"""Application settings, loaded once from the environment.

Only the three values under "Required" must be present to boot. Everything else
is optional so the app can run before its later phases are wired up -- a missing
key surfaces as a clear error at the point of use, not as a crash on startup.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Required ---------------------------------------------------------
    database_url: str
    session_secret: str
    encryption_key: str

    # Postgres schema to use. Empty means the default (`public`), which is
    # where the real app lives. The test suite sets this to `test_postpilot`
    # so tests can never read or modify real users' posts -- see
    # tests/conftest.py.
    db_schema: str = ""

    # --- LinkedIn ---------------------------------------------------------
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_api_version: str = "202609"
    app_url: str = "http://localhost:8000"

    # --- Models -----------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    gemini_cooldown_minutes: int = 30
    model_timeout_seconds: int = 45

    # --- Photos and storage -----------------------------------------------
    pexels_api_key: str = ""
    storage_url: str = ""
    storage_key: str = ""
    storage_bucket: str = "post-images"

    # --- Email ------------------------------------------------------------
    resend_api_key: str = ""
    email_from: str = ""

    # --- Observability ----------------------------------------------------
    sentry_dsn: str = ""
    environment: str = "development"

    # --- Behaviour --------------------------------------------------------
    dry_run: bool = True
    # Run the publishing worker inside the web process (plan section 14,
    # option 1). Safe alongside a separate worker: claims use SKIP LOCKED.
    run_scheduler: bool = True
    daily_post_limit: int = 3
    daily_rewrite_limit: int = 10
    admin_emails: str = ""

    # --- Derived ----------------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return f"{self.app_url.rstrip('/')}/auth/callback"

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    def require(self, *names: str) -> None:
        """Raise a clear error when a phase needs a key that isn't configured."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise RuntimeError(
                "Missing required settings: "
                + ", ".join(n.upper() for n in missing)
                + ". Add them to .env (see .env.example)."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
