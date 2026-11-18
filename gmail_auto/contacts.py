"""People the owner deals with: clients, students, suppliers, friends...

Contacts are built from mail: everyone who wrote and was not a robot, and everyone the owner wrote to. The
model suggests what each person is to the owner (with a reason) and reads company, title and phone from
signatures; the owner confirms or corrects. A field the owner edited is never changed by the model again.
"""

import re
import secrets
import time
from email.utils import getaddresses

from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.storage import LOCK, load_doc, save_doc

TYPES = {
    "client": "Client",
    "student": "Student",
    "supplier": "Supplier",
    "partner": "Partner",
    "friend": "Friend",
    "family": "Family",
    "institution": "Institution",
    "other": "Other",
}
# Replies to these people never go out on their own, whatever the sending mode.
ALWAYS_ASK = {"friend", "family"}
EDITABLE = ("name", "company", "title", "phone", "address", "type", "language", "tone", "salutation", "notes")
TONES = {"": "Not set", "formal": "Formal", "casual": "Casual"}
LANGUAGES = {"": "Not set", "en": "English", "zh": "Chinese", "bilingual": "Chinese and English"}
FREE_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "yahoo.co.nz",
    "icloud.com", "me.com", "mac.com", "qq.com", "163.com", "126.com", "foxmail.com", "sina.com", "proton.me",
    "protonmail.com", "xtra.co.nz", "aol.com", "gmx.com", "zoho.com", "yandex.com",
}
# A sender becomes a contact when the owner replied to them or wrote to them, or when their letter is waiting
# for the owner now and does not come from a robot-looking address. Skipped mail, and drafts the owner turned
# down or let expire, are mostly robots and newsletters.
_REPLIED = {"sent"}
_WAITING = {"waiting", "drafted"}
_ROBOT_LOCAL = re.compile(
    r"^(?:no-?reply|do-?not-?reply|support|info|hello|hi|team|news(?:letter)?|billing|service|services|customerservice|"
    r"notifications?|notify|admin|contact|system|sender|alerts?|campaign|updates?|welcome|events|careers|recruitment|"
    r"marketing|promo|offers?|mail|mailer|robot|receipts?|invoices?|statements|verify|security|accounts|hr|jobs|remind|"
    r"express|changelog|e?mail-?en|concierge|feedback|enquiries|sales|orders?|help|community|members|rewards)(?:[+._-].*)?$"
)
_ROBOT_SUBDOMAIN = re.compile(
    r"^(?:mail|email|e|em\d*|send\d*|notice|notifications?|notificationmail|promo|news|info|m|msg|mx\d+|hello|ship|join|"
    r"order|experience|inbound|insideapple|rides-promotions|e-mail|marketing|campaign|updates)$"
)


def looks_automated(address: str) -> bool:
    """Addresses that belong to systems, shops and mailing tools rather than a person."""
    local, _, domain = (address or "").lower().partition("@")
    if not domain or _ROBOT_LOCAL.match(local):
        return True
    labels = domain.split(".")
    base = 3 if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "net", "govt", "ac", "school"} and len(labels[-1]) == 2 else 2
    return len(labels) > base and bool(_ROBOT_SUBDOMAIN.match(labels[0]))
_ID = re.compile(r"^[a-f0-9]{10}$")


# ---- reading ------------------------------------------------------------------------


def all_contacts(include_hidden: bool = False) -> list[dict]:
    items = [item for item in _load().values() if include_hidden or not item.get("hidden")]
    items.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
    return items


def get(contact_id: str) -> dict | None:
    return _load().get(contact_id) if _ID.match(contact_id or "") else None


def find(address: str) -> dict | None:
    wanted = (address or "").strip().lower()
    if not wanted:
        return None
    for item in _load().values():
        if wanted in item.get("emails") or []:
            return None if item.get("hidden") else item
    return None


def by_name(name: str) -> list[dict]:
    """Contacts whose name, first name or company matches what the owner said ("Lee", "ABC Ltd")."""
    wanted = " ".join((name or "").split()).lower()
    if len(wanted) < 2:
        return []
    exact, partial = [], []
    for item in all_contacts():
        names = [(item.get("name") or "").lower(), (item.get("company") or "").lower()]
        if wanted in names:
            exact.append(item)
        elif any(wanted in value.split() or value.startswith(wanted) for value in names if value):
            partial.append(item)
    return exact or partial


