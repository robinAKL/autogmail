"""Sign-in by email code.

The challenge lives in the platform account store (not the browser cookie).
Login email is the account index. Business data uses a separate random user id,
so signing in with a mailbox that someone else has connected opens a new account.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.storage import workspace_operation
from gmail_auto.tenant import saas_enabled
from gmail_auto.token_store import kv_get, kv_set, storage_ready

# Platform namespace. Tenant storage must not prefix this key with a user id.
_KEY = "autogmail:platform:accounts"
_CODE_TTL = 10 * 60
_RESEND_WAIT = 60
_WINDOW = 15 * 60
_MAX_TRIES = 5
_EMAIL_SEND_MAX = 5
_IP_SEND_MAX = 20
_IP_FAIL_MAX = 30
STALE_AFTER = 6 * 24 * 60 * 60


def normalize_email(value: str) -> str:
    return value.strip().lower()


def start_verification(email: str, ip: str = "") -> str:
    """Create a one-time code and store only its hash. Returns the code to send."""
    email = _valid_email(email)
    ip = _clean_ip(ip)
    _ensure_allowed(email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(8)
    now = time.time()
    with workspace_operation("accounts", platform=True):
        data = _load()
        _prune(data, now)
        _check_send_limits(data, email, ip, now)
        data.setdefault("otp", {})[email] = {
            "salt": salt,
            "hash": _digest(salt, code),
            "sent_at": now,
            "expires": now + _CODE_TTL,
            "tries": 0,
        }
        _stamp(data, "sends", "email", email, now)
        if ip:
            _stamp(data, "sends", "ip", ip, now)
        _save(data)
    return code


def cancel_verification(email: str, code: str) -> None:
    """Drop a challenge when the platform could not send it. Rate-limit stamps stay."""
    try:
        email = _valid_email(email)
    except ConfigError:
        return
    with workspace_operation("accounts", platform=True):
        data = _load()
        otp = data.get("otp")
        current = otp.get(email) if isinstance(otp, dict) else None
        if current and secrets.compare_digest(_digest(current["salt"], code), current["hash"]):
            otp.pop(email)
            _save(data)


def finish_verification(email: str, code: str, ip: str = "") -> dict:
    """Consume a valid code and return the account. The same code cannot be used twice."""
    email = _valid_email(email)
    ip = _clean_ip(ip)
    entered = "".join(ch for ch in code if ch.isdigit())
    now = time.time()
    with workspace_operation("accounts", platform=True):
        data = _load()
        otp = data.get("otp") if isinstance(data.get("otp"), dict) else {}
        current = otp.get(email)
        _prune(data, now)
        if ip and len(_bucket(data, "fails", "ip", ip)) >= _IP_FAIL_MAX:
            _save(data)
            raise ConfigError("Too many attempts from this network. Wait a few minutes.")
        if not isinstance(current, dict):
            _save(data)
            raise ConfigError("Ask for a code first.")
        if now > float(current.get("expires") or 0):
            data.setdefault("otp", {}).pop(email, None)
            _save(data)
            raise ConfigError("That code has expired. Ask for a new one.")
        if int(current.get("tries") or 0) >= _MAX_TRIES:
            data["otp"].pop(email, None)
            _save(data)
            raise ConfigError("That code is no longer valid. Ask for a new one.")
        salt = str(current.get("salt") or "")
        expected = str(current.get("hash") or "")
        actual = _digest(salt, entered)
        if len(actual) != len(expected) or not secrets.compare_digest(actual, expected):
            current["tries"] = int(current.get("tries") or 0) + 1
            if ip:
                _stamp(data, "fails", "ip", ip, now)
            if current["tries"] >= _MAX_TRIES:
                data["otp"].pop(email, None)
                _save(data)
                raise ConfigError("That code is no longer valid. Ask for a new one.")
            _save(data)
            raise ConfigError("That code is not right.")
        data["otp"].pop(email, None)
        user_id = _remember_user(data, email, now)
        _save(data)
    return {"email": email, "user_id": user_id}


def _saas_scope() -> tuple[bool, str | None]:
    from gmail_auto.tenant import current_user_id, saas_enabled

    return saas_enabled(), current_user_id()


def mark_gmail_authorized(email: str, at: float | None = None) -> None:
    saas, uid = _saas_scope()
    payload = {"email": normalize_email(email) if email else "", "authorized_at": time.time() if at is None else at}
    if saas:
        if not uid:
            raise ConfigError("Sign in before saving a Gmail connection.")
        from gmail_auto.storage import save_doc

        save_doc("gmail_connection", payload)
        return
    with workspace_operation("accounts", platform=True):
        data = _load()
        data["gmail"] = payload
        _save(data)


def gmail_status() -> dict:
    saas, uid = _saas_scope()
    if saas:
        if not uid:
            return {"email": "", "authorized_at": 0.0, "authorized_text": "", "stale": False}
        from gmail_auto.storage import load_doc

        gmail = load_doc("gmail_connection", {})
        if not isinstance(gmail, dict):
            gmail = {}
    else:
        data = _load()
        gmail = data.get("gmail") if isinstance(data.get("gmail"), dict) else {}
    authorized_at = float(gmail.get("authorized_at") or 0)
    return {
        "email": str(gmail.get("email") or ""),
        "authorized_at": authorized_at,
        "authorized_text": _when(authorized_at),
        "stale": bool(authorized_at) and time.time() - authorized_at > STALE_AFTER,
    }


def _remember_user(data: dict, email: str, now: float) -> str:
    users = data.setdefault("users", {})
    record = users.get(email)
    if not isinstance(record, dict) or not str(record.get("user_id") or ""):
        user_id = secrets.token_hex(16)
        users[email] = {"user_id": user_id, "created_at": now, "last_login": now}
        return user_id
    record["last_login"] = now
    return str(record["user_id"])


def _check_send_limits(data: dict, email: str, ip: str, now: float) -> None:
    email_stamps = _bucket(data, "sends", "email", email)
    if email_stamps and now - email_stamps[-1] < _RESEND_WAIT:
        raise ConfigError("A code was just sent. Wait a minute before asking for another.")
    if len(email_stamps) >= _EMAIL_SEND_MAX:
        raise ConfigError("Too many codes for this email. Wait a few minutes.")
    if ip and len(_bucket(data, "sends", "ip", ip)) >= _IP_SEND_MAX:
        raise ConfigError("Too many codes from this network. Wait a few minutes.")


def _stamp(data: dict, kind: str, side: str, key: str, now: float) -> None:
    _bucket(data, kind, side, key).append(now)


def _bucket(data: dict, kind: str, side: str, key: str) -> list:
    group = data.setdefault(kind, {})
    if not isinstance(group, dict):
        group = {}
        data[kind] = group
    side_map = group.setdefault(side, {})
    if not isinstance(side_map, dict):
        side_map = {}
        group[side] = side_map
    stamps = side_map.get(key)
    if not isinstance(stamps, list):
        stamps = []
        side_map[key] = stamps
    return stamps


def _prune(data: dict, now: float) -> None:
    for kind in ("sends", "fails"):
        group = data.get(kind)
        if not isinstance(group, dict):
            continue
        for side_map in group.values():
            if not isinstance(side_map, dict):
                continue
            empty = []
            for key, stamps in side_map.items():
                if not isinstance(stamps, list):
                    empty.append(key)
                    continue
                kept = [float(item) for item in stamps if now - float(item) < _WINDOW]
                if kept:
                    side_map[key] = kept
                else:
                    empty.append(key)
            for key in empty:
                side_map.pop(key, None)
    otp = data.get("otp")
    if isinstance(otp, dict):
        stale = [key for key, item in otp.items() if not isinstance(item, dict) or now > float(item.get("expires") or 0)]
        for key in stale:
            otp.pop(key, None)


def _valid_email(value: str) -> str:
    email = normalize_email(value or "")
    if len(email) < 6 or len(email) > 254 or email.count("@") != 1:
        raise ConfigError("Enter your email address.")
    name, domain = email.split("@", 1)
    if not name or "." not in domain or any(ch.isspace() for ch in email):
        raise ConfigError("Enter your email address.")
    return email


def _clean_ip(value: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > 64 or any(ch.isspace() for ch in text):
        return ""
    return text


def _ensure_allowed(email: str) -> None:
    raw = os.getenv("ALLOWED_EMAILS", "").strip()
    if not raw:
        if os.environ.get("VERCEL") and not saas_enabled():
            raise ConfigError("Sign-in is temporarily unavailable.")
        return
    allowed = {normalize_email(item) for item in raw.split(",") if item.strip()}
    if email not in allowed:
        raise ConfigError("This email is not allowed to sign in.")


def _digest(salt: str, code: str) -> str:
    value = f"{salt}:{code}".encode("utf-8")
    if saas_enabled():
        secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
        if not secret:
            raise ConfigError("Sign-in is temporarily unavailable.")
        return hmac.digest(secret.encode("utf-8"), value, "sha256").hex()
    return hashlib.sha256(value).hexdigest()


def _when(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _blank() -> dict:
    return {"users": {}, "otp": {}, "sends": {"email": {}, "ip": {}}, "fails": {"ip": {}}, "gmail": {}}


def _account_path():
    filename = "saas_accounts.json" if saas_enabled() else "accounts.json"
    return data_dir(create=False, platform=True) / filename


def _account_key() -> str:
    return _KEY if saas_enabled() else "autogmail:accounts"


def _load() -> dict:
    if storage_ready():
        return _parse(kv_get(_account_key()))
    if saas_enabled() and os.environ.get("VERCEL"):
        raise ConfigError("Sign-in is temporarily unavailable.")
    try:
        return _parse(_account_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blank()
    except OSError as exc:
        raise ConfigError("Could not read sign-in records.") from exc


def _parse(raw) -> dict:
    data = _blank()
    if not raw:
        return data
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return data
    if not isinstance(loaded, dict):
        return data
    for key in ("users", "otp", "gmail"):
        if isinstance(loaded.get(key), dict):
            data[key] = loaded[key]
    for kind in ("sends", "fails"):
        group = loaded.get(kind)
        if isinstance(group, dict):
            data[kind] = group
    return data


def _save(data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    if storage_ready():
        kv_set(_account_key(), raw)
        return
    if saas_enabled() and os.environ.get("VERCEL"):
        raise ConfigError("Sign-in is temporarily unavailable.")
    try:
        atomic_write(_account_path(), raw + "\n")
    except OSError as exc:
        raise ConfigError("Could not save sign-in records.") from exc
