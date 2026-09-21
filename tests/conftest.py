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

import pytest  # noqa: E402

TABLES = ("post_performance", "post_images", "usage", "posts", "users")


def _guard(schema: str) -> None:
    if schema in ("", "public") or not schema.startswith("test"):
        pytest.exit(
            f"Refusing to run: tests would use schema {schema!r}. "
            "They must run in a schema whose name starts with 'test'.",
            returncode=2,
        )


@pytest.fixture(scope="session", autouse=True)
def isolated_database():
    from app.config import get_settings

    settings = get_settings()
    _guard(settings.db_schema)
    if settings.db_schema != TEST_SCHEMA:
        pytest.exit("DB_SCHEMA was read before conftest set it.", returncode=2)

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

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


def pytest_report_header(config):
    return f"database schema: {TEST_SCHEMA} (isolated from public)"