def always_ask(address: str) -> dict | None:
    contact = find(address)
    return contact if contact and contact.get("type") in ALWAYS_ASK else None


def card(contact: dict | None) -> str:
    """What a reply or a new email should know about this person, for the model."""
    if not contact:
        return ""
    lines = [f"Name: {contact.get('name') or contact['emails'][0]}"]
    if contact.get("type"):
        lines.append(f"Relationship to me: {TYPES.get(contact['type'], contact['type'])}")
    for key, label in (("company", "Company"), ("title", "Title"), ("salutation", "Greet them as"), ("notes", "Notes")):
        if contact.get(key):
            lines.append(f"{label}: {contact[key]}")
    if contact.get("tone"):
        lines.append(f"Tone: {TONES.get(contact['tone'], contact['tone'])}")
    if contact.get("language"):
        lines.append(f"Language they prefer: {LANGUAGES.get(contact['language'], contact['language'])}")
    return "\n".join(lines)


# ---- building from mail ---------------------------------------------------------------


def note_letter(mail, brief: dict | None = None) -> dict | None:
    """Add or refresh the sender of a letter that reached the reply stage (so not a robot)."""
    from gmail_auto.filters import is_system_address

    address = (mail.reply_email or mail.from_email or "").strip().lower()
    if not address or is_system_address(address) or looks_automated(address):
        return None
    sender = (brief or {}).get("sender") or {}
    when = time.strftime("%Y-%m-%d %H:%M")
    with LOCK:
        data = _load()
        contact = _find_in(data, address) or _new(address, mail.from_name)
        if contact.get("hidden"):
            return None
        _suggest(contact, "name", mail.from_name)
        for key in ("company", "title", "phone", "address"):
            _suggest(contact, key, sender.get(key))
        contact["letters"] = int(contact.get("letters") or 0) + 1
        contact["last_seen"] = max(contact.get("last_seen") or "", when)
        contact["first_seen"] = contact.get("first_seen") or when
        contact["updated_at"] = when
        data[contact["id"]] = contact
        _save(data)
        return contact


def build(records: list[dict], sent: list | None = None, own: set[str] | None = None) -> dict:
    """Make contacts from handled mail and from mail the owner sent. Returns counts for the page.

    own: the owner's own addresses, never listed as contacts.
    """
    from gmail_auto.filters import is_system_address

    own = {address.strip().lower() for address in own or set() if address}
    seen: dict[str, dict] = {}
    for record in records:
        address = (record.get("from_email") or "").strip().lower()
        if not address or address in own or record.get("sensitive") or is_system_address(address):
            continue
        # Robots are left out even when a reply went to them: earlier sending modes auto-answered some.
        if looks_automated(address) or record.get("action") not in _REPLIED | _WAITING:
            continue
        when = str(record.get("processed_at") or record.get("date") or "")[:16]
        entry = seen.setdefault(address, {"name": "", "letters": 0, "replied": 0, "last": "", "first": when})
        entry["name"] = entry["name"] or (record.get("from_name") or "")
        entry["letters"] += 1
        entry["replied"] += 1 if record.get("action") == "sent" else 0
        entry["last"] = max(entry["last"], when)
        entry["first"] = min(entry["first"] or when, when) if when else entry["first"]
    for mail in sent or []:
        when = str(getattr(mail, "date", "") or "")[:16]
        for name, address in getaddresses([(getattr(mail, "headers", {}) or {}).get("to", "")]):
            address = (address or "").strip().lower()
            if not address or address in own or is_system_address(address) or looks_automated(address):
                continue
            entry = seen.setdefault(address, {"name": "", "letters": 0, "replied": 0, "last": "", "first": when})
            entry["name"] = entry["name"] or name
            entry["replied"] += 1
            entry["last"] = max(entry["last"], when)
    added = updated = 0
    with LOCK:
        data = _load()
        for address, entry in seen.items():
            contact = _find_in(data, address)
            if contact is None:
                contact = _new(address, entry["name"])
                added += 1
            elif contact.get("hidden"):
                continue
            else:
                updated += 1
            _suggest(contact, "name", entry["name"])
            contact["letters"] = max(int(contact.get("letters") or 0), entry["letters"])
            contact["replied"] = max(int(contact.get("replied") or 0), entry["replied"])
            contact["last_seen"] = max(contact.get("last_seen") or "", entry["last"])
            contact["first_seen"] = min(filter(None, [contact.get("first_seen"), entry["first"]]), default="")
            data[contact["id"]] = contact
        _save(data)
    return {"added": added, "updated": updated, "total": len([item for item in data.values() if not item.get("hidden")])}


