import base64
import html
import json
import os
import re
from email.message import EmailMessage
from email.header import decode_header
from email.utils import formataddr, parseaddr

from gmail_auto.console import say
from gmail_auto.errors import ConfigError, NotConnected
from gmail_auto.filters import reply_subject
from gmail_auto.models import Mail
from gmail_auto.paths import atomic_write, root

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
UNREAD_QUERY = (
    "is:unread in:inbox -category:promotions -category:social "
    "-category:forums -in:chats -in:spam -in:trash -label:autogmail-done"
)
DONE_LABEL = "autogmail-done"
RETRY_LABEL = "autogmail-retry"
RECENT_QUERY = "in:inbox -in:chats -in:spam -in:trash"
SENT_QUERY = "in:sent -in:chats"


class GmailClient:
    provider = "gmail"

    def __init__(self, allow_browser: bool = False):
        creds = load_credentials(allow_browser=allow_browser)
        self.service = _build_service(creds)
        self._email = ""
        self._label_ids: dict[str, str] = {}

    def get_user_email(self) -> str:
        if not self._email:
            try:
                profile = self.service.users().getProfile(userId="me").execute()
            except Exception as exc:
                raise _translate(exc) from exc
            self._email = (profile.get("emailAddress") or "").strip()
        if not self._email:
            raise ConfigError("Could not read the address of this Gmail account.")
        return self._email

    def list_unread(self, limit: int = 20) -> list[Mail]:
        return self._list(UNREAD_QUERY, limit)

    def list_recent(self, limit: int = 25) -> list[Mail]:
        return self._list(RECENT_QUERY, limit)

    def list_sent(self, limit: int = 25) -> list[Mail]:
        return self._list(SENT_QUERY, limit)

    def get_message(self, message_id: str) -> Mail:
        try:
            full = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as exc:
            raise _translate(exc) from exc
        return self._stamp(parse_message(full))

    def send_reply(self, mail: Mail, body: str, user_email: str) -> str:
        payload = build_send_body(mail, body, user_email)
        try:
            sent = self.service.users().messages().send(userId="me", body=payload).execute()
        except Exception as exc:
            raise _translate(exc) from exc
        return sent.get("id", "")

    def send_notice(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["To"] = to
        message["From"] = self.get_user_email()
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as exc:
            raise _translate(exc) from exc

    def mark_read(self, message_id: str) -> None:
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception as exc:
            raise _translate(exc) from exc

    def has_retry(self, mail: Mail) -> bool:
        label_id = self._label_id(RETRY_LABEL, create=False)
        return bool(label_id and label_id in (mail.label_ids or []))

    def note_retry(self, message_id: str) -> None:
        self._add_label(message_id, RETRY_LABEL)

    def note_done(self, message_id: str) -> None:
        done = self._label_id(DONE_LABEL, create=True)
        body: dict = {"addLabelIds": [done]}
        retry = self._label_ids.get(RETRY_LABEL)
        if retry:
            body["removeLabelIds"] = [retry]
        try:
            self.service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        except Exception as exc:
            raise _translate(exc) from exc

    def _add_label(self, message_id: str, name: str) -> None:
        label_id = self._label_id(name, create=True)
        try:
            self.service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except Exception as exc:
            raise _translate(exc) from exc

    def _label_id(self, name: str, create: bool) -> str | None:
        if name in self._label_ids:
            return self._label_ids[name]
        try:
            listed = self.service.users().labels().list(userId="me").execute()
        except Exception as exc:
            raise _translate(exc) from exc
        for label in listed.get("labels") or []:
            label_name = label.get("name")
            label_id = label.get("id")
            if label_name and label_id:
                self._label_ids[label_name] = label_id
        if name in self._label_ids or not create:
            return self._label_ids.get(name)
        try:
            created = self.service.users().labels().create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            ).execute()
        except Exception as exc:
            raise _translate(exc) from exc
        self._label_ids[name] = created["id"]
        return created["id"]

    def _list(self, query: str, limit: int) -> list[Mail]:
        size = max(1, min(int(limit), 25))
        try:
            listed = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=size)
                .execute()
            )
        except Exception as exc:
            raise _translate(exc) from exc
        ids = [item["id"] for item in listed.get("messages", [])]
        if not ids:
            return []
        found: dict[str, dict] = {}
        try:
            # One round trip for the whole page instead of one per letter.
            def keep(request_id, response, exception):
                if exception is None and isinstance(response, dict):
                    found[request_id] = response

            batch = self.service.new_batch_http_request(callback=keep)
            for message_id in ids:
                batch.add(
                    self.service.users().messages().get(userId="me", id=message_id, format="full"),
                    request_id=message_id,
                )
            batch.execute()
        except Exception:
            found = {}
        mails = []
        for message_id in ids:
            full = found.get(message_id)
            if full is None:
                try:
                    full = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
                except Exception as exc:
                    raise _translate(exc) from exc
            mails.append(self._stamp(parse_message(full)))
        return mails

    def _stamp(self, mail: Mail) -> Mail:
        mail.account = self._email
        mail.provider = self.provider
        return mail


