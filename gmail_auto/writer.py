"""Writing one email together with the owner, over several messages (web page or WhatsApp).

A session holds the draft (to, subject, body, attachments) and the conversation. Each owner message is one
model step: it may change the draft, attach files from the owner's documents, or start a draft quote,
invoice or contract for the owner to check. Nothing is sent from here: a finished draft goes to the
approvals queue, or is sent when the owner presses Send.
"""

import re
import secrets
import time

from gmail_auto import approvals, contacts, knowledge, outgoing
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.storage import LOCK, load_doc, save_doc

_KEEP = 25
_ID = re.compile(r"^[a-f0-9]{10}$")
_REF = re.compile(r"^(doc|invoice|contract):([A-Za-z0-9]{6,32})$")

_SYSTEM = """You are the owner's writing assistant. Together you write ONE email, over several messages.
Each turn you see the conversation, the current draft, and what the owner has: contacts, files, issued
quotes and invoices, finished contracts and contract templates, and relevant knowledge.

Do what the owner asks in their latest message:
- Write or change the email in their voice. Use the contact's greeting, tone, language and company.
- Use only facts from the knowledge, the catalogs and the owner's words. Never invent prices, dates,
  promises or file contents. When something needed is missing, ask in "reply" and leave a clear gap.
- Attach only items listed below, by their ref (e.g. "doc:ab12cd", "invoice:...", "contract:...").
- If they want a quote, invoice or contract that does not exist yet, ask for it with "make"; it becomes a
  draft the owner checks and issues before it can be attached. Never pretend it is attached.
- Keep "reply" short (one to three sentences), in the language the owner writes in.

If the owner's latest message is clearly about something else (not this email), return only
{"off_topic": true}.

Return JSON only:
{"reply": "what you tell the owner",
 "to": "email address(es) separated by commas, or a contact's name, or empty to keep",
 "subject": "new subject or empty to keep",
 "body": "the whole email text, or empty to keep it unchanged",
 "attach": ["ref", ...], "detach": ["ref", ...],
 "make": null or {"kind": "quote" | "invoice" | "contract", "request": "what it is for, with quantities",
                  "template": "contract template ref (template:...) or empty"}}"""


# ---- sessions ---------------------------------------------------------------------------


