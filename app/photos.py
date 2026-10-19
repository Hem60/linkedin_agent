"""Generated post images -- Cloudflare Workers AI, FLUX.1 Schnell.

This replaced a Pexels stock search. A search returns someone else's photo of
roughly the right subject; generation returns an image of *this* post, which is
the thing that was actually wanted.

The model cannot write. Asked for a terminal or an editor it produces
convincing-looking letterforms that spell nothing, and a LinkedIn reader
notices immediately -- it reads as carelessness and undercuts a post written
carefully. Telling it not to makes matters worse, so the styling names only
shapes, light and material and never mentions writing at all. See `_STYLE`.

There is no photographer to credit, so `PostImage.source` records that the
image was generated rather than licensed, and nothing is attributed.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

API_ROOT = "https://api.cloudflare.com/client/v4/accounts"

# Workers AI's flux-1-schnell takes `prompt` and `steps` and nothing else:
# sending width/height is a 400, "Additional or unevaluated properties". So
# the size is the model's own 1024 square, not a choice made here. Square is
# fine in the LinkedIn feed -- it is tall images that get cropped (plan
# section 11) -- and anything else would mean cropping after the fact.
SIZE = 1024

# Long enough for a real description, short enough that the model keeps the
# whole thing in view rather than losing the subject behind the styling.
MAX_SUBJECT_CHARS = 180

# Phrased entirely as what to draw, never as what to avoid. Schnell has no
# negation and Workers AI exposes no negative_prompt, so "no text, no letters"
# simply feeds it the words text and letters: tested, it produced a gibberish
# headline and a made-up logo in the same image that asked for neither. Naming
# only shapes, light and material gets a clean picture, because writing was
# never raised.
_STYLE = (
    "abstract geometric composition, overlapping translucent shapes, "
    "flat muted palette, generous empty space, soft even light, "
    "minimal poster art"
)

class ImageGenError(Exception):
    """Cloudflare could not be reached, or refused the request."""


def _compose_prompt(subject: str) -> str:
    """Wrap the draft's subject in styling that keeps writing out of the image.

    The model gets the subject first so it stays the subject: put the style
    rules first and Schnell tends to render the styling and forget what the
    picture was meant to be of.
    """
    cleaned = " ".join(subject.split())[:MAX_SUBJECT_CHARS]
    if not cleaned:
        return ""
    return f"{cleaned}. {_STYLE}"


def generate(subject: str) -> bytes:
    """Generate one landscape image for a post. Returns JPEG bytes.

    Raises ImageGenError with a message meant for the user. An empty subject
    raises rather than returning nothing: the caller asked for an image, and
    silently handing back none would look like a bug at the other end.
    """
    s = get_settings()
    s.require("cloudflare_account_id", "cloudflare_api_token")

    prompt = _compose_prompt(subject)
    if not prompt:
        raise ImageGenError("Describe the image you want first.")

    url = f"{API_ROOT}/{s.cloudflare_account_id}/ai/run/{s.image_model}"
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {s.cloudflare_api_token}"},
            json={"prompt": prompt, "steps": s.image_steps},
            # Generation is slower than a search; the old 20s was tuned for one.
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise ImageGenError(f"Could not reach the image service: {exc}") from exc

    if resp.status_code in (401, 403):
        raise ImageGenError("The Cloudflare API token was rejected.")
    if resp.status_code == 429:
        raise ImageGenError("Daily image allowance reached. Try again tomorrow.")
    if resp.status_code != 200:
        raise ImageGenError(f"The image service returned {resp.status_code}.")

    return _decode(resp)


def _decode(resp: httpx.Response) -> bytes:
    """Pull the image out of Workers AI's JSON envelope.

    It answers 200 with {"success": false, "errors": [...]} for a refused
    prompt, so the status line alone does not mean there is an image.
    """
    try:
        body = resp.json()
    except ValueError as exc:
        raise ImageGenError("The image service sent a reply we could not read.") from exc

    if not body.get("success", False):
        detail = "; ".join(
            str(e.get("message", e)) for e in (body.get("errors") or [])
        )
        raise ImageGenError(detail or "The image service refused the request.")

    encoded = (body.get("result") or {}).get("image")
    if not encoded:
        raise ImageGenError("The image service returned no image.")

    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError("The generated image was not readable.") from exc
