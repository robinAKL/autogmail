"""An Outlook mailbox through Microsoft Graph, with the same shape as the Gmail client."""

import base64
import html
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from gmail_auto import microsoft
from gmail_auto.errors import ConfigError
from gmail_auto.extractors import strip_html
from gmail_auto.models import Mail

DONE = "autogmail-done"
RETRY = "autogmail-retry"
# Listing with HTML bodies and headers is what makes Graph return 502/503 on large mailboxes.
_SELECT_LIST = (
    "id,conversationId,subject,from,replyTo,toRecipients,receivedDateTime,sentDateTime,bodyPreview,"
    "isRead,internetMessageId,categories,inferenceClassification"
)
_SELECT = _SELECT_LIST + ",body,internetMessageHeaders"
_HTML = {"Prefer": 'outlook.body-content-type="html"'}


class OutlookClient:
    provider = "outlook"

    def __init__(self):
        self._email = ""

    def get_user_email(self) -> str:
        if not self._email:
            self._email = (microsoft.status("outlook").get("account") or "").strip()
        if not self._email:
            me = microsoft.graph("outlook", "GET", "/me", params={"$select": "mail,userPrincipalName"})
            self._email = (me.get("mail") or me.get("userPrincipalName") or "").strip()
        if not self._email:
            raise ConfigError("Could not read the address of this Outlook account.")
        return self._email

    def list_unread(self, limit: int = 20, since_ts: int | None = None) -> list[Mail]:
        mails = self._list(
            "inbox",
            limit + 10,
            {
                "$filter": f"receivedDateTime ge {_since(since_ts)} and isRead eq false",
                "$orderby": "receivedDateTime desc",
            },
        )
        wanted = [mail for mail in mails if "DONE" not in mail.label_ids][:limit]
        return [mail for mail in self._hydrate(wanted) if "DONE" not in mail.label_ids]

    def list_recent(self, limit: int = 25, full: bool = False) -> list[Mail]:
        # Newest page only. A date filter on a large folder makes Graph scan and time out.
        mails = self._list_newest("inbox", limit, "receivedDateTime")
        return self._hydrate(mails) if full else mails

    def list_received_history(self, limit: int = 25, full: bool = False) -> list[Mail]:
        """Received mail still in the mailbox, including Archive.

        Junk, Deleted, Sent, and Drafts are not read. The inbox screen keeps list_recent.
        """
        want = max(1, min(int(limit), 25))
        found: dict[str, Mail] = {}
        for folder in ("inbox", "archive"):
            try:
                page = self._list_newest(folder, want, "receivedDateTime")
            except ConfigError:
                continue
            for mail in page:
                if mail.id and mail.id not in found:
                    found[mail.id] = mail
        mails = sorted(found.values(), key=lambda mail: mail.received_ts or 0, reverse=True)[:want]
        return self._hydrate(mails) if full else mails

    def list_recent_page(self, limit: int = 20, token: str = "", full: bool = False) -> tuple[list[Mail], str]:
        """One inbox page, plus Graph's nextLink. Does not follow that link."""
        want = max(1, min(int(limit), 50))
        path = token or "/me/mailFolders/inbox/messages"
        params = None if token else {"$top": want, "$select": _SELECT_LIST, "$orderby": "receivedDateTime desc"}
        try:
            data = microsoft.graph("outlook", "GET", path, params=params, timeout=8, attempts=1)
        except ConfigError:
            if token:
                raise
            data = microsoft.graph(
                "outlook",
                "GET",
                path,
                params={"$top": want, "$select": _SELECT_LIST},
                timeout=8,
                attempts=1,
            )
        mails = [self._stamp(parse_message(item)) for item in data.get("value") or []]
        if full:
            mails = self._hydrate(mails)
        return mails[:want], data.get("@odata.nextLink") or ""

    def search(self, text: str, limit: int = 25, raw: bool = False) -> list[Mail]:
        """Outlook's own search over every folder. Graph takes plain words; Gmail operators are dropped."""
        words = re.sub(r"\b(?:subject|from|to|newer_than|older_than|after|before|in|label|has):\S*", " ", text or "")
        words = re.sub(r"[\"“”()]|\bOR\b", " ", words)
        words = " ".join(words.split())[:200]
        if not words:
            return []
        data = microsoft.graph(
            "outlook",
            "GET",
            "/me/messages",
            params={"$search": f'"{words}"', "$top": max(1, min(int(limit), 50)), "$select": _SELECT_LIST},
            timeout=10,
            attempts=1,
        )
        return [self._stamp(parse_message(item)) for item in data.get("value") or []]

    def list_sent(self, limit: int = 25, full: bool = False) -> list[Mail]:
        mails = self._list_newest("sentitems", limit, "sentDateTime")
        return self._hydrate(mails) if full else mails

    def get_message(self, message_id: str) -> Mail:
        raw = microsoft.graph("outlook", "GET", f"/me/messages/{message_id}", params={"$select": _SELECT}, headers=_HTML)
        return self._stamp(parse_message(raw))

    def send_reply(
        self,
        mail: Mail,
        body: str,
        user_email: str,
        attachments: list | None = None,
        html_suffix: str = "",
        inline_images: list | None = None,
    ) -> str:
        """Reply on the same conversation. The draft Outlook builds already quotes the original below.

        html_suffix is appended as HTML after the escaped text. inline_images are
        (content_id, filename, mime, bytes) and referenced from that HTML as cid:content_id.
        A saved logo for this Outlook account is added the same way.
        """
        logo_html, logo_images = _saved_outlook_logo(self)
        images = _inline_attachments([*(inline_images or []), *logo_images])
        draft = microsoft.graph("outlook", "POST", f"/me/messages/{mail.id}/createReply", headers=_HTML)
        draft_id = draft.get("id")
        if not draft_id:
            raise ConfigError("Outlook did not create the reply.")
        quoted = (draft.get("body") or {}).get("content") or ""
        mine = _escaped_html(body)
        suffix = (html_suffix or "").strip()
        if logo_html:
            suffix = f"{suffix}<br>{logo_html}" if suffix else logo_html
        if suffix:
            mine += suffix + "<br>"
        microsoft.graph(
            "outlook",
            "PATCH",
            f"/me/messages/{draft_id}",
            body={"body": {"contentType": "HTML", "content": mine + quoted}},
        )
        _add_attachments(draft_id, attachments)
        _add_inline_images(draft_id, images)
        microsoft.graph("outlook", "POST", f"/me/messages/{draft_id}/send")
        return draft_id

    def send_notice(self, to: str, subject: str, body: str) -> None:
        self.send_new(to, subject, body)

    def send_new(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list | None = None,
        cc: str = "",
        html_suffix: str = "",
        inline_images: list | None = None,
    ) -> None:
        """A new email (not a reply). to and cc may hold several addresses separated by commas."""
        logo_html, logo_images = _saved_outlook_logo(self)
        images = _inline_attachments([*(inline_images or []), *logo_images])
        suffix = (html_suffix or "").strip()
        if logo_html:
            suffix = f"{suffix}<br>{logo_html}" if suffix else logo_html
        if suffix or images:
            content = _escaped_html(body)
            if suffix:
                content += suffix
            body_part = {"contentType": "HTML", "content": content}
        else:
            body_part = {"contentType": "Text", "content": body}
        message = {
            "subject": subject,
            "body": body_part,
            "toRecipients": [{"emailAddress": {"address": address.strip()}} for address in to.split(",") if address.strip()],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": address.strip()}} for address in cc.split(",") if address.strip()]
        file_bytes = sum(len(data) for _, _, data in attachments or [])
        inline_bytes = sum(len(item[3]) for item in [*(inline_images or []), *logo_images])
        if any(len(data) > _INLINE_BYTES for _, _, data in attachments or []) or file_bytes + inline_bytes > _INLINE_BYTES:
            # Too big to send in one call: make a draft, add the files (big ones through an upload session), send it.
            draft = microsoft.graph("outlook", "POST", "/me/messages", body=message)
            draft_id = draft.get("id")
            if not draft_id:
                raise ConfigError("Outlook did not create the email.")
            _add_attachments(draft_id, attachments)
            _add_inline_images(draft_id, images)
            microsoft.graph("outlook", "POST", f"/me/messages/{draft_id}/send")
            return
        files = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename,
                "contentType": mime or "application/octet-stream",
                "contentBytes": base64.b64encode(data).decode("ascii"),
            }
            for filename, mime, data in attachments or []
        ]
        if files or images:
            message["attachments"] = files + images
        microsoft.graph("outlook", "POST", "/me/sendMail", body={"message": message})

    def mark_read(self, message_id: str) -> None:
        microsoft.graph("outlook", "PATCH", f"/me/messages/{message_id}", body={"isRead": True})

    def archive(self, message_id: str) -> None:
        """Delete the letter into Deleted Items."""
        try:
            microsoft.graph("outlook", "POST", f"/me/messages/{message_id}/move", body={"destinationId": "deleteditems"})
        except ConfigError:
            microsoft.graph("outlook", "POST", f"/me/messages/{message_id}/move", body={"destinationId": "archive"})

    def archive_many(self, message_ids: list[str]) -> None:
        for message_id in message_ids:
            if message_id:
                self.archive(message_id)

    def has_retry(self, mail: Mail) -> bool:
        return "RETRY" in (mail.label_ids or [])

    def note_retry(self, message_id: str) -> None:
        self._categorize(message_id, add=RETRY)

    def note_done(self, message_id: str) -> None:
        self._categorize(message_id, add=DONE, remove=RETRY)

    def _categorize(self, message_id: str, add: str, remove: str = "") -> None:
        current = microsoft.graph("outlook", "GET", f"/me/messages/{message_id}", params={"$select": "categories"})
        categories = [item for item in current.get("categories") or [] if item != remove]
        if add not in categories:
            categories.append(add)
        microsoft.graph("outlook", "PATCH", f"/me/messages/{message_id}", body={"categories": categories})

    def _list_newest(self, folder: str, limit: int, time_field: str) -> list[Mail]:
        extra = {"$orderby": f"{time_field} desc"}
        try:
            return self._list(folder, limit, extra)
        except ConfigError:
            return self._list(folder, min(int(limit), 12), {})

    def _list(self, folder: str, limit: int, extra: dict) -> list[Mail]:
        want = max(1, min(int(limit), 500))
        out: list[Mail] = []
        params: dict | None = {"$top": min(50, want), "$select": _SELECT_LIST, **extra}
        path: str | None = f"/me/mailFolders/{folder}/messages"
        while path and len(out) < want:
            data = microsoft.graph("outlook", "GET", path, params=params, timeout=8, attempts=1)
            out.extend(self._stamp(parse_message(item)) for item in data.get("value") or [])
            path = data.get("@odata.nextLink") or ""
            params = None
        return out[:want]

    def _hydrate(self, mails: list[Mail]) -> list[Mail]:
        """Load text bodies in small batches. Keep the list preview if Graph refuses a batch."""
        if not mails:
            return []
        filled: dict[int, Mail] = {}
        for start in range(0, len(mails), 5):
            chunk = mails[start : start + 5]
            requests = []
            for index, mail in enumerate(chunk):
                encoded = urllib.parse.quote(mail.id, safe="")
                requests.append(
                    {
                        "id": str(index),
                        "method": "GET",
                        "url": f"/me/messages/{encoded}?$select={_SELECT_LIST},body",
                        "headers": {"Prefer": 'outlook.body-content-type="text"'},
                    }
                )
            try:
                data = microsoft.graph("outlook", "POST", "/$batch", body={"requests": requests}, timeout=12, attempts=1)
            except ConfigError:
                for index, mail in enumerate(chunk):
                    filled[start + index] = mail
                continue
            by_id = {str(item.get("id")): item for item in data.get("responses") or []}
            for index, mail in enumerate(chunk):
                item = by_id.get(str(index)) or {}
                payload = item.get("body")
                status = int(item.get("status") or 0)
                if 200 <= status < 300 and isinstance(payload, dict):
                    filled[start + index] = self._stamp(parse_message(payload))
                else:
                    filled[start + index] = mail
        return [filled[index] for index in range(len(mails))]

    def _stamp(self, mail: Mail) -> Mail:
        mail.account = self._email
        return mail


