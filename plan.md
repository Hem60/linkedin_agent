# LinkedIn Post Agent: Project Plan

> **Revision 4.** Tracked in git from Phase 1 onward.
>
> Rev 2: scope split into v1 / v2 / cut, features removed where LinkedIn gates the API
> access they need, five features added, build order reversed so the riskiest work happens
> first.
>
> Rev 3: every API assumption checked against LinkedIn's live documentation (§2). Company
> page access is an application, not a partnership — harder than rev 2 claimed, but not
> closed. §9 rewritten around LinkedIn's `little` text format, which neither earlier
> revision knew about.
>
> **Rev 4: Phase 0 has been run against the live API and passed.** Results in §2. Two
> changes came out of it: F21 (first comment) is blocked by a 403 and moves to v2, and §9
> now documents the measured failure mode — unescaped text returns 201 and is then
> silently truncated, which is worse than an error and undetectable from the response.

## 1. Project Goal

A web app where a user signs in with LinkedIn, types a topic, and the agent writes a
professional post with hashtags and an image, then publishes it to the user's own
LinkedIn profile now or at a scheduled time. Posts are written by Gemini, with Groq as
the backup. Business accounts get a brand post style and a copy-to-post flow for company
pages.

**What this is not:** it does not post to company pages automatically, and it does not
read likes or comments from LinkedIn. Both need API products that LinkedIn grants only to
approved partners. See §2.

## 2. Access Reality Check

*Verified against LinkedIn's developer documentation, September 2026. Sources at the end
of this section.*

### Confirmed available — "Open Permissions", self-serve, no approval

LinkedIn's wording: "Open Permissions are the only permissions that are available to all
developers without special approval... may be added via self-service through the
Developer Portal, under the Products tab on your application page."

| Product | Permission | Gives us |
|---|---|---|
| Sign in with LinkedIn using OpenID Connect | `profile`, `email` | Login, name, headline, photo, primary email |
| Share on LinkedIn | `w_member_social` | **"Post, comment and like posts on behalf of an authenticated member."** |

That single sentence covers F18 (post), F21 (first comment) and the whole v1 publishing
path. This is the entire technical foundation of the project and it is open to anyone.

### Confirmed restricted

| Wanted | Blocker | Our answer |
|---|---|---|
| Read likes/comments on a member's own posts | Needs `r_member_social` — "**restricted**, available to **approved users only**". LinkedIn's FAQ is blunter: "`r_member_social` is a **closed** permission. We're not accepting access requests at this time due to resource constraints." | F26, self-reported numbers |
| Refresh tokens | "LinkedIn supports programmatic refresh tokens for **all approved Marketing Developer Platform (MDP) partners**." Not us. | Access token lives 60 days, then the member re-authorizes. F5 is permanent, not an edge case. |

### Correction: company page posting is harder, not impossible

The Community Management API is a **vetted product with two tiers**, not a closed partner
program:

- **Development Tier** — apply through the Developer Portal, limited volume (500
  requests/app, 100/member). Approval is an application, not a partnership.
- **Standard Tier** — requires your organization's legal name, registered address,
  website and privacy policy, plus a screencast under 5 minutes showing the OAuth flow and
  a user posting to their page through your app. **You must upgrade within 12 months or
  LinkedIn removes your access.**

The catch that shapes our architecture: *"Only request Community Management API
Development Tier access with new developer applications that don't have access to other
API products."* Community Management cannot be added to the app that holds Share on
LinkedIn. It needs a **second developer app**, verified separately, and then merged back
per LinkedIn's FAQ #1.

So native company page posting stays in v2 as its own track, and F20 (copy-to-post) is
what ships in v1. Note that Community Management would also unlock member Post Statistics,
which would replace F26 — another reason it is the highest-value v2 item.

### Sources

- [Getting Access to LinkedIn APIs](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access) — Open Permissions table
- [Community Management Overview](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview) — tiers, FAQ, closed `r_member_social`
- [Refresh Tokens with OAuth 2.0](https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens) — MDP-partners-only
- [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api) — endpoint, permissions, errors
- [Comments API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/comments-api) — first-comment endpoint
- [little Text Format](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format) — escaping rules, see §9

