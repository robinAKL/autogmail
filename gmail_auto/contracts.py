"""Contracts drafted from the owner's own Word contract.

A contract template is an ordinary upload to the knowledge base that was recognised as a contract. Its
fields are the parts that change per client: either placeholders the owner wrote ({{Client name}},
[Start date]) or the text of an earlier filled-in contract (the client's name, fee, dates), found by the
model and checked to appear word for word. A new contract is the original .docx with those parts replaced,
so the owner's layout, fonts and clauses stay exactly as they were.
"""

import html
import io
import re
import secrets
import time
import zipfile

from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.storage import LOCK, load_doc, save_doc

MAX_FIELDS = 25
_ID = re.compile(r"^[a-f0-9]{12}$")
_BRACES = re.compile(r"\{\{\s*([^{}\n]{1,60}?)\s*\}\}")
_BRACKETS = re.compile(r"\[([A-Z][A-Za-z0-9 /'&-]{1,40})\]")
_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


# ---- reading a template -------------------------------------------------------------


def find_fields(text: str, complete=None) -> list[dict]:
    """The parts of a contract that change for each client, as {"name", "example"}.

    Placeholders the owner typed are used as they are. Otherwise the model lists the client-specific text
    of a filled-in contract; anything it names that is not in the document word for word is dropped.
    """
    text = text or ""
    fields = []
    for pattern in (_BRACES, _BRACKETS):
        for match in pattern.finditer(text):
            name = " ".join(match.group(1).split())
            if not any(field["example"] == match.group(0) for field in fields):
                fields.append({"name": name, "example": match.group(0)})
    if fields:
        return fields[:MAX_FIELDS]
    if complete is None:
        from gmail_auto.llm import model_ready

        if not model_ready():
            return []
        from gmail_auto.llm import complete
    from gmail_auto.llm import parse_json

    try:
        raw = complete(
            "This is a contract the owner used with one client. List the parts that would change for a new client "
            "or job: client name and company, addresses, contact details, dates, fees and amounts, duration, scope "
            "or service description, and similar. Do not list standard clauses or the owner's own details. "
            'Return JSON only: {"fields": [{"name": "short label, e.g. Client name", "example": "the exact text as it '
            'appears in the contract, copied character for character"}]}',
            text[:15000],
            max_tokens=1200,
            temperature=0,
        )
    except ReplyError:
        return []
    flat = _flat(text)
    for item in (parse_json(raw) or {}).get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:60]
        example = " ".join(str(item.get("example") or "").split())
        if name and len(example) >= 2 and example in flat and not any(field["example"] == example for field in fields):
            fields.append({"name": name, "example": example})
    return fields[:MAX_FIELDS]


# ---- drafts ---------------------------------------------------------------------------


def list_drafts() -> list[dict]:
    items = list(_all().values())
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return items


def get(contract_id: str) -> dict | None:
    return _all().get(contract_id) if _ID.match(contract_id or "") else None


def create(template: dict, values: dict, missing: list, mail_key: str = "") -> dict:
    fields = (template.get("meta") or {}).get("fields") or []
    contract = {
        "id": secrets.token_hex(6),
        "template_id": template["id"],
        "template_title": template.get("title") or "Contract",
        "mail_key": mail_key,
        "fields": fields,
        "values": {field["name"]: _value(values.get(field["name"])) for field in fields},
        "missing": [" ".join(str(item).split())[:200] for item in (missing or [])[:8] if str(item).strip()],
        "created_at": time.time(),
        "created_text": time.strftime("%Y-%m-%d %H:%M"),
        "file_id": "",
    }
    _store(contract)
    return contract


def update(contract_id: str, values: dict) -> dict:
    with LOCK:
        contract = get(contract_id)
        if contract is None:
            raise ConfigError("That contract draft was not found.")
        for field in contract["fields"]:
            if field["name"] in values:
                contract["values"][field["name"]] = _value(values[field["name"]])
        contract["file_id"] = ""
        _store(contract)
        return contract


def delete(contract_id: str) -> None:
    with LOCK:
        data = _all()
        if data.pop(contract_id, None) is not None:
            save_doc("contracts", data)


def set_file(contract_id: str, file_id: str) -> None:
    with LOCK:
        data = _all()
        if contract_id in data:
            data[contract_id]["file_id"] = file_id
            save_doc("contracts", data)


def empty_fields(contract: dict) -> list[str]:
    return [field["name"] for field in contract["fields"] if not (contract["values"].get(field["name"]) or "").strip()]


_FILL_SYSTEM = """You fill in a contract for the owner, from the client's letter, the owner's request and the
knowledge base. For each field, give the value for THIS client and job. Use only what those sources say:
never invent fees, dates or names. Leave a value empty and add a short note to "missing" when it is not known.
Write each value the way it should read in the contract (e.g. "15 January 2027", "NZD 4,500 + GST").
Return JSON only: {"values": {"<field name>": "<value>"}, "missing": ["..."]}"""


