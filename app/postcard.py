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
_ICONS = _FONTS / "MaterialSymbolsOutlined.ttf"


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
    # Which of the three arrangements to draw. The references are not
    # variations on one card: a comparison of two things and a product with a
    # list of claims want genuinely different furniture, and forcing either
    # into the other's layout is what makes a template look like a template.
    #   "feature" -- photograph behind, claims down the right
    #   "split"   -- photograph left, words and claims right
    #   "compare" -- centred headline, two labelled columns
    layout: str = "feature"
    eyebrow: str = ""
    subhead: str = ""
    # The reference runs a bold line under the headline and then a short
    # paragraph under that -- a claim, then the explanation of it.
    body: str = ""
    features: list[Feature] = field(default_factory=list)
    # Short labels for the strip above the footer: two or three words each,
    # the row of small reassurances every reference finishes with.
    badges: list[Feature] = field(default_factory=list)
    # The two sides of a comparison card: a label, a name and a line each.
    columns: list[Feature] = field(default_factory=list)
    brand: str = ""
    tagline: str = ""
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

# Material Symbols, drawn as glyphs rather than shapes.
#
# These were hand-drawn with Pillow primitives first, and it showed: a leaf
# built from a pie slice and a line does not sit beside real typography. A
# font renders at any size with proper curves and consistent stroke weight,
# and Pillow can draw font glyphs without the native libraries SVG would need
# -- which matters, because Render is not this laptop.
#
# Names are the project's, codepoints are Material's, taken from the published
# list rather than guessed.
_GLYPHS = {
    "leaf": "",       # eco
    "drop": "",       # water_drop
    "shield": "",     # verified_user
    "globe": "",      # public
    "recycle": "",    # recycling
    "temp": "",       # device_thermostat
    "ship": "",       # local_shipping
    "box": "",        # inventory_2
    "bolt": "",       # bolt
    "growth": "",     # trending_up
    "people": "",     # groups
    "clock": "",      # schedule
    "check": "",      # check_circle
}

ICON_NAMES = tuple(_GLYPHS)


