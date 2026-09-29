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

## Limits, email and admin

`DAILY_POST_LIMIT` caps how many posts one account can generate per day, to stop
one person exhausting the shared Gemini/Groq free quota. The day is the *user's*
day, not UTC — a limit that resets at UTC midnight would reset at 05:30 in India.
Set it to `0` for a private deployment where the quota is nobody else's problem.

Email uses [Resend](https://resend.com) and is entirely optional: without
`RESEND_API_KEY` and `EMAIL_FROM` the app runs normally and simply doesn't send.
Two messages exist, both because the alternative is silence:

- a scheduled post gave up (sent once, when it stops retrying)
- the LinkedIn connection expires within 7 days (at most weekly)

On Resend's free tier, until you verify a domain you can only send from
`onboarding@resend.dev` and only to the address the Resend account was created
with. A rejected send is logged, never raised.

`/admin` shows usage per day and per person for the last 14 days. It is visible
only to addresses listed in `ADMIN_EMAILS`, and shows counts only — never post
text. Anyone else gets a 404 rather than a 403, so the page's existence isn't
confirmed.

## Business mode

A business account gets the brand post structure -- emoji hook, product
paragraph, feature bullets, a call to action naming the buyers -- and 8-10
hashtags mixing brand, product, country and buyer tags. Not the 30 the original
plan allowed: that suppresses reach rather than extending it.

Publishing to a LinkedIn **company page** is not possible from this app.
`w_organization_social` needs Community Management access, which is a separate
application on a separate developer app (plan section 2). So the app does
everything up to the last step and hands over the finished text: Copy, open your
page, paste.

What it hands over is the *rendered* text, not the escaped `commentary`. You are
pasting into LinkedIn's own composer, which takes plain text; the escaping
exists only for the API, and pasting it would publish visible backslashes.

Marking a post as done sets `copied_at`. It is deliberately not a status change:
the app never sees the post reach LinkedIn, so calling it `posted` would be a
claim it cannot support.

## Security and privacy

**CSRF.** Every unsafe request needs a token tied to the session, and its Origin
must match `APP_URL` when the browser sends one. Session cookies are
`SameSite=Lax` as a third layer. Without this, another site could make a
signed-in browser publish a post or delete an account. The token is rotated when
someone signs in, since the session is cleared on a privilege change.

**Leaving.** `/account` offers both halves of it:

- **Export** — everything held about you as JSON. The LinkedIn access token is
  deliberately excluded: it is a credential held on your behalf, not data about
  you, and a plaintext token in a downloaded file is a hazard.
- **Delete** — a real delete, not a flag. It reaches the database rows, the
  image files in the storage bucket (no cascade goes there), and the LinkedIn
  token. Typing DELETE is required. A failing bucket does not abandon the
  deletion: a half-deleted account is worse than a leftover file.

`/privacy` states what is stored, what is not, and which three services see
which piece.

**Ownership** is checked route by route, and `test_privacy.py` walks every
registered `/posts/{post_id}` route to confirm none of them serves another
user's post.

## How did it do?

LinkedIn will not give an app the likes and comments on a member's own posts:
`r_member_social` is closed, in their own words "not accepting access requests
at this time". So the numbers come from the person who can see them.

Three days after a post is published, the dashboard asks. Typing the numbers is
one option; a thumbs up or down counts as an answer too, because most people
will not go and count comments and a weak signal offered willingly beats a
precise one nobody gives.

Only genuinely published posts are asked about -- a dry run never reached
LinkedIn, so there is nothing to have gone well or badly. Answers can be
corrected, and appear next to the post in history. This is what v2's insights
page will be built on.

## Safety

`DRY_RUN=true` is the default and means nothing is published to LinkedIn: posts
are built, sanitized and stored, but the API call is skipped. Turn it off only
when you intend to post under a real name.
