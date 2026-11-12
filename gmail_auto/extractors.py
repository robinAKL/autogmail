"""Turn an uploaded or downloaded file into plain text for the knowledge base."""

import html
import io
import re
import zipfile
from email import message_from_bytes
from email.policy import default as email_policy
from xml.etree import ElementTree

from gmail_auto.errors import ConfigError

TEXT_TYPES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml", ".ini", ".rtf"}
SUPPORTED = TEXT_TYPES | {".html", ".htm", ".eml", ".pdf", ".docx", ".pptx", ".xlsx"}
_MEMBER_LIMIT = 30_000_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def extension(filename: str) -> str:
    name = (filename or "").lower().strip()
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else ""


def is_supported(filename: str) -> bool:
    return extension(filename) in SUPPORTED


def extract_text(filename: str, data: bytes) -> str:
    kind = extension(filename)
    if kind not in SUPPORTED:
        listed = ", ".join(sorted(item.lstrip(".") for item in SUPPORTED))
        raise ConfigError(f"'{filename}' is not a supported file type. Supported: {listed}.")
    try:
        if kind in TEXT_TYPES:
            text = decode(data)
        elif kind in (".html", ".htm"):
            text = strip_html(decode(data))
        elif kind == ".eml":
            text = _eml(data)
        elif kind == ".pdf":
            text = _pdf(data)
        elif kind == ".docx":
            text = _docx(data)
        elif kind == ".pptx":
            text = _pptx(data)
        else:
            text = _xlsx(data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"'{filename}' could not be read. It may be damaged or password protected.") from exc
    text = tidy(text)
    if not text:
        raise ConfigError(f"No text was found in '{filename}'. Scanned images are not read.")
    return text


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("latin-1", errors="replace")


def strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style|head|title)\b.*?>.*?</\1>", " ", value)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>|</div>|</tr>|</li>|</h[1-6]>|</table>", "\n", text)
    text = re.sub(r"(?i)</td>|</th>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tidy(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _eml(data: bytes) -> str:
    message = message_from_bytes(data, policy=email_policy)
    lines = [f"{name}: {message.get(name, '')}" for name in ("From", "To", "Date", "Subject") if message.get(name)]
    body = message.get_body(preferencelist=("plain", "html"))
    content = ""
    if body is not None:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = strip_html(content)
    return "\n".join(lines) + "\n\n" + content


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ConfigError("Reading PDF files needs the pypdf library. Run: pip install -r requirements.txt") from exc
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ConfigError("This PDF is password protected and was not read.")
    pages = []
    for page in reader.pages[:400]:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages)


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        tree = ElementTree.fromstring(_member(archive, "word/document.xml"))
    paragraphs = []
    for paragraph in tree.iter(_W + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == _W + "t":
                parts.append(node.text or "")
            elif node.tag in (_W + "tab",):
                parts.append("\t")
            elif node.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _pptx(data: bytes) -> str:
    slides = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        names.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
        for index, name in enumerate(names, start=1):
            tree = ElementTree.fromstring(_member(archive, name))
            lines = []
            for paragraph in tree.iter(_A + "p"):
                line = "".join(node.text or "" for node in paragraph.iter(_A + "t"))
                if line.strip():
                    lines.append(line)
            if lines:
                slides.append(f"Slide {index}\n" + "\n".join(lines))
    return "\n\n".join(slides)


def _xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            tree = ElementTree.fromstring(_member(archive, "xl/sharedStrings.xml"))
            for item in tree.iter(_S + "si"):
                shared.append("".join(node.text or "" for node in item.iter(_S + "t")))
        sheets = [name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
        sheets.sort(key=lambda name: int(re.search(r"(\d+)\.xml$", name).group(1)))
        blocks = []
        for index, name in enumerate(sheets, start=1):
            tree = ElementTree.fromstring(_member(archive, name))
            rows = []
            for row in tree.iter(_S + "row"):
                cells = []
                for cell in row.iter(_S + "c"):
                    value = cell.find(_S + "v")
                    if cell.get("t") == "inlineStr":
                        text = "".join(node.text or "" for node in cell.iter(_S + "t"))
                    elif value is None or value.text is None:
                        text = ""
                    elif cell.get("t") == "s":
                        position = int(value.text)
                        text = shared[position] if 0 <= position < len(shared) else ""
                    else:
                        text = value.text
                    cells.append(text)
                if any(cell.strip() for cell in cells):
                    rows.append("\t".join(cells).rstrip())
                if len(rows) >= 5000:
                    break
            if rows:
                blocks.append(f"Sheet {index}\n" + "\n".join(rows))
    return "\n\n".join(blocks)


def _member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > _MEMBER_LIMIT:
        raise ConfigError("This file is too large to read.")
    return archive.read(name)