def _icon(d: ImageDraw.ImageDraw, kind: str, box: tuple[int, int, int, int], colour) -> None:
    """Draw one icon centred in `box`."""
    x0, y0, x1, y1 = box
    size = max(12, int(min(x1 - x0, y1 - y0)))
    glyph = _GLYPHS.get(kind, _GLYPHS["leaf"])
    font = _font(_ICONS, size)
    # anchor="mm" puts the glyph's middle on the point, which is what makes a
    # row of different icons line up rather than wobble.
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), glyph, font=font, fill=colour, anchor="mm")


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale and centre-crop to fill, as object-fit: cover does."""
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                     Image.LANCZOS)
    left, top = (img.width - tw) // 2, (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _scrim(size: tuple[int, int], palette: Palette, stop: float) -> Image.Image:
    """A cream veil over the top of the photograph, fading out by `stop`.

    The reference overlays its headline directly on the picture. That only
    stays legible if the picture is bright where the words are, and a
    generated photograph is not reliably anything. So the ground colour is
    laid over the top as a gradient: solid enough at the crown to read dark
    green type against, gone by the time the product appears.
    """
    w, h = size
    veil = Image.new("RGBA", (1, h))
    px = veil.load()
    edge = max(1, int(h * stop))
    for y in range(h):
        if y >= edge:
            px[0, y] = (*palette.ground, 0)
        else:
            # Hold near-solid for the first third, then ease away.
            t = y / edge
            alpha = 247 if t < 0.34 else int(247 * (1 - (t - 0.34) / 0.66) ** 1.35)
            px[0, y] = (*palette.ground, max(0, min(247, alpha)))
    return veil.resize((w, h))


def _render_feature(card: Card, photo: bytes | None) -> bytes:
    """Draw the card and return JPEG bytes, ready for the same upload path a
    generated image takes.

    The photograph is the background, not a panel inside the layout. That is
    the single thing that makes the reference read as designed rather than
    assembled, and everything else here sits on top of it.
    """
    p = card.palette

    if photo:
        try:
            img = Image.open(io.BytesIO(photo)).convert("RGB")
        except OSError as exc:
            raise PostcardError("That photo could not be read.") from exc
        canvas = _cover(img, (W, H))
    else:
        # No photograph: a spent allowance costs the picture, not the card.
        # The wash has to carry the whole canvas on its own, so it is drawn
        # across the full height rather than as a crown above a photograph --
        # otherwise the lower two thirds are a flat empty band, which is what
        # a card with nothing in it looked like in production.
        canvas = Image.new("RGB", (W, H), p.ground)
        wash = Image.new("RGB", (W, H), p.ground)
        wd = ImageDraw.Draw(wash)
        wd.ellipse([-300, -520, 880, 560], fill=p.wash)
        wd.ellipse([W - 520, H - 880, W + 420, H + 160], fill=p.wash)
        canvas = Image.blend(canvas, wash.filter(ImageFilter.GaussianBlur(130)), 0.95)

    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(_scrim((W, H), p, 0.62))
    canvas = canvas.convert("RGB")
    d = ImageDraw.Draw(canvas)

    # With a photograph the words sit in the top third and the picture fills
    # the rest. Without one there is nothing to fill it, so the text starts
    # lower and the claims spread -- otherwise the card is a paragraph with
    # six hundred empty pixels under it, which is what production produced
    # the first time the allowance ran out.
    has_photo = photo is not None
    head_y = _brand_header(d, canvas, card, W, compact=True, on_photo=has_photo)
    y = head_y if has_photo else head_y + 90

    # --- eyebrow -----------------------------------------------------------
    if card.eyebrow:
        ef = _font(_BOLD, 21)
        tw = d.textlength(card.eyebrow, font=ef)
        d.rounded_rectangle([MARGIN, y, MARGIN + tw + 44, y + 42], radius=21, fill=p.dark)
        d.text((MARGIN + 22, y + 10), card.eyebrow, font=ef, fill=p.white)
        y += 60

    # --- headline ----------------------------------------------------------
    # Mixed case and large, as the reference sets it. Capitals shout where the
    # original speaks, and at this size they also wrap far sooner.
    #
    # The size is chosen, not fixed: a headline the model wrote is whatever
    # length it is, and at a fixed 70pt "Without a Trace." fell onto a third
    # line while the reference holds two. So the type steps down until each
    # written line fits on one drawn line -- which is what a designer does,
    # and keeps the block the shape the layout expects.
    text_w = 640
    written = [raw.removeprefix("|")
               for raw in card.headline.splitlines()]
    size = 70
    while size > 40:
        hf = _font(_BOLD, size)
        if all(d.textlength(line, font=hf) <= text_w for line in written if line):
            break
        size -= 2
    hf = _font(_BOLD, size)
    line_h = int(size * 1.12)

    for raw in card.headline.splitlines():
        accent = raw.startswith("|")
        line_text = raw[1:] if accent else raw
        for line in _wrap(d, line_text, hf, text_w):
            d.text((MARGIN, y), line, font=hf, fill=p.accent if accent else p.dark)
            y += line_h

    # The short rule the reference draws under the headline.
    y += 8
    d.line([MARGIN, y, MARGIN + 92, y], fill=p.accent, width=6)
    y += 30

    if card.subhead:
        sf = _font(_BOLD, 31)
        for line in _wrap(d, card.subhead, sf, text_w)[:2]:
            d.text((MARGIN, y), line, font=sf, fill=p.dark)
            y += 42
        y += 6

    if card.body:
        bf = _font(_REG, 24)
        for line in _wrap(d, card.body, bf, text_w - 40)[:3]:
            d.text((MARGIN, y), line, font=bf, fill=p.muted)
            y += 33

    # --- features down the right -------------------------------------------
    # White discs with a ring, not flat tints: on a photograph a flat circle
    # disappears, and the ring is what the reference uses to hold them.
    if card.features:
        shown = card.features[:5]
        col_x = W - MARGIN - 300
        top = head_y + (54 if has_photo else 110)
        room = H - FOOTER_H - (300 if has_photo else 190) - top
        slot = min(104 if has_photo else 150, room / max(1, len(shown)))
        lf = _font(_BOLD, 22)

        for i, feat in enumerate(shown):
            cy = top + i * slot + slot / 2
            if i:
                d.line([col_x + 86, cy - slot / 2, W - MARGIN, cy - slot / 2],
                       fill=(255, 255, 255, 90), width=2)

            d.ellipse([col_x, cy - 33, col_x + 66, cy + 33], fill=p.white,
                      outline=p.dark, width=3)
            _icon(d, feat.icon, (col_x + 18, cy - 15, col_x + 48, cy + 15), p.dark)

            lines = _wrap(d, feat.label, lf, 214)[:2]
            ty = cy - (len(lines) * 26) / 2
            for line in lines:
                d.text((col_x + 86, ty), line, font=lf, fill=p.dark)
                ty += 26

    # --- badge strip above the footer --------------------------------------
    if card.badges:
        strip_h = 128
        sy = H - FOOTER_H - strip_h - 26
        d.rounded_rectangle([MARGIN - 20, sy, W - MARGIN + 20, sy + strip_h],
                            radius=22, fill=p.ground)
        shown = card.badges[:5]
        slot = (W - (MARGIN - 20) * 2 - 40) / len(shown)
        bf = _font(_BOLD, 18)
        for i, badge in enumerate(shown):
            cx = MARGIN + i * slot + slot / 2
            _icon(d, badge.icon, (cx - 19, sy + 22, cx + 19, sy + 60), p.dark)
            if i:
                d.line([cx - slot / 2, sy + 24, cx - slot / 2, sy + strip_h - 24],
                       fill=p.wash, width=2)
            ty = sy + 72
            for line in _wrap(d, badge.label, bf, slot - 26)[:2]:
                lw = d.textlength(line, font=bf)
                d.text((cx - lw / 2, ty), line, font=bf, fill=p.dark)
                ty += 23

    # --- footer ------------------------------------------------------------
    d.rectangle([0, H - FOOTER_H, W, H], fill=p.dark)
    fy = H - FOOTER_H

    if card.tagline:
        tf = _font(_REG, 23)
        d.ellipse([MARGIN, fy + 30, MARGIN + 44, fy + 74], outline=p.accent, width=3)
        _icon(d, "leaf", (MARGIN + 11, fy + 41, MARGIN + 33, fy + 63), p.accent)
        # Stop before the contact line. A tagline the model wrote is whatever
        # length it is, and a long one ran straight through it.
        room = W - MARGIN * 2 - 60 - _contacts_width(d, card) - 40
        line = _wrap(d, card.tagline, tf, room)[:1]
        if line:
            d.text((MARGIN + 60, fy + 40), line[0], font=tf, fill=p.white)

    # No brand name here. It is in the header at the top of every layout now,
    # and printed twice on one card it read as a mistake rather than a motif.
    if card.contacts:
        cf = _font(_REG, 19)
        line = "  ·  ".join(card.contacts[:3])
        cw = d.textlength(line, font=cf)
        d.text((W - MARGIN - cw, fy + 42), line, font=cf, fill=p.accent)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def _contacts_width(d: ImageDraw.ImageDraw, card: Card) -> float:
    """How much of the footer the contact line will take."""
    if not card.contacts:
        return 0.0
    return d.textlength("  ·  ".join(card.contacts[:3]), font=_font(_REG, 19))


def _footer(d: ImageDraw.ImageDraw, card: Card) -> None:
    """The dark bar every reference finishes with."""
    p = card.palette
    d.rectangle([0, H - FOOTER_H, W, H], fill=p.dark)
    fy = H - FOOTER_H

    if card.tagline:
        tf = _font(_REG, 23)
        d.ellipse([MARGIN, fy + 30, MARGIN + 44, fy + 74], outline=p.accent, width=3)
        _icon(d, "leaf", (MARGIN + 11, fy + 41, MARGIN + 33, fy + 63), p.accent)
        room = W - MARGIN * 2 - 60 - _contacts_width(d, card) - 40
        line = _wrap(d, card.tagline, tf, room)[:1]
        if line:
            d.text((MARGIN + 60, fy + 40), line[0], font=tf, fill=p.white)

    # No brand name here. It is in the header at the top of every layout now,
    # and printed twice on one card it read as a mistake rather than a motif.
    if card.contacts:
        cf = _font(_REG, 19)
        line = "  ·  ".join(card.contacts[:3])
        cw = d.textlength(line, font=cf)
        d.text((W - MARGIN - cw, fy + 42), line, font=cf, fill=p.accent)


def _fitted(d: ImageDraw.ImageDraw, lines: list[str], width: float,
            start: int, floor: int = 38):
    """The largest bold size at which every line still fits on one line."""
    size = start
    while size > floor:
        f = _font(_BOLD, size)
        if all(d.textlength(line, font=f) <= width for line in lines if line):
            break
        size -= 2
    return _font(_BOLD, size), int(size * 1.12)


def _open_photo(photo: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(photo)).convert("RGB")
    except OSError as exc:
        raise PostcardError("That photo could not be read.") from exc


# The brand's own logo, when there is one. Drop a PNG with transparency at
# app/static/brand/logo.png and every card picks it up; until then the mark
# below stands in. Deliberately not an error when absent -- a missing logo
# should cost a badge, not the card.
LOGO_PATH = Path(__file__).resolve().parent / "static" / "brand" / "logo.png"


def _contain(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fit inside `size` whole, as object-fit: contain does.

    The counterpart to _cover, and the right one for a product. Cover crops
    whatever will not fit, which on a catalogue photograph means slicing the
    ends off the thing being sold.
    """
    tw, th = size
    scale = min(tw / img.width, th / img.height)
    return img.resize((max(1, round(img.width * scale)),
                       max(1, round(img.height * scale))), Image.LANCZOS)


