"""Vercel KV / Upstash access. Used for everything that must outlive a hosted request."""

import json
import os
import urllib.error
import urllib.request

from gmail_auto.errors import ConfigError

_KEY = "autogmail:token"


def storage_ready() -> bool:
    return _redis() is not None


def load_token_raw() -> str | None:
    if not storage_ready():
        return None
    try:
        result = _command(["GET", _KEY])
    except Exception as exc:
        raise ConfigError("Could not read the saved Gmail grant. Check that Vercel KV is connected.") from exc
    if not result:
        return None
    return str(result)


def kv_get(key: str):
    if not storage_ready():
        return None
    return _command(["GET", key])


def kv_set(key: str, value: str) -> None:
    if not storage_ready():
        raise ConfigError("There is nowhere to save this. Connect Vercel KV first.")
    _command(["SET", key, value])


def kv_delete(key: str) -> None:
    if not storage_ready():
        return
    _command(["DEL", key])


def save_token_raw(raw: str) -> bool:
    if not storage_ready():
        return False
    try:
        _command(["SET", _KEY, raw])
    except Exception as exc:
        raise ConfigError("The Gmail grant was not saved. Check that Vercel KV is connected.") from exc
    return True


def clear_token() -> None:
    if storage_ready():
        _command(["DEL", _KEY])


def _redis() -> tuple[str, str] | None:
    url = (os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL") or "").strip().rstrip("/")
    token = (os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    if not url or not token:
        return None
    return url, token


def _command(command: list[str]):
    pair = _redis()
    if pair is None:
        return None
    url, token = pair
    request = urllib.request.Request(
        url,
        data=json.dumps(command).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConfigError("Could not reach Vercel KV.") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ConfigError("Vercel KV did not complete this read or write.")
    return payload.get("result")
