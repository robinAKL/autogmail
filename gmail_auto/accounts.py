"""Sign-in by email code. The code lives in the login session, so it works without Vercel KV."""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.token_store import kv_get, kv_set, storage_ready

_KEY = "autogmail:accounts"
_CODE_TTL = 10 * 60
_RESEND_WAIT = 60
_MAX_TRIES = 5
STALE_AFTER = 6 * 24 * 60 * 60


def normalize_email(value: str) -> str:
    return value.strip().lower()


def start_verification(email: str, purpose: str, pending: dict | None = None) -> tuple[str, dict]:
    email = normalize_email(email)
    purpose = _purpose(purpose)
    _ensure_allowed(email)
    _check_known_user(email, purpose)
    current = pending if isinstance(pending, dict) else {}
    sent_at = float(current.get("sent_at") or 0)
    same = current.get("email") == email and current.get("purpose") == purpose
    if same and sent_at and time.time() - sent_at < _RESEND_WAIT:
        raise ConfigError("A code was just sent. Wait a minute before asking for another.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    return code, {
        "email": email,
        "purpose": purpose,
        "salt": salt,
        "hash": _digest(salt, code),
        "sent_at": time.time(),
        "expires": time.time() + _CODE_TTL,
        "tries": 0,
    }


def finish_verification(email: str, purpose: str, code: str, pending: dict | None = None) -> str:
    email = normalize_email(email)
    purpose = _purpose(purpose)
    entered = "".join(ch for ch in code if ch.isdigit())
    current = pending if isinstance(pending, dict) else {}
    if current.get("email") != email or current.get("purpose") != purpose:
        raise ConfigError("Ask for a code first.")
    if time.time() > float(current.get("expires") or 0):
        current.clear()
        raise ConfigError("That code has expired. Ask for a new one.")
    if int(current.get("tries") or 0) >= _MAX_TRIES:
        current.clear()
        raise ConfigError("That code is no longer valid. Ask for a new one.")
    salt = str(current.get("salt") or "")
    expected = str(current.get("hash") or "")
    actual = _digest(salt, entered)
    if len(actual) != len(expected) or not secrets.compare_digest(actual, expected):
        current["tries"] = int(current.get("tries") or 0) + 1
        raise ConfigError("That code is not right.")
    current.clear()
    _remember_user(email, purpose)
    return email


def _check_known_user(email: str, purpose: str) -> None:
    if not _can_store_users():
        return
    users = _load().get("users") or {}
    if purpose == "register" and email in users:
        raise ConfigError("This email is already registered. Sign in instead.")
    if purpose == "login" and email not in users:
        raise ConfigError("This email is not registered yet.")


def _remember_user(email: str, purpose: str) -> None:
    if not _can_store_users():
        return
    data = _load()
    now = time.time()
    users = data.setdefault("users", {})
    if purpose == "register" or email not in users:
        users[email] = {"created_at": now, "last_login": now}
    else:
        record = users.get(email)
        if isinstance(record, dict):
            record["last_login"] = now
    _save(data)


def mark_gmail_authorized(email: str, at: float | None = None) -> None:
    data = _load()
    data["gmail"] = {"email": normalize_email(email) if email else "", "authorized_at": time.time() if at is None else at}
    _save(data)


def gmail_status() -> dict:
    data = _load()
    gmail = data.get("gmail") if isinstance(data.get("gmail"), dict) else {}
    authorized_at = float(gmail.get("authorized_at") or 0)
    return {
        "email": str(gmail.get("email") or ""),
        "authorized_at": authorized_at,
        "authorized_text": _when(authorized_at),
        "stale": bool(authorized_at) and time.time() - authorized_at > STALE_AFTER,
    }


def _purpose(value: str) -> str:
    if value not in {"login", "register"}:
        raise ConfigError("Choose sign in or register.")
    return value


def _ensure_allowed(email: str) -> None:
    raw = os.getenv("ALLOWED_EMAILS", "").strip()
    if not raw:
        return
    allowed = {normalize_email(item) for item in raw.split(",") if item.strip()}
    if email not in allowed:
        raise ConfigError("This email is not allowed to sign in.")


def _digest(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def _when(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _load() -> dict:
    if os.environ.get("VERCEL") or storage_ready():
        if not storage_ready():
            return {"users": {}, "otp": {}, "gmail": {}}
        raw = kv_get(_KEY)
        return _parse(raw)
    path = data_dir(create=False) / "accounts.json"
    if not path.exists():
        return {"users": {}, "otp": {}, "gmail": {}}
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {"users": {}, "otp": {}, "gmail": {}}


def _parse(raw) -> dict:
    if not raw:
        return {"users": {}, "otp": {}, "gmail": {}}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"users": {}, "otp": {}, "gmail": {}}
    if not isinstance(data, dict):
        return {"users": {}, "otp": {}, "gmail": {}}
    data.setdefault("users", {})
    data.setdefault("otp", {})
    data.setdefault("gmail", {})
    return data


def _can_store_users() -> bool:
    if os.environ.get("VERCEL"):
        return storage_ready()
    return True


def _save(data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if os.environ.get("VERCEL") or storage_ready():
        if not storage_ready():
            return
        kv_set(_KEY, raw)
        return
    atomic_write(data_dir() / "accounts.json", raw + "\n")
