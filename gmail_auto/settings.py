"""How replies behave: language, when to ask first, and where to notify."""

import re

from gmail_auto.errors import ConfigError
from gmail_auto.storage import load_doc, save_doc

_CLEANUP_LINES = 60
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
AI_MODELS = {
    "": "Default (from environment: OPENAI_MODEL / gpt-4o-mini)",
    "openai/gpt-5-mini": "OpenAI GPT-5 mini ($0.25 / $2.00 per 1M tokens)",
    "openai/gpt-5-nano": "OpenAI GPT-5 nano ($0.05 / $0.40 per 1M tokens)",
    "openai/gpt-4o-mini": "OpenAI GPT-4o mini ($0.15 / $0.60 per 1M tokens)",
    "google/gemini-2.5-flash": "Google Gemini 2.5 Flash ($0.30 / $2.50 per 1M tokens)",
    "google/gemini-2.5-flash-lite": "Google Gemini 2.5 Flash-Lite ($0.10 / $0.40 per 1M tokens)",
    "anthropic/claude-haiku-5.5": "Anthropic Claude Haiku 5.5 ($0.10 / $0.50 per 1M tokens)",
}
TRANSLATION_MODES = {
    "seamless": "Seamless (view translated)",
    "bilingual": "Bilingual",
    "off": "Off",
}
DEFAULTS = {
    "reply_language": "en",
    "approval_mode": "smart",
    "ai_model": "",
    "super_model": "",
    "whatsapp_to": "",
    "notify_auto_replies": True,
    "notify_important": True,
    "notify_preset": "busy",
    "notify_kinds": None,
    "notify_calendar": True,
    "daily_digest": True,
    "digest_hour": 8,
    "digest_jobs": False,
    "cleanup_match": None,
    "cleanup_keep": None,
    "cleanup_skipped": True,
    "cleanup_pack": 0,
    "auto_calendar": True,
    "ui_language": "en",
    "first_language": "zh",
    "translation_mode": "seamless",
    "translate_outgoing": True,
    "preserve_langs": [],
    "default_sender": "",
    "auto_reply_allow": [],
    "auto_reply_block": [],
    "mail_signatures": {},
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
    if "notify_kinds" in changes and "notify_preset" not in changes:
        merged["notify_preset"] = "custom"
    cleaned = _clean(merged, strict=True)
    save_doc("settings", cleaned)
    return cleaned


# Countries whose local numbers start with a trunk 0 that is dropped after the
# country code (+64 021... is really +64 21...). Country codes are prefix-free,
# so matching the start of the digits is unambiguous. Italy (39) keeps its 0.
_TRUNK_ZERO = ("353", "27", "31", "32", "33", "41", "43", "44", "46", "49", "60", "61", "62", "63", "64", "66", "81", "82", "86", "91")


def normalize_phone(value: str) -> str:
    """Digits only, the way WhatsApp reports a sender, without a trunk 0 after the country code."""
    digits = re.sub(r"\D", "", value or "")
    for code in _TRUNK_ZERO:
        if digits.startswith(code + "0"):
            return code + digits[len(code) + 1 :]
    return digits


def _clean(data: dict, strict: bool = False) -> dict:
    language = data.get("reply_language")
    if language not in LANGUAGES:
        language = DEFAULTS["reply_language"]
    approval = data.get("approval_mode")
    if approval not in APPROVALS:
        approval = DEFAULTS["approval_mode"]
    ai_model = str(data.get("ai_model") or "").strip()
    if ai_model not in AI_MODELS:
        ai_model = DEFAULTS["ai_model"]
    super_model = str(data.get("super_model") or "").strip()
    if super_model not in AI_MODELS:
        super_model = DEFAULTS["super_model"]
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

    match = _lines(data.get("cleanup_match"), DEFAULT_MATCH, strict)
    keep = _lines(data.get("cleanup_keep"), DEFAULT_KEEP, strict)
    try:
        pack = int(data.get("cleanup_pack") or 0)
    except (TypeError, ValueError):
        pack = 0
    if pack < 3:
        match = _union_lines(match, DEFAULT_MATCH)
        keep = _union_lines(keep, DEFAULT_KEEP)
        pack = 3

    from gmail_auto.importance import INTERRUPT, KINDS, PRESETS, preset_of

    kinds = _kinds(data.get("notify_kinds"))
    preset = str(data.get("notify_preset") or "").strip()
    if preset not in PRESETS:
        preset = preset_of({"notify_preset": "", "notify_kinds": kinds if data.get("notify_kinds") is not None else list(KINDS)})
    if preset in ("urgent", "busy"):
        kinds = list(INTERRUPT)

    translation_mode = str(data.get("translation_mode") or "").strip().lower()
    if translation_mode not in TRANSLATION_MODES:
        translation_mode = DEFAULTS["translation_mode"]
    first_language = str(data.get("first_language") or "").strip().lower() or DEFAULTS["first_language"]
    raw_preserve = data.get("preserve_langs")
    if isinstance(raw_preserve, str):
        preserve_langs = [p.strip().lower() for p in raw_preserve.split(",") if p.strip()]
    elif isinstance(raw_preserve, (list, tuple, set)):
        preserve_langs = [str(p).strip().lower() for p in raw_preserve if str(p).strip()]
    else:
        preserve_langs = list(DEFAULTS["preserve_langs"])
    default_sender = str(data.get("default_sender") or "").strip().lower()
    if default_sender not in {"", "gmail", "outlook"}:
        if strict:
            raise ConfigError("Choose Gmail, Outlook, or automatic for the default sender.")
        default_sender = ""

    return {
        "reply_language": language,
        "approval_mode": approval,
        "ai_model": ai_model,
        "super_model": super_model,
        "whatsapp_to": phone,
        "notify_auto_replies": bool(data.get("notify_auto_replies")),
        "notify_important": bool(data.get("notify_important")),
        "notify_preset": preset,
        "notify_kinds": kinds,
        "notify_calendar": bool(data.get("notify_calendar")),
        "daily_digest": bool(data.get("daily_digest")),
        "digest_hour": min(23, max(0, hour)),
        "digest_jobs": bool(data.get("digest_jobs")),
        "cleanup_match": match,
        "cleanup_keep": keep,
        "cleanup_skipped": bool(data.get("cleanup_skipped")),
        "cleanup_pack": pack,
        "auto_calendar": bool(data.get("auto_calendar")),
        "ui_language": "zh" if data.get("ui_language") == "zh" else "en",
        "first_language": first_language,
        "translation_mode": translation_mode,
        "translate_outgoing": bool(data.get("translate_outgoing", DEFAULTS["translate_outgoing"])),
        "preserve_langs": preserve_langs,
        "default_sender": default_sender,
        "auto_reply_allow": _rule_lines(data.get("auto_reply_allow"), strict),
        "auto_reply_block": _rule_lines(data.get("auto_reply_block"), strict),
        "mail_signatures": _clean_signatures(data.get("mail_signatures"), strict),
    }


def _kinds(value) -> list[str]:
    """Which kinds of important mail go to WhatsApp. Unset means all of them."""
    from gmail_auto.importance import KINDS

    if value is None:
        return list(KINDS)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return list(KINDS)
    chosen = {str(item) for item in value}
    return [kind for kind in KINDS if kind in chosen]


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


def _union_lines(items: list[str], fallback: list[str]) -> list[str]:
    seen = {item.lower() for item in items}
    out = list(items)
    for item in fallback:
        if item.lower() not in seen:
            out.append(item)
            seen.add(item.lower())
    return out[:_CLEANUP_LINES]


def _rule_lines(value, strict: bool) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        raw = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, (list, tuple)):
        raw = [str(item).strip() for item in value if str(item).strip()]
    else:
        return []
    if len(raw) > _CLEANUP_LINES:
        if strict:
            raise ConfigError(f"Keep auto-reply rules to {_CLEANUP_LINES} or fewer lines.")
        raw = raw[:_CLEANUP_LINES]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if len(item) > _CLEANUP_CHARS:
            if strict:
                raise ConfigError("One of the auto-reply rules is too long.")
            item = item[:_CLEANUP_CHARS]
        key = item.lower()
        if key not in seen:
            out.append(item)
            seen.add(key)
    return out


