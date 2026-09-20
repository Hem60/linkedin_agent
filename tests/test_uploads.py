"""Upload validation -- plan.md section 11 (F15).

An uploaded file is untrusted input. These tests care less about happy paths
than about what a hostile or careless file can do: lie about its type, decode
to an enormous bitmap, or carry the uploader's GPS coordinates into a public
post.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app import uploads
from app.uploads import ImageRejected, prepare_upload


def make(fmt="JPEG", size=(1200, 800), colour=(10, 102, 194), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Accepting what we should
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_accepted_formats_round_trip(fmt):
    out = prepare_upload(make(fmt))
    assert out.format == fmt
    assert out.width == 1200 and out.height == 800
    assert Image.open(io.BytesIO(out.data)).format == fmt


def test_content_type_matches_the_real_format():
    assert prepare_upload(make("PNG")).content_type == "image/png"


def test_a_square_image_is_fine():
    assert not prepare_upload(make(size=(1000, 1000))).warnings


# ---------------------------------------------------------------------------
# Rejecting what we should
# ---------------------------------------------------------------------------

def test_empty_file_is_rejected():
    with pytest.raises(ImageRejected, match="empty"):
        prepare_upload(b"")


def test_a_text_file_renamed_as_an_image_is_rejected():
    """The extension is a claim; the bytes are the fact."""
    with pytest.raises(ImageRejected, match="doesn't look like an image"):
        prepare_upload(b"#!/bin/sh\nrm -rf /\n", "innocent.jpg")


def test_truncated_image_data_is_rejected():
    with pytest.raises(ImageRejected):
        prepare_upload(make()[:200])


def test_an_oversized_file_is_rejected_before_decoding():
    with pytest.raises(ImageRejected, match="limit is 5 MB"):
        prepare_upload(b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024))


def test_an_unsupported_image_format_is_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (400, 400)).save(buf, format="BMP")
    with pytest.raises(ImageRejected, match="aren't supported"):
        prepare_upload(buf.getvalue())


def test_a_tiny_image_is_rejected():
    with pytest.raises(ImageRejected, match="at least 200px"):
        prepare_upload(make(size=(150, 150)))


def test_a_decompression_bomb_cannot_exhaust_memory(monkeypatch):
    """A small file that decodes to an enormous bitmap must not be decoded."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ImageRejected):
        prepare_upload(make(size=(1200, 800)))


# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------

def test_large_images_are_resized_to_the_long_edge():
    out = prepare_upload(make(size=(4000, 3000)))
    assert max(out.width, out.height) == uploads.MAX_EDGE
    assert out.width / out.height == pytest.approx(4000 / 3000, rel=0.01)
    assert any("Resized" in w for w in out.warnings)


def test_a_tall_image_warns_about_cropping():
    out = prepare_upload(make(size=(600, 1200)))
    assert any("crops tall images" in w for w in out.warnings)


def test_transparency_is_flattened_onto_white_for_jpeg():
    """Compositing avoids the black background a naive RGB convert produces."""
    buf = io.BytesIO()
    Image.new("RGBA", (400, 400), (255, 0, 0, 0)).save(buf, format="PNG")
    png = prepare_upload(buf.getvalue())
    assert png.format == "PNG"          # PNG keeps its alpha

    rgb = Image.open(io.BytesIO(uploads._encode(Image.open(io.BytesIO(buf.getvalue())), "JPEG")[0]))
    assert rgb.getpixel((5, 5))[0] > 200, "transparent areas should become white"


def test_a_greyscale_image_is_handled():
    assert prepare_upload(make(mode="L", colour=128)).width == 1200


# ---------------------------------------------------------------------------
# Privacy: EXIF must not survive
# ---------------------------------------------------------------------------

def _jpeg_with_gps() -> bytes:
    import piexif  # optional; skipped when unavailable

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (1, 2, 3)).save(buf, format="JPEG")
    exif = {
        "0th": {}, "Exif": {}, "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((23, 1), (1, 1), (0, 1)),
        }, "1st": {}, "thumbnail": None,
    }
    out = io.BytesIO()
    piexif.insert(piexif.dump(exif), buf.getvalue(), out)
    return out.getvalue()


def test_gps_metadata_does_not_survive_the_upload():
    """Publishing someone's holiday photo should not publish their address."""
    piexif = pytest.importorskip("piexif")
    raw = _jpeg_with_gps()
    assert piexif.load(raw)["GPS"], "fixture should start with GPS data"
    cleaned = prepare_upload(raw)
    assert not piexif.load(cleaned.data).get("GPS")


def test_exif_is_dropped_even_without_the_optional_library():
    """Re-encoding from pixels is what removes metadata; no library needed."""
    buf = io.BytesIO()
    img = Image.new("RGB", (800, 600), (1, 2, 3))
    img.save(buf, format="JPEG", comment=b"secret note")
    out = prepare_upload(buf.getvalue())
    assert b"secret note" not in out.data


def test_trailing_data_after_the_image_is_discarded():
    """A polyglot file with a payload appended must not keep the payload."""
    raw = make("PNG") + b"<?php system($_GET['c']); ?>"
    assert b"<?php" not in prepare_upload(raw).data


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_size_and_extension_are_reported_for_the_ui():
    out = prepare_upload(make("PNG"))
    assert out.extension == "png"
    assert out.size_kb > 0
