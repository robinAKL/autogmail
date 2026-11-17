"""Files the owner attaches to replies: uploads, and the originals of knowledge-base documents.

They live in a Cloudflare R2 bucket when R2_* is set (the hosted site needs it, since its disk does not
persist), otherwise in data/files on this computer. The list of files is a small JSON record.
"""

import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import time
from datetime import datetime, timezone

from gmail_auto.errors import ConfigError
from gmail_auto.paths import data_dir
from gmail_auto.storage import LOCK, load_doc, save_doc

MAX_FILE_BYTES = 3_000_000  # Outlook takes attachments up to 3 MB in one call; the hosted upload limit is ~4 MB.
MAX_TOTAL_BYTES = 15_000_000
_PREFIX = "autogmail/files/"
_ID = re.compile(r"^[a-f0-9]{16}$")


def r2_ready() -> bool:
    return all(os.getenv(name, "").strip() for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def ready() -> bool:
    """True when files can be kept: R2 is set, or this is not the hosted site."""
    return r2_ready() or not os.environ.get("VERCEL")


def save(data: bytes, filename: str, mime: str = "", origin: str = "") -> dict:
    """Keep a file and return its record: id, name, mime, size."""
    if not ready():
        raise ConfigError("File storage is not set up. Add R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET in Vercel.")
    if not data:
        raise ConfigError("The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ConfigError(f"{filename} is larger than 3 MB. Share a link to it instead.")
    name = clean_name(filename)
    mime = (mime or "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
    file_id = secrets.token_hex(8)
    _put(file_id, data, mime)
    record = {
        "id": file_id,
        "name": name,
        "mime": mime,
        "size": len(data),
        "origin": origin,
        "added_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    with LOCK:
        index = _index()
        index[file_id] = record
        save_doc("files", index)
    return record


def get(file_id: str) -> dict | None:
    return _index().get(file_id) if _ID.match(file_id or "") else None


def read(file_id: str) -> bytes:
    if get(file_id) is None:
        raise ConfigError("That file is no longer kept.")
    return _get(file_id)


def remove(file_id: str) -> None:
    with LOCK:
        index = _index()
        if index.pop(file_id, None) is None:
            return
        save_doc("files", index)
    try:
        _delete(file_id)
    except ConfigError:
        pass


def clean_name(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\"<>|:*?]", "", name)
    return name[:120] or "file"


def human_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(1, round(size / 1000))} KB"


def _index() -> dict:
    data = load_doc("files", {})
    return data if isinstance(data, dict) else {}


# ---- where the bytes go ---------------------------------------------------------


def _put(file_id: str, data: bytes, mime: str) -> None:
    if r2_ready():
        _r2("PUT", file_id, data, mime)
        return
    folder = data_dir() / "files"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file_id).write_bytes(data)


def _get(file_id: str) -> bytes:
    if r2_ready():
        return _r2("GET", file_id)
    path = data_dir(create=False) / "files" / file_id
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError("That file could not be read.") from exc


def _delete(file_id: str) -> None:
    if r2_ready():
        _r2("DELETE", file_id)
        return
    try:
        (data_dir(create=False) / "files" / file_id).unlink(missing_ok=True)
    except OSError:
        pass


def _r2(method: str, file_id: str, body: bytes = b"", mime: str = "") -> bytes:
    import requests

    account = os.environ["R2_ACCOUNT_ID"].strip()
    bucket = os.environ["R2_BUCKET"].strip()
    host = f"{account}.r2.cloudflarestorage.com"
    path = f"/{bucket}/{_PREFIX}{file_id}"
    headers = {"content-type": mime} if mime else {}
    signed = sign_v4(
        method,
        host,
        path,
        headers,
        body,
        os.environ["R2_ACCESS_KEY_ID"].strip(),
        os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        region="auto",
    )
    try:
        response = requests.request(method, f"https://{host}{path}", data=body or None, headers=signed, timeout=40)
    except requests.RequestException as exc:
        raise ConfigError("Could not reach the R2 file storage.") from exc
    if response.status_code in (401, 403):
        raise ConfigError("R2 refused the request. Check the R2 keys and that they can read and write the bucket.")
    if response.status_code == 404:
        raise ConfigError("That file is no longer in R2.")
    if response.status_code >= 400:
        raise ConfigError(f"R2 file storage returned an error ({response.status_code}).")
    return response.content


def sign_v4(method: str, host: str, path: str, headers: dict, body: bytes, access_key: str, secret_key: str, region: str, service: str = "s3", now: datetime | None = None) -> dict:
    """AWS Signature Version 4, which R2 uses. Paths here only hold characters that need no escaping."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    payload = hashlib.sha256(body or b"").hexdigest()
    sent = {key.lower(): str(value).strip() for key, value in headers.items()}
    sent.update({"host": host, "x-amz-content-sha256": payload, "x-amz-date": stamp})
    names = sorted(sent)
    signed_headers = ";".join(names)
    canonical = "\n".join(
        [method.upper(), path, "", "".join(f"{name}:{sent[name]}\n" for name in names), signed_headers, payload]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    sent["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    sent.pop("host")
    return sent
