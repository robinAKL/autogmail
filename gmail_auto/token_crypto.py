"""Symmetric encryption for stored OAuth tokens in SaaS mode."""

import base64
import hashlib
import os

from gmail_auto.errors import ConfigError

_PREFIX = "enc:v1:"


def _saas_enabled() -> bool:
    try:
        from gmail_auto.tenant import saas_enabled
    except ImportError:
        return False
    return bool(saas_enabled())


def _raw_key() -> str:
    return os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()


def encryption_ready() -> bool:
    return bool(_raw_key())


def _fernet():
    raw = _raw_key()
    if not raw:
        raise ConfigError("TOKEN_ENCRYPTION_KEY is not configured.")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise ConfigError("cryptography is required for token encryption.") from exc
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(value: str) -> str:
    if not value:
        return value
    if not encryption_ready():
        if _saas_enabled():
            raise ConfigError("TOKEN_ENCRYPTION_KEY is required in SaaS mode.")
        return value
    if value.startswith(_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_token(value: str) -> str:
    if not value:
        return value
    if not value.startswith(_PREFIX):
        if _saas_enabled():
            raise ConfigError("Stored token is not encrypted.")
        return value
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ConfigError("Stored token could not be decrypted. Check TOKEN_ENCRYPTION_KEY.") from exc
