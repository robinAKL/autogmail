"""WhatsApp as the live channel to the owner.

Works with either Meta's WhatsApp Cloud API or Twilio, whichever has its
settings filled in. Only the owner's own number is ever written to or obeyed.
"""

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from gmail_auto.errors import ConfigError, NotConnected
from gmail_auto.settings import load_settings, normalize_phone

_META_LIMIT = 3800
_TWILIO_LIMIT = 1500
_WINDOW_CLOSED = 131047


@dataclass
class Incoming:
    sender: str
    text: str = ""
    button: str = ""
    context_id: str = ""
    message_id: str = ""


def provider() -> str:
    if os.getenv("WHATSAPP_TOKEN", "").strip() and os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip():
        return "meta"
    if all(os.getenv(name, "").strip() for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")):
        return "twilio"
    return ""


def provider_label() -> str:
    return {"meta": "WhatsApp Cloud API (Meta)", "twilio": "Twilio"}.get(provider(), "")


def recipient() -> str:
    return load_settings()["whatsapp_to"] or normalize_phone(os.getenv("WHATSAPP_TO", ""))


def ready() -> bool:
    return bool(provider() and recipient())


def is_owner(sender: str) -> bool:
    mine = recipient()
    return bool(mine) and normalize_phone(sender) == mine


def send_text(body: str) -> str:
    """Send a plain message to the owner. Returns the provider's message id."""
    kind, to = _target()
    if kind == "twilio":
        return _twilio_send(to, _clip(body, _TWILIO_LIMIT))
    return _meta_send(to, {"type": "text", "text": {"preview_url": False, "body": _clip(body, _META_LIMIT)}})


def send_notice(item: dict) -> str:
    """Tell the owner an important letter arrived, with the number to reply against."""
    return send_text(notice_text(item))


def notice_text(item: dict) -> str:
    who = item.get("from_name") or item.get("from_email") or "Unknown sender"
    kind = item.get("kind") or "mail"
    return "\n".join(
        [
            f"Important · {kind} (#{item['id']})",
            f"From: {who}",
            f"Subject: {item.get('subject') or '(no subject)'}",
            item.get("reason") or item.get("summary") or "",
            "",
            f"To reply, send: {item['id']} <what you want to say>",
            "I will draft it and ask before sending.",
        ]
    ).strip()


def send_approval(item: dict) -> list[str]:
    """Show the owner a waiting reply and how to answer. Returns the message ids, for matching replies."""
    kind, to = _target()
    detail = approval_text(item)
    if kind == "twilio":
        return [_twilio_send(to, _clip(detail + "\n\nReply YES to send, NO to skip, or tell me what to change.", _TWILIO_LIMIT))]
    first = _meta_send(to, {"type": "text", "text": {"preview_url": False, "body": _clip(detail, _META_LIMIT)}})
    buttons = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": f"Send this reply to {_short(item.get('from_name') or item.get('from_email'), 60)}? (#{item['id']})\nOr just type what to change."},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"approve:{item['id']}", "title": "Send"}},
                    {"type": "reply", "reply": {"id": f"reject:{item['id']}", "title": "Don't send"}},
                ]
            },
        },
    }
    return [first, _meta_send(to, buttons)]


def approval_text(item: dict) -> str:
    who = item.get("from_name") or item.get("from_email") or "Unknown sender"
    if item.get("from_name") and item.get("from_email"):
        who = f"{item['from_name']} <{item['from_email']}>"
    lines = [
        f"{'Important · ' + item['kind'] + ' · ' if item.get('kind') else ''}New email needs your OK (#{item['id']})",
        f"From: {who}",
        f"Subject: {item.get('subject') or '(no subject)'}",
    ]
    if item.get("summary"):
        lines.append(f"They want: {item['summary']}")
    if item.get("reason"):
        lines.append(f"Why I'm asking: {item['reason']}")
    lines += ["", "Draft reply:", _clip(item.get("draft", ""), 1800)]
    return "\n".join(lines)


# ---- incoming ---------------------------------------------------------------