def _clean_signatures(value, strict: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_sig in value.items():
        key = str(raw_key or "").strip().lower()
        if not key or not isinstance(raw_sig, str):
            continue
        sig = raw_sig.replace("\r\n", "\n").strip()
        if len(sig) > 2000:
            if strict:
                raise ConfigError("Keep each mailbox signature to 2000 characters or fewer.")
            sig = sig[:2000]
        if sig:
            out[key] = sig
    return out


def _rule_matches(rule: str, sender: str, haystack: str) -> bool:
    needle = (rule or "").strip().lower()
    if not needle:
        return False
    if needle.startswith("@"):
        return needle in sender
    if "@" in needle and " " not in needle:
        return needle in sender or needle in haystack
    return needle in sender or needle in haystack


def evaluate_auto_reply_rules(mail, settings: dict) -> tuple[bool, str]:
    """Tighten whether a reply may be auto-sent in smart/never modes."""
    sender = " ".join(
        part for part in (
            getattr(mail, "from_email", "") or "",
            getattr(mail, "reply_email", "") or "",
            getattr(mail, "from_name", "") or "",
        ) if part
    ).lower()
    haystack = " ".join(
        part for part in (
            getattr(mail, "subject", "") or "",
            getattr(mail, "body", "") or "",
            getattr(mail, "snippet", "") or "",
        ) if part
    ).lower()
    for rule in settings.get("auto_reply_block") or []:
        if _rule_matches(rule, sender, haystack):
            return False, f"Held by never-auto-reply rule: {rule}"
    allow_rules = settings.get("auto_reply_allow") or []
    if allow_rules and not any(_rule_matches(rule, sender, haystack) for rule in allow_rules):
        return False, "Held because it did not match your auto-reply allow rules."
    return True, ""


def signature_for_mailbox(settings: dict, provider: str = "", email: str = "") -> str:
    sigs = settings.get("mail_signatures") or {}
    if not isinstance(sigs, dict):
        return ""
    for candidate in ((email or "").strip().lower(), (provider or "").strip().lower()):
        if candidate and isinstance(sigs.get(candidate), str) and sigs[candidate].strip():
            return sigs[candidate].strip()
    return ""


def append_signature(body: str, signature: str) -> str:
    text = (body or "").rstrip()
    sig = (signature or "").strip()
    if not sig:
        return text
    if not text:
        return sig
    if text.endswith(sig):
        return text
    return f"{text}\n\n{sig}"