def parse_message(raw: dict) -> Mail:
    sender = (raw.get("from") or {}).get("emailAddress") or {}
    reply_to = raw.get("replyTo") or []
    reply_email = ""
    if reply_to:
        reply_email = ((reply_to[0] or {}).get("emailAddress") or {}).get("address") or ""
    headers: dict[str, str] = {}
    for item in raw.get("internetMessageHeaders") or []:
        name = (item.get("name") or "").strip().lower()
        if name and name not in headers:
            headers[name] = item.get("value") or ""
    if raw.get("internetMessageId"):
        headers.setdefault("message-id", raw["internetMessageId"])
    recipients = [((item or {}).get("emailAddress") or {}).get("address") or "" for item in raw.get("toRecipients") or []]
    if recipients:
        headers.setdefault("to", ", ".join(item for item in recipients if item))

    body = raw.get("body") or {}
    content = body.get("content") or ""
    rich = content if (body.get("contentType") or "").lower() == "html" else ""
    text = strip_html(content) if rich else content

    labels = ["INBOX"]
    if raw.get("isRead") is False:
        labels.append("UNREAD")
    categories = raw.get("categories") or []
    if DONE in categories:
        labels.append("DONE")
    if RETRY in categories:
        labels.append("RETRY")

    received = _timestamp(raw.get("receivedDateTime") or raw.get("sentDateTime") or "")
    return Mail(
        id=raw.get("id", ""),
        thread_id=raw.get("conversationId", ""),
        from_name=(sender.get("name") or "").strip(),
        from_email=(sender.get("address") or "").strip(),
        reply_email="" if reply_email.lower() == (sender.get("address") or "").lower() else reply_email.strip(),
        subject=(raw.get("subject") or "").strip(),
        date=datetime.fromtimestamp(received).strftime("%Y-%m-%d %H:%M") if received else "",
        body=text.strip(),
        headers=headers,
        label_ids=labels,
        snippet=raw.get("bodyPreview") or "",
        html=rich,
        provider="outlook",
        received_ts=received,
    )


