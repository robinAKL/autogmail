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
from gmail_auto.storage import load_doc, save_doc, workspace_operation
from gmail_auto.tenant import saas_enabled
from gmail_auto.store import update_processed

EXPIRE_AFTER = 7 * 24 * 3600
_KEEP_DECIDED = 200


def create(mail: Mail, draft, language: str, role: str = "ask", kind: str = "") -> dict:
    with workspace_operation("approvals"):
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
            "draft_l1": getattr(draft, "reply_l1", "") or "",
            "reply_lang": getattr(draft, "reply_lang", "") or language,
            "translated_subject": getattr(mail, "translated_subject", "") or "",
            "translated_body": getattr(mail, "translated_body", "") or "",
            "detected_lang": getattr(mail, "detected_lang", "") or "",
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


NEW_EMAIL_NOTE = "(This is a new email the owner is writing, not a reply to a letter.)"


def create_new(to: str, subject: str, body: str, provider: str = "gmail", language: str = "en", reason: str = "", sources=None, cc: str = "") -> dict:
    """A new email the owner asked for. It waits here like a reply, and is sent as a new message."""
    with workspace_operation("approvals"):
        data = _load()
        data["seq"] = int(data.get("seq", 0)) + 1
        item_id = str(data["seq"])
        item = {
            "id": item_id,
            "mode": "new",
            "key": f"new:{item_id}",
            "provider": provider or "gmail",
            "account": "",
            "mail_id": "",
            "thread_id": "",
            "to": to,
            "cc": cc,
            "from_name": to,
            "from_email": to,
            "reply_email": to,
            "subject": subject,
            "date": _now(),
            "received_ts": 0.0,
            "message_id": "",
            "references": "",
            "body": NEW_EMAIL_NOTE,
            "summary": f"New email to {to}",
            "reason": reason or "You asked for this email.",
            "draft": body,
            "language": language,
            "sources": list(sources or []),
            "status": "pending",
            "role": "ask",
            "kind": "",
            "created_at": time.time(),
            "created_text": _now(),
            "decided_at": 0,
            "decided_text": "",
            "via": "",
            "edited": False,
            "wa_ids": [],
        }
        data["items"][item_id] = item
        _save(data)
        return item


def set_new_fields(item_id: str, to: str, subject: str, cc: str | None = None, provider: str | None = None) -> dict:
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        if item.get("mode") != "new":
            return item
        to, subject = (to or "").strip(), " ".join((subject or "").split())
        if to:
            item.update({"to": to, "from_name": to, "from_email": to, "reply_email": to, "summary": f"New email to {to}"})
        if subject:
            item["subject"] = subject[:200]
        if cc is not None:
            item["cc"] = cc.strip()
        if provider in ("gmail", "outlook"):
            item["provider"] = provider
        _save(data)
        return item


def pending() -> list[dict]:
    items = [
        item
        for item in _load()["items"].values()
        if item.get("status") in ("pending", "unconfirmed") and item.get("role") != "notice"
    ]
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return items


def open_items() -> list[dict]:
    items = [item for item in _load()["items"].values() if item.get("status") in ("pending", "unconfirmed")]
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
        if len(part) > 2 and part not in {
            "the", "and", "for", "you", "want", "what", "say", "tell", "them", "that", "this", "with", "from",
            "will", "we'll", "drop", "skip", "reject", "cancel", "discard", "ignore", "reply", "send",
            "archive", "search", "find", "list", "unread",
        }
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
    hidden = {"pending", "unconfirmed"} if saas_enabled() else {"pending", "sending", "unconfirmed"}
    items = [item for item in _load()["items"].values() if item.get("status") not in hidden]
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
    with workspace_operation("approvals"):
        data = _load()
        item = data["items"].get(str(item_id))
        if item is None:
            return
        item["wa_ids"] = (item.get("wa_ids") or []) + ids
        _save(data)


