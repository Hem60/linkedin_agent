"""User image uploads -- plan.md section 11 (F15).

Accepts JPG, PNG and WebP up to 5 MB, and normalises whatever arrives into
something safe to store and publish.

"Safe" is doing real work here. An uploaded file is untrusted input:

* The extension and the browser-supplied content type are claims, not facts.
  The bytes are decoded to find out what the file actually is.
* A small file can decode to an enormous bitmap -- a decompression bomb. Pillow
  is capped so a 10 KB upload cannot allocate gigabytes.
* Photos carry EXIF, and EXIF carries GPS coordinates. Re-encoding drops all of
  it, so a user who uploads a holiday photo does not publish their address.
  Orientation is applied first, so the image does not end up sideways when the
  tag that described the rotation is removed.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow's own guard, tightened. 50 MP is far beyond anything a LinkedIn post
# needs and well below what exhausts memory.
Image.MAX_IMAGE_PIXELS = 50_000_000

MAX_BYTES = 5 * 1024 * 1024
MAX_EDGE = 1920            # LinkedIn shows nothing larger; bigger is wasted bytes
MIN_EDGE = 200             # below this it renders as a thumbnail
JPEG_QUALITY = 88

ACCEPTED = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# LinkedIn crops tall images in the feed (plan section 11). Warn, do not block.
TALL_RATIO = 0.8


class ImageRejected(Exception):
    """The upload cannot be used, with a reason worth showing the user."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    content_type: str
    width: int
    height: int
    format: str
    warnings: tuple[str, ...] = ()

    @property
    def extension(self) -> str:
        return {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[self.format]

    @property
    def size_kb(self) -> int:
        return round(len(self.data) / 1024)


def prepare_upload(raw: bytes, filename: str = "") -> PreparedImage:
    """Validate, normalise and re-encode an uploaded image.

    Raises ImageRejected with a message intended for the user.
    """
    if not raw:
        raise ImageRejected("That file is empty.")
    if len(raw) > MAX_BYTES:
        mb = len(raw) / 1024 / 1024
        raise ImageRejected(
            f"That file is {mb:.1f} MB. The limit is {MAX_BYTES // 1024 // 1024} MB."
        )

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()                      # decode now, so a corrupt file fails here
    except Image.DecompressionBombError as exc:
        raise ImageRejected("That image is too large to process.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected(
            "That doesn't look like an image file. JPG, PNG or WebP please."
        ) from exc

    fmt = (img.format or "").upper()
    if fmt not in ACCEPTED:
        pretty = fmt or "unknown"
        raise ImageRejected(f"{pretty} images aren't supported. Use JPG, PNG or WebP.")

    # Apply the EXIF rotation before the tag is discarded by re-encoding.
    img = ImageOps.exif_transpose(img)

    width, height = img.size
    if min(width, height) < MIN_EDGE:
        raise ImageRejected(
            f"That image is {width}x{height}. It needs to be at least "
            f"{MIN_EDGE}px on the shorter side."
        )

    warnings: list[str] = []
    if width / height < TALL_RATIO:
        warnings.append("LinkedIn crops tall images in the feed. Landscape or square works better.")

    if max(width, height) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        warnings.append(f"Resized to {img.width}x{img.height}.")

    data, out_format = _encode(img, fmt)

    # Re-encoding a huge PNG can still exceed the limit; fall back to JPEG.
    if len(data) > MAX_BYTES and out_format != "JPEG":
        data, out_format = _encode(img, "JPEG")
        warnings.append("Converted to JPEG to fit the size limit.")
    if len(data) > MAX_BYTES:
        raise ImageRejected("That image is too large even after resizing.")

    return PreparedImage(
        data=data,
        content_type=ACCEPTED[out_format],
        width=img.width,
        height=img.height,
        format=out_format,
        warnings=tuple(warnings),
    )


# Pillow carries `img.info` through a save, so re-encoding alone does NOT drop
# everything: a JPEG COM comment written by the uploader survives into the new
# file. Found by test_exif_is_dropped_even_without_the_optional_library.
# `transparency` is deliberately absent from this list -- palette PNGs need it
# to render correctly, and it holds an index, not metadata.
METADATA_KEYS = (
    "exif", "comment", "icc_profile", "xmp", "XML:com.adobe.xmp",
    "photoshop", "adobe", "adobe_transform", "dpi", "iptc", "Raw profile type exif",
)


def _strip_metadata(img: Image.Image) -> Image.Image:
    for key in METADATA_KEYS:
        img.info.pop(key, None)
    return img


def _encode(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    """Re-encode from pixels, with the source file's metadata removed first.

    This is what drops EXIF (including GPS), colour profiles, XMP, embedded
    comments and any payload appended after the image data.
    """
    img = _strip_metadata(img)
    buf = io.BytesIO()

    if fmt == "JPEG":
        # JPEG has no alpha; compositing onto white avoids a black background.
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            backdrop = Image.new("RGB", img.size, (255, 255, 255))
            backdrop.paste(img, mask=img.split()[-1])
            img = backdrop
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "JPEG"

    if fmt == "PNG":
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "PNG"

    img.save(buf, format="WEBP", quality=JPEG_QUALITY, method=4)
    return buf.getvalue(), "WEBP"
