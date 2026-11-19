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

# Gmail takes a message of up to 25 MB once encoded, which leaves about 18 MB of files. Outlook takes files over
# 3 MB through an upload session. Files over ~4 MB go from the browser straight to R2 (see start_direct), since
# the hosted site turns away any request over 4.5 MB.
MAX_FILE_BYTES = 18_000_000
MAX_TOTAL_BYTES = 18_000_000
DIRECT_FROM = 4_000_000
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
        raise ConfigError(f"{filename} is larger than {human_size(MAX_FILE_BYTES)}. Share a link to it instead.")
    name = clean_name(filename)
    mime = (mime or "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
    file_id = secrets.token_hex(8)
    _put(file_id, data, mime)
    return _record(file_id, name, mime, len(data), origin)


def direct_ready() -> bool:
    """Big files can go from the browser straight to R2."""
    return r2_ready()


def start_direct(filename: str, mime: str, size: int) -> dict:
    """A short-lived address the browser can PUT one file to in R2, past the hosted site's 4.5 MB limit."""
    if not direct_ready():
        raise ConfigError("Big files need R2 file storage.")
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise ConfigError("The file is empty.")
    name = clean_name(filename)
    if size > MAX_FILE_BYTES:
        raise ConfigError(f"{name} is {human_size(size)}. Files can be up to {human_size(MAX_FILE_BYTES)}.")
    mime = (mime or "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
    file_id = secrets.token_hex(8)
    return {"id": file_id, "name": name, "mime": mime, "url": presign("PUT", file_id, expires=900)}


def finish_direct(file_id: str, filename: str, mime: str, origin: str = "") -> dict:
    """Record a file the browser put in R2, after checking it is there and not too big."""
    if not _ID.match(file_id or ""):
        raise ConfigError("That upload is not one of ours.")
    known = get(file_id)
    if known is not None:
        return known
    size = _r2_size(file_id)
    if size <= 0:
        raise ConfigError("The upload did not arrive. Try again.")
    if size > MAX_FILE_BYTES:
        _delete(file_id)
        raise ConfigError(f"That file is larger than {human_size(MAX_FILE_BYTES)}.")
    name = clean_name(filename)
    mime = (mime or "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return _record(file_id, name, mime, size, origin)


def download_url(record: dict) -> str:
    """A short-lived link straight to the file in R2, for files too big to pass through the hosted site."""
    if not r2_ready():
        return ""
    return presign("GET", record["id"], expires=600, filename=record.get("name") or "file")


def _record(file_id: str, name: str, mime: str, size: int, origin: str) -> dict:
    record = {
        "id": file_id,
        "name": name,
        "mime": mime,
        "size": size,
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


def _r2_size(file_id: str) -> int:
    import requests

    host, path = _where(file_id)
    signed = sign_v4("HEAD", host, path, {}, b"", os.environ["R2_ACCESS_KEY_ID"].strip(), os.environ["R2_SECRET_ACCESS_KEY"].strip(), region="auto")
    try:
        response = requests.head(f"https://{host}{path}", headers=signed, timeout=20)
    except requests.RequestException as exc:
        raise ConfigError("Could not reach the R2 file storage.") from exc
    if response.status_code == 404:
        return 0
    if response.status_code >= 400:
        raise ConfigError(f"R2 file storage returned an error ({response.status_code}).")
    return int(response.headers.get("content-length") or 0)


def _where(file_id: str) -> tuple[str, str]:
    account = os.environ["R2_ACCOUNT_ID"].strip()
    bucket = os.environ["R2_BUCKET"].strip()
    return f"{account}.r2.cloudflarestorage.com", f"/{bucket}/{_PREFIX}{file_id}"


def presign(method: str, file_id: str, expires: int = 600, filename: str = "", now: datetime | None = None) -> str:
    """A pre-signed R2 address (AWS Signature Version 4 in the query string)."""
    from urllib.parse import quote

    host, path = _where(file_id)
    extra = {"response-content-disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"} if filename else {}
    return presign_url(
        method, host, path, os.environ["R2_ACCESS_KEY_ID"].strip(), os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        "auto", expires, extra, now,
    )


def presign_url(
    method: str, host: str, path: str, access_key: str, secret_key: str, region: str, expires: int,
    extra: dict | None = None, now: datetime | None = None,
) -> str:
    from urllib.parse import quote

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    scope = f"{day}/{region}/s3/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
        **(extra or {}),
    }
    query = "&".join(f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}" for key, value in sorted(params.items()))
    canonical = "\n".join([method.upper(), path, query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{path}?{query}&X-Amz-Signature={signature}"


def allow_browser_uploads(origin: str) -> bool:
    """Let the site's pages PUT files into the bucket (an R2 CORS rule). False when the keys may not change it."""
    import requests

    account = os.environ["R2_ACCOUNT_ID"].strip()
    bucket = os.environ["R2_BUCKET"].strip()
    host = f"{account}.r2.cloudflarestorage.com"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?><CORSConfiguration><CORSRule>'
        f"<AllowedOrigin>{origin}</AllowedOrigin><AllowedMethod>PUT</AllowedMethod><AllowedMethod>GET</AllowedMethod>"
        "<AllowedHeader>*</AllowedHeader><MaxAgeSeconds>3600</MaxAgeSeconds></CORSRule></CORSConfiguration>"
    ).encode()
    signed = sign_v4(
        "PUT", host, f"/{bucket}", {"content-type": "application/xml", "content-md5": _md5(body)}, body,
        os.environ["R2_ACCESS_KEY_ID"].strip(), os.environ["R2_SECRET_ACCESS_KEY"].strip(), region="auto", query="cors=",
    )
    try:
        response = requests.put(f"https://{host}/{bucket}?cors", data=body, headers=signed, timeout=20)
    except requests.RequestException:
        return False
    return response.status_code < 300


def _md5(body: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")


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


def sign_v4(
    method: str, host: str, path: str, headers: dict, body: bytes, access_key: str, secret_key: str, region: str,
    service: str = "s3", now: datetime | None = None, query: str = "",
) -> dict:
    """AWS Signature Version 4, which R2 uses. Paths here only hold characters that need no escaping."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    payload = hashlib.sha256(body or b"").hexdigest()
    sent = {key.lower(): str(value).strip() for key, value in headers.items()}
    sent.update({"host": host, "x-amz-content-sha256": payload, "x-amz-date": stamp})
    names = sorted(sent)
    signed_headers = ";".join(names)
    canonical = "\n".join(
        [method.upper(), path, query, "".join(f"{name}:{sent[name]}\n" for name in names), signed_headers, payload]
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
