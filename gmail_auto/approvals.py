"""Replies that wait for the owner's OK before they are sent.

A waiting reply can be approved, changed or dropped from WhatsApp or from the
Approvals page. Nothing here is sent until one of those says yes.
"""

import re
import time
from datetime import datetime

from gmail_auto import stats
from gmail_auto.errors import ConfigError
from gmail_auto.models import Mail
from gmail_auto.storage import LOCK, load_doc, save_doc
from gmail_auto.store import update_processed

EXPIRE_AFTER = 7 * 24 * 3600
_KEEP_DECIDED = 200


def create(mail: Mail, draft, language: str, role: str = "ask", kind: str = "") -> dict:
    with LOCK:
        data = _load()
        data["seq"] = int(data.get("seq", 0)) + 1
        item = {
            "id": str(data["seq"]),
            "key": mail.key,
            "provider": getattr(mail, "provider", "gmail") or "gmail",
            "account": getattr(mail, "account", ""),
            "mail_id": mail.id,
            "thread_id": mail.thread_id,
            "from_name": mail.from_name,
            "from_email": mail.from_email,
            "reply_email": mail.reply_email,
            "subject": mail.subject,
            "date": mail.date,
            "received_ts": float(getattr(mail, "received_ts", 0) or 0),
            "message_id": mail.headers.get("message-id", ""),
            "references": mail.headers.get("references", ""),
            "body": (mail.body or "")[:6000],
            "summary": draft.summary,
            "reason": draft.reason,
            "draft": draft.reply,
            "language": language,
            "sources": list(draft.sources or []),
            "status": "pending",
            "role": role,
            "kind": kind,
            "created_at": time.time(),
            "created_text": _now(),
            "decided_at": 0,
            "decided_text": "",
            "via": "",
            "edited": False,
            "wa_ids": [],
        }
        data["items"][item["id"]] = item
        _save(data)
        return item


def pending() -> list[dict]:
    items = [
        item
        for item in _load()["items"].values()
        if item.get("status") == "pending" and item.get("role") != "notice"
    ]
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return items


def open_items() -> list[dict]:
    items = [item for item in _load()["items"].values() if item.get("status") == "pending"]
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return items


def find_by_hint(text: str) -> list[dict]:
    needle = re.sub(r"^(reply|re|回[复覆]?)\s+", "", (text or "").strip(), flags=re.IGNORECASE).lower()
    needle = re.sub(r"#?\s*\d{1,6}\b", " ", needle).strip()
    if len(needle) < 3:
        return []
    words = [
        part
        for part in re.split(r"\s+", needle)
        if len(part) > 2 and part not in {"the", "and", "for", "you", "want", "what", "say", "tell", "them", "that", "this", "with", "from", "will", "we'll"}
    ]
    if not words:
        return []
    found = []
    for item in open_items():
        blob = f"{item.get('from_name') or ''} {item.get('from_email') or ''} {item.get('subject') or ''} {item.get('kind') or ''}".lower()
        if needle in blob or any(word in blob for word in words):
            found.append(item)
    return found


def recent(limit: int = 20) -> list[dict]:
    items = [item for item in _load()["items"].values() if item.get("status") not in ("pending", "sending")]
    items.sort(key=lambda item: item.get("decided_at", 0), reverse=True)
    return items[:limit]


def get(item_id: str) -> dict | None:
    return _load()["items"].get(str(item_id))


def find_by_whatsapp(message_id: str) -> dict | None:
    if not message_id:
        return None
    for item in _load()["items"].values():
        if message_id in (item.get("wa_ids") or []):
            return item
    return None


def remember_whatsapp(item_id: str, message_ids: list[str]) -> None:
    ids = [value for value in message_ids if value]
    if not ids:
        return
    with LOCK:
        data = _load()
        item = data["items"].get(str(item_id))
        if item is None:
            return
        item["wa_ids"] = (item.get("wa_ids") or []) + ids
        _save(data)