def draft_translation_languages(item: dict, settings: dict) -> tuple[str, str] | None:
    from gmail_auto.translator import SUPPORTED_LANGUAGES

    if item.get("mode") == "new" or settings.get("translation_mode") == "off" or not settings.get("translate_outgoing"):
        return None
    source = settings.get("first_language", "zh")
    target = item.get("reply_lang") or item.get("detected_lang") or item.get("language")
    if source != target and source in SUPPORTED_LANGUAGES and target in SUPPORTED_LANGUAGES:
        return source, target
    return None


def set_draft(item_id: str, text: str, *, translate: bool = False) -> dict:
    text = (text or "").strip()
    if not text:
        raise ConfigError("The reply is empty.")
    if len(text) > 5000:
        raise ConfigError("The reply is too long.")
    original = ""
    if translate:
        from gmail_auto.settings import load_settings
        from gmail_auto.translator import translate_outbound_reply

        item = _pending_item(_load(), item_id)
        languages = draft_translation_languages(item, load_settings())
        if languages is None:
            raise ConfigError("Translation is unavailable for this reply.")
        source, target = languages
        original = text
        text = translate_outbound_reply(text, target, source)
        if len(text) > 5000:
            raise ConfigError("The translated reply is too long.")
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        if text != item.get("draft"):
            original_text = item["draft"]
            item["draft"] = text
            # A hand edit makes the old first-language preview stale; a translated edit brings its own.
            item["preview_stale"] = not original and (bool(item.get("draft_l1")) or bool(item.get("preview_stale")))
            item["draft_l1"] = original
            item["edited"] = True
            item["role"] = "ask"
            _save(data)
            try:
                from gmail_auto.reviews import record_edit
                record_edit(item_id, original_text, text)
            except ConfigError:
                pass  # Preference learning must not discard a saved draft.
        return item


def configure_review(item_id: str, followup_days: int | None = None, reviewed: bool = False) -> dict:
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        if followup_days is not None:
            if followup_days not in (0, 1, 3, 5, 7):
                raise ConfigError('Choose a supported follow-up interval.')
            if followup_days:
                from gmail_auto.workspace_db import available
                if not available():
                    raise ConfigError('Set up the matters database before turning on follow-up reminders.')
            item['followup_days'] = followup_days
        if reviewed:
            item['reviewed_at'] = time.time()
        _save(data)
        return item


def attach(item_id: str, record: dict) -> dict:
    """Add a kept file (see files.py) to a waiting reply."""
    from gmail_auto.files import MAX_TOTAL_BYTES

    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        listed = list(item.get("attachments") or [])
        if any(entry.get("id") == record["id"] for entry in listed):
            return item
        if sum(int(entry.get("size") or 0) for entry in listed) + int(record.get("size") or 0) > MAX_TOTAL_BYTES:
            raise ConfigError("That is too much to attach to one email (15 MB in all).")
        listed.append({key: record.get(key) for key in ("id", "name", "mime", "size")})
        item["attachments"] = listed
        item["role"] = "ask"
        _save(data)
        return item


