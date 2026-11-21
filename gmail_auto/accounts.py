"""Sign-in by email code. The code's hash stays in server storage, not in the browser cookie."""

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


def start_verification(email: str, purpose: str, pending: dict | None = None, remote: str = "") -> str:
    """Send a new code. `pending` is ignored; older callers passed the cookie copy."""
    del pending
    email = normalize_email(email)
    purpose = _purpose(purpose)
    _ensure_allowed(email, purpose)
    _check_known_user(email, purpose)
    data = _load()
    _note_remote(data, remote)
    current = (data.get("otp") or {}).get(email) or {}
    sent_at = float(current.get("sent_at") or 0)
    same = current.get("email") == email and current.get("purpose") == purpose
    if same and sent_at and time.time() - sent_at < _RESEND_WAIT:
        raise ConfigError("A code was just sent. Wait a minute before asking for another.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    data.setdefault("otp", {})[email] = {
        "email": email,
        "purpose": purpose,
        "salt": salt,
        "hash": _digest(salt, code),
        "sent_at": time.time(),
        "expires": time.time() + _CODE_TTL,
        "tries": 0,
    }
    _save(data)
    return code


def finish_verification(email: str, purpose: str, code: str, pending: dict | None = None) -> str:
    del pending
    email = normalize_email(email)
    purpose = _purpose(purpose)
    entered = "".join(ch for ch in code if ch.isdigit())
    data = _load()
    current = (data.get("otp") or {}).get(email) or {}
    if current.get("email") != email or current.get("purpose") != purpose:
        raise ConfigError("Ask for a code first.")
    if time.time() > float(current.get("expires") or 0):
        clear_verification(email)
        raise ConfigError("That code has expired. Ask for a new one.")
    if int(current.get("tries") or 0) >= _MAX_TRIES:
        clear_verification(email)
        raise ConfigError("That code is no longer valid. Ask for a new one.")
    salt = str(current.get("salt") or "")
    expected = str(current.get("hash") or "")
    actual = _digest(salt, entered)
    if len(actual) != len(expected) or not secrets.compare_digest(actual, expected):
        current["tries"] = int(current.get("tries") or 0) + 1
        data.setdefault("otp", {})[email] = current
        _save(data)
        raise ConfigError("That code is not right.")
    clear_verification(email)
    _remember_user(email, purpose)
    return email


def clear_verification(email: str) -> None:
    email = normalize_email(email)
    data = _load()
    otp = data.get("otp") or {}
    if email in otp:
        otp.pop(email, None)
        data["otp"] = otp
        _save(data)


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


def _ensure_allowed(email: str, purpose: str) -> None:
    if os.environ.get("VERCEL") and not storage_ready():
        raise ConfigError("Sign-in needs storage. Connect Redis in Vercel, then redeploy.")
    raw = os.getenv("ALLOWED_EMAILS", "").strip()
    if os.getenv('WORKSPACE_HOSTED') and not raw:
        raise ConfigError('Hosted workspaces require ALLOWED_EMAILS. Ask the owner to configure the sign-in allowlist.')
    if raw:
        allowed = {normalize_email(item) for item in raw.split(",") if item.strip()}
        if email not in allowed:
            raise ConfigError("This email is not allowed to sign in.")
        return
    if purpose == "register" and _can_store_users():
        users = _load().get("users") or {}
        if users and email not in users:
            raise ConfigError("This site already has an owner. Sign in with that email.")


def _note_remote(data: dict, remote: str) -> None:
    remote = (remote or "").strip()[:80]
    if not remote:
        return
    now = time.time()
    hits = data.setdefault("otp_hits", {})
    recent = [stamp for stamp in hits.get(remote) or [] if now - float(stamp) < 3600]
    if len(recent) >= 8:
        raise ConfigError("Too many codes were sent from this network. Wait and try again.")
    recent.append(now)
    hits[remote] = recent[-8:]


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
