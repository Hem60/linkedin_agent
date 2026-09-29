"""Onboarding -- the second half of signing up (plan.md phase 8 / F2, F3).

Collected once, then used in every prompt so posts read like the user rather
than like a generic assistant. Personal accounts give role, niche, tone and
timezone; business accounts additionally give company, products, markets and
buyer types.

Kept separate from auth.py, which is already carrying the OAuth flow.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_user
from app.database import get_db
from app.templating import templates
from app.models import AccountType, Tone, User

router = APIRouter(tags=["onboarding"])

# Offered in the picker. Any IANA zone is accepted; these are just the common ones.
SUGGESTED_TIMEZONES = [
    "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo",
    "Europe/London", "Europe/Berlin", "Europe/Amsterdam",
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Australia/Sydney", "UTC",
]


def _valid_timezone(name: str) -> str:
    """Reject unknown zones rather than storing something zoneinfo cannot load
    later -- a bad timezone would break scheduling at publish time, far from here."""
    try:
        zoneinfo.ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


@router.get("/onboarding")
def show(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "user": user,
            "timezones": SUGGESTED_TIMEZONES,
            "tones": [t.value for t in Tone],
        },
    )


@router.post("/onboarding")
def save(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    account_type: str = Form("personal"),
    role_title: str = Form(""),
    niche: str = Form(""),
    default_tone: str = Form("professional"),
    timezone: str = Form("UTC"),
    company_name: str = Form(""),
    products: str = Form(""),
    target_markets: str = Form(""),
    buyer_types: str = Form(""),
):
    user.account_type = (
        AccountType.business if account_type == "business" else AccountType.personal
    )
    user.role_title = role_title.strip()[:200]
    user.niche = niche.strip()[:300]
    user.default_tone = Tone(default_tone) if default_tone in Tone._value2member_map_ else Tone.professional
    user.timezone = _valid_timezone(timezone.strip())

    if user.account_type is AccountType.business:
        user.company_name = company_name.strip()[:200]
        user.products = products.strip()
        user.target_markets = target_markets.strip()
        user.buyer_types = buyer_types.strip()
    else:
        # Switching back to personal clears business context rather than leaving
        # stale company details to leak into a future business post.
        user.company_name = ""
        user.products = ""
        user.target_markets = ""
        user.buyer_types = ""

    user.onboarded_at = dt.datetime.now(dt.timezone.utc)
    db.commit()

    return RedirectResponse("/", status_code=303)