def parse_meta(payload: dict) -> list[Incoming]:
    found = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                kind = message.get("type")
                text = ""
                button = ""
                if kind == "text":
                    text = (message.get("text") or {}).get("body") or ""
                elif kind == "interactive":
                    reply = (message.get("interactive") or {}).get("button_reply") or {}
                    button = reply.get("id") or ""
                    text = reply.get("title") or ""
                elif kind == "button":
                    pressed = message.get("button") or {}
                    button = pressed.get("payload") or ""
                    text = pressed.get("text") or ""
                else:
                    continue
                found.append(
                    Incoming(
                        sender=str(message.get("from") or ""),
                        text=text.strip(),
                        button=button.strip(),
                        context_id=str((message.get("context") or {}).get("id") or ""),
                        message_id=str(message.get("id") or ""),
                    )
                )
    return found


def parse_twilio(form) -> list[Incoming]:
    sender = str(form.get("From") or "").replace("whatsapp:", "")
    body = str(form.get("Body") or "").strip()
    if not sender or not body:
        return []
    return [
        Incoming(
            sender=sender,
            text=body,
            button=str(form.get("ButtonPayload") or ""),
            context_id=str(form.get("OriginalRepliedMessageSid") or ""),
            message_id=str(form.get("MessageSid") or ""),
        )
    ]


def meta_signature_ok(raw: bytes, header: str) -> bool:
    """Meta signs each delivery with the app secret. Without the secret set, nothing is accepted."""
    secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


def twilio_signature_ok(url: str, form, header: str) -> bool:
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if not token or not header:
        return False
    signed = url + "".join(f"{key}{form.get(key, '')}" for key in sorted(form.keys()))
    digest = hmac.new(token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), header)


def verify_token_ok(value: str) -> bool:
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    return bool(expected) and hmac.compare_digest(expected, value or "")


# ---- providers --------------------------------------------------------------


def _target() -> tuple[str, str]:
    kind = provider()
    if not kind:
        raise NotConnected("WhatsApp is not set up yet. Add the WhatsApp settings shown on the Connections page.")
    to = recipient()
    if not to:
        raise NotConnected("Add your own WhatsApp number on the Connections page first.")
    return kind, to


def _meta_send(to: str, message: dict) -> str:
    import requests

    version = os.getenv("WHATSAPP_API_VERSION", "").strip() or "v25.0"
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN', '').strip()}"}
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ConfigError("Could not reach WhatsApp.") from exc
    data = _json(response)
    if response.status_code >= 400:
        error = data.get("error") or {}
        code = error.get("code")
        details = (error.get("error_data") or {}).get("details") or error.get("message") or ""
        if code == _WINDOW_CLOSED or "24 hours" in str(details):
            _meta_reopen(url, headers, to)
            raise ConfigError(
                "WhatsApp only lets a business write freely within 24 hours of your last message to it. "
                "Send any message to the bot number from your phone, then try again."
            )
        if response.status_code in (401, 403) or code == 190:
            raise ConfigError("WhatsApp rejected the access token. Check WHATSAPP_TOKEN.")
        raise ConfigError(f"WhatsApp did not accept the message ({code or response.status_code}).")
    messages = data.get("messages") or []
    return str(messages[0].get("id") or "") if messages else ""


def _meta_reopen(url: str, headers: dict, to: str) -> None:
    """Outside the 24-hour window only an approved template may be sent. Use it to invite a reply."""
    template = os.getenv("WHATSAPP_TEMPLATE", "").strip()
    if not template:
        return
    import requests

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": os.getenv("WHATSAPP_TEMPLATE_LANG", "").strip() or "en_US"}},
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=20)
    except requests.RequestException:
        return


def _twilio_send(to: str, body: str) -> str:
    import requests

    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    if not sender.startswith("whatsapp:"):
        sender = "whatsapp:+" + normalize_phone(sender)
    try:
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": sender, "To": f"whatsapp:+{to}", "Body": body},
            auth=(sid, os.getenv("TWILIO_AUTH_TOKEN", "").strip()),
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ConfigError("Could not reach Twilio.") from exc
    data = _json(response)
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise ConfigError("Twilio rejected the account SID or auth token.")
        raise ConfigError(f"Twilio did not accept the message ({data.get('code') or response.status_code}).")
    return str(data.get("sid") or "")


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (cut short)"


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
