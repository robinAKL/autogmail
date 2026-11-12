"""Microsoft sign-in and Graph calls, shared by Outlook mail and SharePoint files.

Outlook and SharePoint are connected separately, so a personal Outlook.com
account (which has no SharePoint) can still be used for mail.
"""

import os
import re
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
        payload = payload if isinstance(payload, dict) else {}
        reason = explain_error(str(payload.get("error") or ""), str(payload.get("error_description") or ""))
        raise ConfigError(f"Microsoft did not accept the sign-in. {reason}")
    return payload


# Microsoft's error codes that people actually run into, in plain words.
_KNOWN = {
    "AADSTS7000215": "MS_CLIENT_SECRET is not right. In the app's Certificates & secrets, copy the secret's Value column, not its Secret ID, then redeploy.",
    "AADSTS7000222": "The client secret has expired. Create a new one under Certificates & secrets and update MS_CLIENT_SECRET.",
    "AADSTS700016": "MS_CLIENT_ID does not match an app Microsoft can find. Copy the Application (client) ID from the app's Overview page.",
    "AADSTS50011": "The redirect address does not match. Add the address shown on the Connections page to the app's Authentication page, type Web.",
    "AADSTS50194": "The app only accepts accounts from its own organisation. Set MS_TENANT_ID to your Directory (tenant) ID, or change Supported account types to include other organisations.",
    "AADSTS50020": "This account is not allowed by the app's Supported account types. Change them in the app's Authentication page.",
    "AADSTS65001": "Your organisation needs an administrator to approve this app. In Microsoft Entra open the app, go to API permissions, add the permissions and choose Grant admin consent.",
    "AADSTS65004": "The permission screen was declined. If it asked for an administrator, open the app in Microsoft Entra, go to API permissions and choose Grant admin consent.",
    "AADSTS90094": "Your organisation needs an administrator to approve this app. In Microsoft Entra open the app, go to API permissions, add the permissions and choose Grant admin consent.",
    "AADSTS900971": "Microsoft did not get a redirect address. Try again from the Connections page.",
    "AADSTS9002313": "The request to Microsoft was malformed. Try again from the Connections page.",
    "AADSTS54005": "That sign-in code was already used. Try connecting again.",
    "AADSTS70008": "That sign-in code expired. Try connecting again.",
}
_BY_ERROR = {
    "access_denied": _KNOWN["AADSTS65004"],
    "consent_required": _KNOWN["AADSTS65001"],
    "invalid_client": "MS_CLIENT_ID or MS_CLIENT_SECRET is not right. Copy the secret's Value, not its Secret ID, then redeploy.",
    "unauthorized_client": "The app is not set up for this kind of sign-in. Check that the redirect address was added with platform Web.",
}


def explain_error(error: str, description: str) -> str:
    """Microsoft's reply in a sentence someone can act on, keeping its code for searching."""
    match = re.search(r"AADSTS\d+", description or "")
    code = match.group(0) if match else ""
    if code in _KNOWN:
        return f"{_KNOWN[code]} ({code})"
    if error in _BY_ERROR:
        return _BY_ERROR[error] + (f" ({code})" if code else "")
    first = re.split(r"[\r\n]|Trace ID", description or "")[0].strip()
    first = re.sub(r"^AADSTS\d+:\s*", "", first)[:240]
    if first:
        return f"Microsoft said: {first}" + (f" ({code})" if code else "")
    return f"Microsoft returned '{error}'." if error else "Try again."


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
