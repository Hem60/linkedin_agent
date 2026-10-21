"""Generated post images -- Cloudflare Workers AI.

No network: httpx is replaced. These cover the prompt actually sent, the size
actually asked for, and the JSON envelope Workers AI answers with -- which is
where this went wrong the first time round.
"""

from __future__ import annotations

import base64

import pytest

from app import imagegen

# A few JPEG magic bytes are enough; nothing here decodes the image.
PIXEL = bytes.fromhex("ffd8ffdb")


def ok_body(data: bytes = PIXEL) -> dict:
    return {"success": True, "errors": [], "messages": [],
            "result": {"image": base64.b64encode(data).decode()}}


# Distinct from None, which means "use the default success body".
NOT_JSON = object()


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = ok_body() if payload is None else payload
        self.text = text

    def json(self):
        if self._payload is NOT_JSON:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(imagegen, "get_settings", lambda: type("S", (), {
        "cloudflare_account_id": "acct", "cloudflare_api_token": "tok",
        "image_model": "@cf/leonardo/lucid-origin",
        "image_width": 1024, "image_height": 576,
        "image_steps": 20, "image_guidance": 4.5,
        "require": lambda self, *a: None})())


def capture(monkeypatch, response=None):
    """Replace the POST and hand back the call it received."""
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen.update(kw)
        return response or FakeResponse()

    monkeypatch.setattr(imagegen.httpx, "post", fake_post)
    return seen


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def test_a_generated_image_comes_back_as_bytes(configured, monkeypatch):
    capture(monkeypatch)
    assert imagegen.generate("a quiet workshop") == PIXEL


def test_the_subject_leads_the_prompt(configured, monkeypatch):
    """Style first and the model renders the style, losing the subject."""
    seen = capture(monkeypatch)
    imagegen.generate("a harbour at dawn")
    assert seen["json"]["prompt"].startswith("a harbour at dawn.")


def test_the_prompt_never_asks_for_the_absence_of_anything(configured, monkeypatch):
    """A diffusion model with no negative_prompt reads a forbidden noun as a
    requested one. The first attempt at this said "no text, no letters" and got
    a garbled headline and an invented logo for its trouble."""
    seen = capture(monkeypatch)
    imagegen.generate("a developer terminal")
    style = seen["json"]["prompt"].lower().split(".", 1)[1]
    for negation in ("no ", "without", "avoid", "not "):
        assert negation not in style, f"{negation!r} in styling: {style}"


def test_a_typed_prompt_is_sent_exactly_as_written(configured, monkeypatch):
    """Someone typing a prompt has already said what they want. Appending the
    house style to it argues with them: "a perfect photo for the peanut" plus
    portrait styling returned a studio portrait of a dog, Peanut being a common
    dog name and the styling having asked for that kind of picture."""
    seen = capture(monkeypatch)
    typed = "make a perfect photo for the peanut"
    imagegen.generate(typed, styled=False)
    assert seen["json"]["prompt"] == typed


def test_the_drafts_own_query_still_gets_the_house_style(configured, monkeypatch):
    """photo_query arrives as a bare noun phrase from the writing model and
    would come back looking like clip art without it."""
    seen = capture(monkeypatch)
    imagegen.generate("server rack data center", styled=True)
    prompt = seen["json"]["prompt"]
    assert prompt.startswith("server rack data center.")
    assert "editorial photograph" in prompt


def test_the_image_is_landscape_and_sized_from_settings(configured, monkeypatch):
    """The feed crops tall images, and tiles are charged by area: 1024x576 is
    2.25 tiles against 4 for a square, which roughly doubles how many fit in a
    day's free allowance."""
    seen = capture(monkeypatch)
    imagegen.generate("anything")
    body = seen["json"]
    assert body["width"] == 1024
    assert body["height"] == 576
    assert body["width"] > body["height"]


def test_the_model_is_taken_from_settings_not_hard_coded(configured, monkeypatch):
    """So a better model can be put in from the dashboard without a redeploy."""
    seen = capture(monkeypatch)
    imagegen.generate("anything")
    assert seen["url"].endswith("/ai/run/@cf/leonardo/lucid-origin")


def test_the_subject_is_capped_and_whitespace_collapsed(configured, monkeypatch):
    seen = capture(monkeypatch)
    imagegen.generate("  spaced   out  " + "x" * 400)
    subject = seen["json"]["prompt"].split(".")[0]
    assert subject.startswith("spaced out ")
    assert len(subject) <= imagegen.MAX_SUBJECT_CHARS


def test_an_empty_subject_is_refused_rather_than_silently_skipped(configured, monkeypatch):
    seen = capture(monkeypatch)
    with pytest.raises(imagegen.ImageGenError):
        imagegen.generate("   ")
    assert "url" not in seen, "no request should be made"


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,match", [
    (401, "rejected"), (403, "rejected"), (429, "allowance"), (500, "500"),
])
def test_api_errors_are_reported_clearly(configured, monkeypatch, status, match):
    capture(monkeypatch, FakeResponse(status, {}))
    with pytest.raises(imagegen.ImageGenError, match=match):
        imagegen.generate("anything")


def test_running_out_of_allowance_points_at_the_other_source(configured, monkeypatch):
    """There is a second way to get a picture, and the message should say so
    rather than leaving the page looking broken for the rest of the day."""
    capture(monkeypatch, FakeResponse(429, {}))
    with pytest.raises(imagegen.ImageGenError, match="Search for a photo"):
        imagegen.generate("anything")


def test_a_200_that_says_it_failed_is_still_a_failure(configured, monkeypatch):
    """Workers AI answers 200 with success:false for a refused prompt."""
    capture(monkeypatch, FakeResponse(200, {
        "success": False, "errors": [{"message": "prompt was filtered"}]}))
    with pytest.raises(imagegen.ImageGenError, match="filtered"):
        imagegen.generate("anything")


def test_a_reply_with_no_image_is_an_error_not_empty_bytes(configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, {"success": True, "result": {}}))
    with pytest.raises(imagegen.ImageGenError, match="no image"):
        imagegen.generate("anything")


def test_unreadable_base64_is_reported_not_raised_raw(configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, {
        "success": True, "result": {"image": "not base64!!"}}))
    with pytest.raises(imagegen.ImageGenError, match="not readable"):
        imagegen.generate("anything")


def test_a_non_json_reply_is_reported_clearly(configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, NOT_JSON))
    with pytest.raises(imagegen.ImageGenError, match="could not read"):
        imagegen.generate("anything")
