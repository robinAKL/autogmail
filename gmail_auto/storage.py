"""One place to keep small JSON documents.

On this computer they live in data/<name>.json. When Vercel KV / Upstash is
connected they live there instead, because a hosted disk does not persist.
"""

import json
import os
import re
import threading

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.token_store import kv_delete, kv_get, kv_set, storage_ready

_PREFIX = "autogmail:"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
LOCK = threading.RLock()


def persistent() -> bool:
    """False on Vercel without KV: anything saved there is gone on the next request."""
    return storage_ready() or not os.environ.get("VERCEL")


def load_doc(name: str, default=None):
    _check(name)
    if storage_ready():
        try:
            raw = kv_get(_PREFIX + name)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError("Could not read from storage.") from exc
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"The saved '{name}' record is damaged. It was left unchanged.") from exc
    path = data_dir(create=False) / f"{name}.json"
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is damaged. It was left unchanged.") from exc
    except OSError:
        return default


def save_doc(name: str, data) -> None:
    _check(name)
    if storage_ready():
        kv_set(_PREFIX + name, json.dumps(data, ensure_ascii=False))
        return
    try:
        atomic_write(data_dir() / f"{name}.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        if os.environ.get("VERCEL"):
            return
        raise ConfigError("Could not write local records right now.")


def delete_doc(name: str) -> None:
    _check(name)
    if storage_ready():
        kv_delete(_PREFIX + name)
        return
    path = data_dir(create=False) / f"{name}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _check(name: str) -> None:
    if not _NAME.match(name or ""):
        raise ConfigError("Invalid record name.")
