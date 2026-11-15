"""Super agent: after a worker brief, decide ignore / auto-reply / ask the owner."""

from dataclasses import dataclass

from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import rule_signals
from gmail_auto.llm import complete, model_ready, parse_json
from gmail_auto.models import Mail

_DISPOSITIONS = {"ignore", "reply", "ask"}
_SYSTEM = """You are the owner's Super agent. You only decide what to do. You never write a reply.
You see a clerk's brief, not necessarily the full letter. Rule signals can be wrong (a real bill can mention payment).

Choose ONE disposition:
- ignore: junk, ads, newsletters, phishing, scams, fake invoices, gambling, bulk marketing. No reply will be written.
- reply: a genuine letter that can be answered from the owner's material with no new commitment.
- ask: a genuine letter that needs the owner to confirm (money, meetings, legal, unclear identity, or anything they should see).

Return JSON only:
{"disposition":"ignore"|"reply"|"ask","reason":"one short English sentence","summary":"what the sender wants","code":"junk"|"phishing"|"ad"|"notification"|"person"|"bill"|"other"}
"""


@dataclass(frozen=True)
class Screen:
    disposition: str
    reason: str = ""
    summary: str = ""
    code: str = "other"


def screen_mail(mail: Mail, brief: dict | None = None) -> Screen | None:
    """Return a verdict, or None if Super is unavailable so drafting can continue."""
    if not model_ready():
        return None
    from gmail_auto.worker import format_brief

    sender = f"{mail.from_name} <{mail.from_email}>" if mail.from_name else (mail.from_email or "")
    prompt = (
        f"From: {sender}\n"
        f"Address: {mail.from_email}\n"
        f"Subject: {mail.subject or '(no subject)'}\n"
        f"Date header: {mail.date or ''}\n"
        f"Rule signals: {', '.join(rule_signals(mail)) if rule_signals(mail) else '(none)'}\n"
        "Clerk brief:\n"
        f"{format_brief(brief or {}) or '(none)'}"
    )
    try:
        raw = complete(_SYSTEM, prompt, max_tokens=220, temperature=0.1, role="super").strip()
    except (ConfigError, ReplyError):
        return None
    data = parse_json(raw)
    if not isinstance(data, dict):
        return Screen("ask", "The screen was unclear, so this waits for you.", "", "other")
    disposition = str(data.get("disposition") or "").strip().lower()
    if disposition not in _DISPOSITIONS:
        disposition = "ask"
    code = str(data.get("code") or "other").strip().lower() or "other"
    if disposition == "ignore" and code not in {"junk", "phishing", "ad", "notification"}:
        code = "junk"
    return Screen(
        disposition,
        _line(data.get("reason")) or "screened",
        _line(data.get("summary")),
        code,
    )


def _line(value) -> str:
    text = " ".join(str(value or "").split())
    return text[:300]
