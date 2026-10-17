"""Encryption for data that must not be readable if the database leaks.

Scope: LinkedIn access tokens (plan.md section 13). Fernet is authenticated
encryption, so a tampered ciphertext raises rather than decrypting to something
subtly different.

ENCRYPTION_KEY is a Fernet key. Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotating it invalidates every stored token, which logs every user out and forces
them to reconnect LinkedIn. That is recoverable, but it is not free -- treat the
key as permanent for a deployment.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class DecryptionError(Exception):
    """Ciphertext was corrupt, tampered with, or encrypted under another key."""


# Set by tests; production always reads the configured key.
_key_override: str | None = None
_fernet_cached: Fernet | None = None


def _fernet() -> Fernet:
    global _fernet_cached
    if _fernet_cached is None:
        key = _key_override or get_settings().encryption_key
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY is not set. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        try:
            _fernet_cached = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
            ) from exc
    return _fernet_cached


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as exc:
        raise DecryptionError("could not decrypt value") from exc


def encrypt_optional(plaintext: str | None) -> str | None:
    """Empty and missing both store as NULL -- an empty token is not a token."""
    if not plaintext:
        return None
    return encrypt(plaintext)


def decrypt_optional(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    return decrypt(ciphertext)