### Phase 0 results — run 18 Sep 2026, app "PostPilot", `urn:li:person:ca21Gt_PnM`

| Probe | Result |
|---|---|
| OAuth, four scopes | ✅ `openid profile email w_member_social` granted |
| Access token TTL | ✅ 60 days, **no refresh token** — as predicted |
| `POST /rest/posts`, text | ✅ 201 |
| Unescaped reserved characters | ⚠️ **201, then silently truncated** — see §9 |
| Escaped text | ✅ published complete, backslashes invisible |
| Hashtag templates | ✅ rendered as real clickable hashtags |
| Images API + image post | ✅ two-step upload worked |
| **First comment** | ❌ **403 ACCESS_DENIED** |

The comment failure settles the open question, against us:

```
403 {"serviceErrorCode":100,"code":"ACCESS_DENIED",
     "message":"Not enough permissions to access: partnerApiSocialActions.CREATE"}
```

Despite the Open Permissions table describing `w_member_social` as *"Post, **comment** and
like posts,"* and the Auth tab describing it as *"Create, modify, and delete posts,
**comments**, and reactions,"* the versioned socialActions endpoint refuses it. The
Comments API page was right: commenting needs `w_member_social_feed`, a Community
Management permission. **F21 moves to v2**; links go in the post body.

Everything else in v1 is confirmed working against the live API. Phase 0 is complete and
the gate is passed.

## 3. Scope

### v1 — build this (27 features)

| # | Feature |
|---|---|
| F1 | Sign in with LinkedIn (OpenID Connect), one click |
| F2 | Profile setup once: role, niche, timezone, personal or business |
| F3 | Company profile for business accounts: name, products, target markets, buyer types |
| F4 | Account deletion and data export |
| F5 | Token expiry warning with one-click reconnect |
| F6 | Topic-only input |
| F7 | Post type selection, 8 types, each with its own template |
| F8 | One short type-specific follow-up question, skippable |
| F9 | Tone selection: professional, storytelling, direct |
| F10 | Three drafts from a **single** model call |
| F11 | Hashtags: 3–5 personal, 8–10 business, emitted as `little` hashtag templates (§9) |
| F12 | Quality score returned inline with each draft, no extra call |
| F13 | Duplicate topic warning |
| F14 | Pexels stock photo with alternatives |
| F15 | Upload your own image, with type/size/dimension checks and resizing |
| F16 | **LinkedIn-accurate preview:** `little`-format escaping, markdown stripped, live 3,000-character counter |
| F17 | Edit text, swap image, remove image, or request a rewrite |
| F18 | Post now |
| F19 | Schedule in the user's timezone, reschedule, cancel |
| F20 | **Copy-to-post for company pages:** generate the brand post, user pastes it into their page |
| ~~F21~~ | ~~First comment field~~ — **moved to v2**, blocked by a 403 (§2) |
| F22 | **Publish idempotency guard** so a retry or restart cannot double-post |
| F23 | Dry-run mode, builds everything without publishing |
| F24 | Post history with failure reason, retry, and an email alert on failure |
| F25 | Gemini main, Groq backup, model recorded per post |
| F26 | **Self-reported performance:** the user enters likes and comments after posting |
| F27 | Per-user data isolation, daily limits, admin usage view |

### v2 — after v1 is live and used

- First comment for links (F21) — needs `w_member_social_feed` via Community Management
- Insights dashboard built on F26 data
- Best posting time suggestion
- Content calendar month view
- Learning the user's voice from their edited posts
- Multiple images in one post
- Brand kit (logo, colors) and generated brand graphics
- Bring your own Gemini or Groq key
- **Community Management API track** — a second developer app, Development Tier
  application, then Standard Tier with a screencast. Unlocks native company page posting
  *and* real member post statistics (replacing F26). Highest-value v2 item; see §2 for why
  it cannot share the v1 app.

> Note: the brand kit moved to v2 with the generated graphics. In v1 it would store logos
> and colors that nothing consumes.

### Cut entirely

