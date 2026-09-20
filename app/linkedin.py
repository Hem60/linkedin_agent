"""The LinkedIn write API -- plan.md sections 2 and 15.

Everything here was confirmed against the live API in phase 0 and again when
the sanitizer was verified: the endpoints, the headers, the two-step image
upload, and the fact that `commentary` must arrive `little`-escaped or the post
is silently truncated behind a 201.

Scope is deliberately small. This module knows how to talk to LinkedIn and
nothing about when to. Deciding *whether* to publish -- the claim lock, dry
run, retries -- belongs to publisher.py.

Not here, and why:
* comments -- w_member_social returns 403 on socialActions (plan section 2),
  so F21 is v2 and links go in the post body.
* reading a post back -- r_member_social is a closed permission, so there is
  no way to verify a published post through the API.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.linkedin.com"
TIMEOUT = 60.0


class LinkedInError(Exception):
    """A LinkedIn API call failed.

    `retryable` separates "try again later" (their capacity, our network) from
    "this will fail identically forever" (a revoked token, a malformed post).
    The worker uses it to decide between a backoff and a permanent failure.
    """

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class PublishedPost:
    urn: str

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/feed/update/{self.urn}/"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": get_settings().linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _raise_for(resp: httpx.Response, action: str) -> None:
    """Turn an API failure into something the worker can act on."""
    body = resp.text[:300]
    status = resp.status_code

    if status == 401:
        raise LinkedInError(
            "Your LinkedIn connection has expired. Reconnect to publish again.",
            status=status, retryable=False,
        )
    if status == 403:
        raise LinkedInError(
            "LinkedIn refused this post. The app may be missing a permission.",
            status=status, retryable=False,
        )
    if status == 422:
        raise LinkedInError(
            f"LinkedIn rejected the post content ({body}).",
            status=status, retryable=False,
        )
    if status == 429 or status >= 500:
        raise LinkedInError(
            f"LinkedIn is unavailable right now ({status}).",
            status=status, retryable=True,
        )
    raise LinkedInError(f"{action} failed ({status}): {body}", status=status)


# ---------------------------------------------------------------------------
# Images: initialize, then PUT the bytes
# ---------------------------------------------------------------------------

def upload_image(token: str, author_urn: str, data: bytes, content_type: str) -> str:
    """Upload one image and return its `urn:li:image:...`.

    Two steps by design: LinkedIn hands back a one-time upload URL, and the
    bytes go straight there. Our storage bucket is never involved -- LinkedIn
    does not fetch from us.
    """
    try:
        init = httpx.post(
            f"{API}/rest/images?action=initializeUpload",
            headers=_headers(token),
            json={"initializeUploadRequest": {"owner": author_urn}},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInError(f"Could not reach LinkedIn: {exc}", retryable=True) from exc

    if init.status_code not in (200, 201):
        _raise_for(init, "image upload initialisation")

    value = init.json().get("value") or {}
    upload_url, image_urn = value.get("uploadUrl"), value.get("image")
    if not upload_url or not image_urn:
        raise LinkedInError("LinkedIn did not return an image upload URL.", retryable=True)

    try:
        put = httpx.put(
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            content=data,
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise LinkedInError(f"Image upload failed: {exc}", retryable=True) from exc

    if put.status_code not in (200, 201):
        _raise_for(put, "image upload")

    return image_urn


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def create_post(
    token: str,
    author_urn: str,
    commentary: str,
    *,
    image_urn: str | None = None,
    image_title: str = "",
) -> PublishedPost:
    """Publish one post.

    `commentary` must already be `little`-escaped by sanitize.py. Sending raw
    text returns 201 and publishes a post truncated at the first reserved
    character, with nothing in the response to detect it by.
    """
    payload: dict = {
        "author": author_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        payload["content"] = {"media": {"id": image_urn, "title": image_title[:200]}}

    try:
        resp = httpx.post(
            f"{API}/rest/posts", headers=_headers(token), json=payload, timeout=TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise LinkedInError(f"Could not reach LinkedIn: {exc}", retryable=True) from exc

    if resp.status_code != 201:
        _raise_for(resp, "publish")

    urn = resp.headers.get("x-restli-id", "")
    if not urn:
        # A post exists but we cannot record which one. Retrying would publish
        # it a second time, so this fails permanently and asks for a human.
        raise LinkedInError(
            "LinkedIn accepted the post but returned no id. Check your profile "
            "before retrying -- it may have published.",
            status=201, retryable=False,
        )
    return PublishedPost(urn=urn)


def delete_post(token: str, urn: str) -> bool:
    """Remove a published post. True when it is gone, including if it already was."""
    encoded = urllib.parse.quote(urn, safe="")
    resp = httpx.request(
        "DELETE",
        f"{API}/rest/posts/{encoded}",
        headers=_headers(token) | {"X-RestLi-Method": "DELETE"},
        timeout=TIMEOUT,
    )
    if resp.status_code in (200, 204, 404):
        return True
    log.warning("delete post %s failed %s: %s", urn, resp.status_code, resp.text[:200])
    return False