def fill_from(template: dict, request: str, mail=None, complete=None) -> tuple[dict, list]:
    from gmail_auto import knowledge
    from gmail_auto.llm import model_ready, parse_json

    fields = (template.get("meta") or {}).get("fields") or []
    if not fields:
        return {}, ["No fields were found in this contract template."]
    if complete is None:
        if not model_ready():
            return {}, ["No AI model is set, so fill in the fields yourself."]
        from gmail_auto.llm import complete
    parts = [f"OWNER'S REQUEST:\n{(request or 'Draft the contract for this client.').strip()}"]
    if mail is not None:
        parts.append(
            f"THE LETTER from {mail.from_name or ''} <{mail.from_email or ''}>\nSubject: {mail.subject or ''}\n"
            f"{(mail.body or mail.snippet or '')[:6000]}"
        )
        context = knowledge.context_for(mail)
    else:
        context = {"brief": knowledge.load_index()["brief"]["text"], "rules": [], "passages": knowledge.search(request or "", 5)}
    rendered = knowledge.render_context(context)
    if rendered:
        parts.append(rendered)
    parts.append(
        "FIELDS (name: how it read in the old contract, for format only; do not reuse the old value):\n"
        + "\n".join(f"- {field['name']}: {field['example']}" for field in fields)
    )
    try:
        data = parse_json(complete(_FILL_SYSTEM, "\n\n".join(parts), max_tokens=1200, temperature=0.1)) or {}
    except ReplyError as exc:
        return {}, [f"The model could not help: {exc}"]
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    return {field["name"]: _value(values.get(field["name"])) for field in fields}, list(data.get("missing") or [])


# ---- the Word file ----------------------------------------------------------------------


def render_docx(original: bytes, contract: dict) -> bytes:
    """The template with each field's old text replaced by the new value. Fields left empty keep a marker."""
    swaps = []
    for field in contract["fields"]:
        value = (contract["values"].get(field["name"]) or "").strip() or f"[{field['name']}]"
        swaps.append((field["example"], value))
    swaps.sort(key=lambda pair: len(pair[0]), reverse=True)
    try:
        source = zipfile.ZipFile(io.BytesIO(original))
    except zipfile.BadZipFile as exc:
        raise ConfigError("The contract template is not a Word (.docx) file. Upload the Word version to draft contracts.") from exc
    out = io.BytesIO()
    with source, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if _PARTS.match(info.filename):
                data = _swap_xml(data.decode("utf-8"), swaps).encode("utf-8")
            target.writestr(info, data)
    return out.getvalue()


def file_name(contract: dict) -> str:
    who = ""
    for field in contract["fields"]:
        if re.search(r"(?i)client|customer|party|name|company", field["name"]):
            who = contract["values"].get(field["name"]) or ""
            if who:
                break
    stem = re.sub(r"[^A-Za-z0-9]+", "-", f"{contract.get('template_title', 'Contract').rsplit('.', 1)[0]} {who}").strip("-")
    return f"{stem[:70] or 'Contract'}.docx"


def _swap_xml(xml: str, swaps: list[tuple[str, str]]) -> str:
    """Replace text inside each paragraph, even when Word split it across several runs."""
    spans = _innermost_paragraphs(xml)
    for start, end in reversed(spans):
        para = xml[start:end]
        runs = list(re.finditer(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", para, flags=re.S))
        if not runs:
            continue
        texts = [html.unescape(run.group(2)) for run in runs]
        before = list(texts)
        for old, new in swaps:
            # Spacing may differ from what the model copied (double spaces, tabs), so match loosely.
            words = old.split()
            if not words:
                continue
            pattern = re.compile(r"\s+".join(re.escape(word) for word in words))
            position = 0
            while True:
                found = pattern.search("".join(texts), position)
                if found is None:
                    break
                _replace_across(texts, found.start(), found.end(), new)
                position = found.start() + len(new)
        if texts == before:
            continue
        pieces = []
        last = 0
        for run, text in zip(runs, texts):
            pieces.append(para[last : run.start()])
            opening = run.group(1)
            if "xml:space" not in opening:
                opening = opening[:-1] + ' xml:space="preserve">'
            pieces.append(opening + html.escape(text, quote=False) + run.group(3))
            last = run.end()
        pieces.append(para[last:])
        xml = xml[:start] + "".join(pieces) + xml[end:]
    return xml


def _replace_across(texts: list[str], begin: int, finish: int, new: str) -> None:
    """Put `new` where characters begin..finish were, touching only the runs that held them, so the
    formatting of the rest of the line (a bold label before the client's name, say) is kept."""
    offset = 0
    first = last = None
    for index, text in enumerate(texts):
        end = offset + len(text)
        if first is None and begin < end:
            first = (index, begin - offset)
        if finish <= end:
            last = (index, finish - offset)
            break
        offset = end
    if first is None or last is None:
        return
    (i, start_in), (j, end_in) = first, last
    if i == j:
        texts[i] = texts[i][:start_in] + new + texts[i][end_in:]
        return
    texts[i] = texts[i][:start_in] + new
    for k in range(i + 1, j):
        texts[k] = ""
    texts[j] = texts[j][end_in:]


def _innermost_paragraphs(xml: str) -> list[tuple[int, int]]:
    spans = []
    stack: list[list] = []
    for match in re.finditer(r"<w:p(?=[\s>/])[^>]*?(/?)>|</w:p>", xml):
        token = match.group(0)
        if token == "</w:p>":
            if stack:
                start, has_child = stack.pop()
                if not has_child:
                    spans.append((start, match.end()))
        elif match.group(1) == "/":
            continue
        else:
            if stack:
                stack[-1][1] = True
            stack.append([match.start(), False])
    spans.sort()
    return spans


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def _value(value) -> str:
    return " ".join(str(value or "").split())[:400]


def _all() -> dict:
    data = load_doc("contracts", {})
    return data if isinstance(data, dict) else {}


def _store(contract: dict) -> None:
    with LOCK:
        data = _all()
        data[contract["id"]] = contract
        save_doc("contracts", data)
