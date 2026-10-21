"""Generated post images -- Cloudflare Workers AI, Leonardo lucid-origin.

Sits beside photos.py rather than replacing it. A search finds someone else's
photograph of roughly the right subject; generation makes one of this post.
Neither wins outright, so the editor offers both and the free allowance decides
how often the second is available.

On the model. flux-1-schnell was tried first and failed twice over: it rejects
width and height outright, and it cannot render writing -- asked for a screen
it produced a confident headline that spelled nothing, beside an invented logo.
Telling it not to made matters worse, since it has no negation and Workers AI
exposes no negative_prompt, so "no text" simply fed it the word text.
lucid-origin takes a size, follows the prompt, and renders incidental text the
way a camera does: angled, soft, out of focus.

On whose words win. There are two ways a subject arrives here, and they want
opposite treatment. The draft's own photo_query is a bare noun phrase the
writing model produced -- "server rack data center" -- and it needs styling to
become a photograph. A prompt somebody typed is already a description of what
they want, and adding to it overrides them: "a perfect photo for the peanut"
plus this module's old portrait styling returned a studio portrait of a dog,
Peanut being a common dog name and the styling having asked for exactly that
kind of picture. So a typed prompt is sent as written. See `compose_prompt`.

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
# behind anything appended to it.
MAX_SUBJECT_CHARS = 400

# Applied only to the draft's own photo_query, which arrives as a bare noun
# phrase and would otherwise come back looking like clip art. Phrased only as
# what to photograph: a diffusion model with no negative_prompt reads a
# forbidden noun as a requested one, which is how an earlier version produced
# the very text it was told to avoid.
STYLE = (
    "professional editorial photograph, clean composition, "
    "soft natural light, muted palette"
)


class ImageGenError(Exception):
    """Cloudflare could not be reached, or refused the request."""


def compose_prompt(subject: str, *, styled: bool = True) -> str:
    """Build the prompt actually sent to the model.

    `styled` is the difference between the two callers. The draft's photo_query
    gets the house style appended; a prompt someone typed does not, because
    they have already said what they want and anything added here argues with
    it.
    """
    cleaned = " ".join(subject.split())[:MAX_SUBJECT_CHARS]
    if not cleaned:
        return ""
    return f"{cleaned}. {STYLE}" if styled else cleaned


def generate(subject: str, *, styled: bool = True) -> bytes:
    """Generate one landscape image for a post, returning the raw bytes.

    Raises ImageGenError with a message meant for the user. An empty subject
    raises rather than returning nothing: the caller asked for an image, and
    quietly handing back none looks like a bug at the other end.
    """
    s = get_settings()
    s.require("cloudflare_account_id", "cloudflare_api_token")

    prompt = compose_prompt(subject, styled=styled)
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
            # Measured at about four seconds; the ceiling is for a bad day.
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
