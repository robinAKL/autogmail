import os
import re
from datetime import datetime

from gmail_auto.errors import ConfigError
from gmail_auto.paths import atomic_write, data_dir
from gmail_auto.storage import LOCK, load_doc, save_doc

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
_KEEP_RECORDS = 3000


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_live() -> bool:
    data = load_doc("state", {"live_send": False})
    return bool(isinstance(data, dict) and data.get("live_send"))


def set_live(live: bool) -> None:
    with LOCK:
        data = load_doc("state", {})
        if not isinstance(data, dict):
            data = {}
        data["live_send"] = bool(live)
        save_doc("state", data)


def get_state(key: str, default=None):
    data = load_doc("state", {})
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def set_state(key: str, value) -> None:
    with LOCK:
        data = load_doc("state", {})
        if not isinstance(data, dict):
            data = {}
        data[key] = value
        save_doc("state", data)


def load_processed() -> dict:
    data = load_doc("processed", {})
    if not isinstance(data, dict):
        raise ConfigError("The processed-mail record has the wrong format. It was left unchanged.")
    return data


def get_processed(key: str) -> dict | None:
    item = load_processed().get(key)
    return item if isinstance(item, dict) else None


def mark_processed(record: dict) -> None:
    with LOCK:
        data = load_processed()
        data[record["id"]] = record
        if len(data) > _KEEP_RECORDS:
            ordered = sorted(data.items(), key=lambda pair: str(pair[1].get("processed_at", "")) if isinstance(pair[1], dict) else "")
            data = dict(ordered[-_KEEP_RECORDS:])
        save_doc("processed", data)


def update_processed(key: str, changes: dict) -> dict | None:
    with LOCK:
        data = load_processed()
        record = data.get(key)
        if not isinstance(record, dict):
            return None
        record.update(changes)
        save_doc("processed", data)
        return record


def list_processed(limit: int = 25) -> list[dict]:
    rows = [item for item in load_processed().values() if isinstance(item, dict)]
    rows.sort(key=lambda item: item.get("processed_at", ""), reverse=True)
    return rows[:limit]


def bump_failure(key: str) -> int:
    with LOCK:
        data = load_doc("failures", {})
        if not isinstance(data, dict):
            data = {}
        count = int(data.get(key, 0) or 0) + 1
        data[key] = count
        save_doc("failures", data)
        return count


def clear_failure(key: str) -> None:
    with LOCK:
        data = load_doc("failures", {})
        if isinstance(data, dict) and key in data:
            data.pop(key, None)
            save_doc("failures", data)


def load_last_run() -> dict | None:
    data = load_doc("last_run", None)
    return data if isinstance(data, dict) else None


def save_last_run(payload: dict) -> None:
    save_doc("last_run", payload)


def write_draft(mail, reply: str, note: str = "Draft only. Nothing was sent.") -> str:
    """Keep a readable copy of a draft on this computer. Hosted runs have no disk, so they skip it."""
    if os.environ.get("VERCEL"):
        return ""
    name = safe_id(mail.key) + ".txt"
    who = mail.from_name or mail.from_email or "Unknown sender"
    if mail.from_name and mail.from_email:
        who = f"{mail.from_name} <{mail.from_email}>"
    text = (
        f"From: {who}\n"
        f"Subject: {mail.subject or '(no subject)'}\n"
        f"Received: {mail.date}\n"
        f"Note: {note}\n\n"
        f"{reply}\n"
    )
    try:
        atomic_write(data_dir() / "outbox" / name, text)
    except OSError as exc:
        raise ConfigError("Could not write local records right now.") from exc
    return f"data/outbox/{name}"


def safe_id(value: str) -> str:
    cleaned = _SAFE_ID.sub("_", value or "")
    if not cleaned:
        raise ConfigError("Invalid message id.")
    return cleaned[-120:]
