"""Token encryption -- plan.md section 13.

LinkedIn access tokens let anyone post as the member. They are encrypted at rest
with Fernet, which is authenticated: a modified ciphertext fails to decrypt
rather than yielding altered plaintext.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app import security


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    """Use a dedicated key so tests never depend on the developer's .env."""
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(security, "_fernet_cached", None, raising=False)
    monkeypatch.setattr(security, "_key_override", key, raising=False)
    yield
    monkeypatch.setattr(security, "_fernet_cached", None, raising=False)


def test_round_trip():
    token = "AQV8nd2kXITHELmWblJigbHEuoFdfRhOwGA0QNnumBI8XOVSs0Ht"
    assert security.decrypt(security.encrypt(token)) == token


def test_ciphertext_does_not_contain_the_plaintext():
    token = "super-secret-linkedin-token"
    assert token not in security.encrypt(token)


def test_same_plaintext_encrypts_differently_each_time():
    """Fernet includes a random IV, so identical tokens must not look identical
    in the database -- otherwise rows leak which users share a value."""
    a = security.encrypt("same")
    b = security.encrypt("same")
    assert a != b
    assert security.decrypt(a) == security.decrypt(b) == "same"


def test_tampered_ciphertext_is_rejected():
    blob = security.encrypt("token")
    tampered = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
    with pytest.raises(security.DecryptionError):
        security.decrypt(tampered)


def test_garbage_is_rejected():
    with pytest.raises(security.DecryptionError):
        security.decrypt("not-a-fernet-token")


def test_a_different_key_cannot_decrypt(monkeypatch):
    blob = security.encrypt("token")
    monkeypatch.setattr(security, "_fernet_cached", None, raising=False)
    monkeypatch.setattr(security, "_key_override", Fernet.generate_key().decode())
    with pytest.raises(security.DecryptionError):
        security.decrypt(blob)


def test_empty_values_round_trip_as_none():
    assert security.encrypt_optional(None) is None
    assert security.encrypt_optional("") is None
    assert security.decrypt_optional(None) is None


def test_optional_round_trip():
    blob = security.encrypt_optional("t")
    assert blob is not None
    assert security.decrypt_optional(blob) == "t"
