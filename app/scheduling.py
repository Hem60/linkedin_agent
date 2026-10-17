"""Turning "Tuesday 9:30 my time" into a UTC instant -- plan.md F19, section 18.

Rule from the plan: store UTC, convert only for display. Everything in the
database is `timestamptz`; the user's timezone is consulted exactly twice --
when reading the time they typed, and when showing a time back to them.

The two traps, both of which only bite users in zones with daylight saving:

* A time that does not exist. On the spring-forward night in New York, the
  clock goes 01:59 -> 03:00, so "02:30" never happens. zoneinfo will not
  refuse it; it quietly produces an instant that reads back as 03:30. We
  detect that by round-tripping and ask the user to pick again, rather than
  publishing an hour off from what they chose.
* A time that happens twice. On the fall-back night, 01:30 occurs in both EDT
  and EST. We take the first (fold=0), which is what a person means by "1:30am"
  on a night they were awake for it.

Asia/Kolkata has no daylight saving, so these never affect this account; they
matter the moment someone in London or New York signs up.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import User

UTC = dt.UTC

# Enough that a scheduled post is not already due by the time the worker's
# next tick comes round, which would make "schedule" a slower "publish now".
MIN_LEAD = dt.timedelta(minutes=2)

INPUT_FORMAT = "%Y-%m-%dT%H:%M"      # what <input type="datetime-local"> sends


class ScheduleError(ValueError):
    """The requested time can't be used, with a message for the user."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def parse_local(value: str, tz_name: str) -> dt.datetime:
    """Read a wall-clock time in the user's zone and return it as UTC."""
    try:
        # Naive on purpose, and the noqa below says so. The user types a
        # time meaning it in their own zone; it is given that zone three
        # lines down and converted. A %z here would demand an offset the
        # form never collects.
        naive = dt.datetime.strptime(value.strip(), INPUT_FORMAT)  # noqa: DTZ007
    except ValueError as exc:
        raise ScheduleError("Pick a date and time.") from exc

    tz = zone(tz_name)
    local = naive.replace(tzinfo=tz, fold=0)
    utc = local.astimezone(UTC)

    # Round trip: a time that does not exist in this zone comes back different.
    back = utc.astimezone(tz).replace(tzinfo=None)
    if back != naive:
        raise ScheduleError(
            f"{naive:%H:%M} doesn't exist on {naive:%d %b} in {tz.key} — the clocks "
            f"change that night. Pick a time outside {naive:%H}:00–{back:%H}:00."
        )
    return utc


def validate(when: dt.datetime, user: User, *, now: dt.datetime | None = None) -> None:
    """Refuse times that would fail or surprise the user later, not now."""
    now = now or now_utc()

    if when < now + MIN_LEAD:
        raise ScheduleError("Pick a time at least a couple of minutes from now.")

    # LinkedIn gives standard apps 60 days and no refresh token (plan section 2).
    # A post scheduled after the token dies would fail at publish time, silently
    # from the user's point of view, days after they stopped thinking about it.
    if user.token_expires_at and when >= user.token_expires_at:
        expires = to_local(user.token_expires_at, user.timezone)
        raise ScheduleError(
            f"Your LinkedIn connection expires on {expires:%d %b} at {expires:%H:%M}. "
            "Reconnect first, or pick an earlier time."
        )


def to_local(when: dt.datetime, tz_name: str) -> dt.datetime:
    return when.astimezone(zone(tz_name))


def display(when: dt.datetime | None, tz_name: str) -> str:
    """'Tue 23 Sep, 09:30 IST' -- the only form a user ever sees."""
    if when is None:
        return ""
    local = to_local(when, tz_name)
    return f"{local:%a %d %b, %H:%M} {local.tzname() or tz_name}"


def input_value(when: dt.datetime | None, tz_name: str) -> str:
    """Prefill for a datetime-local input, in the user's own zone."""
    if when is None:
        return ""
    return to_local(when, tz_name).strftime(INPUT_FORMAT)


def suggest(tz_name: str, *, now: dt.datetime | None = None) -> str:
    """A sensible default: tomorrow at 09:00 local, when feeds are busiest."""
    local_now = to_local(now or now_utc(), tz_name)
    tomorrow = (local_now + dt.timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return tomorrow.strftime(INPUT_FORMAT)
