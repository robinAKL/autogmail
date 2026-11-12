"""How replies behave: language, when to ask first, and where to notify."""

import re

from gmail_auto.errors import ConfigError
from gmail_auto.storage import load_doc, save_doc

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
    return {
        "reply_language": language,
        "approval_mode": approval,
        "whatsapp_to": phone,
        "notify_auto_replies": bool(data.get("notify_auto_replies")),
        "daily_digest": bool(data.get("daily_digest")),
        "digest_hour": min(23, max(0, hour)),
    }