def _since(since_ts: int | None = None) -> str:
    if since_ts and int(since_ts) > 0:
        return datetime.fromtimestamp(int(since_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# Graph takes a file inside one request up to 3 MB; bigger ones go through an upload session in pieces.
_INLINE_BYTES = 3_000_000
_CHUNK = 320 * 1024 * 12  # pieces must be multiples of 320 KiB


def _saved_outlook_logo(client) -> tuple[str, list]:
    """Saved logo for this Outlook mailbox, with a content id we generate."""
    account = getattr(client, "_email", "") or ""
    if not account:
        try:
            account = (microsoft.status("outlook").get("account") or "").strip()
        except Exception:
            account = ""
    if not account:
        return "", []
    from gmail_auto.signatures import load_logo
    import secrets

    logo = load_logo("outlook", account)
    if not logo:
        return "", []
    filename, mime, data = logo
    subtype = "img"
    if isinstance(mime, str) and "/" in mime:
        subtype = mime.split("/", 1)[1].split("+", 1)[0]
    subtype = "".join(ch for ch in subtype.lower() if ch.isalnum())[:12] or "img"
    cid = f"logo.{subtype}.{secrets.token_hex(6)}@autogmail"
    image = f'<img src="cid:{html.escape(cid, quote=True)}" alt="Company logo" width="120">'
    return image, [(cid, filename or "logo", mime or "image/png", data)]


def _escaped_html(body: str) -> str:
    return "<div>" + html.escape(body or "").replace("\n", "<br>") + "</div><br>"


def _inline_attachments(images: list | None) -> list[dict]:
    """Graph inline files. A logo bigger than one request can hold is refused, not sent as a download."""
    parts: list[dict] = []
    for item in images or []:
        if not isinstance(item, (tuple, list)) or len(item) != 4:
            raise ConfigError("An inline image needs a content id, filename, type, and bytes.")
        content_id, filename, mime, data = item
        cid = str(content_id or "").strip()
        if cid.lower().startswith("cid:"):
            cid = cid[4:].strip()
        if cid.startswith("<") and cid.endswith(">"):
            cid = cid[1:-1].strip()
        if not cid or any(mark in cid for mark in ' "\'<>'):
            raise ConfigError("An inline image needs a content id.")
        raw = bytes(data)
        if len(raw) > _INLINE_BYTES:
            raise ConfigError(f"{filename or 'The signature image'} is too large to embed in the email.")
        parts.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": filename or "image",
                "contentType": mime or "image/png",
                "contentBytes": base64.b64encode(raw).decode("ascii"),
                "isInline": True,
                "contentId": cid,
            }
        )
    return parts


