"""Redis storage for the hosted site: everything that must outlive a single request.

Vercel connects Redis under different variable names depending on which
integration was picked (Upstash, "Redis", a custom prefix). Both the Upstash
REST style and a plain redis:// address are accepted.
"""

import json
import os
import re
import urllib.error
import urllib.request

from gmail_auto.errors import ConfigError

_KEY = "autogmail:token"
_STORAGE_NAME = re.compile(r"KV|REDIS|UPSTASH", re.IGNORECASE)
_client: dict = {"url": None, "client": None}


def storage_ready() -> bool:
    return _redis() is not None or _tcp_url() is not None


def storage_kind() -> str:
    if _redis() is not None:
        return "Redis (Upstash REST)"
    if _tcp_url() is not None:
        return "Redis"
    return ""


def storage_hint() -> str:
    """What to do when storage is missing, naming the related variables that do exist (names only)."""
    names = sorted(name for name in os.environ if _STORAGE_NAME.search(name))
    if names:
        found = ", ".join(names[:8])
        return (
            f"Storage variables found: {found}, but none of them is a usable address. "
            "Expected KV_REST_API_URL + KV_REST_API_TOKEN, UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN, "
            "or REDIS_URL starting with redis:// or rediss://."
        )
    return (
        "No storage variables were found. In Vercel open Storage, create an Upstash for Redis database, "
        "connect it to this project for Production, then redeploy."
    )


def load_token_raw() -> str | None:
    if not storage_ready():
        return None
    try:
        result = _command(["GET", _KEY])
    except Exception as exc:
        raise ConfigError("Could not read the saved Gmail grant from storage.") from exc
    if not result:
        return None
    return str(result)


def kv_get(key: str):
    if not storage_ready():
        return None
    return _command(["GET", key])


def kv_set(key: str, value: str) -> None:
    if not storage_ready():
        raise ConfigError("There is nowhere to save this. " + storage_hint())
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
        raise ConfigError("The Gmail grant was not saved to storage.") from exc
    return True


def clear_token() -> None:
    if storage_ready():
        _command(["DEL", _KEY])


def _redis() -> tuple[str, str] | None:
    """An Upstash-style REST address and token, under whichever prefix Vercel used."""
    for url_name, token_name in (
        ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
        ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
    ):
        pair = _pair(url_name, token_name)
        if pair:
            return pair
    for name in sorted(os.environ):
        upper = name.upper()
        if upper.endswith("REST_API_URL") or upper.endswith("REDIS_REST_URL"):
            pair = _pair(name, name[: -len("URL")] + "TOKEN")
            if pair:
                return pair
    return None


def _pair(url_name: str, token_name: str) -> tuple[str, str] | None:
    url = (os.getenv(url_name) or "").strip().strip("\"'").rstrip("/")
    token = (os.getenv(token_name) or "").strip().strip("\"'")
    if url.lower().startswith("https://") and token:
        return url, token
    return None


def _tcp_url() -> str | None:
    """A redis:// or rediss:// address, as Vercel's Redis integrations provide."""
    names = ["REDIS_URL", "KV_URL"] + sorted(
        name for name in os.environ if name.upper().endswith(("_REDIS_URL", "_KV_URL")) and name not in ("REDIS_URL", "KV_URL")
    )
    for name in names:
        value = (os.getenv(name) or "").strip().strip("\"'")
        if value.lower().startswith(("redis://", "rediss://")):
            return value
    return None


def _command(command: list[str]):
    pair = _redis()
    if pair is not None:
        return _rest(pair, command)
    url = _tcp_url()
    if url is None:
        return None
    return _tcp(url, command)


def _rest(pair: tuple[str, str], command: list[str]):
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
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ConfigError("Storage refused the token. Reconnect the Redis database to this project and redeploy.") from exc
        raise ConfigError("Could not reach storage.") from exc
    except urllib.error.URLError as exc:
        raise ConfigError("Could not reach storage.") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ConfigError("Storage did not complete this read or write.")
    return payload.get("result")


def _tcp(url: str, command: list[str]):
    try:
        import redis
    except ImportError as exc:
        raise ConfigError("The redis library is not installed. Run: pip install -r requirements.txt") from exc
    if _client["url"] != url or _client["client"] is None:
        _client["client"] = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10, socket_connect_timeout=10)
        _client["url"] = url
    try:
        return _client["client"].execute_command(*command)
    except redis.AuthenticationError as exc:
        raise ConfigError("Storage refused the password in REDIS_URL. Reconnect the database and redeploy.") from exc
    except redis.RedisError as exc:
        _client["client"] = None
        raise ConfigError("Could not reach storage.") from exc