_SORT_SYSTEM = """You sort the people the owner emails with. For each person, decide what they are to the
owner and fill in what the mail shows. Types: client (pays the owner or asks for a quote/service), student
(takes the owner's courses or classes), supplier (sells to the owner: bills, quotes to the owner, services
used), partner (works with the owner on something), friend, family, institution (school, government, bank,
council, university), other. Use only the evidence given; when unsure, use "other" and say why.
Return JSON only:
{"people": [{"id": "...", "type": "...", "reason": "one short line of evidence", "company": "", "title": "",
 "language": "en|zh|", "tone": "formal|casual|", "salutation": "how the owner should greet them, e.g. Hi Lee"}]}"""


BATCH = 12


def sort_pending(limit: int = 24, complete=None) -> int:
    """Ask the model to suggest a type (and company, tone...) for contacts not sorted yet, a few at a time.

    Returns how many were sorted. Raises ConfigError when the model answered but nothing could be read,
    so the page can say so instead of quietly sorting nobody.
    """
    todo = [item for item in all_contacts() if not item.get("type")][:limit]
    done = 0
    failure = ""
    for start in range(0, len(todo), BATCH):
        try:
            done += _sort_batch(todo[start : start + BATCH], complete)
        except ConfigError as exc:
            failure = str(exc)
    if failure and not done:
        raise ConfigError(failure)
    return done


def _sort_batch(todo: list[dict], complete=None) -> int:
    from gmail_auto.llm import model_ready, parse_json

    if not todo:
        return 0
    if complete is None:
        if not model_ready():
            return 0
        from gmail_auto.llm import complete
    evidence = _evidence({address for item in todo for address in item.get("emails") or []})
    blocks = []
    for item in todo:
        lines = evidence.get(item["emails"][0], [])[:5]
        blocks.append(
            f"id={item['id']} | {item.get('name') or ''} <{item['emails'][0]}> | company: {item.get('company') or '-'} | "
            f"letters from them: {item.get('letters', 0)} | owner wrote to them: {item.get('replied', 0)}\n"
            + "\n".join(f"  - {line}" for line in lines)
        )
    try:
        raw = complete(_SORT_SYSTEM, "\n\n".join(blocks), max_tokens=4000, temperature=0)
    except ReplyError as exc:
        raise ConfigError(f"Sorting contacts failed: {exc}") from exc
    data = parse_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("people"), list):
        raise ConfigError("Sorting contacts failed: the model's answer could not be read. Press Scan mail again.")
    done = 0
    with LOCK:
        stored = _load()
        for row in data.get("people") or []:
            if not isinstance(row, dict):
                continue
            contact = stored.get(str(row.get("id") or ""))
            if contact is None or contact.get("type"):
                continue
            kind = str(row.get("type") or "").strip().lower()
            if kind not in TYPES:
                continue
            if "type" not in (contact.get("locked") or []):
                contact["type"] = kind
                contact["type_status"] = "suggested"
                contact["type_reason"] = " ".join(str(row.get("reason") or "").split())[:200]
            for key in ("company", "title", "salutation"):
                _suggest(contact, key, row.get(key), replace=key == "company" and contact.get("company_guessed"))
            if str(row.get("language") or "") in LANGUAGES:
                _suggest(contact, "language", row.get("language"))
            if str(row.get("tone") or "") in TONES:
                _suggest(contact, "tone", row.get("tone"))
            done += 1
        _save(stored)
    return done


# ---- the owner's changes --------------------------------------------------------------