def _trim(img: Image.Image, tol: int = 244) -> Image.Image:
    """Crop the plain border off a catalogue photograph.

    The product shots are shot on white with a generous margin, and inside a
    panel that margin is drawn twice -- the photograph's and the layout's --
    so the product ends up small in the middle of a lot of nothing.

    Only trimmed when what is left is still most of the picture. A shot that
    is mostly background, or one on a dark ground, is left alone rather than
    cropped to something nobody chose.
    """
    grey = img.convert("L").point(lambda v: 0 if v >= tol else 255)
    box = grey.getbbox()
    if not box:
        return img
    w, h = box[2] - box[0], box[3] - box[1]
    if w < img.width * 0.25 or h < img.height * 0.25:
        return img
    pad = round(min(w, h) * 0.04)
    return img.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                     min(img.width, box[2] + pad), min(img.height, box[3] + pad)))


def _brand_header(d: ImageDraw.ImageDraw, canvas: Image.Image, card: Card,
                  width: int, y: int = MARGIN, compact: bool = False,
                  on_photo: bool = False) -> int:
    """The band across the top: logo left, brand name right, tagline beneath.

    The rule under the names runs the full width on purpose. Everything below
    it is two columns, and a line drawn across both is what says they are one
    card rather than two things that happen to be side by side.

    `compact` leaves the tagline out, for the layouts that already print it
    somewhere else -- the photograph-backed ones carry it in the dark footer,
    and printing it twice on one card is worse than not printing it at all.

    `on_photo` is for those same layouts: the rule is drawn in the brand's
    dark green rather than the pale wash, which disappears over a photograph.

    Returns the y to carry on from.
    """
    p = card.palette
    size = 76

    if LOGO_PATH.exists():
        try:
            mark = Image.open(LOGO_PATH).convert("RGBA")
            scale = size / max(mark.width, mark.height)
            mark = mark.resize((max(1, round(mark.width * scale)),
                                max(1, round(mark.height * scale))), Image.LANCZOS)
            canvas.paste(mark, (MARGIN, y), mark)
        except OSError:
            LOGO_PATH.exists()      # unreadable file: fall through to the mark
    else:
        d.ellipse([MARGIN, y, MARGIN + size, y + size], outline=p.dark, width=3)
        _icon(d, "leaf", (MARGIN + 20, y + 20, MARGIN + 56, y + 56), p.dark)

    if card.brand:
        bf = _font(_BOLD, 32)
        bw = d.textlength(card.brand, font=bf)
        d.text((width - MARGIN - bw, y + (size - 34) // 2), card.brand,
               font=bf, fill=p.dark)

    y += size + 22
    d.line([MARGIN, y, width - MARGIN, y],
           fill=p.dark if on_photo else p.wash, width=3)
    y += 28
    if compact:
        return y - 6

    # Set large. It is the line the post is actually making, and at 21pt under
    # a 32pt brand name it read as a caption on the logo rather than a
    # statement of its own.
    if card.tagline:
        tf, line_h = _fitted(d, [card.tagline], width - MARGIN * 2, 38, floor=26)
        for line in _wrap(d, card.tagline, tf, width - MARGIN * 2)[:2]:
            d.text((MARGIN, y), line, font=tf, fill=p.dark)
            y += line_h + 4
        y += 10
    return y + 18


def _split_column(d: ImageDraw.ImageDraw, card: Card, x: float, col_w: float,
                  y: float, floor: float) -> float:
    """Draw the detail column, and return the y it finished at.

    Also used to measure. Called with a throwaway canvas it reports the height
    the words need without drawing them, which is how the panel can be made
    the size of its contents instead of a fixed height with the bottom third
    empty -- which is what it was.
    """
    p = card.palette

    if card.eyebrow:
        ef = _font(_BOLD, 20)
        tw = d.textlength(card.eyebrow, font=ef)
        d.rounded_rectangle([x, y, x + tw + 34, y + 38], radius=10, fill=p.wash)
        d.text((x + 17, y + 8), card.eyebrow, font=ef, fill=p.dark)
        y += 54

    written = [raw.removeprefix("|") for raw in card.headline.splitlines()]
    hf, line_h = _fitted(d, written, col_w, 50)
    for raw in card.headline.splitlines():
        accent = raw.startswith("|")
        for line in _wrap(d, raw[1:] if accent else raw, hf, col_w):
            d.text((x, y), line, font=hf, fill=p.accent if accent else p.ink)
            y += line_h

    y += 12
    d.line([x, y, x + 74, y], fill=p.accent, width=5)
    y += 26

    if card.body:
        bf = _font(_REG, 22)
        for line in _wrap(d, card.body, bf, col_w)[:4]:
            d.text((x, y), line, font=bf, fill=p.muted)
            y += 31
        y += 18

    lf = _font(_BOLD, 21)
    for feat in card.features[:4]:
        if y + 56 > floor:
            break
        d.ellipse([x, y, x + 56, y + 56], outline=p.accent, width=3)
        _icon(d, feat.icon, (x + 15, y + 15, x + 41, y + 41), p.dark)
        lines = _wrap(d, feat.label.upper(), lf, col_w - 74)[:2]
        ty = y + 28 - len(lines) * 12
        for line in lines:
            d.text((x + 74, ty), line, font=lf, fill=p.ink)
            ty += 25
        y += 84
    return y


def _render_split(card: Card, photo: bytes | None) -> bytes:
    """Header across the top, then one panel holding two columns.

    One panel, not two. An earlier version gave each column its own card and
    the result still read as two things side by side -- the gutter between
    them was the same hard seam the version before it had, just in cream. A
    single surface with a hairline down it is a grid; two surfaces are two
    cards.

    The panel is as tall as its contents, measured first. At a fixed height it
    ended a third of the way up from the footer with nothing in the rest.

    The product is contained rather than cropped to fill: smaller on the page
    but whole, which a catalogue photograph has to be.
    """
    p = card.palette
    canvas = Image.new("RGB", (W, H), p.ground)
    d = ImageDraw.Draw(canvas)

    head_y = _brand_header(d, canvas, card, W)

    pad = 30
    inner_w = W - MARGIN * 2 - pad * 2
    left_w, gutter = 380, 44
    right_w = inner_w - left_w - gutter
    left_x = MARGIN + pad
    right_x = left_x + left_w + gutter

    top = head_y
    bottom = H - FOOTER_H - 44

    d.rounded_rectangle([MARGIN, top, W - MARGIN, bottom], radius=28, fill=p.white)

    # Both columns centre on the same axis. Sizing the panel to its contents
    # instead left a band of bare ground between it and the footer; filling
    # the panel and hanging the words from the top left the same band inside
    # it. Centred, what is spare is shared top and bottom and reads as margin.
    measured = _split_column(ImageDraw.Draw(Image.new("RGB", (8, 8))), card,
                             right_x, right_w, 0, 1e6)
    y = top + max(pad, (bottom - top - measured) // 2)

    if photo:
        shot = _contain(_trim(_open_photo(photo)), (left_w, bottom - top - pad * 2))
        canvas.paste(shot, (left_x + (left_w - shot.width) // 2,
                            top + (bottom - top - shot.height) // 2))
    else:
        d.rounded_rectangle([left_x, top + pad, left_x + left_w, bottom - pad],
                            radius=18, fill=p.wash)

    # The hairline is the grid: it says the two columns belong to one card.
    rule_x = left_x + left_w + gutter / 2
    d.line([rule_x, top + 44, rule_x, bottom - 44], fill=p.wash, width=2)

    _split_column(d, card, right_x, right_w, y, bottom - pad + 10)

    _contact_bar(d, card)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def _render_compare(card: Card, photo: bytes | None) -> bytes:
    """Centred headline over two labelled columns, photograph beneath.

    For a post that sets two things against each other, which the reference
    does with "One Crop. Two Outputs." A list of claims cannot say that, and
    forcing a comparison into one is how a template starts misrepresenting
    what was written.
    """
    p = card.palette
    canvas = Image.new("RGB", (W, H), p.white)
    d = ImageDraw.Draw(canvas)

    y = _brand_header(d, canvas, card, W, compact=True) + 18
    written = [raw.removeprefix("|")
               for raw in card.headline.splitlines()]
    hf, line_h = _fitted(d, written, W - MARGIN * 2, 76)
    for raw in card.headline.splitlines():
        accent = raw.startswith("|")
        for line in _wrap(d, raw[1:] if accent else raw, hf, W - MARGIN * 2):
            lw = d.textlength(line, font=hf)
            d.text(((W - lw) / 2, y), line, font=hf, fill=p.accent if accent else p.ink)
            y += line_h

    y += 18
    d.line([(W - 300) / 2, y, (W + 300) / 2, y], fill=p.ink, width=3)
    y += 26

    if card.body:
        bf = _font(_BOLD, 26)
        for line in _wrap(d, card.body, bf, 720)[:2]:
            lw = d.textlength(line, font=bf)
            d.text(((W - lw) / 2, y), line, font=bf, fill=p.ink)
            y += 36
    y += 36

    pairs = (card.columns or card.features)[:2]
    if pairs:
        half = W / 2
        lab, name, note = _font(_BOLD, 21), _font(_BOLD, 34), _font(_BOLD, 20)
        for i, col in enumerate(pairs):
            cx = MARGIN if i == 0 else half + 40
            cw = half - MARGIN - 40
            d.text((cx, y), f"Output {i + 1}", font=lab, fill=p.muted)
            d.line([cx, y + 30, cx + 54, y + 30], fill=p.accent, width=4)
            ty = y + 46
            for line in _wrap(d, col.label, name, cw)[:2]:
                d.text((cx, ty), line, font=name, fill=p.ink)
                ty += 42
            for line in _wrap(d, col.detail, note, cw)[:2]:
                d.text((cx, ty + 4), line, font=note, fill=p.muted)
                ty += 26
        y += 190

    if photo:
        height = int(H - FOOTER_H - y)
        if height > 80:
            canvas.paste(_cover(_open_photo(photo), (W, height)), (0, int(y)))

    _footer(ImageDraw.Draw(canvas), card)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


LAYOUTS = {
    "feature": _render_feature,
    "split": _render_split,
    "compare": _render_compare,
}


def render(card: Card, photo: bytes | None = None) -> bytes:
    """Draw the card in the arrangement its layout asks for."""
    draw = LAYOUTS.get(card.layout, _render_feature)
    return draw(card, photo)


def _contact_bar(d: ImageDraw.ImageDraw, card: Card) -> None:
    """The footer both of these references use: three contacts, each behind a
    ringed icon, divided by thin vertical rules.

    Different from `_footer`, which carries a tagline and a brand. This one is
    all detail -- phone, email, site -- and the rules are what stop three
    unrelated strings reading as one sentence.
    """
    p = card.palette
    d.rectangle([0, H - FOOTER_H, W, H], fill=p.dark)
    items = card.contacts[:3]
    if not items:
        return

    mid = H - FOOTER_H / 2
    slot = W / len(items)
    cf = _font(_REG, 21)
    glyphs = ("clock", "box", "globe")

    for i, text in enumerate(items):
        left = i * slot
        if i:
            d.line([left, mid - 26, left, mid + 26], fill=p.accent, width=2)
        cx = left + 46
        d.ellipse([cx - 19, mid - 19, cx + 19, mid + 19], outline=p.white, width=2)
        _icon(d, glyphs[i % len(glyphs)], (cx - 11, mid - 11, cx + 11, mid + 11), p.white)
        d.text((cx + 32, mid - 12), text, font=cf, fill=p.white)


def _disc_icon(d: ImageDraw.ImageDraw, kind: str, cx: float, cy: float,
               r: float, palette: Palette) -> None:
    """A filled dark disc with a white glyph, as the references draw them.

    An outlined circle disappears against a photograph; a filled one holds its
    shape wherever it lands.
    """
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=palette.dark)
    _icon(d, kind, (cx - r * 0.5, cy - r * 0.5, cx + r * 0.5, cy + r * 0.5), palette.white)


def _render_range(card: Card, photo: bytes | None) -> bytes:
    """Claims down the left, product photograph filling the right.

    The mirror of `split`, and the arrangement the reference uses when the
    point is the breadth of what is on offer rather than one item: the eye
    reads the list first and the picture confirms it.
    """
    p = card.palette
    canvas = Image.new("RGB", (W, H), p.ground)
    d = ImageDraw.Draw(canvas)

    # The photograph occupies the right two-fifths, softened into the page by
    # a wedge of ground colour rather than stopping at a straight edge.
    # The header owns the top of the card, so the photograph starts under it
    # rather than at y=0 -- it used to run up behind the brand name.
    head_y = _brand_header(d, canvas, card, W)

    if photo:
        pw, ph = 560, H - FOOTER_H - head_y
        canvas.paste(_cover(_open_photo(photo), (pw, ph)), (W - pw, head_y))
        fade = 190
        veil = Image.new("RGBA", (fade, ph), (0, 0, 0, 0))
        vd = ImageDraw.Draw(veil)
        for i in range(fade):
            vd.line([i, 0, i, ph], fill=(*p.ground, int(255 * (1 - i / fade) ** 1.2)))
        canvas.paste(Image.alpha_composite(
            canvas.crop((W - pw, head_y, W - pw + fade, head_y + ph)).convert("RGBA"),
            veil).convert("RGB"), (W - pw, head_y))
        d = ImageDraw.Draw(canvas)

    y = head_y + 8

    text_w = 560
    written = [line.removeprefix("|") for line in card.headline.splitlines()]
    hf, line_h = _fitted(d, written, text_w, 62)
    for raw in card.headline.splitlines():
        accent = raw.startswith("|")
        for line in _wrap(d, raw.removeprefix("|"), hf, text_w):
            d.text((MARGIN, y), line, font=hf, fill=p.accent if accent else p.dark)
            y += line_h
    y += 24

    if card.features:
        shown = card.features[:4]
        lf, df = _font(_BOLD, 23), _font(_REG, 18)
        # Spread down the column rather than bunching under the headline: at a
        # fixed step four claims left a third of the card empty.
        # A tight group, centred in what is left. Stretching them to fill the
        # column put 200px between each and they stopped reading as one list;
        # packing them under the headline left a dead band beneath. Centring
        # the block spends the slack at both ends, where it looks deliberate.
        step = 96
        room = H - FOOTER_H - 90 - y
        top = y + max(0, (room - len(shown) * step) / 2)
        for i, feat in enumerate(shown):
            fy = top + i * step
            _disc_icon(d, feat.icon, MARGIN + 33, fy + 33, 33, p)
            ty = fy + 8 if feat.detail else fy + 20
            for line in _wrap(d, feat.label, lf, 330)[:2]:
                d.text((MARGIN + 84, ty), line, font=lf, fill=p.dark)
                ty += 26
            if feat.detail:
                for line in _wrap(d, feat.detail, df, 330)[:1]:
                    d.text((MARGIN + 84, ty + 2), line, font=df, fill=p.muted)
        y = top + len(shown) * step

    if card.subhead:
        # On the left, over the ground colour: at the right edge it sat on the
        # photograph and vanished into it.
        sf = _font(_BOLD, 19)
        d.text((MARGIN, H - FOOTER_H - 46), card.subhead, font=sf, fill=p.muted)

    _contact_bar(d, card)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


def _render_spotlight(card: Card, photo: bytes | None) -> bytes:
    """One product, large, with its claims floating around it on discs.

    The reference's product spotlight. The callouts sit over the photograph
    rather than in a column, which is what makes it read as a picture of a
    thing being pointed at rather than a list beside an image.
    """
    p = card.palette
    canvas = Image.new("RGB", (W, H), p.ground)

    if photo:
        hero = _cover(_open_photo(photo), (W, H - FOOTER_H))
        canvas.paste(hero, (0, 0))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(_scrim((W, H), p, 0.46))
        canvas = canvas.convert("RGB")
    d = ImageDraw.Draw(canvas)

    y = _brand_header(d, canvas, card, W, compact=True, on_photo=photo is not None)
    if card.eyebrow:
        ef = _font(_BOLD, 21)
        tw = d.textlength(card.eyebrow, font=ef)
        x = W - MARGIN - tw - 44
        d.rounded_rectangle([x, y, x + tw + 44, y + 42], radius=21, fill=p.dark)
        d.text((x + 22, y + 10), card.eyebrow, font=ef, fill=p.white)
        y += 62

    # The headline sits right, away from the product, and is set as large as
    # it will go -- the reference leads with the product name at display size.
    written = [line.removeprefix("|") for line in card.headline.splitlines()]
    hf, line_h = _fitted(d, written, 500, 84, floor=46)
    for raw in card.headline.splitlines():
        accent = raw.startswith("|")
        for line in _wrap(d, raw.removeprefix("|"), hf, 500):
            lw = d.textlength(line, font=hf)
            d.text((W - MARGIN - lw, y), line, font=hf,
                   fill=p.accent if accent else p.dark)
            y += line_h

    if card.subhead:
        sf = _font(_BOLD, 21)
        for line in _wrap(d, card.subhead, sf, 460)[:2]:
            lw = d.textlength(line, font=sf)
            d.text((W - MARGIN - lw, y + 6), line, font=sf, fill=p.muted)
            y += 30

    # Callouts down the left, each a disc with its label beside it, stepped in
    # so they trace the edge of the product rather than forming a column.
    if card.features:
        shown = card.features[:3]
        lf = _font(_BOLD, 22)
        top = y + 90
        for i, feat in enumerate(shown):
            cx = MARGIN + 44 + (i % 2) * 70
            cy = top + i * 150
            lines = _wrap(d, feat.label, lf, 220)[:2]
            # A pill behind the words. Dark green on a photograph of anything
            # busy is unreadable, and these land wherever the picture happens
            # to be -- the reference sets its callouts on a light ground for
            # the same reason.
            tw = max(d.textlength(line, font=lf) for line in lines)
            box_h = len(lines) * 26 + 16
            d.rounded_rectangle(
                [cx + 30, cy - box_h / 2, cx + 66 + tw + 18, cy + box_h / 2],
                radius=box_h / 2, fill=p.ground)
            _disc_icon(d, feat.icon, cx, cy, 40, p)
            ty = cy - len(lines) * 13 + 2
            for line in lines:
                d.text((cx + 56, ty), line, font=lf, fill=p.dark)
                ty += 26

    if card.body:
        # Wrapped. Set as one line it was as wide as the model's sentence
        # happened to be, and ran off both edges of the card.
        bf = _font(_BOLD, 19)
        lines = _wrap(d, card.body, bf, W - MARGIN * 2 - 80)[:2]
        bw = max(d.textlength(line, font=bf) for line in lines)
        by = H - FOOTER_H - 30 - len(lines) * 26
        d.rounded_rectangle([(W - bw) / 2 - 22, by - 10,
                             (W + bw) / 2 + 22, by + len(lines) * 26 + 6],
                            radius=20, fill=p.ground)
        for line in lines:
            lw = d.textlength(line, font=bf)
            d.text(((W - lw) / 2, by), line, font=bf, fill=p.dark)
            by += 26

    _contact_bar(d, card)
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


LAYOUTS.update({"range": _render_range, "spotlight": _render_spotlight})


# Portrait 4:5, like the rest. This was square to match one reference, but the
# brand brief asks for 1080x1350 on every image, and a feed where one card is a
# different shape from the others is the thing a brand guide exists to stop.
PW, PH = 1080, 1350
PFOOTER = 104


def _feathered(img: Image.Image, size: tuple[int, int], ground) -> Image.Image:
    """The photograph with its edges dissolved into the ground colour.

    The reference has the product cut out and standing on the cream. Cutting
    a subject out of an arbitrary generated photograph needs segmentation we
    do not have, so this does the next honest thing: an elliptical feather
    that fades the frame away. The product still reads as sitting on the page
    rather than inside a window, which is the effect that mattered.
    """
    w, h = size
    hero = _cover(img, size)
    mask = Image.new("L", (w, h), 0)
    # Inside the panel, not past it. Drawn larger than the frame the blur had
    # nothing to work on and the photograph kept four hard edges -- the exact
    # window effect this exists to avoid.
    ImageDraw.Draw(mask).ellipse([w * 0.06, h * 0.05, w * 0.94, h * 0.95], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(w * 0.09))
    plate = Image.new("RGB", (w, h), ground)
    plate.paste(hero, (0, 0), mask)
    return plate


def _render_product(card: Card, photo: bytes | None) -> bytes:
    """Square product card: photograph left, name and claims right.

    Matches the reference's product spotlight -- the eyebrow in a pale pill,
    the name set large in capitals, and four claims each behind a big soft
    disc. The furniture is lighter than the other layouts because the product
    is doing the work and everything else is a label on it.
    """
    p = card.palette
    canvas = Image.new("RGB", (PW, PH), p.ground)
    d = ImageDraw.Draw(canvas)

    head_y = _brand_header(d, canvas, card, PW, compact=True)

    # --- product, left ------------------------------------------------------
    if photo:
        panel = _feathered(_open_photo(photo), (600, 720), p.ground)
        canvas.paste(panel, (-40, head_y + 60))
        d = ImageDraw.Draw(canvas)

    x = 600
    col_w = PW - x - 48
    y = head_y + 10

    # --- eyebrow ------------------------------------------------------------
    if card.eyebrow:
        ef = _font(_BOLD, 22)
        spaced = " ".join(card.eyebrow)        # the reference letter-spaces it
        tw = d.textlength(spaced, font=ef)
        d.rounded_rectangle([x, y, x + tw + 52, y + 46], radius=23, fill=p.wash)
        d.text((x + 26, y + 11), spaced, font=ef, fill=p.dark)
        y += 70

    # --- name ---------------------------------------------------------------
    # Capitals here, unlike the others: this is a product name, and the
    # reference sets it as a label rather than a sentence.
    written = [line.removeprefix("|").upper() for line in card.headline.splitlines()]
    hf, line_h = _fitted(d, written, col_w, 58, floor=34)
    for line in written:
        d.text((x, y), line, font=hf, fill=p.dark)
        y += line_h

    if card.subhead:
        sf = _font(_BOLD, 23)
        for line in _wrap(d, card.subhead, sf, col_w)[:2]:
            d.text((x, y + 6), line, font=sf, fill=p.ink)
            y += 32
    y += 34

    # --- claims -------------------------------------------------------------
    if card.features:
        shown = card.features[:4]
        lf = _font(_BOLD, 24)
        # Centred in the space left rather than stacked from the top: the
        # header pushed everything down and the claims ended half way up the
        # card with the bottom third empty.
        room = PH - PFOOTER - 40 - y
        step = 126
        top = y + max(0, (room - len(shown) * step) / 2)
        for i, feat in enumerate(shown):
            cy = top + i * step + step / 2
            # A large pale disc, as the reference draws them -- the icon sits
            # inside it rather than beside the words.
            d.ellipse([x, cy - 46, x + 92, cy + 46], fill=p.wash)
            _icon(d, feat.icon, (x + 24, cy - 22, x + 68, cy + 22), p.dark)
            lines = _wrap(d, feat.label, lf, col_w - 112)[:2]
            ty = cy - len(lines) * 14
            for line in lines:
                d.text((x + 112, ty), line, font=lf, fill=p.ink)
                ty += 28

    # --- footer -------------------------------------------------------------
    d.rectangle([0, PH - PFOOTER, PW, PH], fill=p.dark)
    items = card.contacts[:3]
    if items:
        mid = PH - PFOOTER / 2
        slot = PW / len(items)
        cf = _font(_REG, 20)
        glyphs = ("clock", "box", "globe")
        for i, text in enumerate(items):
            left = i * slot
            if i:
                d.line([left, mid - 26, left, mid + 26], fill=p.accent, width=2)
            cx = left + 46
            # White filled discs with a dark glyph, which is what the
            # reference uses -- an outline would vanish on the dark bar.
            d.ellipse([cx - 20, mid - 20, cx + 20, mid + 20], fill=p.white)
            _icon(d, glyphs[i % len(glyphs)], (cx - 11, mid - 11, cx + 11, mid + 11), p.dark)
            d.text((cx + 34, mid - 11), text, font=cf, fill=p.white)

    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=92, optimize=True)
    return out.getvalue()


LAYOUTS["product"] = _render_product