def _add_inline_images(message_id: str, images: list[dict]) -> None:
    for image in images:
        microsoft.graph("outlook", "POST", f"/me/messages/{message_id}/attachments", body=image)


def _add_attachments(message_id: str, attachments: list | None) -> None:
    for filename, mime, data in attachments or []:
        if len(data) <= _INLINE_BYTES:
            microsoft.graph(
                "outlook",
                "POST",
                f"/me/messages/{message_id}/attachments",
                body={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": mime or "application/octet-stream",
                    "contentBytes": base64.b64encode(data).decode("ascii"),
                },
            )
        else:
            _upload_large(message_id, filename, mime, data)


def _upload_large(message_id: str, filename: str, mime: str, data: bytes) -> None:
    import requests

    session = microsoft.graph(
        "outlook",
        "POST",
        f"/me/messages/{message_id}/attachments/createUploadSession",
        body={"AttachmentItem": {"attachmentType": "file", "name": filename, "size": len(data), "contentType": mime or "application/octet-stream"}},
    )
    url = session.get("uploadUrl")
    if not url:
        raise ConfigError(f"Outlook would not take {filename}.")
    start = 0
    while start < len(data):
        piece = data[start : start + _CHUNK]
        end = start + len(piece) - 1
        try:
            # The upload address carries its own permission; sending the sign-in token to it is refused.
            response = requests.put(
                url,
                data=piece,
                headers={"Content-Length": str(len(piece)), "Content-Range": f"bytes {start}-{end}/{len(data)}"},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise ConfigError(f"Could not upload {filename} to Outlook.") from exc
        if response.status_code >= 400:
            raise ConfigError(f"Outlook refused {filename} ({response.status_code}).")
        start = end + 1
