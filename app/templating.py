"""One Jinja environment for the whole app.

Each router used to build its own Jinja2Templates, so anything added for every
page -- the admin link, say -- had to be remembered in four places. One instance
means one place.
"""

from __future__ import annotations

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

# Every form needs this; see app/csrf.py.
from app.csrf import field as _csrf_field  # noqa: E402

templates.env.globals["csrf"] = _csrf_field