def web_client_config() -> dict | None:
    client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def load_credentials(allow_browser: bool):
    from gmail_auto.token_store import load_token_raw

    stored = load_token_raw()
    if stored:
        return _refresh_saved(_credentials_from_raw(stored), None, persist_remote=True)

    in_session = _session_token()
    if in_session:
        return _refresh_saved(_credentials_from_raw(in_session), None, persist_remote=False, persist_session=True)

    env_creds = _credentials_from_env()
    if env_creds is not None:
        return _refresh_saved(env_creds, None, persist_remote=False)

    token_path = root() / "token.json"
    creds = None
    if token_path.exists():
        try:
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise ConfigError("Dependencies are not installed. Run: pip install -r requirements.txt") from exc
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, OSError) as exc:
            if not allow_browser:
                raise ConfigError("token.json could not be opened. Delete it and start the program again.") from exc
            creds = None
        creds = _refresh_saved(creds, token_path, persist_remote=False)
        if creds and creds.valid and creds.has_scopes(SCOPES):
            return creds

    if not allow_browser or os.environ.get("VERCEL"):
        if creds and not creds.has_scopes(SCOPES):
            raise ConfigError("The saved grant is missing permission to read or send mail. Connect Gmail again.")
        if os.environ.get("VERCEL"):
            raise NotConnected("Gmail is not connected yet. Open Connections and choose Connect Gmail.")
        raise NotConnected("Gmail is not connected on this computer yet. Start the program again and approve it in the browser that opens.")

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_file = find_client_file()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError("The Gmail credentials file could not be opened. Use the JSON downloaded for a Desktop app.") from exc
    say("Opening the browser. Approve access with your own Gmail account, then close that page.")
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="If the browser did not open, copy this link into it: {url}",
        success_message="Gmail is connected. You can close this page.",
    )
    atomic_write(token_path, creds.to_json())
    return creds


def _credentials_from_env():
    raw = os.getenv("GMAIL_TOKEN_JSON", "").strip()
    if not raw:
        return None
    return _credentials_from_raw(raw)


def _credentials_from_raw(raw: str):
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("The saved Gmail grant is not valid JSON.") from exc
    if not isinstance(info, dict):
        raise ConfigError("The saved Gmail grant is not valid JSON.")
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise ConfigError("Dependencies are not installed. Run: pip install -r requirements.txt") from exc
    try:
        return Credentials.from_authorized_user_info(info, SCOPES)
    except ValueError as exc:
        raise ConfigError("The saved Gmail grant is missing refresh_token, client_id or client_secret.") from exc


def _session_token() -> str | None:
    try:
        from flask import has_request_context, session
    except ImportError:
        return None
    if not has_request_context():
        return None
    raw = session.get("gmail_token")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _remember_session_token(raw: str) -> None:
    try:
        from flask import has_request_context, session
    except ImportError:
        return
    if has_request_context():
        session["gmail_token"] = raw
        session.modified = True


def _refresh_saved(creds, token_path, persist_remote: bool, persist_session: bool = False):
    if not (creds and creds.expired and creds.refresh_token):
        return creds
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise ConfigError("Dependencies are not installed. Run: pip install -r requirements.txt") from exc
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        if token_path is not None:
            token_path.unlink(missing_ok=True)
        again = "Open Connections and connect Gmail again." if token_path is None else "Start the program again and approve it once more."
        raise ConfigError(f"The saved Gmail grant has expired. {again}") from exc
    except Exception as exc:
        raise ConfigError("The Gmail grant could not be refreshed. Check the network and try again.") from exc
    raw = creds.to_json()
    if token_path is not None:
        try:
            atomic_write(token_path, raw)
        except OSError:
            pass
    if persist_remote:
        from gmail_auto.token_store import save_token_raw

        save_token_raw(raw)
    if persist_session:
        _remember_session_token(raw)
    return creds


