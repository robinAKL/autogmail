"""Which letters are worth a WhatsApp ping, even when no reply is drafted yet."""

import re

from gmail_auto.models import Mail

PING_SKIPS = {"system", "empty", "auto"}
LABELS = {
    "invoice": "invoice",
    "enquiry": "enquiry",
    "meeting": "meeting",
    "school": "school",
    "person": "person",
    "company": "company",
}

_INVOICE = re.compile(
    r"\b(invoice|tax invoice|payment due|statement of account|\bbill\b|receipt|overdue)\b",
    re.IGNORECASE,
)
_ENQUIRY = re.compile(
    r"\b(quot(?:e|ation)|enquir(?:y|ies)|inquir(?:y|ies)|\brfq\b|pricing|how much|day rate)\b",
    re.IGNORECASE,
)
_SCHOOL = ("westlake", "wairau", "etap.co.nz", "school.nz")
_COMPANY = ("kiwiknowledge", "kiwi knowledge", "innopro")


def assess(mail: Mail) -> dict:
    """Return kind, whether it is important, and a short English why."""
    sender = f"{mail.from_email or ''} {mail.from_name or ''}".lower()
    text = f"{mail.subject or ''}\n{mail.body or mail.snippet or ''}"
    blob = f"{sender}\n{text}"
    if _INVOICE.search(blob):
        return _tag("invoice", "Looks like a bill or invoice.")
    if _ENQUIRY.search(blob):
        return _tag("enquiry", "Looks like a quote or enquiry.")
    if _is_meeting(mail):
        return _tag("meeting", "Looks like a meeting or invitation.")
    if any(token in blob.lower() for token in _SCHOOL):
        return _tag("school", "School or parent mail.")
    if _known_person(mail, blob):
        return _tag("person", "Someone already in your knowledge or mail history.")
    if any(token in blob.lower() for token in _COMPANY):
        return _tag("company", "Your company or a known organisation.")
    return {"kind": "other", "important": False, "why": "", "label": ""}


def _tag(kind: str, why: str) -> dict:
    return {"kind": kind, "important": True, "why": why, "label": LABELS.get(kind, kind)}


def _is_meeting(mail: Mail) -> bool:
    try:
        from gmail_auto.calendars import suggest_event

        return suggest_event(mail) is not None
    except Exception:
        return False


def _known_person(mail: Mail, blob: str) -> bool:
    email = (mail.from_email or "").strip().lower()
    name = (mail.from_name or "").strip().lower()
    if _from_knowledge(email, name, blob):
        return True
    if not email:
        return False
    try:
        from gmail_auto.store import list_processed

        for record in list_processed(80):
            if (record.get("from_email") or "").strip().lower() == email and record.get("action") in {"sent", "approved_sent"}:
                return True
    except Exception:
        return False
    return False


def _from_knowledge(email: str, name: str, blob: str) -> bool:
    try:
        from gmail_auto.knowledge import load_index

        items = load_index()["model"]["items"]
    except Exception:
        return False
    for item in items:
        if item.get("kind") not in {"person", "fact"}:
            continue
        card = f"{item.get('text') or ''} {' '.join(item.get('related') or [])}".lower()
        if email and email in card:
            return True
        if name and len(name) > 3 and name in card:
            return True
    return False
