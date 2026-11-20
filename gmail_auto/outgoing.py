"""New emails the owner asks for, from the web page or WhatsApp.

The model writes the email from the owner's points and the knowledge base. It then waits in the
approvals queue like any reply, with its attachments, until the owner sends it.
"""

import re

from gmail_auto import approvals, files, knowledge
from gmail_auto.errors import ConfigError, ReplyError

_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")
_SYSTEM = """You write a new email for the owner, in their voice, to the person named. Use the owner's points
and only facts from the profile and knowledge base; never invent prices, dates or promises. If files are
attached, mention them briefly. Keep it short and natural. Sign off with the owner's name.
Return JSON only: {"subject": "short subject", "body": "the email text"}"""


def clean_recipients(text: str) -> str:
    """One or more addresses separated by commas or semicolons, checked. Raises ConfigError if any is wrong."""
    found = [part.strip().strip("<>") for part in re.split(r"[,;\s]+", text or "") if part.strip()]
    if not found:
        raise ConfigError("Add who the email is to.")
    wrong = [part for part in found if not _ADDRESS.match(part)]
    if wrong:
        raise ConfigError(f"Not an email address: {', '.join(wrong)}.")
    if len(found) > 10:
        raise ConfigError("Send to ten people at most.")
    return ", ".join(dict.fromkeys(found))


def write(to: str, points: str, subject: str, profile: dict, attached: list[str] | None = None, complete=None) -> tuple[str, str, list]:
    """Subject, body and the knowledge sources used. Without a model, the points are the body as typed."""
    from gmail_auto.generator import check_basics, clean_reply, profile_as_prompt
    from gmail_auto.llm import model_ready, parse_json

    points = (points or "").strip()
    subject = " ".join((subject or "").split())
    if complete is None:
        if not model_ready():
            if not points:
                raise ConfigError("Write what the email should say.")
            return subject or points.split("\n", 1)[0][:80], points, []
        from gmail_auto.llm import complete
    hits = knowledge.search(f"{subject}\n{points}", 4)
    index = knowledge.load_index()
    parts = ["PROFILE:\n" + profile_as_prompt(profile)]
    if index["brief"]["text"]:
        parts.append("KNOWLEDGE BASE BRIEFING:\n" + index["brief"]["text"])
    if index["rules"]:
        parts.append("RULES:\n" + "\n".join(f"- {rule}" for rule in index["rules"]))
    for hit in hits:
        parts.append(f"FROM \"{hit['title']}\":\n{hit['text']}")
    parts.append(f"TO: {to}")
    for address in to.split(","):
        person = _person(address.strip())
        if person:
            parts.append(f"ABOUT {address.strip()} (from my contacts; follow the greeting and tone):\n{person}")
    if subject:
        parts.append(f"SUBJECT GIVEN BY THE OWNER: {subject}")
    if attached:
        parts.append("ATTACHED FILES: " + ", ".join(attached))
    parts.append(f"OWNER'S POINTS:\n{points or 'Write a short, friendly email.'}")
    try:
        data = parse_json(complete(_SYSTEM, "\n\n".join(parts), max_tokens=900, temperature=0.3)) or {}
    except ReplyError as exc:
        raise ConfigError(f"The email could not be written: {exc}") from exc
    body = clean_reply(str(data.get("body") or ""))
    try:
        check_basics(body)
    except ReplyError as exc:
        raise ConfigError(f"The email could not be written: {exc}") from exc
    return subject or " ".join(str(data.get("subject") or "").split())[:120] or "Hello", body, _unique(hit["title"] for hit in hits)


def start(to: str, points: str, subject: str, profile: dict, records: list[dict], provider: str = "", complete=None, cc: str = "") -> dict:
    """Write the email, put it in the queue and attach the files. Returns the waiting item."""
    from gmail_auto.mailboxes import PROVIDERS, connected_clients

    to = clean_recipients(to)
    cc = clean_recipients(cc) if (cc or "").strip() else ""
    chosen_provider = (provider or "").strip().lower()
    if chosen_provider:
        if chosen_provider not in PROVIDERS:
            raise ConfigError("Choose a connected mailbox to send from.")
        try:
            clients, _ = connected_clients()
        except Exception:
            clients = []
        available = {getattr(client, "provider", "") for client in clients}
        if available and chosen_provider not in available:
            raise ConfigError(f"{PROVIDERS[chosen_provider]} is not connected.")
    subject, body, sources = write(to, points, subject, profile, [record["name"] for record in records], complete=complete)
    item = approvals.create_new(to, subject, body, provider=chosen_provider or default_provider(), sources=sources, cc=cc)
    for record in records:
        approvals.attach(item["id"], record)
    return approvals.get(item["id"])


