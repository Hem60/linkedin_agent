"""Generated post images -- Cloudflare Workers AI, Leonardo lucid-origin.

Sits beside photos.py rather than replacing it. A search finds someone else's
photograph of roughly the right subject; generation makes one of this post.
Neither wins outright, so the editor offers both and the free allowance decides
how often the second is available.

On the model. flux-1-schnell was tried first and failed twice over: it rejects
width and height outright, and it cannot render writing -- asked for a screen
it produced a confident headline that spelled nothing, plus an invented logo.
Telling it not to made things worse, because it has no negation and Workers AI
exposes no negative_prompt, so "no text" simply fed it the word text.
lucid-origin takes a size, follows the prompt, and renders incidental text the
way a camera does: softened, angled, out of focus. Verified against the real
photo_query values this app had already produced.

Cost is the live constraint. lucid-origin is 636 Neurons per 512x512 tile
against a free 10,000 a day, so a 1024x576 image is about 1,431 -- roughly
seven a day. Square would be four. That is why the size is what it is.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4/accounts"

# Long enough for a real description, short enough that the subject is not lost
# behind the styling that follows it.
MAX_SUBJECT_CHARS = 180

# Phrased only as what to photograph. Nothing here says "no" to anything: a
# diffusion model without a negative_prompt reads a forbidden noun as a
# requested one, which is how the first attempt produced the very text it was
# told to avoid.
STYLE = (
    "professional editorial photograph, clean composition, "
    "soft natural light, muted palette, shallow depth of field"
)


class ImageGenError(Exception):
    """Cloudflare could not be reached, or refused the request."""


def compose_prompt(subject: str) -> str:
    """Put the subject first, then the styling.

    Order matters: with the style leading, the model renders the style and
    loses the subject.
    """
    cleaned = " ".join(subject.split())[:MAX_SUBJECT_CHARS]
    return f"{cleaned}. {STYLE}" if cleaned else ""


def generate(subject: str) -> bytes:
    """Generate one landscape image for a post, returning the raw bytes.

    Raises ImageGenError with a message meant for the user. An empty subject
    raises rather than returning nothing: the caller asked for an image, and
    quietly handing back none looks like a bug at the other end.
    """
    s = get_settings()
    s.require("cloudflare_account_id", "cloudflare_api_token")

    prompt = compose_prompt(subject)
    if not prompt:
        raise ImageGenError("Describe the image you want first.")

    url = f"{API_ROOT}/{s.cloudflare_account_id}/ai/run/{s.image_model}"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {s.cloudflare_api_token}"},
            json={
                "prompt": prompt,
                "width": s.image_width,
                "height": s.image_height,
                "steps": s.image_steps,
                "guidance": s.image_guidance,
            },
            # Measured at about four seconds; the ceiling is for a bad day, not
            # a typical one.
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise ImageGenError(f"Could not reach the image service: {exc}") from exc

    if resp.status_code in (401, 403):
        raise ImageGenError("The Cloudflare API token was rejected.")
    if resp.status_code == 429:
        raise ImageGenError(
            "Today's image allowance is used up. Search for a photo instead, "
            "or generate again tomorrow."
        )
    if resp.status_code != 200:
        raise ImageGenError(f"The image service returned {resp.status_code}.")

    return _decode(resp)


def _decode(resp: httpx.Response) -> bytes:
    """Pull the image out of Workers AI's JSON envelope.

    A refused prompt comes back as 200 with success false, so the status line
    alone does not mean there is an image to use.
    """
    try:
        body = resp.json()
    except ValueError as exc:
        raise ImageGenError("The image service sent a reply we could not read.") from exc

    if not body.get("success", False):
        detail = "; ".join(str(e.get("message", e)) for e in (body.get("errors") or []))
        raise ImageGenError(detail or "The image service refused the request.")

    encoded = (body.get("result") or {}).get("image")
    if not encoded:
        raise ImageGenError("The image service returned no image.")

    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError("The generated image was not readable.") from exc
