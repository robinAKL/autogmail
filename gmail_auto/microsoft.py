"""Microsoft sign-in and Graph calls, shared by Outlook mail and SharePoint files.

Outlook and SharePoint are connected separately, so a personal Outlook.com
account (which has no SharePoint) can still be used for mail.
"""

import os
import time
import urllib.parse
from datetime import datetime

from gmail_auto.errors import ConfigError, NotConnected
from gmail_auto.storage import delete_doc, load_doc, persistent, save_doc

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = {
    "outlook": ["offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send"],
    "sharepoint": ["offline_access", "User.Read", "Sites.Read.All", "Files.Read.All"],
}
LABELS = {"outlook": "Outlook", "sharepoint": "SharePoint"}


def configured() -> bool:
    return bool(_client_id() and _client_secret())


def authorize_url(kind: str, redirect_uri: str, state: str) -> str:
    _kind(kind)
    query = {
        "client_id": _client_id(),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES[kind]),
        "state": state,
        "prompt": "select_account",
    }
    return f"{_authority()}/authorize?{urllib.parse.urlencode(query)}"


def finish_sign_in(kind: str, code: str, redirect_uri: str) -> str:
    """Trade the code from Microsoft for tokens, keep them, and return the signed-in address."""
    _kind(kind)
    if not persistent():
        raise ConfigError("This hosted site has nowhere to keep the Microsoft sign-in. Connect Vercel KV first.")
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES[kind]),
        }
    )
    if not tokens.get("refresh_token"):
        raise ConfigError("Microsoft did not return a long-lived grant. Try connecting again.")
    record = _record(tokens, {})
    save_doc(_doc(kind), record)
    try:
        me = graph(kind, "GET", "/me", params={"$select": "mail,userPrincipalName,displayName"})
        record["account"] = (me.get("mail") or me.get("userPrincipalName") or "").strip()
    except ConfigError:
        record["account"] = ""
    record["connected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_doc(_doc(kind), record)
    return record["account"]


def status(kind: str) -> dict:
    record = _load(kind)
    return {
        "connected": bool(record.get("refresh_token")),
        "account": record.get("account", ""),
        "connected_at": record.get("connected_at", ""),
    }


def disconnect(kind: str) -> None:
    delete_doc(_doc(_kind(kind)))


def access_token(kind: str) -> str:
    record = _load(kind)
    if not record.get("refresh_token"):
        raise NotConnected(f"{LABELS[kind]} is not connected yet. Open Connections and connect it.")
    if record.get("access_token") and float(record.get("expires_at") or 0) - 120 > time.time():
        return record["access_token"]
    if not configured():
        raise ConfigError("MS_CLIENT_ID and MS_CLIENT_SECRET are not set.")
    try:
        tokens = _token_request(
            {"grant_type": "refresh_token", "refresh_token": record["refresh_token"], "scope": " ".join(SCOPES[kind])}
        )
    except ConfigError as exc:
        raise ConfigError(f"The {LABELS[kind]} sign-in has expired. Open Connections and connect it again.") from exc
    record = _record(tokens, record)
    save_doc(_doc(kind), record)
    return record["access_token"]


def graph(kind: str, method: str, path: str, params: dict | None = None, body: dict | None = None, headers: dict | None = None, raw: bool = False):
    """Call Microsoft Graph. Returns parsed JSON, or the bytes of a file when raw is set."""
    import requests

    url = path if path.startswith("https://") else GRAPH + path
    sent = {"Authorization": f"Bearer {access_token(kind)}"}
    sent.update(headers or {})
    try:
        response = requests.request(method, url, params=params, json=body, headers=sent, timeout=40)
    except requests.RequestException as exc:
        raise ConfigError(f"Could not reach {LABELS[kind]}.") from exc
    if response.status_code >= 400:
        raise _translate(kind, response)
    if raw:
        return response.content
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class GraphError(ConfigError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _translate(kind: str, response) -> GraphError:
    status = response.status_code
    label = LABELS[kind]
    if status == 401:
        return GraphError(f"{label} rejected the sign-in. Open Connections and connect it again.", status)
    if status == 403:
        return GraphError(f"{label} refused this request. Your account may not be allowed to do this, or an admin must approve the app.", status)
    if status == 404:
        return GraphError(f"{label} could not find that item.", status)
    if status == 429:
        return GraphError(f"{label} is rate limiting requests. Try again in a little while.", status)
    return GraphError(f"{label} returned an error ({status}).", status)


def _token_request(fields: dict) -> dict:
    import requests

    if not configured():
        raise ConfigError("Set MS_CLIENT_ID and MS_CLIENT_SECRET first. The README explains how to create them.")
    data = {"client_id": _client_id(), "client_secret": _client_secret(), **fields}
    try:
        response = requests.post(f"{_authority()}/token", data=data, timeout=30)
    except requests.RequestException as exc:
        raise ConfigError("Could not reach Microsoft sign-in.") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
        raise ConfigError("Microsoft did not accept the sign-in. Check MS_CLIENT_ID, MS_CLIENT_SECRET and the redirect address.")
    return payload


def _record(tokens: dict, previous: dict) -> dict:
    record = dict(previous)
    record["access_token"] = tokens.get("access_token", "")
    record["refresh_token"] = tokens.get("refresh_token") or previous.get("refresh_token", "")
    record["expires_at"] = time.time() + float(tokens.get("expires_in") or 3000)
    return record


def _load(kind: str) -> dict:
    data = load_doc(_doc(_kind(kind)), {})
    return data if isinstance(data, dict) else {}


def _doc(kind: str) -> str:
    return f"ms_{kind}"


def _kind(kind: str) -> str:
    if kind not in SCOPES:
        raise ConfigError("Unknown Microsoft connection.")
    return kind


def _authority() -> str:
    tenant = os.getenv("MS_TENANT_ID", "").strip() or "common"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def _client_id() -> str:
    return os.getenv("MS_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("MS_CLIENT_SECRET", "").strip()