| Was | Why |
|---|---|
| Separate safety-check model call | Gemini and Groq already refuse offensive output; this spends free quota to re-check our own model |
| Up to 30 hashtags | Reduces reach on LinkedIn rather than increasing it |
| API-based likes and comments fetching | The data is not available for personal posts |
| Separate quality-score model call | Folded into the generation call at zero cost |

## 4. Tech Stack

| Part | Tool | Change from rev 1 |
|---|---|---|
| Language | Python 3.11+ | |
| Backend | FastAPI | |
| Frontend | Jinja2 templates + Tailwind CSS | |
| Main writer | Google Gemini API | |
| Backup writer | Groq API (Llama) | |
| Photos | Pexels API | |
| Photo storage | Supabase Storage | |
| Upload handling | FastAPI uploads + Pillow | |
| Graphic generation | Pillow only | **Playwright removed** — too heavy for a 512 MB instance |
| Login and publishing | LinkedIn OIDC + Share on LinkedIn | Community Management removed |
| Database | PostgreSQL (Supabase or Neon) | |
| Database access | SQLAlchemy 2.x | |
| **Migrations** | **Alembic** | **Added** — was missing entirely |
| Scheduler | APScheduler + DB row locking | Locking added |
| Timezones | `zoneinfo` | |
| **Email** | **Resend (or SMTP)** | **Added** — F5 and F24 need to reach the user |
| **Monitoring** | **Sentry + structured logging** | **Added** — a silently failed scheduled post is otherwise invisible |
| **Tests** | **pytest** | **Added** |
| Security | `cryptography` (Fernet) | |
| Sessions | Signed secure cookies | |
| Settings | pydantic-settings + `.env` | |
| Hosting | Render | See §14 on worker cost |

## 5. Keys and Settings

| Key | Used for | Cost |
|---|---|---|
| `GEMINI_API_KEY` | Main writer | Free tier |
| `GROQ_API_KEY` | Backup writer | Free tier |
| `PEXELS_API_KEY` | Photo search | Free |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | Login and posting | Free |
| `DATABASE_URL` | PostgreSQL | Free tier |
| `STORAGE_KEY` | Image storage | Free tier |
| `RESEND_API_KEY` | Failure and expiry emails | Free tier |
| `SENTRY_DSN` | Error reporting | Free tier |
| `ENCRYPTION_KEY` | Encrypts tokens | Generated once |
| `SESSION_SECRET` | Session cookies | Generated once |
| `ADMIN_EMAILS` | Who can open the admin view | — |

Settings: `DRY_RUN`, `DAILY_POST_LIMIT`, `DAILY_REWRITE_LIMIT`,
`GEMINI_COOLDOWN_MINUTES`, `APP_URL`, `LINKEDIN_API_VERSION`.

Per-user data created automatically: LinkedIn access token, person ID, token expiry.

## 6. How the Agent Works

1. User signs in with LinkedIn and sets role, niche, timezone, and account type once.
2. User enters a topic, picks a post type and tone, and answers one optional question.
3. App checks the daily limit and warns if a similar topic was posted before.
4. One call to Gemini (or Groq on failure) returns three drafts, each with hashtags, a
   photo phrase, and a self-assessed score.
5. Pexels returns a photo, or the user uploads their own, or the post goes out text-only.
6. The draft is **sanitized to plain text** and shown in a preview matching how LinkedIn
   will actually render it, with a live character count.
7. User edits, confirms the image, then chooses post now or a time in their timezone.
   Business users can instead copy the finished post for their company page.
8. The worker claims due posts with a row lock, publishes with that user's token, and
   records the LinkedIn post ID.
9. Later, the user optionally enters how the post performed.

## 7. Model Contract

One call returns everything. No second call for scoring.

```json
{
  "drafts": [
    {
      "body": "plain text, no markdown, blank lines between paragraphs",
      "hashtags": ["#tag1", "#tag2", "#tag3"],
      "photo_query": "two or three words for Pexels",
      "score": { "hook": 8, "clarity": 7, "readability": 9 },
      "improvement": "one sentence"
    }
  ]
}
```

Enforced in code, not trusted to the model:

- Reply is not valid JSON → retry once, then fall back to the other model.
- `body` contains markdown (`**`, `*`, `#`, `- `, backticks) → strip it (§9).
- `body` is then escaped to `little` format and hashtags are templated — always, in code,
  never by asking the model (§9).
