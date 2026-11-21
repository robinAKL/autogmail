"""Revocable SaaS sign-in sessions stored separately from mailbox grants."""

import json
import os
import re
import secrets
import time

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.token_store import kv_delete, kv_get, kv_set_with_ttl, storage_ready

_TTL = 12 * 60 * 60
_SESSION_ID = re.compile(r"[0-9a-f]{64}")


def create(user_id: str) -> str:
    session_id = secrets.token_hex(32)
    value = json.dumps({"user_id": user_id, "expires": time.time() + _TTL})
    if storage_ready():
        kv_set_with_ttl(_key(session_id), value, _TTL)
    else:
        _require_local()
        atomic_write(_path(session_id), value)
    return session_id


def is_active(session_id: object, user_id: str) -> bool:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return False
    try:
        if storage_ready():
            raw = kv_get(_key(session_id))
        else:
            _require_local()
            raw = _path(session_id).read_text(encoding="utf-8")
        data = json.loads(raw) if raw else {}
        return data.get("user_id") == user_id and time.time() < data.get("expires", 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def revoke(session_id: object) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return
    if storage_ready():
        kv_delete(_key(session_id))
    else:
        _require_local()
        _path(session_id).unlink(missing_ok=True)


def _key(session_id: str) -> str:
    return f"autogmail:platform:session:{session_id}"


def _path(session_id: str):
    return data_dir(create=False, platform=True) / "saas_sessions" / f"{session_id}.json"


def _require_local() -> None:
    if os.environ.get("VERCEL"):
        raise ConfigError("Sign-in is temporarily unavailable.")
