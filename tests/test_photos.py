"""Image generation and object storage -- plan.md section 11.

No network: httpx is replaced. These cover the prompt the model is actually
sent, the JSON envelope Workers AI answers with, path namespacing and the
download cap -- which is where the mistakes would be.
"""

from __future__ import annotations

import base64

import pytest

from app import photos, storage

# A one-pixel JPEG is enough: nothing here decodes it, only moves it around.
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
def cf_configured(monkeypatch):
    monkeypatch.setattr(photos, "get_settings", lambda: type("S", (), {
        "cloudflare_account_id": "acct", "cloudflare_api_token": "tok",
        "image_model": "@cf/black-forest-labs/flux-1-schnell", "image_steps": 4,
        "require": lambda self, *a: None})())


def capture(monkeypatch, response=None):
    """Replace the POST and hand back the call it received."""
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen.update(kw)
        return response or FakeResponse()

    monkeypatch.setattr(photos.httpx, "post", fake_post)
    return seen


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_a_generated_image_comes_back_as_bytes(cf_configured, monkeypatch):
    capture(monkeypatch)
    assert photos.generate("a quiet workshop") == PIXEL


def test_the_prompt_never_asks_for_the_absence_of_anything(cf_configured, monkeypatch):
    """Schnell has no negation and Workers AI exposes no negative_prompt, so
    "no text" is read as "text". Tested against the live model, that prompt
    produced a gibberish headline and an invented logo. The styling must say
    only what to draw."""
    seen = capture(monkeypatch)
    photos.generate("a developer terminal")
    prompt = seen["json"]["prompt"].lower()
    style = prompt.split(".", 1)[1] if "." in prompt else prompt
    for negation in ("no ", "without", "avoid", "not "):
        assert negation not in style, f"{negation!r} in styling: {style}"


def test_the_subject_leads_the_prompt(cf_configured, monkeypatch):
    """Style first and Schnell renders the styling and forgets the subject."""
    seen = capture(monkeypatch)
    photos.generate("a harbour at dawn")
    assert seen["json"]["prompt"].startswith("a harbour at dawn.")


def test_the_subject_is_capped_and_whitespace_collapsed(cf_configured, monkeypatch):
    seen = capture(monkeypatch)
    photos.generate("  spaced   out  " + "x" * 400)
    subject = seen["json"]["prompt"].split(".")[0]
    assert subject.startswith("spaced out ")
    assert len(subject) <= photos.MAX_SUBJECT_CHARS


def test_an_empty_subject_is_refused_rather_than_silently_skipped(cf_configured, monkeypatch):
    """The caller asked for an image; returning none would look like a bug at
    the other end."""
    called = capture(monkeypatch)
    with pytest.raises(photos.ImageGenError):
        photos.generate("   ")
    assert "url" not in called, "no request should be made"


def test_no_size_is_sent(cf_configured, monkeypatch):
    """flux-1-schnell accepts prompt and steps only. Sending width or height
    is a 400 from Workers AI, so every generation would fail -- which is how
    this was found."""
    seen = capture(monkeypatch)
    photos.generate("anything")
    assert set(seen["json"]) == {"prompt", "steps"}, seen["json"]


@pytest.mark.parametrize("status,match", [
    (401, "rejected"), (403, "rejected"), (429, "allowance"), (500, "500"),
])
def test_api_errors_are_reported_clearly(cf_configured, monkeypatch, status, match):
    capture(monkeypatch, FakeResponse(status, {}))
    with pytest.raises(photos.ImageGenError, match=match):
        photos.generate("anything")


def test_a_200_that_says_it_failed_is_still_a_failure(cf_configured, monkeypatch):
    """Workers AI answers 200 with success:false for a refused prompt, so the
    status line alone does not mean there is an image."""
    capture(monkeypatch, FakeResponse(200, {
        "success": False, "errors": [{"message": "prompt was filtered"}]}))
    with pytest.raises(photos.ImageGenError, match="filtered"):
        photos.generate("anything")


def test_a_reply_with_no_image_is_an_error_not_empty_bytes(cf_configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, {"success": True, "result": {}}))
    with pytest.raises(photos.ImageGenError, match="no image"):
        photos.generate("anything")


def test_unreadable_base64_is_reported_not_raised_raw(cf_configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, {
        "success": True, "result": {"image": "not base64!!"}}))
    with pytest.raises(photos.ImageGenError, match="not readable"):
        photos.generate("anything")


def test_a_non_json_reply_is_reported_clearly(cf_configured, monkeypatch):
    capture(monkeypatch, FakeResponse(200, NOT_JSON))
    with pytest.raises(photos.ImageGenError, match="could not read"):
        photos.generate("anything")


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

def test_paths_are_namespaced_per_user():
    """Plan section 13: nothing is shared between accounts."""
    assert storage.object_path(44, "jpg").startswith("users/44/posts/")


def test_paths_are_not_guessable():
    """Objects are served publicly for previews, so sequential names would let
    anyone walk another user's images."""
    a = storage.object_path(1, "jpg")
    b = storage.object_path(1, "jpg")
    assert a != b
    assert len(a.rsplit("/", 1)[1]) > 16


def test_path_keeps_the_extension():
    assert storage.object_path(7, "webp").endswith(".webp")


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------

class FakeDeleteResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_a_missing_object_is_treated_as_already_deleted():
    """Supabase reports a missing object as HTTP 400 with an inner 404, so the
    status code alone would make a retried deletion look like a failure."""
    resp = FakeDeleteResponse(400, {"statusCode": "404", "error": "not_found",
                                    "code": "NoSuchKey"})
    assert storage._already_gone(resp)


def test_a_plain_404_also_counts_as_gone():
    assert storage._already_gone(FakeDeleteResponse(404))


def test_a_real_failure_is_not_mistaken_for_a_missing_object():
    denied = FakeDeleteResponse(400, {"statusCode": "403", "error": "Unauthorized"})
    assert not storage._already_gone(denied)


def test_a_non_json_error_body_is_not_treated_as_gone():
    assert not storage._already_gone(FakeDeleteResponse(400, None, "<html>502</html>"))


def test_delete_is_silent_when_the_object_was_already_removed(monkeypatch, caplog):
    monkeypatch.setattr(storage, "_base", lambda: ("https://x", "key", "bucket"))
    monkeypatch.setattr(storage.httpx, "request", lambda *a, **k: FakeDeleteResponse(
        400, {"statusCode": "404", "error": "not_found"}))
    with caplog.at_level("WARNING"):
        storage.delete("users/1/posts/gone.jpg")
    assert not caplog.records, "a retried deletion should not log a failure"