- Rendered length over 3,000 characters → ask the model to shorten once, then truncate at
  a paragraph boundary.
- Wrong number of drafts or missing fields → retry once.

## 8. Model Fallback

**Order:** Gemini, then Groq.

- 429 or quota message from Gemini → switch to Groq immediately.
- Timeout or malformed reply → retry Gemini once, then Groq.
- After a quota switch, Groq is used for `GEMINI_COOLDOWN_MINUTES`, then Gemini is retried.
- The same prompt and the same JSON shape go to both, so style stays consistent.
- Both fail → the post is marked `failed` with the reason. Scheduled posts retry with
  backoff up to 3 times, then email the user.
- `model_used` is stored on every post.

**Quota budget.** With scoring folded in, a normal post costs **one** call and a rewrite
costs one more. That is what lets a shared free tier support multiple users. The original
design cost four to seven calls per post, which would exhaust the free quota at a handful
of active users.

## 9. Post Style and the `little` Text Format

**This is the single most important implementation detail in the project, and it is worse
than "LinkedIn has no markdown."**

The `commentary` field of the Posts API is not plain text. It is LinkedIn's **`little`
text format**, and it reserves fifteen characters:

```
|  {  }  @  [  ]  (  )  <  >  #  \  *  _  ~
```

LinkedIn's rule, quoted: *"All reserved characters need to be escaped with a backslash,
**even if those characters are not used in one of the supported elements or templates**."*

Read that twice. An ordinary sentence like

> We shipped v2 (finally) — here's what I learned.

contains two reserved characters and must be sent as `v2 \(finally\)`. Every parenthesis,
every asterisk the model emits for emphasis, every underscore in a variable name, every
`#`.

### What actually happens — measured, not assumed

Phase 0 tested this on a real profile. The failure mode is **worse than an error**.

Sent unescaped:

> Testing the LinkedIn API from a throwaway script.
> This line contains reserved characters: (parentheses), an \*asterisk\*, an \_underscore\_ and a \~tilde\~.
> If you are reading this as a clean sentence, escaping worked.

The API returned **201 Created**. The published post read, in full:

> Testing the LinkedIn API from a throwaway script.
> This line contains reserved characters:

**Everything from the first `(` onward was silently discarded.** No error, no warning, no
indication in the response. The same text, escaped, published complete and correct with
the backslashes invisible to readers.

So this is not a validation problem you can catch by checking status codes — a truncated
post looks like a successful post to your application, and like a half-finished thought to
everyone reading it under a real name. The sanitizer and its tests are the only thing
standing between the model's output and that outcome.

**Hashtags are not plain text either.** A literal `#AI` must be escaped to `\#AI`, which
renders as inert text. To produce a real, clickable hashtag you must emit LinkedIn's
template:

```
{hashtag|\#|AI}
```

So F11 does not append `#AI #Python` to the body. It emits hashtag templates, built in
code from the tag list the model returned.

**The pipeline, in order:**

1. Model returns `body` and `hashtags` as ordinary text (it is never asked to produce
   `little` format — that is a job for code, not a prompt).
2. Strip markdown: emphasis, headings, code fences, link syntax. List items become `•` or
   `→` plus a space.
3. Escape all fifteen reserved characters in the body.
4. Append hashtags as `{hashtag|\#|Tag}` templates.
5. Check length, and handle the API's `FIELD_LENGTH_TOO_LONG` error as a real case, not a
   surprise. Target LinkedIn's 3,000-character post limit; count the *rendered* length,
   not the escaped string, since backslashes are encoding and not visible text.
6. Preview shows the **rendered** result — what the reader will see — while the escaped
   string is what gets sent.

`test_sanitize.py` covers all fifteen reserved characters, hashtag templating, and
round-tripping a post with parentheses, an em dash, emoji and a code-ish word.

### Style rules for the prompt

- Hook line under 12 words
- Paragraphs of 1–2 lines with a blank line between them
- 3 or 4 practical points
- Professional but human, no buzzwords
- Ends with a question to invite comments
- No fixed length; the model decides based on the topic
- Written for the user's role and niche, in the selected type and tone

