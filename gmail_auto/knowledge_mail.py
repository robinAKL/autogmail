"""Provider cursors and cleanup rules for analysis of historic email."""

import json
from urllib.parse import quote

from gmail_auto import cleanup, microsoft
from gmail_auto.errors import ConfigError
from gmail_auto.filters import Skip, classify, is_status_notice, obvious_junk
from gmail_auto.models import Mail
from gmail_auto.outlook_client import _SELECT_LIST, parse_message

_OUTLOOK_FOLDERS = "/me/mailFolders"
_SKIP_FOLDERS = ("junkemail", "deleteditems", "drafts", "outbox", "sentitems", "recoverableitemsdeletions")
_SKIP_LABELS = {"SPAM", "TRASH", "DRAFT", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
_SKIP_CODES = {"self", "sensitive", "system", "auto", "list", "promotions", "social", "forums", "junk", "notification"}


def page(client, direction: str, cursor: str, limit: int) -> tuple[list[Mail], str]:
    if direction not in ("inbox", "sent"):
        raise ConfigError("Invalid mail history direction.")
    if client.provider == "gmail":
        return client.list_history_page(direction, limit, cursor)
    if client.provider != "outlook":
        raise ConfigError("This mailbox does not support history analysis.")
    if direction == "sent":
        return _outlook_messages(cursor or f"{_OUTLOOK_FOLDERS}/sentitems/messages", limit, sent=True)
    return _outlook_received(cursor, limit)


def exclude(mail: Mail, owner_email: str, settings: dict) -> bool:
    if _SKIP_LABELS.intersection(mail.label_ids) or obvious_junk(mail) or is_status_notice(mail):
        return True
    if cleanup.should_clean(mail, owner_email, settings):
        return True
    if "SENT" in mail.label_ids:
        return False
    decision = classify(mail, owner_email)
    return isinstance(decision, Skip) and decision.code in _SKIP_CODES


def _outlook_received(cursor: str, limit: int) -> tuple[list[Mail], str]:
    if not cursor:
        state = {"excluded": _outlook_exclusions(), "lists": [_OUTLOOK_FOLDERS], "folders": [], "messages": ""}
        return [], json.dumps(state, separators=(",", ":"))
    state = json.loads(cursor)
    if state["messages"] or state["folders"]:
        path = state["messages"]
        if not path:
            path = f"{_OUTLOOK_FOLDERS}/{quote(state['folders'].pop(0), safe='')}/messages"
        letters, state["messages"] = _outlook_messages(path, limit)
    else:
        path = state["lists"].pop(0)
        _discover_folders(path, state)
        letters = []
    if state["messages"] or state["folders"] or state["lists"]:
        return letters, json.dumps(state, separators=(",", ":"))
    return letters, ""


def _outlook_exclusions() -> list[str]:
    payload = {"requests": [
        {"id": name, "method": "GET", "url": f"{_OUTLOOK_FOLDERS}/{name}?$select=id"}
        for name in _SKIP_FOLDERS
    ]}
    result = _graph("POST", "/$batch", body=payload)
    replies = {item["id"]: item for item in result.get("responses") or []}
    excluded = []
    for name in _SKIP_FOLDERS:
        reply = replies.get(name) or {}
        if reply.get("status") == 404:
            continue
        folder_id = (reply.get("body") or {}).get("id")
        if reply.get("status") != 200 or not folder_id:
            raise ConfigError("Outlook could not identify folders to exclude from analysis. Try again.")
        excluded.append(folder_id)
    return excluded


def _discover_folders(path: str, state: dict) -> None:
    params = {"$select": "id,childFolderCount", "$top": 50} if path.startswith("/") else None
    result = _graph("GET", path, params=params)
    if result.get("@odata.nextLink"):
        state["lists"].insert(0, result["@odata.nextLink"])
    for folder in result.get("value") or []:
        folder_id = folder.get("id")
        if not folder_id or folder_id in state["excluded"]:
            continue
        if folder.get("@odata.type", "").endswith("mailSearchFolder"):
            continue
        state["folders"].append(folder_id)
        if folder.get("childFolderCount"):
            state["lists"].append(f"{_OUTLOOK_FOLDERS}/{quote(folder_id, safe='')}/childFolders")


def _outlook_messages(path: str, limit: int, sent: bool = False) -> tuple[list[Mail], str]:
    params = None
    if path.startswith("/"):
        params = {
            "$top": limit,
            "$select": _SELECT_LIST + ",uniqueBody,internetMessageHeaders",
            "$filter": "isDraft eq false",
        }
    result = _graph("GET", path, params=params, headers={"Prefer": 'outlook.body-content-type="text"'})
    mails = []
    for raw in result.get("value") or []:
        mail = parse_message({
            **raw,
            "body": raw.get("uniqueBody") or {},
            "receivedDateTime": raw.get("sentDateTime") if sent else raw.get("receivedDateTime"),
        })
        if not mail.id:
            continue
        if sent:
            mail.label_ids.append("SENT")
        mails.append(mail)
    return mails, result.get("@odata.nextLink") or ""


def _graph(method: str, path: str, **kwargs):
    if not (path.startswith("/") or path.startswith(microsoft.GRAPH + "/")):
        raise ConfigError("Outlook returned an invalid history cursor.")
    return microsoft.graph("outlook", method, path, timeout=10, attempts=1, **kwargs)