def default_provider(available: list[str] | None = None) -> str:
    from gmail_auto.mailboxes import connected_clients
    from gmail_auto.settings import load_settings

    preferred = (load_settings().get("default_sender") or "").strip().lower()
    if available is None:
        try:
            clients, _ = connected_clients()
        except Exception:
            clients = []
        available = [getattr(client, "provider", "") for client in clients if getattr(client, "provider", "")]
    if preferred and preferred in available:
        return preferred
    if available:
        return available[0]
    return preferred or "gmail"


# ---- things to attach -------------------------------------------------------------------


def invoice_file(invoice: dict) -> dict:
    """The issued invoice's PDF in file storage, made once."""
    from gmail_auto import invoices

    if invoice.get("status") != "issued":
        raise ConfigError(f"Issue the {invoices.kind_of(invoice)} before sending it.")
    record = files.get(invoice.get("file_id") or "")
    if record is None:
        record = files.save(invoices.render_pdf(invoice), invoices.file_name(invoice), "application/pdf", origin="invoice")
        invoices.set_file(invoice["id"], record["id"])
    return record


def contract_file(contract: dict) -> dict:
    from gmail_auto import contracts

    empty = contracts.empty_fields(contract)
    if empty:
        raise ConfigError("Fill in every part of the contract first: " + ", ".join(empty) + ".")
    data, mime, name = contracts.render(contract)
    record = files.save(data, name, mime, origin="contract")
    contracts.set_file(contract["id"], record["id"])
    return record


def knowledge_file(doc_id: str) -> dict:
    doc = next((item for item in knowledge.attachable_documents() if item["id"] == doc_id), None)
    record = files.get(((doc or {}).get("meta") or {}).get("file_id") or "") if doc else None
    if record is None:
        raise ConfigError("That knowledge file is no longer kept. Upload it again.")
    return record


def find_attachments(wanted: list[str], to: str = "", text: str = "") -> tuple[list[dict], list[str]]:
    """Turn what the owner asked to attach ("invoice", "price list", "contract") into files.

    Returns the files and the things that could not be found, with why.
    """
    from gmail_auto import contracts, invoices

    records, missing = [], []
    addresses = {part.strip().lower() for part in (to or "").split(",") if part.strip()}
    for raw in wanted or []:
        name = " ".join(str(raw or "").split())
        lowered = name.lower()
        if not lowered:
            continue
        try:
            if re.search(r"invoice|发票|账单|quote|quotation|报价单|报价", lowered):
                kind = "quote" if re.search(r"quote|quotation|报价", lowered) else "invoice"
                issued = [item for item in invoices.list_invoices(kind) if item.get("status") == "issued"]
                match = next((item for item in issued if (item.get("client_email") or "").lower() in addresses and addresses), None)
                if match is None:
                    match = next((item for item in issued if _mentions(text, item.get("client_name"))), None)
                if match is None:
                    missing.append(f"{name}: no issued {kind} for this person. Make one under Documents first.")
                    continue
                records.append(invoice_file(match))
            elif re.search(r"contract|agreement|合同|协议", lowered):
                ready = [item for item in contracts.list_drafts() if not contracts.empty_fields(item)]
                match = next((item for item in ready if _mentions(text, " ".join(item["values"].values()))), None) or (ready[0] if len(ready) == 1 else None)
                if match is None:
                    missing.append(f"{name}: no finished contract draft to attach. Draft one on the Contracts page first.")
                    continue
                records.append(contract_file(match))
            else:
                doc = _best_document(lowered)
                if doc is None:
                    missing.append(f"{name}: no kept file with that name in the knowledge base.")
                    continue
                records.append(knowledge_file(doc["id"]))
        except ConfigError as exc:
            missing.append(f"{name}: {exc}")
    unique = []
    for record in records:
        if record["id"] not in {item["id"] for item in unique}:
            unique.append(record)
    return unique, missing


def _best_document(name: str) -> dict | None:
    words = [word for word in re.findall(r"[a-z0-9]+|[一-鿿]+", name) if len(word) > 1 and word not in {"the", "my", "our", "file", "pdf"}]
    best, score = None, 0
    for doc in knowledge.attachable_documents():
        title = (doc.get("title") or "").lower()
        points = sum(1 for word in words if word in title)
        role = knowledge.role_of(doc)
        if role == "price_list" and re.search(r"price|pricing|rate|价目|价格", name):
            points += 2
        if role == "brochure" and re.search(r"company|about|introduction|profile|brochure|公司|简介|介绍", name):
            points += 2
        if role == "product_info" and re.search(r"product|service|course|deck|ppt|catalogue|产品|课程|服务|介绍", name):
            points += 2
        if points > score:
            best, score = doc, points
    return best


def _mentions(text: str, name) -> bool:
    name = " ".join(str(name or "").split()).lower()
    return bool(name) and len(name) > 2 and name in (text or "").lower()


def _person(address: str) -> str:
    from gmail_auto import contacts

    try:
        return contacts.card(contacts.find(address))
    except ConfigError:
        return ""


def _unique(items) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
