"""Test isolation: the suite runs in its own Postgres schema, never in `public`.

Why this exists. The tests used to run against the real tables, and some of
them are necessarily global: the publish lock's claim query takes *any* due
scheduled post, because that is what the worker does. Run against real data,
the suite would claim a real user's scheduled post and "publish" it through a
fake LinkedIn -- marking it posted with an invented id, so it would never
actually go out. It also left a scheduled test post behind in `public` that
the phase 10 worker would have tried to publish.

So:
  1. DB_SCHEMA is forced to a test schema before anything imports the app.
     This file is loaded by pytest ahead of every test module, and settings are
     cached on first read, so the order matters.
  2. The schema is migrated to head, exactly as production is.
  3. Its tables are emptied once per run, by schema-qualified name. Qualified,
     not unqualified: if migration had somehow failed, an unqualified TRUNCATE
     would fall through the search path and empty `public`.
  4. The whole run aborts if the schema is not a test schema.
"""

from __future__ import annotations

import os

TEST_SCHEMA = os.environ.get("TEST_DB_SCHEMA", "test_postpilot")

# Must happen before `app` is imported anywhere.
os.environ["DB_SCHEMA"] = TEST_SCHEMA
os.environ["DRY_RUN"] = "true"
# The worker must not tick in the background while tests arrange data.
os.environ["RUN_SCHEDULER"] = "false"
# Pin the allowance too, for the same reason as the two above: the suite must
# not depend on whoever ran it. Setting DAILY_POST_LIMIT=0 in a real .env --
# a supported way to turn the limit off -- silently broke a test that asserted
# the compose page shows what is left. Tests needing another value patch it.
os.environ["DAILY_POST_LIMIT"] = "3"

import pytest

TABLES = ("post_performance", "post_images", "usage", "posts", "users")


# ---------------------------------------------------------------------------
# CSRF: behave like a browser
# ---------------------------------------------------------------------------
#
# Every unsafe request needs the session's CSRF token. A real browser gets it
# from the hidden field in the page it is submitting; a TestClient has no page.
# Rather than thread a token through three hundred existing tests -- or, worse,
# add a switch that turns the protection off and could reach production -- the
# client reads it from the session cookie, which is signed rather than
# encrypted and therefore readable by anything holding it. The protection
# itself is exercised directly in tests/test_privacy.py.

def _install_csrf_aware_test_client() -> None:
    import json
    from base64 import b64decode

    from itsdangerous import BadSignature, TimestampSigner
    from starlette.testclient import TestClient

    from app.config import get_settings
    from app.csrf import HEADER, SAFE_METHODS, SESSION_KEY

    signer = TimestampSigner(str(get_settings().session_secret))
    original = TestClient.request

    def request(self, method, url, **kwargs):
        if method.upper() not in SAFE_METHODS:
            raw = self.cookies.get("session")
            token = None
            if raw:
                try:
                    # Starlette signs standard-base64 JSON; it is signed, not
                    # encrypted, so the session's contents are readable here.
                    data = json.loads(b64decode(signer.unsign(raw)))
                    token = data.get(SESSION_KEY)
                except (BadSignature, ValueError, KeyError):
                    token = None
            if token:
                headers = dict(kwargs.get("headers") or {})
                headers.setdefault(HEADER, token)
                kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    TestClient.request = request


_install_csrf_aware_test_client()


def _guard(schema: str) -> None:
    if schema in ("", "public") or not schema.startswith("test"):
        pytest.exit(
            f"Refusing to run: tests would use schema {schema!r}. "
            "They must run in a schema whose name starts with 'test'.",
            returncode=2,
        )


_needs_database = False


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    """Note whether this run actually touches the database.

    Without this, `pytest -m "not integration"` still paid for a migration and a
    live connection, so the unit tests could not run offline at all -- which is
    most of the value of having them separate.

    trylast matters: `-m` deselection happens in this same hook, so running
    first would still see the integration tests and connect anyway.
    """
    global _needs_database
    _needs_database = any("integration" in item.keywords for item in items)


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    if not _needs_database:
        yield
        return

    from app.config import get_settings

    settings = get_settings()
    _guard(settings.db_schema)
    if settings.db_schema != TEST_SCHEMA:
        pytest.exit("DB_SCHEMA was read before conftest set it.", returncode=2)

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.database import engine

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    with engine.begin() as conn:
        # Confirm the tables really are in the test schema before emptying
        # anything, then empty them by fully qualified name.
        found = {
            r[0] for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :s"),
                {"s": TEST_SCHEMA},
            )
        }
        missing = set(TABLES) - found
        if missing:
            pytest.exit(f"Test schema is missing tables {missing}; not truncating.", returncode=2)
        qualified = ", ".join(f"{TEST_SCHEMA}.{t}" for t in TABLES)
        conn.execute(text(f"TRUNCATE {qualified} RESTART IDENTITY CASCADE"))

    yield


def purge_users(prefix: str, session=None, attempts: int = 3) -> None:
    """Delete this module's test users, surviving a dropped connection.

    Supabase's session-mode pooler hands out a server connection per client
    and reclaims the idle ones. pool_recycle retires them on our side first,
    but nothing can save a connection that dies mid-statement -- and when that
    lands in a teardown, a test that passed is reported as an error. It has
    cost four full runs.

    So the cleanup retries on a fresh pool rather than failing the test it was
    tidying up after. A cleanup is idempotent by nature: deleting rows that are
    already gone is free.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.database import SessionLocal, engine

    # The caller's session may hold a transaction, or a connection that has
    # already died. Releasing it must not itself raise: that is the failure
    # this helper exists to prevent, one line earlier.
    if session is not None:
        try:
            session.rollback()
        except OperationalError:
            pass

    last: Exception | None = None
    for attempt in range(attempts):
        session = SessionLocal()
        try:
            session.execute(
                text("DELETE FROM users WHERE linkedin_person_urn LIKE :p"),
                {"p": f"urn:li:person:{prefix}%"},
            )
            session.commit()
            return
        except OperationalError as exc:
            last = exc
            # The connection is gone; rolling it back would raise again.
            try:
                session.rollback()
            except OperationalError:
                pass
            engine.dispose()        # discard the whole dead pool
        finally:
            try:
                session.close()
            except OperationalError:
                pass
    if last is not None:
        raise last


def pytest_report_header(config):
    return f"database schema: {TEST_SCHEMA} (isolated from public)"