**Business post shape:** emoji tagline hook, a line on the problem or market, a product
paragraph, feature bullets with emoji, a company line, a call to action naming the buyers,
a contact invite, then **8–10** hashtags mixing brand, product, industry and buyer tags.

## 10. Post Types

| Type | Structure | Extra question |
|---|---|---|
| Learning | What you tried, what went wrong, what you learned, the takeaway | None |
| Achievement | The result, the effort, credit to others, thanks | What made it hard |
| Project announcement | Problem, what you built, tech used, what is next, the link | Repo or demo link |
| Tips or how-to | Short intro then numbered practical points | None |
| Industry insight | A trend or opinion, your reasoning, what it means for readers | None |
| Career update | The news, a short reflection, thanks | Company or course name |
| Event recap | Where you were, what you did, takeaways, people met | Event name |
| Business or brand post | Emoji hook, product paragraph, feature bullets, company line, CTA, contact invite | Product being promoted |

Each type is a template in `prompts.py`. The chosen type is stored on the post so v2
insights can compare types. Extra questions are optional; skipping one just means less
detail.

**Links go in the post body in v1.** Putting them in the first comment is the better
practice, but the Comments API refuses `w_member_social` with a 403 (§2), so F21 is v2.
The Project announcement type places its link at the end of the body instead.

## 11. Images

**Option 1 — Stock photo (default).** The model returns a search phrase, Pexels returns
matches, the chosen one is copied to our storage. Pexels asks for credit where possible,
so a stock photo adds an attribution line to the first comment.

**Option 2 — Upload your own.** JPG, PNG or WebP, maximum 5 MB, resized if larger.
Landscape or square recommended because LinkedIn crops tall images. Stored under the
user's account only, never shared. Replaceable any number of times before publishing.

**Option 3 — Text only.** Fully supported.

Generated brand graphics are v2.

## 12. Quality and Safety

- **Quality score:** returned inline with each draft (§7). Low-scoring drafts are flagged
  in the UI with the model's one-line suggestion.
- **Duplicate check:** normalized topic plus `pg_trgm` similarity against the user's last
  50 topics. Above 0.6 shows a warning, never a block. Chosen over embeddings so no vector
  column and no extra API call are needed.
- **Length check:** enforced in code, not by the prompt.
- **Safety:** relies on the providers' own refusal behaviour plus a short blocklist. No
  extra model call.
- **Human approval:** every post is approved by the user before publishing. There is no
  fully automatic mode in v1, because posts go out under a real person's name.

## 13. Performance Tracking (F26)

LinkedIn will not give us the numbers — `r_member_social` is closed, "not accepting access
requests at this time" (§2) — so the user gives them to us:

- Three days after a post is published, the user sees a prompt: "How did this do?"
- They enter likes and comments, or just tap 👍 / 👎.
- Stored with the post's type, tone, hook, and publish hour.
- v2 turns this into the insights dashboard and the best-time suggestion.

This is weaker data than the API would give, but it is data we can actually have, and it
costs one input field.

## 14. Limits, Cost, and the Worker

- `DAILY_POST_LIMIT` per user, default 3. Rewrites have their own smaller limit.
- Usage counted in a `usage` table with a composite primary key, so two concurrent
  requests cannot both pass the check.
- Admin view (F27) shows per-day totals across users, restricted to `ADMIN_EMAILS`.

**Hosting reality:** Render background workers are a paid service, and free web services
sleep when idle. Two options for v1:

1. **Recommended to start:** run the scheduler inside the web process and keep it awake
   with an external cron pinger. Free, with a risk of posts firing a few minutes late.
2. Pay for a worker (about $7/month) once scheduling matters enough to justify it.

Either way the publish path must be duplicate-safe — see F22 and §15.

## 15. Database Design

All timestamps are `timestamptz` stored in UTC. Every query filters by `user_id`.

**users**

| Column | Notes |
|---|---|
| id | PK |
| linkedin_person_urn | **unique, not null** |
| name, email | From LinkedIn |
| access_token | Encrypted |
| token_expires_at | Drives F5 |
| role_title, niche | Personal context |
| account_type | `personal` or `business` |
| company_name, products, target_markets, buyer_types | Business context |
| default_tone, timezone | |
| deleted_at | Soft delete for F4 |
| created_at | |

