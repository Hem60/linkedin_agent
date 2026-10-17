"""The publishing worker -- plan.md phase 10 and section 14.

Once a minute: recover anything stuck, then claim and publish what is due.

Two ways to run it, and both are safe at the same time:

    inside the web app     RUN_SCHEDULER=true (the default). Free tier: one
                           process, kept awake by an external pinger.
    as its own process     python -m worker.scheduler

Running several is fine. Each tick claims rows with SELECT ... FOR UPDATE SKIP
LOCKED (publisher.claim_due), so two workers split the due posts between them
instead of both publishing the same one -- the guarantee proven by
test_two_workers_racing_publish_exactly_once.
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import notify, publisher
from app.config import get_settings
from app.database import SessionLocal
from app.models import User

log = logging.getLogger("worker")

TICK_SECONDS = 60
BATCH = 10

# A publish is a handful of HTTP calls; a minute is generous. Fifteen means a
# row still in `publishing` after that was interrupted -- a crash, a deploy, a
# killed process -- not merely slow.
STUCK_AFTER = dt.timedelta(minutes=15)

STUCK_MESSAGE = (
    "Publishing was interrupted partway through. It may or may not have reached "
    "LinkedIn -- check your profile before you retry, or it could post twice."
)


@dataclass
class TickResult:
    recovered: int = 0
    warned: int = 0
    claimed: int = 0
    published: int = 0
    failed: int = 0


def recover_stuck(db: Session, *, older_than: dt.timedelta = STUCK_AFTER) -> int:
    """Surface posts abandoned in `publishing`.

    These are marked failed, never retried automatically. The crash could have
    happened after LinkedIn accepted the post but before we recorded its id,
    and there is no way to ask LinkedIn -- reading a member's own posts needs
    r_member_social, a closed permission (plan section 2). Retrying blind risks
    a duplicate under the user's real name; a human can check in five seconds.
    """
    rows = db.execute(
        text("""
            UPDATE posts
            SET status = 'failed', error_message = :msg
            WHERE status = 'publishing'
              AND (claimed_at IS NULL OR claimed_at < now() - :age)
            RETURNING id
        """),
        {"msg": STUCK_MESSAGE, "age": older_than},
    ).all()
    db.commit()
    if rows:
        log.warning("recovered %d stuck post(s): %s", len(rows), [r[0] for r in rows])
    return len(rows)


WARN_WITHIN = dt.timedelta(days=7)
# Re-warn at most weekly. LinkedIn's window is 60 days, so a second nudge a
# week later is a reminder; a daily one is noise people filter out.
REWARN_AFTER = dt.timedelta(days=7)


def warn_expiring_tokens(db: Session, *, now: dt.datetime | None = None) -> int:
    """F5. Email users whose LinkedIn connection is about to lapse.

    Runs on the same tick as everything else but is cheap: the query matches
    nothing on all but a handful of days per user per two months.
    """
    now = now or dt.datetime.now(dt.UTC)
    users = db.execute(
        text("""
            SELECT id FROM users
            WHERE deleted_at IS NULL
              AND access_token IS NOT NULL
              AND token_expires_at IS NOT NULL
              AND token_expires_at < :deadline
              AND (expiry_notified_at IS NULL OR expiry_notified_at < :rewarn)
        """),
        {"deadline": now + WARN_WITHIN, "rewarn": now - REWARN_AFTER},
    ).scalars().all()

    sent = 0
    for user_id in users:
        user = db.get(User, user_id)
        days = (user.token_expires_at - now).days
        if notify.token_expiring(user, days, get_settings().app_url):
            sent += 1
        # Stamped either way: if email is switched off or Resend is refusing,
        # retrying every minute forever helps nobody.
        user.expiry_notified_at = now
        db.commit()

    if sent:
        log.info("warned %d user(s) about expiring LinkedIn tokens", sent)
    return sent


def run_once(db: Session | None = None, *, limit: int = BATCH) -> TickResult:
    """One tick. Never raises: a worker that dies on one bad post stops
    publishing everyone else's."""
    own = db is None
    db = db or SessionLocal()
    result = TickResult()
    try:
        result.recovered = recover_stuck(db)
        result.warned = warn_expiring_tokens(db)
        claimed = publisher.claim_due(db, limit=limit)
        result.claimed = len(claimed)
        for post in claimed:
            try:
                publisher.publish(db, post)
                result.published += 1
            except publisher.PublishError as exc:
                # The failure is already recorded on the row, with a retry
                # scheduled if it was retryable.
                result.failed += 1
                log.warning("post %s failed: %s", post.id, str(exc)[:160])
    except Exception:
        log.exception("scheduler tick failed")
        db.rollback()
    finally:
        if own:
            db.close()

    if result.claimed or result.recovered or result.warned:
        log.info("tick: %s", result)
    return result


# ---------------------------------------------------------------------------
# Running inside the web app
# ---------------------------------------------------------------------------

class _Loop:
    """A daemon thread that ticks until told to stop.

    A plain thread rather than APScheduler: there is exactly one job, and
    SKIP LOCKED already provides the concurrency guarantee a job store would.
    """

    def __init__(self, interval: float = TICK_SECONDS):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler started, every %ss", self.interval)

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        log.info("scheduler stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            run_once()
            self._stop.wait(self.interval)


loop = _Loop()


# ---------------------------------------------------------------------------
# Running on its own
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s")
    stopping = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())

    log.info("worker running, every %ss (Ctrl+C to stop)", TICK_SECONDS)
    while not stopping.is_set():
        run_once()
        stopping.wait(TICK_SECONDS)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
