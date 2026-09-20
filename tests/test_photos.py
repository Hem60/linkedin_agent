"""Pexels search and object storage -- plan.md section 11.

No network: httpx is replaced. These cover parsing, path namespacing and the
download cap, which is where the mistakes would be.
"""

from __future__ import annotations

import pytest

from app import photos, storage


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


PEXELS_PAYLOAD = {
    "photos": [
        {
            "id": 1234,
            "width": 4000,
            "height": 2667,
            "url": "https://www.pexels.com/photo/server-room-1234/",
            "photographer": "Ada Lovelace",
            "photographer_url": "https://www.pexels.com/@ada",
            "alt": "A dim server room",
            "src": {
                "original": "https://images.pexels.com/photos/1234/original.jpg",
                "large": "https://images.pexels.com/photos/1234/large.jpg",
                "medium": "https://images.pexels.com/photos/1234/medium.jpg",
            },
        }
    ]
}


@pytest.fixture
def pexels_key(monkeypatch):
    monkeypatch.setattr(photos, "get_settings", lambda: type(
        "S", (), {"pexels_api_key": "key", "require": lambda self, *a: None})())


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_parses_a_photo(pexels_key, monkeypatch):
    monkeypatch.setattr(photos.httpx, "get", lambda *a, **k: FakeResponse(200, PEXELS_PAYLOAD))
    photo = photos.search("server room")[0]
    assert photo.id == 1234
    assert photo.url.endswith("large.jpg")
    assert photo.preview_url.endswith("medium.jpg")
    assert photo.photographer == "Ada Lovelace"
    assert photo.alt == "A dim server room"


def test_credit_line_is_ready_for_display(pexels_key, monkeypatch):
    """The Pexels licence asks for credit; it must travel with the photo."""
    monkeypatch.setattr(photos.httpx, "get", lambda *a, **k: FakeResponse(200, PEXELS_PAYLOAD))
    assert photos.search("x")[0].credit == "Photo by Ada Lovelace on Pexels"


def test_search_asks_for_landscape_only(pexels_key, monkeypatch):
    """LinkedIn crops tall images, so portrait candidates waste the user's time."""
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params or {})
        return FakeResponse(200, PEXELS_PAYLOAD)

    monkeypatch.setattr(photos.httpx, "get", fake_get)
    photos.search("server room")
    assert seen["orientation"] == "landscape"


def test_an_empty_query_does_not_call_the_api(pexels_key, monkeypatch):
    monkeypatch.setattr(photos.httpx, "get", lambda *a, **k: pytest.fail("should not call"))
    assert photos.search("   ") == []


def test_no_matches_is_an_empty_list_not_an_error(pexels_key, monkeypatch):
    """A post without an image is still a post."""
    monkeypatch.setattr(photos.httpx, "get", lambda *a, **k: FakeResponse(200, {"photos": []}))
    assert photos.search("asdkjhasd") == []


@pytest.mark.parametrize("status,match", [
    (401, "key was rejected"),
    (429, "rate limit"),
    (500, "returned 500"),
])
def test_api_errors_are_reported_clearly(pexels_key, monkeypatch, status, match):
    monkeypatch.setattr(photos.httpx, "get", lambda *a, **k: FakeResponse(status))
    with pytest.raises(photos.PhotoSearchError, match=match):
        photos.search("x")


def test_a_malformed_photo_does_not_crash_the_search(pexels_key, monkeypatch):
    monkeypatch.setattr(photos.httpx, "get",
                        lambda *a, **k: FakeResponse(200, {"photos": [{}]}))
    photo = photos.search("x")[0]
    assert photo.photographer == "Unknown"


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