def start(to: str = "", subject: str = "", body: str = "", refs: list[str] | None = None, source: str = "web") -> dict:
    session = {
        "id": secrets.token_hex(5),
        "source": source,
        "to": (to or "").strip(),
        "subject": " ".join((subject or "").split())[:200],
        "body": (body or "").strip(),
        "attachments": [],
        "messages": [],
        "made": [],
        "approval_id": "",
        "status": "open",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    notes = []
    for ref in refs or []:
        try:
            _attach(session, ref)
        except ConfigError as exc:
            notes.append(str(exc))
    if notes:
        _say(session, "agent", "Could not attach: " + " ".join(notes))
    _store(session)
    return session


def from_approval(item: dict, source: str = "whatsapp") -> dict:
    """A session for a new email that is already waiting in the queue, so it can be changed by talking."""
    session = start(to=item.get("to") or "", subject=item.get("subject") or "", body=item.get("draft") or "", source=source)
    session["attachments"] = [{"ref": f"file:{entry['id']}", **entry} for entry in item.get("attachments") or []]
    session["approval_id"] = item["id"]
    session["status"] = "queued"
    _store(session)
    return session


def get(session_id: str) -> dict | None:
    return _all().get(session_id) if _ID.match(session_id or "") else None


def edit(session_id: str, to: str | None = None, subject: str | None = None, body: str | None = None, detach: str = "") -> dict:
    """The owner typed into the draft directly."""
    session = _open(session_id)
    if to is not None:
        session["to"] = to.strip()
    if subject is not None:
        session["subject"] = " ".join(subject.split())[:200]
    if body is not None:
        session["body"] = body.strip()
    if detach:
        session["attachments"] = [item for item in session["attachments"] if item.get("ref") != detach]
    _store(session)
    _sync(session)
    return session


def say(session_id: str, text: str, profile: dict, complete=None) -> dict:
    """One owner message and the assistant's answer. The draft changes as asked."""
    from gmail_auto.llm import model_ready, parse_json

    session = _open(session_id)
    text = (text or "").strip()[:2000]
    if not text:
        raise ConfigError("Say what the email should do.")
    _say(session, "you", text)
    if complete is None:
        if not model_ready():
            _say(session, "agent", "No AI model is set, so I cannot write for you. Type the email in the draft on the right.")
            _store(session)
            return session
        from gmail_auto.llm import complete
    try:
        raw = complete(_SYSTEM, _prompt(session, profile), max_tokens=1800, temperature=0.3)
    except ReplyError as exc:
        _say(session, "agent", f"Sorry, I could not do that: {exc}")
        _store(session)
        return session
    data = parse_json(raw)
    if not isinstance(data, dict):
        _say(session, "agent", "Sorry, I could not read my own answer. Please say it again.")
        _store(session)
        return session
    if data.get("off_topic"):
        # Not about this email: leave the session as it was and let the caller handle the message.
        session["messages"].pop()
        session["off_topic"] = True
        return session
    notes = _apply(session, data)
    reply = " ".join(str(data.get("reply") or "").split()) or "Done."
    _say(session, "agent", reply + (("\n" + "\n".join(notes)) if notes else ""))
    _store(session)
    _sync(session)
    return session


def queue(session_id: str, provider: str = "") -> dict:
    """Put the finished email in the approvals queue (or update the one already there). Returns the item."""
    session = _open(session_id)
    to = outgoing.clean_recipients(_resolve_to(session["to"]))
    if not session["body"].strip():
        raise ConfigError("The email is empty.")
    subject = session["subject"] or "Hello"
    item = approvals.get(session["approval_id"]) if session.get("approval_id") else None
    if item is None or item.get("status") != "pending":
        item = approvals.create_new(to, subject, session["body"], provider=provider or outgoing.default_provider())
        session["approval_id"] = item["id"]
    session["to"] = to
    session["status"] = "queued"
    _store(session)
    _sync(session)
    return approvals.get(item["id"])


def send(session_id: str, client=None) -> dict:
    item = queue(session_id)
    sent = approvals.approve(item["id"], "web", client)
    session = get(session_id)
    if session is not None:
        session["status"] = "sent"
        _store(session)
    return sent


def last_open(source: str) -> dict | None:
    items = [item for item in _all().values() if item.get("source") == source and item.get("status") in ("open", "queued")]
    items.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return items[0] if items else None


def preview(session: dict) -> str:
    lines = [f"To: {session['to'] or '(who?)'}", f"Subject: {session['subject'] or '(none yet)'}", "", session["body"] or "(no text yet)"]
    if session["attachments"]:
        lines += ["", "Attached: " + ", ".join(item["name"] for item in session["attachments"])]
    return "\n".join(lines)


# ---- one step --------------------------------------------------------------------------


def _prompt(session: dict, profile: dict) -> str:
    from gmail_auto import contracts, invoices
    from gmail_auto.generator import profile_as_prompt

    parts = ["MY PROFILE:\n" + profile_as_prompt(profile)]
    people = contacts.all_contacts()[:80]
    if people:
        parts.append(
            "MY CONTACTS:\n"
            + "\n".join(
                f"- {item.get('name') or ''} <{item['emails'][0]}>"
                + "".join(f" · {value}" for value in (contacts.TYPES.get(item.get("type"), ""), item.get("company"), item.get("tone"), item.get("salutation") and f"greet: {item['salutation']}") if value)
                for item in people
            )
        )
    files = knowledge.attachable_documents()
    if files:
        parts.append("MY FILES (attach by ref):\n" + "\n".join(f"- doc:{doc['id']} | {doc['title']} ({knowledge.ROLES[knowledge.role_of(doc)]})" for doc in files))
    issued = [item for item in invoices.list_invoices() if item.get("status") == "issued"][:20]
    if issued:
        rows = []
        for item in issued:
            sums = invoices.totals(item)
            word = "quote" if invoices.kind_of(item) == "quote" else "invoice"
            rows.append(f"- invoice:{item['id']} | {word} {item['number']} for {item.get('client_name')} <{item.get('client_email') or ''}>, {invoices.money(sums['total'], sums['currency'])}")
        parts.append("ISSUED QUOTES AND INVOICES (attach by ref):\n" + "\n".join(rows))
    ready = [item for item in contracts.list_drafts() if not contracts.empty_fields(item)][:10]
    if ready:
        parts.append("FINISHED CONTRACTS (attach by ref):\n" + "\n".join(f"- contract:{item['id']} | {item['template_title']}: " + ", ".join(f"{k}={v}" for k, v in list(item['values'].items())[:3]) for item in ready))
    templates = [item for item in knowledge.templates("contract_template") if (item.get("meta") or {}).get("fields")]
    if templates:
        parts.append("CONTRACT TEMPLATES (for make):\n" + "\n".join(f"- template:{item['id']} | {item['title']}" for item in templates))
    said = " ".join(message["text"] for message in session["messages"] if message["who"] == "you")[-1500:]
    hits = knowledge.search(f"{session['subject']}\n{said}", 4)
    for hit in hits:
        parts.append(f"KNOWLEDGE \"{hit['title']}\":\n{hit['text']}")
    person = contacts.find(session["to"].split(",")[0].strip()) if "@" in session["to"] else None
    if person:
        parts.append("ABOUT THE RECIPIENT:\n" + contacts.card(person))
    parts.append(
        "CURRENT DRAFT:\n"
        f"To: {session['to'] or '(not set)'}\nSubject: {session['subject'] or '(not set)'}\n"
        f"Attached refs: {', '.join(item['ref'] for item in session['attachments']) or '(none)'}\n"
        f"Body:\n{session['body'] or '(empty)'}"
    )
    talk = "\n".join(f"{'OWNER' if message['who'] == 'you' else 'YOU'}: {message['text']}" for message in session["messages"][-12:])
    parts.append("CONVERSATION:\n" + talk)
    return "\n\n".join(parts)


def _apply(session: dict, data: dict) -> list[str]:
    notes = []
    to = " ".join(str(data.get("to") or "").split())
    if to:
        session["to"] = _resolve_to(to) or to
    if str(data.get("subject") or "").strip():
        session["subject"] = " ".join(str(data["subject"]).split())[:200]
    if str(data.get("body") or "").strip():
        from gmail_auto.generator import clean_reply

        session["body"] = clean_reply(str(data["body"]))
    for ref in data.get("detach") or []:
        session["attachments"] = [item for item in session["attachments"] if item.get("ref") != str(ref)]
    for ref in data.get("attach") or []:
        try:
            _attach(session, str(ref))
        except ConfigError as exc:
            notes.append(f"(Not attached: {exc})")
    make = data.get("make")
    if isinstance(make, dict) and make.get("kind"):
        try:
            notes.append(_make(session, make))
        except (ConfigError, ReplyError) as exc:
            notes.append(f"(Could not start it: {exc})")
    return notes


def _make(session: dict, make: dict) -> str:
    from gmail_auto import contracts, invoices

    kind = str(make.get("kind") or "").lower()
    request = " ".join(str(make.get("request") or "").split())[:600]
    address = session["to"].split(",")[0].strip()
    if kind in ("quote", "invoice"):
        fields = invoices.fill_from(request, None)
        person = contacts.find(address) if "@" in address else None
        if person:
            fields["client_name"] = (person.get("company") + f" (attn. {person.get('name')})") if person.get("company") and person.get("name") else (person.get("name") or person.get("company") or "")
            fields["client_email"] = person["emails"][0]
            if person.get("address"):
                fields["client_address"] = person["address"]
        elif "@" in address:
            fields["client_email"] = address
        item = invoices.create(fields, kind=kind)
        link = f"/invoices/{item['id']}"
        session["made"].append({"kind": kind, "id": item["id"], "url": link})
        return f"(Draft {kind} started: check the lines and prices at {link}, issue it, then tell me to attach it.)"
    if kind == "contract":
        found = [item for item in knowledge.templates("contract_template") if (item.get("meta") or {}).get("fields")]
        wanted = str(make.get("template") or "").replace("template:", "")
        template = next((item for item in found if item["id"] == wanted), found[0] if found else None)
        if template is None:
            raise ConfigError("there is no contract template; upload your Word contract under Documents first")
        values, missing = contracts.fill_from(template, f"{request}\nFor: {address}", None)
        item = contracts.create(template, values, missing)
        link = f"/contracts/{item['id']}"
        session["made"].append({"kind": "contract", "id": item["id"], "url": link})
        return f"(Draft contract started: fill in the parts at {link}, then tell me to attach it.)"
    raise ConfigError(f"I can make a quote, invoice or contract, not a {kind}")


def _attach(session: dict, ref: str) -> None:
    from gmail_auto import contracts, invoices

    match = _REF.match((ref or "").strip())
    if not match:
        raise ConfigError(f"{ref} is not one of your files")
    kind, key = match.groups()
    if any(item.get("ref") == ref for item in session["attachments"]):
        return
    if kind == "doc":
        record = outgoing.knowledge_file(key)
    elif kind == "invoice":
        invoice = invoices.get(key)
        if invoice is None:
            raise ConfigError("that quote or invoice is gone")
        record = outgoing.invoice_file(invoice)
    else:
        contract = contracts.get(key)
        if contract is None:
            raise ConfigError("that contract is gone")
        record = outgoing.contract_file(contract)
    session["attachments"].append({"ref": ref, **{k: record.get(k) for k in ("id", "name", "mime", "size")}})


def _resolve_to(text: str) -> str:
    """Addresses as they are; a contact's name ("Lee", "ABC Ltd") becomes their address."""
    out = []
    for part in re.split(r"[,;]", text or ""):
        part = part.strip()
        if not part:
            continue
        if "@" in part:
            out.append(part.strip("<>"))
            continue
        found = contacts.by_name(part)
        out.append(found[0]["emails"][0] if found else part)
    return ", ".join(out)


def _sync(session: dict) -> None:
    """Keep a queued approval in step with the session."""
    item_id = session.get("approval_id")
    item = approvals.get(item_id) if item_id else None
    if item is None or item.get("status") != "pending":
        return
    try:
        to = outgoing.clean_recipients(_resolve_to(session["to"]))
    except ConfigError:
        to = item.get("to") or ""
    approvals.set_new_fields(item_id, to, session["subject"] or item.get("subject") or "Hello")
    if session["body"].strip() and session["body"] != item.get("draft"):
        approvals.set_draft(item_id, session["body"])
    have = {entry.get("id") for entry in item.get("attachments") or []}
    want = {entry.get("id") for entry in session["attachments"]}
    for entry in item.get("attachments") or []:
        if entry.get("id") not in want:
            approvals.detach(item_id, entry["id"])
    for entry in session["attachments"]:
        if entry.get("id") not in have:
            approvals.attach(item_id, entry)


def _say(session: dict, who: str, text: str) -> None:
    session["messages"].append({"who": who, "text": text, "at": time.strftime("%H:%M")})
    session["messages"] = session["messages"][-40:]


def _open(session_id: str) -> dict:
    session = get(session_id)
    if session is None:
        raise ConfigError("That draft is no longer here. Start a new email.")
    if session.get("status") == "sent":
        raise ConfigError("That email was already sent. Start a new one.")
    return session


def _all() -> dict:
    data = load_doc("writer", {})
    return data if isinstance(data, dict) else {}


def _store(session: dict) -> None:
    session["updated_at"] = time.time()
    with LOCK:
        data = _all()
        data[session["id"]] = session
        if len(data) > _KEEP:
            ordered = sorted(data.values(), key=lambda item: item.get("updated_at", 0))
            for old in ordered[: len(data) - _KEEP]:
                data.pop(old["id"], None)
        save_doc("writer", data)
