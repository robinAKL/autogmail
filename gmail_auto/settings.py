"""How replies behave: language, when to ask first, and where to notify."""

import re

from gmail_auto.errors import ConfigError
from gmail_auto.storage import load_doc, save_doc

_CLEANUP_LINES = 40
_CLEANUP_CHARS = 200

LANGUAGES = {
    "en": "English",
    "match": "Same language as the sender",
    "bilingual": "Chinese, then English",
}
APPROVALS = {
    "smart": "Ask when needed",
    "always": "Ask every time",
    "never": "Send automatically",
}
DEFAULTS = {
    "reply_language": "en",
    "approval_mode": "smart",
    "whatsapp_to": "",
    "notify_auto_replies": True,
    "daily_digest": True,
    "digest_hour": 8,
    "cleanup_match": None,
    "cleanup_keep": None,
    "cleanup_skipped": True,
    "auto_calendar": True,
}


def load_settings() -> dict:
    saved = load_doc("settings", {})
    merged = dict(DEFAULTS)
    if isinstance(saved, dict):
        for key in DEFAULTS:
            if key in saved:
                merged[key] = saved[key]
    return _clean(merged)


def save_settings(changes: dict) -> dict:
    merged = load_settings()
    merged.update({key: value for key, value in changes.items() if key in DEFAULTS})
    cleaned = _clean(merged, strict=True)
    save_doc("settings", cleaned)
    return cleaned


def normalize_phone(value: str) -> str:
    """Digits only, the way WhatsApp reports a sender."""
    return re.sub(r"\D", "", value or "")


def _clean(data: dict, strict: bool = False) -> dict:
    language = data.get("reply_language")
    if language not in LANGUAGES:
        language = DEFAULTS["reply_language"]
    approval = data.get("approval_mode")
    if approval not in APPROVALS:
        approval = DEFAULTS["approval_mode"]
    phone = normalize_phone(str(data.get("whatsapp_to") or ""))
    if phone and not 8 <= len(phone) <= 15:
        if strict:
            raise ConfigError("Enter the WhatsApp number with its country code, for example +64 21 123 4567.")
        phone = ""
    try:
        hour = int(data.get("digest_hour", DEFAULTS["digest_hour"]))
    except (TypeError, ValueError):
        hour = DEFAULTS["digest_hour"]
    from gmail_auto.cleanup import DEFAULT_KEEP, DEFAULT_MATCH

    return {
        "reply_language": language,
        "approval_mode": approval,
        "whatsapp_to": phone,
        "notify_auto_replies": bool(data.get("notify_auto_replies")),
        "daily_digest": bool(data.get("daily_digest")),
        "digest_hour": min(23, max(0, hour)),
        "cleanup_match": _lines(data.get("cleanup_match"), DEFAULT_MATCH, strict),
        "cleanup_keep": _lines(data.get("cleanup_keep"), DEFAULT_KEEP, strict),
        "cleanup_skipped": bool(data.get("cleanup_skipped")),
        "auto_calendar": bool(data.get("auto_calendar")),
    }


def _lines(value, fallback: list[str], strict: bool) -> list[str]:
    if value is None:
        items = list(fallback)
    elif isinstance(value, str):
        items = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                if strict:
                    raise ConfigError("Write one cleanup rule per line.")
                continue
            items.append(item.strip())
    else:
        items = list(fallback)
    if len(items) > _CLEANUP_LINES:
        if strict:
            raise ConfigError(f"Keep cleanup rules to {_CLEANUP_LINES} or fewer lines.")
        items = items[:_CLEANUP_LINES]
    for item in items:
        if len(item) > _CLEANUP_CHARS:
            if strict:
                raise ConfigError("One of the cleanup rules is too long.")
            return [entry[:_CLEANUP_CHARS] for entry in items]
    return items
