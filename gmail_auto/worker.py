"""The clerk agent: read, summarise and write. It does not decide send / ignore / ask."""

from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import rule_signals
from gmail_auto.llm import complete, model_ready, parse_json
from gmail_auto.models import Mail
from gmail_auto.storage import load_doc, save_doc

_BRIEF_SYSTEM = """You are the owner's mail clerk. Read the letter and extract a brief.
Do not decide whether to reply, ignore, or ask the owner. Do not write a reply.
Return JSON only:
{"summary":"5-8 short lines of what the letter is","facts":["sender=...","asks=...","deadline=..."],"quotes":["short phrases copied from the letter"],"spam_cues":["gambling bonus","QQ id","old date", ...]}
"""
_KEEP = 80


def brief_mail(mail: Mail, use_cache: bool = True) -> dict:
    """Summarise one letter for the Super agent and the web reader."""
    if use_cache:
        cached = _load_cached(mail.key)
        if cached:
            return cached
    fallback = _local_brief(mail)
    if not model_ready():
        _store(mail.key, fallback)
        return fallback
    sender = f"{mail.from_name} <{mail.from_email}>" if mail.from_name else (mail.from_email or "")
    prompt = (
        f"From: {sender}\n"
        f"Address: {mail.from_email}\n"
        f"Subject: {mail.subject or '(no subject)'}\n"
        f"Date header: {mail.date or ''}\n"
        f"Rule signals: {', '.join(rule_signals(mail)) or '(none)'}\n"
        "Body:\n"
        f"{(mail.body or mail.snippet or '')[:4000]}"
    )
    try:
        raw = complete(_BRIEF_SYSTEM, prompt, max_tokens=350, temperature=0.1, role="worker")
    except (ConfigError, ReplyError):
        _store(mail.key, fallback)
        return fallback
    data = parse_json(raw) if raw else None
    if not isinstance(data, dict):
        fallback["summary"] = (raw or fallback["summary"])[:800]
        _store(mail.key, fallback)
        return fallback
    brief = {
        "summary": _line(data.get("summary"), 800) or fallback["summary"],
        "facts": _list(data.get("facts")),
        "quotes": _list(data.get("quotes")),
        "spam_cues": _list(data.get("spam_cues")) or fallback["spam_cues"],
    }
    _store(mail.key, brief)
    return brief


def format_brief(brief: dict) -> str:
    lines = [brief.get("summary") or ""]
    facts = brief.get("facts") or []
    if facts:
        lines.append("Facts: " + "; ".join(facts[:12]))
    quotes = brief.get("quotes") or []
    if quotes:
        lines.append("Quotes: " + " | ".join(quotes[:8]))
    cues = brief.get("spam_cues") or []
    if cues:
        lines.append("Spam cues: " + ", ".join(cues[:8]))
    return "\n".join(part for part in lines if part).strip()


def remember_verdict(mail_key: str, verdict) -> None:
    cached = _load_cached(mail_key) or {}
    cached["advice"] = getattr(verdict, "reason", "") or ""
    cached["disposition"] = getattr(verdict, "disposition", "") or ""
    cached["code"] = getattr(verdict, "code", "") or ""
    _store(mail_key, cached)


def load_review(mail_key: str) -> dict:
    return _load_cached(mail_key) or {}


def _local_brief(mail: Mail) -> dict:
    text = " ".join((mail.body or mail.snippet or "").split())
    return {
        "summary": text[:400] or (mail.subject or ""),
        "facts": [
            f"from={mail.from_email or ''}",
            f"name={mail.from_name or ''}",
            f"subject={mail.subject or ''}",
            f"date={mail.date or ''}",
        ],
        "quotes": [mail.subject] if mail.subject else [],
        "spam_cues": rule_signals(mail),
    }


def _load_cached(mail_key: str) -> dict | None:
    if not mail_key:
        return None
    data = load_doc("mail_briefs", {})
    if not isinstance(data, dict):
        return None
    item = data.get(mail_key)
    return item if isinstance(item, dict) else None


def _store(mail_key: str, brief: dict) -> None:
    if not mail_key:
        return
    data = load_doc("mail_briefs", {})
    if not isinstance(data, dict):
        data = {}
    data[mail_key] = brief
    if len(data) > _KEEP:
        extra = list(data.keys())[:-_KEEP]
        for key in extra:
            data.pop(key, None)
    save_doc("mail_briefs", data)


def _line(value, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _list(value) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()[:200]]
    if not isinstance(value, list):
        return []
    return [_line(item, 200) for item in value if str(item).strip()][:12]