Dropped for v1: `logo_url`, `brand_colors`, `company_page_urn`, `gemini_key`, `groq_key`,
`auto_post`. Each returns with its v2 feature.

**posts**

| Column | Notes |
|---|---|
| id | PK |
| user_id | FK, indexed |
| topic, post_category, tone | |
| content | Final sanitized plain text including hashtags |
| first_comment | F21 |
| edited_by_user | bool |
| quality_score | jsonb from §7 |
| post_type | `personal` or `business` |
| image_source | `pexels`, `upload`, or `none` |
| status | `draft`, `scheduled`, **`publishing`**, `posted`, `failed`, `dry_run` |
| scheduled_at | UTC |
| model_used | `gemini` or `groq` |
| linkedin_post_id | |
| error_message, retry_count | |
| created_at, posted_at | |

Index: `(user_id, status, scheduled_at)` — the worker's only query.

The `publishing` status **is** the idempotency guard (F22): the worker moves a row
`scheduled → publishing` inside `SELECT ... FOR UPDATE SKIP LOCKED` before calling
LinkedIn, so a restart or a second instance cannot publish the same post twice.

**post_images** — replaces the previously undefined `image_urls` column

| Column | Notes |
|---|---|
| id | PK |
| post_id | FK |
| url, source, position | `position` gives ordering for v2 multi-image |

**post_performance** — replaces `metrics`

| Column | Notes |
|---|---|
| id | **PK (the original table had none)** |
| post_id | FK, unique |
| likes, comments | Nullable, user-entered |
| sentiment | 👍 / 👎 shortcut |
| reported_at | |

**usage**

| Column | Notes |
|---|---|
| user_id, date | **Composite PK** |
| posts_generated, rewrites | |

## 16. Folder Structure

```
linkedin-post-agent/
    .env  .gitignore  requirements.txt  plan.md  README.md
    alembic/                migrations
    app/
        main.py             FastAPI app and routes
        config.py           settings
        auth.py             LinkedIn OIDC, sessions, CSRF
        llm.py              Gemini, Groq, fallback, JSON validation
        prompts.py          style prompt, type templates, tones
        sanitize.py         markdown stripping, little-format escaping, hashtag
                            templating, length guard, rendered preview string
        quality.py          duplicate check, score handling
        photos.py           Pexels search and storage
        uploads.py          upload checks and resizing
        linkedin.py         image upload, publishing, first comment
        publisher.py        claim-lock-publish, the only code that posts
        performance.py      self-reported numbers
        notify.py           email for failures and token expiry
        database.py  models.py  security.py  limits.py
        templates/  static/
    worker/
        scheduler.py        claims and publishes due posts
    tests/
        test_sanitize.py    all 15 reserved chars escaped, hashtags templated
        test_publisher.py   no double-publish under concurrency
        test_schedule.py    timezone to UTC conversion
        test_fallback.py    Gemini failure falls through to Groq
```

## 17. Build Phases

**Phase 0 — Feasibility spike. Do this first, in one day.**
Create the LinkedIn developer app and self-serve-add **Sign in with LinkedIn using OpenID
Connect** and **Share on LinkedIn** from the Products tab. Then, from a throwaway script,
prove the four things the rest of the plan rests on:

1. `POST /rest/posts` publishes a text post — headers `Authorization`,
   `LinkedIn-Version: YYYYMM`, `X-Restli-Protocol-Version: 2.0.0`; body with `author`,
   `commentary`, `visibility`, `distribution`, `lifecycleState: PUBLISHED`. Returns 201
   with the post URN in the `x-restli-id` response header.
2. A post whose text contains `( ) * _ #` survives escaping and renders correctly — and
   one deliberately *unescaped* post, to see the failure mode first-hand.
3. A hashtag sent as `{hashtag|\#|Test}` renders as a real clickable hashtag.
4. `POST /rest/socialActions/{postUrn}/comments` adds a comment with `w_member_social`.
   **This is the one open question:** the Open Permissions table says `w_member_social`
   covers commenting, but the Comments API reference lists `w_member_social_feed`, a
   Community Management permission. If the call 403s, F21 moves to v2 and links go in the
   post body.

