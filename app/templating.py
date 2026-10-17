"""One Jinja environment for the whole app.

Each router used to build its own Jinja2Templates, so anything added for every
page -- the admin link, say -- had to be remembered in four places. One instance
means one place.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import User

templates = Jinja2Templates(directory="app/templates")


def is_admin(user: User | None) -> bool:
    """Whether to show the usage link. Access itself is checked in admin.py --
    hiding a link is presentation, not security."""
    if user is None:
        return False
    admins = get_settings().admin_email_list
    return bool(admins) and (user.email or "").lower() in admins


templates.env.globals["is_admin"] = is_admin


def initials(name: str) -> str:
    """One or two letters for the header avatar.

    Falls back to a dot rather than an empty circle: LinkedIn does not promise
    a name, and a blank avatar looks like a loading state that never resolves.
    """
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "·"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


templates.env.globals["initials"] = initials


_CSS = Path("app/static/app.css")
_asset_cache: tuple[float, str] | None = None


def asset_version() -> str:
    """A short hash of the stylesheet, appended to its URL as ?v=.

    Without it a returning browser keeps the stylesheet it already has, and a
    deploy that changes only CSS reaches nobody until their cache happens to
    expire.

    Keyed on mtime rather than computed once at import. An import-time hash is
    correct in production -- a deploy is a restart -- but goes stale the moment
    anyone edits the CSS under a running dev server, which is exactly when you
    are looking at the page to see whether the edit worked. The stat costs
    microseconds and the hash is only recomputed when the file actually moves.
    """
    global _asset_cache
    try:
        mtime = _CSS.stat().st_mtime
    except OSError:          # missing file is the server's problem, not the page's
        return "0"
    if _asset_cache is None or _asset_cache[0] != mtime:
        _asset_cache = (mtime, hashlib.sha256(_CSS.read_bytes()).hexdigest()[:10])
    return _asset_cache[1]


templates.env.globals["asset_v"] = asset_version

# Every form needs this; see app/csrf.py.
from app.csrf import field as _csrf_field

templates.env.globals["csrf"] = _csrf_field
