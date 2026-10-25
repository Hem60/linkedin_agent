"""Branded post cards -- a template drawn with Pillow, not a generated image.

Why this exists. The reference posts this was built from are two jobs stitched
together: a photograph a model can make, and a layout with exact words, exact
brand colours and a contact strip, which it cannot. Both free Cloudflare image
models were tried on the second job and failed the same way -- flux-1-schnell
produced a confident headline that spelled nothing, and lucid-origin renders
incidental text well but will not reliably write a sentence you chose. A
diffusion model also returns a different layout every run, so a feed built from
one stops looking like one brand.

So the picture is generated or searched, and everything with meaning in it is
drawn here. Every word is correct because every word was placed, the palette is
exact, and it costs nothing: no API call, no Neurons, no daily allowance.

Fonts are bundled rather than taken from the host. Render has no Arial and
Windows has no DejaVu, and a card whose type silently changes between a laptop
and production is worse than one that will not render at all.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

# LinkedIn shows 4:5 at full height in the feed; wider is cropped.
W, H = 1080, 1350

MARGIN = 64
FOOTER_H = 104

_FONTS = Path(__file__).resolve().parent / "static" / "fonts"
_REG, _BOLD = _FONTS / "DejaVuSans.ttf", _FONTS / "DejaVuSans-Bold.ttf"


class PostcardError(Exception):
    """The card could not be drawn."""


@dataclass(frozen=True)
class Palette:
    """Sampled from the reference posts."""

    dark: tuple[int, int, int] = (20, 62, 40)
    accent: tuple[int, int, int] = (124, 179, 66)
    ground: tuple[int, int, int] = (242, 240, 230)
    wash: tuple[int, int, int] = (225, 236, 214)
    ink: tuple[int, int, int] = (34, 34, 34)
    muted: tuple[int, int, int] = (104, 112, 104)
    white: tuple[int, int, int] = (255, 255, 255)


@dataclass
class Feature:
    label: str
    detail: str = ""
    icon: str = "leaf"


@dataclass
class Card:
    """Everything that changes between posts.

    `headline` splits on newlines, and a line beginning "|" is drawn in the
    accent colour -- the two-tone headline every reference uses.
    """

    headline: str
    eyebrow: str = ""
    subhead: str = ""
    features: list[Feature] = field(default_factory=list)
    brand: str = ""
    contacts: list[str] = field(default_factory=list)
    palette: Palette = field(default_factory=Palette)


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError as exc:  # a missing bundled font is a packaging fault
        raise PostcardError(f"Could not load the card font at {path}") from exc


def _wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_w: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Icons
#
# Drawn rather than loaded: a handful of shapes costs less than shipping an
# icon set, and keeps the card to one dependency the project already has.
# ---------------------------------------------------------------------------

def _icon(d: ImageDraw.ImageDraw, kind: str, box: tuple[int, int, int, int], colour) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = (x1 - x0) / 2

    if kind == "leaf":
        d.pieslice([cx - r, cy - r, cx + r, cy + r], 180, 300, fill=colour)
        d.line([cx - r * 0.5, cy + r * 0.4, cx + r * 0.5, cy - r * 0.5],
               fill=colour, width=3)
    elif kind == "shield":
        d.polygon([(cx, cy - r), (cx + r * 0.8, cy - r * 0.5),
                   (cx + r * 0.6, cy + r * 0.7), (cx, cy + r),
                   (cx - r * 0.6, cy + r * 0.7), (cx - r * 0.8, cy - r * 0.5)],
                  outline=colour, width=3)
        d.line([cx - r * 0.35, cy, cx - r * 0.05, cy + r * 0.3], fill=colour, width=4)
        d.line([cx - r * 0.05, cy + r * 0.3, cx + r * 0.45, cy - r * 0.35],
               fill=colour, width=4)
    elif kind == "drop":
        d.polygon([(cx, cy - r), (cx + r * 0.75, cy + r * 0.25), (cx, cy + r * 0.85),
                   (cx - r * 0.75, cy + r * 0.25)], outline=colour, width=3)
    elif kind == "globe":
        d.ellipse([cx - r * 0.85, cy - r * 0.85, cx + r * 0.85, cy + r * 0.85],
                  outline=colour, width=3)
        d.ellipse([cx - r * 0.35, cy - r * 0.85, cx + r * 0.35, cy + r * 0.85],
                  outline=colour, width=3)
        d.line([cx - r * 0.85, cy, cx + r * 0.85, cy], fill=colour, width=3)
    elif kind == "recycle":
        for angle in (0, 120, 240):
            d.arc([cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8],
                  angle + 10, angle + 90, fill=colour, width=5)
    elif kind == "temp":
        d.rounded_rectangle([cx - r * 0.22, cy - r * 0.85, cx + r * 0.22, cy + r * 0.35],
                            radius=int(r * 0.22), outline=colour, width=3)
        d.ellipse([cx - r * 0.45, cy + r * 0.2, cx + r * 0.45, cy + r],
                  outline=colour, width=3)
    else:  # a dot is better than a crash
        d.ellipse([cx - r * 0.4, cy - r * 0.4, cx + r * 0.4, cy + r * 0.4], fill=colour)


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale and centre-crop to fill, as object-fit: cover does."""
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                     Image.LANCZOS)
    left, top = (img.width - tw) // 2, (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


