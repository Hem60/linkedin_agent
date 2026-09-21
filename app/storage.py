"""Image storage -- plan.md section 4 (Supabase Storage).

Why this exists at all: LinkedIn never fetches images from us. Publishing
uploads the bytes directly to LinkedIn's own upload URL (phase 8). This store
is for our side -- previews, the drafts list, post history -- so an image the
user picked is still there when they come back tomorrow.

Paths are namespaced per user (`users/<id>/...`), matching the rule in plan
section 13 that nothing is shared between accounts.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Storing or removing an object failed."""


@dataclass(frozen=True)
class StoredImage:
    path: str
    url: str
    content_type: str
    bytes: int


def _base() -> tuple[str, str, str]:
    s = get_settings()
    s.require("storage_url", "storage_key")
    return s.storage_url.rstrip("/"), s.storage_key, s.storage_bucket


def _auth_headers(key: str) -> dict[str, str]:
    """Supabase accepts two key formats and wants both headers.

    Legacy keys are signed JWTs ("eyJ..."); newer projects issue opaque secret
    keys ("sb_secret_..."). Either goes in both `Authorization` and `apikey` --
    the gateway checks one and the storage service the other, and which one
    depends on the project's key settings.
    """
    return {"Authorization": f"Bearer {key}", "apikey": key}


def object_path(user_id: int, extension: str, prefix: str = "posts") -> str:
    """A unguessable per-user path.

    The random component matters: bucket objects are served publicly so the
    browser can show a preview, and sequential names would let anyone walk
    another user's images.
    """
    return f"users/{user_id}/{prefix}/{secrets.token_urlsafe(16)}.{extension}"


def path_from_url(url: str) -> str | None:
    """Recover the object path from a public URL, so an image can be deleted
    knowing only what the post_images row stores."""
    _, _, bucket = _base()
    marker = f"/storage/v1/object/public/{bucket}/"
    return url.split(marker, 1)[1] if marker in url else None


def put(data: bytes, path: str, content_type: str) -> StoredImage:
    base, key, bucket = _base()
    resp = httpx.post(
        f"{base}/storage/v1/object/{bucket}/{path}",
        headers=_auth_headers(key) | {
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        content=data,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        log.error("storage upload failed %s: %s", resp.status_code, resp.text[:200])
        raise StorageError(f"Could not save the image ({resp.status_code}).")

    return StoredImage(
        path=path,
        url=f"{base}/storage/v1/object/public/{bucket}/{path}",
        content_type=content_type,
        bytes=len(data),
    )


def _already_gone(resp: httpx.Response) -> bool:
    """Supabase reports a missing object as HTTP 400 with an inner 404.

    Checking the status code alone is not enough: an already-deleted object
    would look like a failure, and account deletion (F4) must be safe to retry.
    """
    if resp.status_code == 404:
        return True
    if resp.status_code != 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return str(body.get("statusCode")) == "404" or body.get("error") == "not_found"


def delete(path: str) -> None:
    """Remove one object. Missing objects are not an error, so deletion is
    idempotent and account deletion (F4) can be retried safely."""
    base, key, bucket = _base()
    resp = httpx.request(
        "DELETE",
        f"{base}/storage/v1/object/{bucket}/{path}",
        headers=_auth_headers(key),
        timeout=30,
    )
    if resp.status_code in (200, 204) or _already_gone(resp):
        return
    log.warning("storage delete failed %s: %s", resp.status_code, resp.text[:200])


def fetch(url: str, *, max_bytes: int = 10 * 1024 * 1024) -> bytes:
    """Download an image we are about to store or publish.

    Capped: a redirect to something enormous should not be pulled into memory.
    """
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise StorageError(f"Could not download the image ({resp.status_code}).")
        chunks, total = [], 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise StorageError("That image is too large to download.")
            chunks.append(chunk)
    return b"".join(chunks)
