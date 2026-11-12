"""Bring SharePoint (or OneDrive) files into the knowledge base."""

import base64
import urllib.parse
from datetime import datetime

from gmail_auto import knowledge, microsoft
from gmail_auto.errors import ConfigError
from gmail_auto.extractors import extract_text, is_supported
from gmail_auto.storage import LOCK, load_doc, save_doc

MAX_FILES = 40
MAX_FILE_BYTES = 12_000_000
_MAX_DEPTH = 3
_SUMMARIZE_FIRST = 10
_FIELDS = "id,name,webUrl,size,folder,file,parentReference,lastModifiedDateTime"


def list_sources() -> list[dict]:
    data = load_doc("kb_sources", [])
    return [item for item in data if isinstance(item, dict) and item.get("url")] if isinstance(data, list) else []


def remove_source(url: str) -> None:
    with LOCK:
        save_doc("kb_sources", [item for item in list_sources() if item["url"] != url])
    for doc in knowledge.list_documents():
        if doc.get("source") == "sharepoint" and (doc.get("meta") or {}).get("root") == url:
            knowledge.delete_document(doc["id"])


def sync(url: str) -> dict:
    """Read the file, folder or site behind a SharePoint link and keep its documents."""
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ConfigError("Paste the full https:// link to a SharePoint site, folder or file.")
    root = _resolve(url)
    files = [root] if root.get("file") else _walk(root)
    if not files:
        raise ConfigError("No readable documents were found there (PDF, Word, PowerPoint, Excel, text).")
    added = 0
    skipped: list[str] = []
    for item in files[:MAX_FILES]:
        name = item.get("name") or "file"
        try:
            drive = (item.get("parentReference") or {}).get("driveId")
            data = microsoft.graph("sharepoint", "GET", f"/drives/{drive}/items/{item['id']}/content", raw=True)
            text = extract_text(name, data)
            knowledge.add_document(
                name,
                text,
                "sharepoint",
                origin=item.get("webUrl") or f"{drive}:{item['id']}",
                meta={"root": url, "modified": item.get("lastModifiedDateTime", "")},
                summarize=added < _SUMMARIZE_FIRST,
            )
            added += 1
        except ConfigError as exc:
            skipped.append(f"{name}: {exc}")
    if not added:
        raise ConfigError(skipped[0] if skipped else "Nothing could be read from that link.")
    entry = {
        "url": url,
        "title": root.get("name") or urllib.parse.urlparse(url).netloc,
        "files": added,
        "synced_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with LOCK:
        save_doc("kb_sources", [item for item in list_sources() if item["url"] != url] + [entry])
    return {**entry, "skipped": skipped, "more": max(0, len(files) - MAX_FILES)}


def share_token(url: str) -> str:
    """Microsoft's way of addressing a file or folder by its web link."""
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return "u!" + encoded


def _resolve(url: str) -> dict:
    try:
        return microsoft.graph("sharepoint", "GET", f"/shares/{share_token(url)}/driveItem", params={"$select": _FIELDS})
    except microsoft.GraphError as exc:
        if exc.status not in (400, 403, 404):
            raise
    # Not a link to a file or folder. Treat it as a site and use its default document library.
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    site_path = "/".join(parts[:2]) if len(parts) >= 2 and parts[0].lower() in ("sites", "teams") else ""
    address = f"/sites/{parsed.netloc}:/{site_path}" if site_path else f"/sites/{parsed.netloc}"
    try:
        site = microsoft.graph("sharepoint", "GET", address, params={"$select": "id,displayName"})
        root = microsoft.graph("sharepoint", "GET", f"/sites/{site['id']}/drive/root", params={"$select": _FIELDS})
    except microsoft.GraphError as exc:
        if exc.status in (400, 404):
            raise ConfigError("That link was not found, or your account cannot open it.") from exc
        raise
    root["name"] = site.get("displayName") or root.get("name")
    return root


def _walk(root: dict) -> list[dict]:
    drive = (root.get("parentReference") or {}).get("driveId")
    if not drive or not root.get("id"):
        return []
    found: list[dict] = []
    queue = [(root["id"], 0)]
    while queue and len(found) < MAX_FILES * 2:
        folder_id, depth = queue.pop(0)
        url = f"/drives/{drive}/items/{folder_id}/children"
        params = {"$top": 200, "$select": _FIELDS}
        while url and len(found) < MAX_FILES * 2:
            page = microsoft.graph("sharepoint", "GET", url, params=params)
            for item in page.get("value") or []:
                if item.get("folder") is not None:
                    if depth + 1 < _MAX_DEPTH:
                        queue.append((item["id"], depth + 1))
                elif item.get("file") is not None and is_supported(item.get("name", "")):
                    if int(item.get("size") or 0) <= MAX_FILE_BYTES:
                        item.setdefault("parentReference", {}).setdefault("driveId", drive)
                        found.append(item)
            url = page.get("@odata.nextLink") or ""
            params = None
    found.sort(key=lambda item: item.get("lastModifiedDateTime", ""), reverse=True)
    return found
