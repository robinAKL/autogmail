"""An Outlook mailbox through Microsoft Graph, with the same shape as the Gmail client."""

import html
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

    def list_unread(self, limit: int = 20) -> list[Mail]:
        mails = self._list(
            "inbox",
            limit + 10,
            {
                "$filter": f"receivedDateTime ge {_since()} and isRead eq false",
                "$orderby": "receivedDateTime desc",
            },
        )
        wanted = [mail for mail in mails if "DONE" not in mail.label_ids][:limit]
        return [mail for mail in self._hydrate(wanted) if "DONE" not in mail.label_ids]

    def list_recent(self, limit: int = 25, full: bool = False) -> list[Mail]:
        # Newest page only. A date filter on a large folder makes Graph scan and time out.
        mails = self._list("inbox", limit, {"$orderby": "receivedDateTime desc"})
        return self._hydrate(mails) if full else mails

    def list_sent(self, limit: int = 25, full: bool = False) -> list[Mail]:
        mails = self._list("sentitems", limit, {"$orderby": "sentDateTime desc"})
        return self._hydrate(mails) if full else mails

    def get_message(self, message_id: str) -> Mail:
        raw = microsoft.graph("outlook", "GET", f"/me/messages/{message_id}", params={"$select": _SELECT}, headers=_HTML)
        return self._stamp(parse_message(raw))

    def send_reply(self, mail: Mail, body: str, user_email: str) -> str:
        """Reply on the same conversation. The draft Outlook builds already quotes the original below."""
        draft = microsoft.graph("outlook", "POST", f"/me/messages/{mail.id}/createReply", headers=_HTML)
        draft_id = draft.get("id")
        if not draft_id:
            raise ConfigError("Outlook did not create the reply.")
        quoted = (draft.get("body") or {}).get("content") or ""
        mine = "<div>" + html.escape(body).replace("\n", "<br>") + "</div><br>"
        microsoft.graph(
            "outlook",
            "PATCH",
            f"/me/messages/{draft_id}",
            body={"body": {"contentType": "HTML", "content": mine + quoted}},
        )
        microsoft.graph("outlook", "POST", f"/me/messages/{draft_id}/send")
        return draft_id

    def mark_read(self, message_id: str) -> None:
        microsoft.graph("outlook", "PATCH", f"/me/messages/{message_id}", body={"isRead": True})

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

    def _list(self, folder: str, limit: int, extra: dict) -> list[Mail]:
        params = {"$top": max(1, min(int(limit), 50)), "$select": _SELECT_LIST, **extra}
        data = microsoft.graph(
            "outlook",
            "GET",
            f"/me/mailFolders/{folder}/messages",
            params=params,
            timeout=8,
            attempts=1,
        )
        return [self._stamp(parse_message(item)) for item in data.get("value") or []]

    def _hydrate(self, mails: list[Mail]) -> list[Mail]:
        """Load HTML bodies in batches of 20 so Graph is not asked for 25 full letters at once."""
        if not mails:
            return []
        filled: dict[int, Mail] = {}
        for start in range(0, len(mails), 20):
            chunk = mails[start : start + 20]
            requests = []
            for index, mail in enumerate(chunk):
                encoded = urllib.parse.quote(mail.id, safe="")
                requests.append(
                    {
                        "id": str(index),
                        "method": "GET",
                        "url": f"/me/messages/{encoded}?$select={_SELECT}",
                        "headers": _HTML,
                    }
                )
            data = microsoft.graph("outlook", "POST", "/$batch", body={"requests": requests})
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


def _since() -> str:
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