Also upload one image via the Images API and post it, to confirm the two-step flow.

**Gate: if publishing does not work, nothing else in this plan matters.**

**Phase 1 — Setup.** Repo, `.env`, `.gitignore`, requirements, Postgres, storage bucket,
Alembic, Sentry.

**Phase 2 — Data model.** Every table from §15 as the first migration, with constraints
and indexes.

**Phase 3 — Auth and onboarding.** OIDC login with a `state` value, encrypted token
storage, profile setup, personal/business branch.

**Phase 4 — Generation.** Style prompt, eight type templates, tones, the §7 JSON
contract, validation and retry.

**Phase 5 — Groq fallback.** Quota detection, switch, cooldown, model logging. Test by
forcing Gemini to fail.

**Phase 6 — Sanitizer and preview.** `sanitize.py` with tests: markdown stripping,
`little`-format escaping of all fifteen reserved characters, hashtag templating, rendered
character counter, exact-render preview. **Before any publishing code** — this is what
stops broken posts going out under a real name, and per §9 it is the highest-risk code in
the project.

**Phase 7 — Images.** Pexels search, storage, upload with validation and resizing, swap
and remove.

**Phase 8 — Publishing.** `publisher.py`: claim lock, image upload to LinkedIn, post,
first comment, dry-run mode. Concurrency test.

**Phase 9 — Web interface.** Login, onboarding, new post, drafts with scores, preview and
edit, history.

**Phase 10 — Scheduling.** Timezone to UTC, schedule, reschedule, cancel, worker loop,
retries with backoff.

**Phase 11 — Limits and notifications.** Daily limits, usage table, admin view, failure
emails, token expiry emails.

**Phase 12 — Business mode.** Business post template, 8–10 hashtags, copy-to-post for
company pages (F20).

**Phase 13 — Security and privacy.** Ownership checks on every query, CSRF on forms,
privacy policy page, account deletion and export.

**Phase 14 — Performance capture.** The "How did this do?" prompt and its storage.

**Phase 15 — Testing.** Two or three real LinkedIn accounts in dry-run. Verify data
isolation, timezone correctness across two zones, limits, token expiry handling, and
fallback. Then publish a small number of real posts.

**Phase 16 — Deployment.** Render, database, storage, production redirect URL, cron
pinger or paid worker.

**Then v2**, in order of value: insights → content calendar → voice learning → multiple
images → brand kit and graphics → BYOK → company page posting if access is ever granted.

## 18. Things to Watch

- **Phase 0 is a gate, not a formality.** Publishing is confirmed available in the docs,
  but confirm it from your own app before building on it.
- **The `little` format escaping (§9) is the highest-risk code you will write.** Measured
  in Phase 0: an unescaped `(` silently truncates the post while the API still returns
  201. You cannot detect this from the response. It has its own phase, its own test file,
  and it runs before anything can publish.
- `r_member_social` is closed, so F26 is the only way to know how a post performed until
  the Community Management track lands.
- Community Management cannot be added to the app holding Share on LinkedIn. If you ever
  want company pages, that is a second app and a 12-month clock to Standard Tier.
- Free Gemini and Groq quotas are shared across all users. One call per post is what keeps
  this viable.
- Free Render web services sleep, so scheduled posts may fire late until a worker is paid
  for.
- LinkedIn tokens expire in about 60 days with no refresh token, so reconnection is a
  permanent part of the product, not an edge case.
- Always store UTC and convert only for display. Test across two timezones.
- Use the versioned `/rest/posts` API with a `LinkedIn-Version` header; `/v2/ugcPosts` is
  deprecated.
- Only allow uploads of images the user owns. Credit Pexels where a stock photo is used.
- Free model tiers may use prompts to improve products, so tell users not to enter
  confidential information.
- Never commit `.env` or any key.

## 19. Future Ideas

- Posts generated from a GitHub repo or an article link
- Carousel and document posts
- Comment reply suggestions
- Team accounts for managing several profiles
- Weekly email performance summary