def render(card: Card, photo: bytes | None = None) -> bytes:
    """Draw the card and return JPEG bytes, ready for the same upload path a
    generated image takes."""
    p = card.palette
    canvas = Image.new("RGB", (W, H), p.ground)

    # A soft wash behind the upper half, as the references have.
    wash = Image.new("RGB", (W, H), p.ground)
    ImageDraw.Draw(wash).ellipse([-280, -560, 820, 520], fill=p.wash)
    canvas.paste(wash.filter(ImageFilter.GaussianBlur(110)), (0, 0))
    d = ImageDraw.Draw(canvas)

    y = MARGIN

    # --- brand -------------------------------------------------------------
    if card.brand:
        bf = _font(_BOLD, 26)
        d.text((MARGIN, y), card.brand, font=bf, fill=p.dark)
        y += 52

    # --- eyebrow -----------------------------------------------------------
    if card.eyebrow:
        ef = _font(_BOLD, 22)
        tw = d.textlength(card.eyebrow, font=ef)
        d.rounded_rectangle([MARGIN, y, MARGIN + tw + 48, y + 46], radius=23, fill=p.dark)
        d.text((MARGIN + 24, y + 12), card.eyebrow, font=ef, fill=p.white)
        y += 70

    # --- headline ----------------------------------------------------------
    hf = _font(_BOLD, 64)
    for raw in card.headline.split("\n"):
        accent = raw.startswith("|")
        text = raw[1:] if accent else raw
        for line in _wrap(d, text, hf, W - MARGIN * 2):
            d.text((MARGIN, y), line, font=hf, fill=p.accent if accent else p.dark)
            y += 74
    y += 6

    if card.subhead:
        sf = _font(_REG, 28)
        for line in _wrap(d, card.subhead, sf, W - MARGIN * 2)[:3]:
            d.text((MARGIN, y), line, font=sf, fill=p.muted)
            y += 38
    y += 24

    # --- photo and features ------------------------------------------------
    # The space left over after the text is given to the picture, so a long
    # headline shrinks the photograph rather than colliding with the footer.
    available = H - FOOTER_H - 40 - y
    # Two to a row, so the space they need is half the count rounded up.
    shown = min(len(card.features), 4)
    feature_h = 96 * ((shown + 1) // 2)
    photo_h = max(260, available - (feature_h + 28 if card.features else 0))

    if photo:
        try:
            img = Image.open(io.BytesIO(photo)).convert("RGB")
        except OSError as exc:
            raise PostcardError("That photo could not be read.") from exc
        hero = _cover(img, (W - MARGIN * 2, int(photo_h)))
        mask = Image.new("L", hero.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, hero.size[0] - 1, hero.size[1] - 1],
                                               radius=30, fill=255)
        canvas.paste(hero, (MARGIN, int(y)), mask)
    else:
        d.rounded_rectangle([MARGIN, y, W - MARGIN, y + photo_h], radius=30,
                            fill=(226, 222, 208))
    y += photo_h + 28

    if card.features:
        lf, df = _font(_BOLD, 25), _font(_REG, 20)
        col_w = (W - MARGIN * 2) // 2
        for i, feat in enumerate(card.features[:4]):
            cx = MARGIN + (i % 2) * col_w
            cy = y + (i // 2) * 96
            d.ellipse([cx, cy, cx + 62, cy + 62], fill=p.wash)
            _icon(d, feat.icon, (cx + 16, cy + 16, cx + 46, cy + 46), p.dark)
            d.text((cx + 78, cy + 6), feat.label, font=lf, fill=p.dark)
            if feat.detail:
                for j, line in enumerate(_wrap(d, feat.detail, df, col_w - 96)[:2]):
                    d.text((cx + 78, cy + 36 + j * 24), line, font=df, fill=p.muted)

    # --- footer ------------------------------------------------------------
    d.rectangle([0, H - FOOTER_H, W, H], fill=p.dark)
    if card.contacts:
        cf = _font(_REG, 22)
        slot = W // len(card.contacts)
        for i, text in enumerate(card.contacts[:3]):
            x = i * slot + 40
            d.ellipse([x, H - 70, x + 34, H - 36], outline=p.accent, width=3)
            d.text((x + 48, H - 64), text, font=cf, fill=p.white)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()