def save_edit(contact_id: str, values: dict) -> dict:
    with LOCK:
        data = _load()
        contact = data.get(contact_id)
        if contact is None:
            raise ConfigError("That contact was not found.")
        locked = set(contact.get("locked") or [])
        for key in EDITABLE:
            if key not in values:
                continue
            value = _clean(key, values[key])
            if key == "type" and value and value not in TYPES:
                continue
            if value != (contact.get(key) or ""):
                contact[key] = value
                locked.add(key)
                if key == "company":
                    contact["company_guessed"] = False
        if "type" in values and contact.get("type"):
            contact["type_status"] = "confirmed"
            locked.add("type")
        extra = values.get("emails")
        if extra is not None:
            emails = [contact["emails"][0]] + [part.strip().lower() for part in re.split(r"[,;\s]+", extra) if "@" in part]
            contact["emails"] = list(dict.fromkeys(emails))[:6]
        contact["locked"] = sorted(locked)
        contact["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
        _save(data)
        return contact


def confirm_type(contact_id: str) -> dict:
    with LOCK:
        data = _load()
        contact = data.get(contact_id)
        if contact is None or not contact.get("type"):
            raise ConfigError("Choose a type first.")
        contact["type_status"] = "confirmed"
        contact["locked"] = sorted(set(contact.get("locked") or []) | {"type"})
        _save(data)
        return contact


def hide(contact_id: str) -> None:
    """Not a contact (a robot that slipped through, say). It stays hidden when contacts are built again."""
    with LOCK:
        data = _load()
        if contact_id in data:
            data[contact_id]["hidden"] = True
            _save(data)


def history(contact: dict, limit: int = 15) -> list[dict]:
    from gmail_auto.store import list_processed

    emails = set(contact.get("emails") or [])
    try:
        records = list_processed(3000)
    except ConfigError:
        return []
    return [record for record in records if (record.get("from_email") or "").lower() in emails and not record.get("sensitive")][:limit]


def company_from_domain(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    if not domain or domain in FREE_MAIL or domain.endswith((".edu", ".gov")):
        return ""
    parts = domain.split(".")
    while len(parts) > 2 and parts[-1] in {"nz", "au", "uk", "cn", "hk", "sg"} and parts[-2] in {"co", "com", "org", "net", "ac", "govt", "school"}:
        parts = parts[:-2] + [parts[-1]]
        break
    label = parts[-2] if len(parts) >= 2 else parts[0]
    return label.replace("-", " ").title() if len(label) > 2 else label.upper()


# ---- internals ------------------------------------------------------------------------


def _new(address: str, name: str) -> dict:
    return {
        "id": secrets.token_hex(5),
        "emails": [address],
        "name": _clean("name", name),
        "company": "",
        "company_guessed": False,
        "title": "",
        "phone": "",
        "address": "",
        "type": "",
        "type_status": "",
        "type_reason": "",
        "language": "",
        "tone": "",
        "salutation": "",
        "notes": "",
        "letters": 0,
        "replied": 0,
        "first_seen": "",
        "last_seen": "",
        "locked": [],
        "hidden": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
        "updated_at": "",
    }


def _suggest(contact: dict, key: str, value, replace: bool = False) -> None:
    """Fill a field from mail or the model unless the owner set it (or it already holds something)."""
    value = _clean(key, value)
    if not value or key in (contact.get("locked") or []):
        return
    if contact.get(key) and not replace:
        return
    contact[key] = value
    if key == "company":
        contact["company_guessed"] = value == company_from_domain(contact["emails"][0])


def _clean(key: str, value) -> str:
    text = str(value or "").strip()
    if key in ("address", "notes"):
        lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
        return "\n".join(line for line in lines if line)[:600]
    text = " ".join(text.split()).strip("\"'")
    if key == "type":
        return text.lower() if text.lower() in TYPES else ""
    if key in ("language",):
        return text if text in LANGUAGES else ""
    if key == "tone":
        return text.lower() if text.lower() in TONES else ""
    return text[:160]


def _evidence(addresses: set[str]) -> dict[str, list[str]]:
    from gmail_auto.store import list_processed

    out: dict[str, list[str]] = {}
    try:
        records = list_processed(3000)
    except ConfigError:
        return out
    for record in records:
        address = (record.get("from_email") or "").lower()
        if address in addresses and len(out.setdefault(address, [])) < 5:
            out[address].append(f"{record.get('subject') or '(no subject)'}: {(record.get('summary') or '')[:160]}")
    return out


def _find_in(data: dict, address: str) -> dict | None:
    for item in data.values():
        if address in item.get("emails") or []:
            return item
    return None


def _load() -> dict:
    data = load_doc("contacts", {})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    save_doc("contacts", data)