def detach(item_id: str, file_id: str) -> dict:
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        item["attachments"] = [entry for entry in item.get("attachments") or [] if entry.get("id") != file_id]
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
    """Send the waiting reply. A second call does not send again unless Sent was checked and the letter is absent."""
    current = get(item_id)
    if current is None:
        raise ConfigError("That reply was not found.")
    if current.get("status") == "unconfirmed":
        if client is None:
            from gmail_auto.mailboxes import client_for

            client = client_for(current.get("provider") or "gmail")
        looked = _already_sent(client, current)
        if looked is True:
            _release_send(item_id)
            return _finish_sent(item_id, via, client, current)
        if looked is None:
            raise ConfigError(
                "The mailbox did not confirm whether this reply was sent. Check Sent before sending it again."
            )
        _release_send(item_id)
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        if not _claim_send(item_id):
            raise ConfigError("That reply is already being sent.")
        item["status"] = "sending"
        _save(data)
    dispatch_started = False
    try:
        if client is None:
            from gmail_auto.mailboxes import client_for

            client = client_for(item.get("provider") or "gmail")
        from gmail_auto.approval_checks import validate, check_latest
        validate(item)
        check_latest(item, client)
        mail = _mail(item)
        attached = _attachment_bytes(item)
        is_new = item.get("mode") == "new"
        if is_new:
            try:
                sender = (client.get_user_email() or "").strip()
            except Exception:
                sender = ""
            user_email = ""
        else:
            user_email = client.get_user_email()
            sender = (user_email or "").strip()
        if saas_enabled() and not is_new:
            original_account = (item.get("account") or "").strip().casefold()
            if not original_account or original_account != user_email.strip().casefold():
                raise ConfigError("Reconnect the mailbox that received this email before sending its reply.")
        from gmail_auto.settings import append_signature, load_settings, signature_for_mailbox

        body = append_signature(
            item["draft"],
            signature_for_mailbox(
                load_settings(),
                provider=item.get("provider") or getattr(client, "provider", ""),
                email=sender,
            ),
        )
        outbound_id = ""
        dispatch_started = True
        if is_new:
            if item.get("cc"):
                outbound_id = client.send_new(item["to"], item.get("subject") or "", body, attachments=attached or None, cc=item["cc"])
            elif attached:
                outbound_id = client.send_new(item["to"], item.get("subject") or "", body, attachments=attached)
            else:
                outbound_id = client.send_notice(item["to"], item.get("subject") or "", body)
        elif attached:
            outbound_id = client.send_reply(mail, body, user_email, attachments=attached)
        else:
            outbound_id = client.send_reply(mail, body, user_email)
    except Exception as exc:
        if saas_enabled() and dispatch_started:
            _release_send(item_id)
            _decide(item_id, "send_unconfirmed", via)
            raise ConfigError("Delivery could not be confirmed. Check Sent mail before trying a new reply.") from exc
        if _send_uncertain(exc):
            _set_status(item_id, "unconfirmed")
            raise ConfigError(
                "The mailbox did not confirm whether this reply was sent. Check Sent before sending it again."
            ) from exc
        _release_send(item_id)
        _set_status(item_id, "pending")
        raise
    _release_send(item_id)
    waited = time.time() - float(item.get("created_at") or time.time())
    item = _decide(item_id, "sent", via)
    _activate_followup(item, outbound_id or '', client)
    if item.get("mode") == "new":
        stats.note_decision(waited, edited=bool(item.get("edited")))
        return item
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
    with workspace_operation("approvals"):
        data = _load()
        item = _pending_item(data, item_id)
        handled = item.get("role") == "notice"
        waited_from = float(item.get("created_at") or time.time())
        item = _record_decision(data, item, "handled" if handled else "rejected", via)
    if handled:
        # Closing a notice records that its owner dealt with it.
        from gmail_auto.store import set_handled

        set_handled(item["key"], True)
        return item
    update_processed(item["key"], {"action": "rejected", "via": via})
    stats.note_outcome("rejected")
    stats.note_decision(time.time() - waited_from)
    return item


def expire_old(now: float | None = None) -> int:
    now = now or time.time()
    expired = []
    with workspace_operation("approvals"):
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
    with workspace_operation("approvals"):
        data = _load()
        item = data["items"][str(item_id)]
        return _record_decision(data, item, status, via)


def _record_decision(data: dict, item: dict, status: str, via: str) -> dict:
    item.update({"status": status, "decided_at": time.time(), "decided_text": _now(), "via": via})
    decided = [value for value in data["items"].values() if value.get("status") not in ("pending", "sending", "send_unconfirmed", "unconfirmed")]
    if len(decided) > _KEEP_DECIDED:
        decided.sort(key=lambda value: value.get("decided_at", 0))
        for old in decided[: len(decided) - _KEEP_DECIDED]:
            data["items"].pop(old["id"], None)
    _save(data)
    return item


