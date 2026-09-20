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


def object_path(user_id: int, extension: str, prefix: str = "posts") -> str:
    """A unguessable per-user path.

    The random component matters: bucket objects are served publicly so the
    browser can show a preview, and sequential names would let anyone walk
    another user's images.
    """
    return f"users/{user_id}/{prefix}/{secrets.token_urlsafe(16)}.{extension}"


def put(data: bytes, path: str, content_type: str) -> StoredImage:
    base, key, bucket = _base()
    resp = httpx.post(
        f"{base}/storage/v1/object/{bucket}/{path}",
        headers={
            "Authorization": f"Bearer {key}",
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


def delete(path: str) -> None:
    """Remove one object. Missing objects are not an error -- deletion is
    idempotent so account deletion (F4) can be retried safely."""
    base, key, bucket = _base()
    resp = httpx.request(
        "DELETE",
        f"{base}/storage/v1/object/{bucket}/{path}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    if resp.status_code not in (200, 204, 404):
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
