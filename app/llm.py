"""Draft generation -- plan.md sections 7 and 8.

Gemini writes; Groq takes over when it cannot. Both receive the identical
prompt and the identical JSON contract, so a fallback draft reads like a
primary one rather than announcing the switch.

Budget: one request per post. Scores come back inside the same response rather
than from a second scoring call, which is what makes a shared free tier viable
for more than a handful of users.

The failure ladder, cheapest recovery first:

    gemini primary  --busy-->  gemini lighter  --still failing-->  groq
                    --quota->  start cooldown, go straight to groq

A quota error means the day's allowance is gone, so Gemini is skipped entirely
for a while. Being momentarily busy is different and does not start a cooldown.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field

from app.config import get_settings
from app.models import ModelUsed, PostCategory, Tone, User
from app.prompts import build_prompt, hashtag_range

log = logging.getLogger(__name__)

MAX_RENDERED_CHARS = 3000       # LinkedIn's post limit
DRAFTS_EXPECTED = 3


class GenerationError(Exception):
    """Generation failed after every retry."""


@dataclass
class Draft:
    body: str
    hashtags: list[str]
    photo_query: str
    score: dict[str, int] = field(default_factory=dict)
    improvement: str = ""

    @property
    def overall(self) -> float:
        vals = [v for v in self.score.values() if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    @property
    def rendered_length(self) -> int:
        """What the reader sees: body plus the hashtag line. Escaping added later
        by sanitize.py is encoding, not visible text, so it is not counted."""
        tags = " ".join("#" + t for t in self.hashtags)
        return len(self.body) + (len(tags) + 2 if tags else 0)


@dataclass
class Generation:
    drafts: list[Draft]
    model_used: ModelUsed
    attempts: int


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _extract_json(text: str) -> dict:
    """Models wrap JSON in fences or add a sentence in front of it despite being
    told not to. Strip the common cases before giving up."""
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", cleaned, 0)


def parse_drafts(text: str, *, tag_range: tuple[int, int] = (3, 5)) -> list[Draft]:
    """Turn a model reply into drafts, or raise ValueError with a reason that is
    worth putting in a retry prompt."""
    payload = _extract_json(text)

    raw = payload.get("drafts")
    if not isinstance(raw, list):
        raise ValueError('missing a "drafts" array')
    if len(raw) != DRAFTS_EXPECTED:
        raise ValueError(f"expected {DRAFTS_EXPECTED} drafts, got {len(raw)}")

    drafts: list[Draft] = []
    for i, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise ValueError(f"draft {i} is not an object")

        body = (d.get("body") or "").strip()
        if not body:
            raise ValueError(f"draft {i} has an empty body")

        tags = d.get("hashtags") or []
        if not isinstance(tags, list):
            raise ValueError(f"draft {i} hashtags is not a list")
        tags = [str(t).lstrip("#").strip() for t in tags]
        tags = [t for t in tags if t]

        score = d.get("score") or {}
        if not isinstance(score, dict):
            score = {}
        score = {k: int(v) for k, v in score.items() if isinstance(v, (int, float))}

        drafts.append(
            Draft(
                body=body,
                hashtags=tags[: tag_range[1]],
                photo_query=str(d.get("photo_query") or "").strip(),
                score=score,
                improvement=str(d.get("improvement") or "").strip(),
            )
        )
    return drafts


def problems(drafts: list[Draft], tag_range: tuple[int, int]) -> list[str]:
    """Soft checks. These do not fail generation -- they are shown to the user
    and used to ask for a rewrite."""
    lo, hi = tag_range
    out = []
    for i, d in enumerate(drafts, 1):
        if d.rendered_length > MAX_RENDERED_CHARS:
            out.append(f"draft {i} is {d.rendered_length} characters, over LinkedIn's {MAX_RENDERED_CHARS}")
        if len(d.hashtags) < lo:
            out.append(f"draft {i} has {len(d.hashtags)} hashtags, wanted at least {lo}")
        if not d.photo_query:
            out.append(f"draft {i} has no photo query")
    bodies = [d.body for d in drafts]
    if len(set(bodies)) < len(bodies):
        out.append("two drafts are identical")
    return out


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

# Two different failures that both look like "it didn't work":
#
#   capacity -- 503/504. The model is busy right now. A lighter model usually
#               has headroom, and the flagship will be fine again shortly.
#   quota    -- 429/RESOURCE_EXHAUSTED. We have spent the day's free allowance.
#               Stepping down models does not help; the limit is on the key.
#               This is what starts the cooldown.
CAPACITY = ("503", "504", "UNAVAILABLE", "DEADLINE_EXCEEDED")
QUOTA = ("429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "rate_limit")


def _is_capacity(exc: Exception) -> bool:
    return any(m in str(exc) for m in CAPACITY)


def _is_quota(exc: Exception) -> bool:
    text = str(exc)
    return any(m.lower() in text.lower() for m in QUOTA)


def _is_transient(exc: Exception) -> bool:
    return _is_capacity(exc) or _is_quota(exc)


# ---------------------------------------------------------------------------
# Gemini cooldown
# ---------------------------------------------------------------------------
#
# After a quota error, Gemini is skipped entirely for a while rather than being
# retried on every request just to collect another 429. Process-local state: a
# second worker keeps its own clock and will hit one 429 of its own before
# settling. That is acceptable for the cost of avoiding a shared store, and is
# the reason the window is minutes rather than seconds.

_cooldown_until: dt.datetime | None = None


def gemini_in_cooldown() -> bool:
    return _cooldown_until is not None and dt.datetime.now(dt.timezone.utc) < _cooldown_until


def start_gemini_cooldown(minutes: int | None = None) -> None:
    global _cooldown_until
    mins = minutes if minutes is not None else get_settings().gemini_cooldown_minutes
    _cooldown_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=mins)
    log.warning("gemini quota reached; using groq for %d minutes", mins)


def clear_gemini_cooldown() -> None:
    global _cooldown_until
    _cooldown_until = None


def _gemini(prompt: str, model: str) -> str:
    from google import genai
    from google.genai import types

    s = get_settings()
    s.require("gemini_api_key")
    # Bound the call. The SDK retries 503 ("high demand") with backoff by
    # default, which on a busy free tier turns one request into minutes of
    # silence. We would rather fail fast and let the caller retry or, from
    # phase 5, fall through to Groq.
    client = genai.Client(
        api_key=s.gemini_api_key,
        http_options=types.HttpOptions(
            timeout=s.model_timeout_seconds * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",   # ask for JSON at the API level
            temperature=0.9,                          # three drafts should differ
            max_output_tokens=4096,
        ),
    )
    return resp.text or ""


def _groq(prompt: str, model: str) -> str:
    from groq import Groq

    s = get_settings()
    s.require("groq_api_key")
    client = Groq(api_key=s.groq_api_key, timeout=float(s.model_timeout_seconds), max_retries=0)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.9,
        max_tokens=4096,
    )
    return resp.choices[0].message.content or ""


def _try_gemini(prompt: str) -> str:
    """Gemini, stepping down to a lighter model when the first is merely busy."""
    s = get_settings()
    models = [m for m in (s.gemini_model, s.gemini_fallback_model) if m]
    if not models:
        raise RuntimeError("no gemini model configured")

    last: Exception | None = None
    for i, model in enumerate(models):
        try:
            return _gemini(prompt, model)
        except Exception as exc:
            last = exc
            if _is_quota(exc):
                start_gemini_cooldown()
                raise
            if not _is_capacity(exc) or i == len(models) - 1:
                raise
            log.info("gemini %s is busy, stepping down: %s", model, str(exc)[:120])
    raise last  # unreachable, but keeps the type checker honest


def _call_model(prompt: str) -> tuple[str, ModelUsed]:
    """Gemini first, Groq second -- plan.md section 8.

    The same prompt and the same JSON contract go to both, so a fallback draft
    reads like a Gemini one rather than announcing itself.
    """
    s = get_settings()
    skip_gemini = gemini_in_cooldown()

    if not skip_gemini:
        try:
            return _try_gemini(prompt), ModelUsed.gemini
        except Exception as exc:
            if not s.groq_api_key:
                raise
            log.info("falling back to groq: %s", str(exc)[:160])

    try:
        return _groq(prompt, s.groq_model), ModelUsed.groq
    except Exception as exc:
        if not skip_gemini:
            raise
        # The cooldown told us to skip Gemini, but Groq is down too. A stale
        # cooldown is a worse reason to fail than a possibly-recovered quota.
        log.info("groq failed during gemini cooldown, trying gemini anyway: %s", str(exc)[:120])
        clear_gemini_cooldown()
        return _try_gemini(prompt), ModelUsed.gemini


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate(
    user: User,
    topic: str,
    category: PostCategory,
    tone: Tone,
    extra_answer: str = "",
    *,
    max_attempts: int = 2,
) -> Generation:
    """Three drafts for one topic.

    A malformed reply is retried once with the parser's complaint appended, which
    is far more likely to succeed than repeating the identical prompt.
    """
    prompt = build_prompt(user, topic, category, tone, extra_answer)
    tags = hashtag_range(user, category)
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        ask = prompt if attempt == 1 else (
            f"{prompt}\n\nYour previous reply was rejected: {last_error}. "
            "Return only the JSON object described above."
        )
        try:
            text, model = _call_model(ask)
        except Exception as exc:  # network, quota, SDK error
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("generation attempt %d failed: %s", attempt, last_error[:200])
            continue

        try:
            drafts = parse_drafts(text, tag_range=tags)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            log.info("attempt %d returned unusable JSON: %s", attempt, last_error[:200])
            continue

        for note in problems(drafts, tags):
            log.info("draft quality note: %s", note)
        return Generation(drafts=drafts, model_used=model, attempts=attempt)

    raise GenerationError(last_error or "no reply from the model")