def set_draft(item_id: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ConfigError("The reply is empty.")
    if len(text) > 5000:
        raise ConfigError("The reply is too long.")
    with LOCK:
        data = _load()
        item = _pending_item(data, item_id)
        if text != item.get("draft"):
            item["draft"] = text
            item["edited"] = True
            item["role"] = "ask"
            _save(data)
        return item


def revise(item_id: str, instruction: str, profile: dict, rewrite=None) -> dict:
    """Rewrite the waiting draft the way the owner asked."""
    item = get(item_id)
    if item is None or item.get("status") != "pending":
        raise ConfigError("That reply is no longer waiting.")
    if rewrite is None:
        from gmail_auto.generator import revise_reply as rewrite
    text = rewrite(item, instruction, profile)
    return set_draft(item_id, text)


def approve(item_id: str, via: str, client=None) -> dict:
    """Send the waiting reply. Safe to call twice: only the first call sends."""
    with LOCK:
        data = _load()
        item = _pending_item(data, item_id)
        item["status"] = "sending"
        _save(data)
    try:
        if client is None:
            from gmail_auto.mailboxes import client_for

            client = client_for(item.get("provider") or "gmail")
        mail = _mail(item)
        client.send_reply(mail, item["draft"], client.get_user_email())
    except Exception:
        _set_status(item_id, "pending")
        raise
    waited = time.time() - float(item.get("created_at") or time.time())
    item = _decide(item_id, "sent", via)
    update_processed(item["key"], {"action": "sent", "approved": True, "reply": item["draft"], "via": via})
    stats.note_outcome("approved_sent", mail, sources=item.get("sources"))
    stats.note_decision(waited, edited=bool(item.get("edited")))
    for step in ("note_done", "mark_read"):
        action = getattr(client, step, None)
        if action is None:
            continue
        try:
            action(mail.id)
        except Exception:
            pass
    return item


def reject(item_id: str, via: str) -> dict:
    with LOCK:
        _pending_item(_load(), item_id)
    waited_from = float((get(item_id) or {}).get("created_at") or time.time())
    item = _decide(item_id, "rejected", via)
    update_processed(item["key"], {"action": "rejected", "via": via})
    stats.note_outcome("rejected")
    stats.note_decision(time.time() - waited_from)
    return item


def expire_old(now: float | None = None) -> int:
    now = now or time.time()
    expired = []
    with LOCK:
        data = _load()
        for item in data["items"].values():
            if item.get("status") == "pending" and now - float(item.get("created_at") or now) > EXPIRE_AFTER:
                item.update({"status": "expired", "decided_at": now, "decided_text": _now(), "via": "timeout"})
                expired.append(item)
        if expired:
            _save(data)
    for item in expired:
        update_processed(item["key"], {"action": "expired"})
        stats.note_outcome("expired")
    return len(expired)


def _decide(item_id: str, status: str, via: str) -> dict:
    with LOCK:
        data = _load()
        item = data["items"][str(item_id)]
        item.update({"status": status, "decided_at": time.time(), "decided_text": _now(), "via": via})
        decided = [value for value in data["items"].values() if value.get("status") not in ("pending", "sending")]
        if len(decided) > _KEEP_DECIDED:
            decided.sort(key=lambda value: value.get("decided_at", 0))
            for old in decided[: len(decided) - _KEEP_DECIDED]:
                data["items"].pop(old["id"], None)
        _save(data)
        return item


def _set_status(item_id: str, status: str) -> None:
    with LOCK:
        data = _load()
        item = data["items"].get(str(item_id))
        if item is not None:
            item["status"] = status
            _save(data)


def _pending_item(data: dict, item_id: str) -> dict:
    item = data["items"].get(str(item_id))
    if item is None:
        raise ConfigError("That reply was not found.")
    if item.get("status") != "pending":
        raise ConfigError("That reply is no longer waiting.")
    return item


def _mail(item: dict) -> Mail:
    headers = {}
    if item.get("message_id"):
        headers["message-id"] = item["message_id"]
    if item.get("references"):
        headers["references"] = item["references"]
    return Mail(
        id=item.get("mail_id", ""),
        thread_id=item.get("thread_id", ""),
        from_name=item.get("from_name", ""),
        from_email=item.get("from_email", ""),
        reply_email=item.get("reply_email", ""),
        subject=item.get("subject", ""),
        date=item.get("date", ""),
        body=item.get("body", ""),
        headers=headers,
        provider=item.get("provider") or "gmail",
        account=item.get("account", ""),
        received_ts=float(item.get("received_ts") or 0),
    )


def _load() -> dict:
    data = load_doc("approvals", {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("items")
    return {"seq": int(data.get("seq", 0) or 0), "items": items if isinstance(items, dict) else {}}


def _save(data: dict) -> None:
    save_doc("approvals", data)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