def _set_status(item_id: str, status: str) -> None:
    with workspace_operation("approvals"):
        data = _load()
        item = data["items"].get(str(item_id))
        if item is not None:
            item["status"] = status
            _save(data)


def _pending_item(data: dict, item_id: str) -> dict:
    item = data["items"].get(str(item_id))
    if item is None:
        raise ConfigError("That reply was not found.")
    if item.get("status") not in ("pending", "unconfirmed"):
        raise ConfigError("That reply is no longer waiting.")
    return item


_UNCERTAIN = ("timed out", "timeout", "could not reach", "did not respond", "connection reset", "connection aborted")


def _send_uncertain(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) and not isinstance(exc, ConfigError):
        return True
    text = str(exc).lower()
    return any(part in text for part in _UNCERTAIN)


def _claim_name(item_id: str) -> str:
    # Approval ids are only unique inside one workspace, so the claim carries the signed-in user.
    from gmail_auto.tenant import current_user_id

    owner = current_user_id() or "owner"
    return f"autogmail:sending:{owner}:{item_id}"


def _claim_send(item_id: str) -> bool:
    from gmail_auto.token_store import claim_key

    return claim_key(_claim_name(item_id))


def _release_send(item_id: str) -> None:
    from gmail_auto.token_store import release_key

    release_key(_claim_name(item_id))


def _already_sent(client, item: dict) -> bool | None:
    """True when Sent has this letter, False when a listing proved it does not, None when Sent could not be read."""
    listing = getattr(client, "list_sent", None)
    if listing is None:
        return None
    try:
        try:
            sent = listing(15, full=False)
        except TypeError:
            sent = listing(15)
    except Exception:
        return None
    thread = str(item.get("thread_id") or "")
    subject = " ".join(str(item.get("subject") or "").split())
    for mail in sent or []:
        if thread and str(getattr(mail, "thread_id", "") or "") == thread:
            return True
        if subject and " ".join(str(getattr(mail, "subject", "") or "").split()) == subject:
            return True
    return False


def _finish_sent(item_id: str, via: str, client, item: dict) -> dict:
    mail = _mail(item)
    decided = _decide(item_id, "sent", via)
    if item.get("mode") != "new":
        update_processed(item["key"], {"action": "sent", "approved": True, "reply": item["draft"], "via": via})
        stats.note_outcome("approved_sent", mail, sources=item.get("sources"))
    return decided


def _activate_followup(item, outbound_id='', client=None):
    if not item.get('followup_days'):
        return
    try:
        from gmail_auto import matters
        linked = dict(item)
        if client and not linked.get('account'):
            linked['account'] = client.get_user_email()
        if client and outbound_id and not linked.get('thread_id'):
            try:
                sent_mail = client.get_message(outbound_id)
                linked['thread_id'] = sent_mail.thread_id
            except Exception:
                pass  # Keep a visible reminder even if automatic thread matching is unavailable.
        matters.after_send(linked, outbound_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Sent successfully but follow-up activation failed: approval %s', item['id'])
        # Never tell the caller that sending failed after the provider accepted it.
        with workspace_operation("approvals"):
            data = _load()
            if item['id'] in data['items']:
                data['items'][item['id']]['followup_error'] = True
                _save(data)


def _attachment_bytes(item: dict) -> list[tuple[str, str, bytes]]:
    listed = item.get("attachments") or []
    if not listed:
        return []
    from gmail_auto.files import read

    out = []
    for entry in listed:
        try:
            out.append((entry.get("name") or "file", entry.get("mime") or "application/octet-stream", read(entry["id"])))
        except ConfigError as exc:
            raise ConfigError(f"Could not attach {entry.get('name') or 'a file'}: {exc}") from exc
    return out


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
        detected_lang=item.get("detected_lang", ""),
        translated_subject=item.get("translated_subject", ""),
        translated_body=item.get("translated_body", ""),
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
