"""Email -- plan.md F24 (failure alerts) and F5 (token expiry).

Both messages exist because the alternative is silence. A scheduled post that
fails at 09:00 is invisible until the user happens to open the app; a LinkedIn
connection that expires stops every future post with no announcement.

Rule for everything here: **sending email must never break the thing that
triggered it.** A publish that succeeded and then failed to send a receipt is a
successful publish. Every entry point catches its own errors and logs.

Without RESEND_API_KEY the module is a no-op that says so once. That keeps the
app fully usable before the key exists, rather than failing at the worst moment.

Note on Resend's free tier: until a domain is verified, mail can only be sent
from onboarding@resend.dev and only to the address the Resend account was
created with. Delivery to anyone else returns 403, which is logged like any
other failure rather than raised.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.models import Post, User

log = logging.getLogger(__name__)

SEND_URL = "https://api.resend.com/emails"
_warned = False


def enabled() -> bool:
    global _warned
    settings = get_settings()
    if settings.resend_api_key and settings.email_from:
        return True
    if not _warned:
        log.info("email disabled (RESEND_API_KEY / EMAIL_FROM not set)")
        _warned = True
    return False


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. Returns whether it went out; never raises."""
    if not enabled() or not to:
        return False

    settings = get_settings()
    try:
        resp = httpx.post(
            SEND_URL,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={
                "from": settings.email_from,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        log.warning("email to %s failed: %s", _mask(to), exc)
        return False

    if resp.status_code in (200, 201):
        return True
    # 403 here is usually Resend's unverified-domain restriction.
    log.warning("email to %s rejected %s: %s", _mask(to), resp.status_code, resp.text[:200])
    return False


def _mask(address: str) -> str:
    """Logs should not become a list of everyone's email address."""
    name, _, domain = address.partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"


# ---------------------------------------------------------------------------
# The two messages
# ---------------------------------------------------------------------------

def post_failed(user: User, post: Post, app_url: str) -> bool:
    """F24. Sent when a post gives up, not on each retry -- three emails for one
    failure is how people learn to ignore email."""
    reason = (post.error_message or "No reason recorded.").strip()
    return send(
        user.email,
        "Your scheduled LinkedIn post didn't go out",
        f"""Hi {user.name.split(' ')[0] if user.name else 'there'},

This post didn't publish:

  "{post.topic}"

Reason: {reason}

Nothing was posted to LinkedIn. The draft is still here, and you can fix and
retry it whenever you like:

  {app_url.rstrip('/')}/posts/{post.id}

-- PostPilot
""",
    )


def token_expiring(user: User, days_left: int, app_url: str) -> bool:
    """F5. LinkedIn gives standard apps 60 days and no way to renew
    automatically, so reconnecting is a normal part of using this."""
    when = "today" if days_left <= 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    return send(
        user.email,
        f"Reconnect LinkedIn — your connection expires {when}",
        f"""Hi {user.name.split(' ')[0] if user.name else 'there'},

Your LinkedIn connection expires {when}. LinkedIn caps this at 60 days and
offers no way to renew it automatically, so this comes round periodically.

Until you reconnect, scheduled posts won't publish.

Reconnecting takes one click:

  {app_url.rstrip('/')}/auth/login

-- PostPilot
""",
    )
