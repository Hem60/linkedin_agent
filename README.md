# PostPilot

Turns a topic into a professional LinkedIn post, then publishes it — now or on a
schedule. See [plan.md](plan.md) for the full design and [spike/](spike/) for the
Phase 0 feasibility work.

## Status

| Phase | State |
|---|---|
| 0 — Feasibility spike | ✅ passed, results in plan.md §2 |
| 1 — Setup | ✅ this commit |
| 2 — Data model | next |

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`. Generate the two secrets with:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`DATABASE_URL` needs a Postgres instance — see below. Everything else can stay
empty until the phase that uses it.

## Database

Postgres, not SQLite. The schema depends on Postgres-only features:

- `JSONB` for quality scores
- `pg_trgm` for the duplicate-topic check (plan §12)
- `SELECT ... FOR UPDATE SKIP LOCKED` for the publish lock (F22, plan §15)

A free Neon or Supabase instance is enough. The URL must use the psycopg v3
driver prefix:

```
DATABASE_URL=postgresql+psycopg://user:password@host/dbname
```

Then:

```
alembic upgrade head
```

## Run

```
uvicorn app.main:app --reload
```

- `GET /healthz` — liveness plus a real database round trip
- `GET /` — placeholder

## Tests

```
pytest -q
```

Tests run in their own Postgres schema, `test_postpilot`, never in `public`.
`tests/conftest.py` forces `DB_SCHEMA` before the app is imported, migrates the
test schema to head, and empties it at the start of each run.

This matters because some tests are necessarily global: the publish lock's
claim query takes *any* due scheduled post, exactly as the worker does. Against
the real tables, a test run would claim a real user's scheduled post and mark it
published through a fake LinkedIn, so it would never actually go out.

The suite refuses to start if the schema isn't a test schema:

```
TEST_DB_SCHEMA=public pytest   # exits 2: "Refusing to run"
```

Unit tests alone, no database, about two seconds:

```
pytest -m "not integration"
```

## Safety

`DRY_RUN=true` is the default and means nothing is published to LinkedIn: posts
are built, sanitized and stored, but the API call is skipped. Turn it off only
when you intend to post under a real name.