def find_client_file():
    primary = root() / "credentials.json"
    if primary.exists():
        chosen = primary
    else:
        matches = sorted(root().glob("client_secret*.json"))
        if len(matches) > 1:
            raise ConfigError("There are several client_secret files in the folder. Keep one, or rename it to credentials.json.")
        if len(matches) == 1:
            chosen = matches[0]
        else:
            raise NotConnected(
                "Gmail credentials were not found. Put the downloaded JSON in the program folder and name it credentials.json."
            )
    try:
        payload = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("The Gmail credentials file could not be opened. Download the Desktop app JSON again.") from exc
    if not isinstance(payload, dict) or "installed" not in payload:
        raise ConfigError("These credentials are not for a Desktop app. Create an OAuth client of type Desktop app in Google Cloud.")
    return chosen


def parse_message(raw: dict) -> Mail:
    payload = raw.get("payload") or {}
    headers: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = (item.get("name") or "").strip().lower()
        if name and name not in headers:
            headers[name] = decode_mime_header(item.get("value") or "")
    from_name, from_email = parseaddr(headers.get("from", ""))
    _, reply_email = parseaddr(headers.get("reply-to", ""))
    internal = raw.get("internalDate")
    received = 0.0
    if internal:
        from datetime import datetime

        received = int(internal) / 1000
        date = datetime.fromtimestamp(received).strftime("%Y-%m-%d %H:%M")
    else:
        date = headers.get("date", "")
    body, rich = extract_parts(payload)
    return Mail(
        id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        from_name=from_name.strip(),
        from_email=from_email.strip(),
        reply_email=reply_email.strip(),
        subject=headers.get("subject", "").strip(),
        date=date,
        body=body.strip(),
        headers=headers,
        label_ids=list(raw.get("labelIds") or []),
        snippet=html.unescape(raw.get("snippet") or ""),
        html=rich,
        received_ts=received,
    )


def extract_body(payload: dict) -> str:
    return extract_parts(payload)[0]


def extract_parts(payload: dict) -> tuple[str, str]:
    """The text used for drafting, and the HTML shown on screen when the letter has one."""
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        disposition = ""
        for item in part.get("headers") or []:
            if (item.get("name") or "").lower() == "content-disposition":
                disposition = item.get("value") or ""
        if "attachment" in disposition.lower():
            return
        data = (part.get("body") or {}).get("data")
        mime = part.get("mimeType") or ""
        if data and mime == "text/plain":
            plain.append(decode_b64(data))
        elif data and mime == "text/html":
            html_parts.append(decode_b64(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    rich = "\n".join(html_parts)
    if plain:
        return "\n".join(plain), rich
    if html_parts:
        return strip_html(rich), rich
    data = (payload.get("body") or {}).get("data")
    if not data:
        return "", ""
    text = decode_b64(data)
    if "html" in (payload.get("mimeType") or ""):
        return strip_html(text), text
    return text, ""


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</tr>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_b64(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    chunks = []
    for text, encoding in decode_header(value):
        if isinstance(text, bytes):
            chunks.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            chunks.append(text)
    return "".join(chunks)


def build_reply_message(mail: Mail, body: str, user_email: str) -> EmailMessage:
    recipient = (mail.reply_email or mail.from_email).strip()
    if not recipient:
        raise ConfigError("This letter has no address to reply to.")
    message = EmailMessage()
    if mail.reply_email:
        message["To"] = recipient
    elif mail.from_name:
        message["To"] = formataddr((mail.from_name, recipient))
    else:
        message["To"] = recipient
    message["From"] = user_email
    message["Subject"] = reply_subject(mail.subject)
    message_id = (mail.headers.get("message-id") or "").strip()
    references = (mail.headers.get("references") or "").strip()
    if message_id:
        message["In-Reply-To"] = message_id
        message["References"] = f"{references} {message_id}".strip()
    message.set_content(body, subtype="plain", charset="utf-8")
    return message


def build_send_body(mail: Mail, body: str, user_email: str) -> dict:
    message = build_reply_message(mail, body, user_email)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    payload = {"raw": raw}
    if mail.thread_id:
        payload["threadId"] = mail.thread_id
    return payload


def _build_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _translate(exc: Exception) -> ConfigError:
    if isinstance(exc, ConfigError):
        return exc
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    if status in (401, 403):
        return ConfigError("Gmail refused this request. Check that you connected your own mailbox and that the Gmail API is enabled.")
    if status == 429:
        return ConfigError("Gmail is rate limiting requests. Try again in a little while.")
    return ConfigError("Could not reach Gmail.")
