"""Stock photos from Pexels -- plan.md section 11, option 1.

The model returns a `photo_query` with each draft; this turns it into a handful
of candidates the user can pick from or ignore.

Attribution: the Pexels licence asks for credit to the photographer where
possible. A LinkedIn post body is not the place for it, so the credit travels
with the photo and is shown in the UI; phase 8 decides where it goes at publish
time. `photographer_url` is kept for that reason and is not decoration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.pexels.com/v1/search"

# Landscape only: LinkedIn crops tall images in the feed (plan section 11).
ORIENTATION = "landscape"
PER_PAGE = 6


class PhotoSearchError(Exception):
    """Pexels could not be reached or refused the request."""


@dataclass(frozen=True)
class Photo:
    id: int
    url: str                # the image itself, sized for a post
    preview_url: str        # smaller, for the picker
    photographer: str
    photographer_url: str
    source_url: str         # the Pexels page, for attribution
    width: int
    height: int
    alt: str = ""

    @property
    def credit(self) -> str:
        return f"Photo by {self.photographer} on Pexels"


def search(query: str, *, limit: int = PER_PAGE) -> list[Photo]:
    """Find candidate photos. Returns an empty list rather than raising when a
    query simply has no matches -- a post without an image is still a post."""
    s = get_settings()
    s.require("pexels_api_key")

    cleaned = " ".join(query.split())[:100]
    if not cleaned:
        return []

    try:
        resp = httpx.get(
            SEARCH_URL,
            params={"query": cleaned, "per_page": limit, "orientation": ORIENTATION},
            headers={"Authorization": s.pexels_api_key},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise PhotoSearchError(f"Could not reach Pexels: {exc}") from exc

    if resp.status_code == 401:
        raise PhotoSearchError("The Pexels API key was rejected.")
    if resp.status_code == 429:
        raise PhotoSearchError("Pexels rate limit reached. Try again shortly.")
    if resp.status_code != 200:
        raise PhotoSearchError(f"Pexels returned {resp.status_code}.")

    return [_photo(p) for p in resp.json().get("photos", [])]


def _photo(raw: dict) -> Photo:
    src = raw.get("src") or {}
    return Photo(
        id=int(raw.get("id", 0)),
        # "large" is ~1880px wide, which matches our upload ceiling.
        url=src.get("large") or src.get("original") or "",
        preview_url=src.get("medium") or src.get("small") or "",
        photographer=raw.get("photographer") or "Unknown",
        photographer_url=raw.get("photographer_url") or "",
        source_url=raw.get("url") or "",
        width=int(raw.get("width") or 0),
        height=int(raw.get("height") or 0),
        alt=(raw.get("alt") or "").strip(),
    )
