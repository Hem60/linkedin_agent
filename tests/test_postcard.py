"""Branded post cards.

These check the things that would silently produce a bad card rather than an
error: the wrong canvas size, text running under the footer, a missing font on
a host that is not this laptop.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.postcard import Card, Feature, H, PostcardError, W, render


def a_card(**kw) -> Card:
    base = {
        "brand": "GLOBAL GREEN",
        "eyebrow": "PRODUCT SPOTLIGHT",
        "headline": "WHY COMPARTMENTS\n|MATTER",
        "subhead": "Designed for meals that need separation.",
        "features": [
            Feature("Keeps food separate", "No mixing in transit.", "shield"),
            Feature("Better presentation", "Arrives looking considered.", "leaf"),
        ],
        "contacts": ["+91 9099774511", "marketing@auraeco.in", "auraeco.in"],
    }
    base.update(kw)
    return Card(**base)


def photo_bytes(size=(1200, 800), colour=(120, 140, 110)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


# ---------------------------------------------------------------------------
# The canvas
# ---------------------------------------------------------------------------

def test_a_card_is_the_size_the_feed_shows_uncropped():
    """LinkedIn displays 4:5 at full height; anything wider loses its edges."""
    img = opened(render(a_card(), photo_bytes()))
    assert (img.width, img.height) == (W, H) == (1080, 1350)


def test_a_card_renders_without_a_photo():
    """A card is still useful before a picture has been chosen, so the photo
    area falls back to a block rather than refusing to draw."""
    img = opened(render(a_card(), None))
    assert (img.width, img.height) == (W, H)


def test_an_unreadable_photo_is_reported_not_raised_raw():
    with pytest.raises(PostcardError, match="could not be read"):
        render(a_card(), b"this is not an image")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_a_long_headline_shrinks_the_photo_rather_than_running_into_the_footer():
    """The footer carries the contact details and must stay legible, so the
    space left after the text is what the picture gets."""
    long_card = a_card(
        headline="SUSTAINABLE PACKAGING FOR EVERY MEAL\n|AND EVERY SINGLE OCCASION",
        subhead="A much longer supporting line that wraps across several lines "
                "to push the layout as far as it will go before breaking.",
    )
    img = opened(render(long_card, photo_bytes()))

    # The footer band should still be its own solid dark colour at the bottom.
    band = [img.getpixel((x, H - 20)) for x in range(100, W - 100, 120)]
    assert len({p for p in band}) == 1, f"footer is not a clean band: {set(band)}"


def test_the_footer_is_drawn_for_every_card():
    img = opened(render(a_card(), photo_bytes()))
    assert img.getpixel((10, H - 10)) == img.getpixel((W - 10, H - 10))


def test_more_features_than_fit_are_dropped_not_overflowed():
    """Four is what the grid holds. A fifth should be left out rather than
    drawn over the footer."""
    many = a_card(features=[Feature(f"Feature {i}", "detail", "leaf") for i in range(8)])
    img = opened(render(many, photo_bytes()))
    band = [img.getpixel((x, H - 20)) for x in range(100, W - 100, 120)]
    assert len({p for p in band}) == 1


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def test_the_fonts_are_bundled_rather_than_taken_from_the_host():
    """Render has no Arial and Windows has no DejaVu. A card whose typeface
    changes between a laptop and production is worse than one that fails."""
    fonts = Path("app/static/fonts")
    assert (fonts / "DejaVuSans.ttf").exists()
    assert (fonts / "DejaVuSans-Bold.ttf").exists()
    assert (fonts / "LICENSE_DEJAVU").exists(), "the licence travels with the fonts"


def test_rendering_needs_no_network_and_no_api_key(monkeypatch):
    """The whole point of drawing the card: it costs nothing and cannot run
    out of allowance half way through a day."""
    import httpx

    def forbidden(*a, **k):
        raise AssertionError("the card renderer made a network call")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    assert render(a_card(), photo_bytes())
