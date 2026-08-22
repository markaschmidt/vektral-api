"""Encrypt GitHub access tokens at rest (Fernet when key set)."""

from __future__ import annotations

import base64
import hashlib
import logging

from app.config import get_settings

logger = logging.getLogger("vektral.crypto")


def _fernet():
    settings = get_settings()
    raw = settings.token_encryption_key
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet

        # Accept raw Fernet key or derive from arbitrary secret.
        try:
            return Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw)
        except Exception:  # noqa: BLE001
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            key = base64.urlsafe_b64encode(digest)
            return Fernet(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fernet unavailable: %s", exc)
        return None


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _fernet()
    if f is None:
        # Dev fallback — prefix so we never confuse with ciphertext
        return f"plain:{plaintext}"
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("plain:"):
        return stored[len("plain:") :]
    f = _fernet()
    if f is None:
        logger.warning("Cannot decrypt token without TOKEN_ENCRYPTION_KEY")
        return ""
    try:
        return f.decrypt(stored.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Token decrypt failed: %s", exc)
        return ""
