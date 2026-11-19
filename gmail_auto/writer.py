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
LANGUAGES = {"auto": "Match the person", "en": "English", "zh": "Chinese", "bilingual": "Chinese and English"}
_LANGUAGE_RULE = {
    "en": "Write the email (subject and body) in English only, even when the owner talks to you in Chinese.",
    "zh": "Write the email (subject and body) in Chinese only, even when the owner talks to you in English.",
    "bilingual": "Write the email body in two parts: Chinese first, then a blank line, then the same in English. The subject in English.",
}
_ID = re.compile(r"^[a-f0-9]{10}$")
_REF = re.compile(r"^(doc|invoice|contract):([A-Za-z0-9]{6,32})$")
# The owner's latest words must agree before a quote or invoice gets its number.
_AGREE = re.compile(r"(?i)\b(yes|yep|ok(?:ay)?|sure|go ahead|issue (?:it|them)|do it|confirm(?:ed)?|send it|looks good|that'?s (?:right|fine))\b|好|可以|行|开出|确认|没问题|就这样|发吧")

_SYSTEM = """You are the owner's writing assistant. Together you write ONE email, over several messages.
Each turn you see the conversation, the current draft, and what the owner has: contacts, files, issued
quotes and invoices, finished contracts and contract templates, and relevant knowledge.

Do what the owner asks in their latest message:
- Write or change the email in their voice. Use the contact's greeting, tone, language and company.
- Use only facts from the knowledge, the catalogs and the owner's words. Never invent prices, dates,
  promises or file contents. When something needed is missing, ask in "reply" and leave a clear gap.
- Attach only items listed below, by their ref (e.g. "doc:ab12cd", "invoice:...", "contract:..."). When the
  owner asks for a kind of file ("our company introduction", "the course outline", "the price list"), pick
  the best match from MY FILES by what it is about; files marked (relevant) match this conversation.
- If they want a quote, invoice or contract that does not exist yet, start it with "make" (a contract
  template is filled in from the conversation, the contact and the knowledge base).
- DRAFTS IN THIS EMAIL lists what you started. A contract with every part filled is attached for you. When a
  part is missing, ask the owner for it; when they answer, put it in with "fill". For a quote or invoice,
  set or correct its lines with "lines" (prices only from the knowledge base or the owner). When it is
  complete, tell the owner the number and total it would get and ask them to confirm; only after they
  clearly agree in their latest message, put its ref in "issue" (that gives it its number and attaches it).
- Never pretend something is attached or issued when it is not.
- Keep "reply" short (one to three sentences), in the language the owner writes in.
- The email's language is set separately (see EMAIL LANGUAGE) and you must follow it, whatever language the
  owner chats in. When it changes, rewrite the whole email in the new language with the same content.

If the owner's latest message is clearly about something else (not this email), return only
{"off_topic": true}.

Return JSON only:
{"reply": "what you tell the owner",
 "to": "email address(es) separated by commas, or a contact's name, or empty to keep",
 "cc": "addresses or contact names to copy in, separated by commas; empty to keep; \"none\" to remove",
 "subject": "new subject or empty to keep",
 "body": "the whole email text, or empty to keep it unchanged",
 "attach": ["ref", ...], "detach": ["ref", ...],
 "make": null or {"kind": "quote" | "invoice" | "contract", "request": "what it is for, with quantities",
                  "template": "contract template ref (template:...) or empty"},
 "fill": null or {"ref": "contract:...", "values": {"<part name>": "<value>"}},
 "lines": null or {"ref": "invoice:...", "items": [{"description": "...", "qty": 1, "unit_price": 0.0}]},
 "issue": "" or "invoice:..."}"""


# ---- sessions ---------------------------------------------------------------------------


def start(to: str = "", subject: str = "", body: str = "", refs: list[str] | None = None, source: str = "web", language: str = "") -> dict:
    session = {
        "id": secrets.token_hex(5),
        "source": source,
        "language": language if language in LANGUAGES else default_language(),
        "to": (to or "").strip(),
        "cc": "",
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
    session["cc"] = item.get("cc") or ""
    session["attachments"] = [{"ref": f"file:{entry['id']}", **entry} for entry in item.get("attachments") or []]
    session["approval_id"] = item["id"]
    session["status"] = "queued"
    _store(session)
    return session


def default_language() -> str:
    """The reply-language setting: English, both, or match the person ("auto")."""
    try:
        from gmail_auto.settings import load_settings

        setting = load_settings().get("reply_language")
    except Exception:
        setting = "en"
    return {"en": "en", "bilingual": "bilingual"}.get(setting, "auto")


def language_rule(session: dict) -> str:
    language = session.get("language") or "auto"
    if language in _LANGUAGE_RULE:
        return _LANGUAGE_RULE[language]
    address = (session.get("to") or "").split(",")[0].strip()
    person = contacts.find(address) if "@" in address else None
    preferred = (person or {}).get("language") or ""
    if preferred in _LANGUAGE_RULE:
        return _LANGUAGE_RULE[preferred] + " (This person prefers it.)"
    return "Write the email in the language the recipient uses with the owner; if that is not known, in English."


def get(session_id: str) -> dict | None:
    return _all().get(session_id) if _ID.match(session_id or "") else None


def edit(
    session_id: str, to: str | None = None, subject: str | None = None, body: str | None = None, detach: str = "", language: str | None = None,
    cc: str | None = None,
) -> dict:
    """The owner typed into the draft directly. A new language is stored; see relanguage()."""
    session = _open(session_id)
    if language in LANGUAGES:
        session["language"] = language
    if cc is not None:
        session["cc"] = cc.strip()
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


def attach_ref(session_id: str, ref: str) -> dict:
    """Attach one of the owner's documents, issued quotes or invoices, or finished contracts by its ref."""
    session = _open(session_id)
    _attach(session, ref)
    _store(session)
    _sync(session)
    return session


def attach_file(session_id: str, record: dict) -> dict:
    """Attach a file the owner uploaded (on the page or by sending it on WhatsApp)."""
    from gmail_auto.files import MAX_TOTAL_BYTES

    session = _open(session_id)
    ref = f"file:{record['id']}"
    if sum(int(item.get("size") or 0) for item in session["attachments"]) + int(record.get("size") or 0) > MAX_TOTAL_BYTES:
        raise ConfigError("That is too much to attach to one email (15 MB in all).")
    if not any(item.get("ref") == ref for item in session["attachments"]):
        session["attachments"].append({"ref": ref, **{key: record.get(key) for key in ("id", "name", "mime", "size")}})
    _store(session)
    _sync(session)
    return session


def issue_draft(session_id: str, invoice_id: str) -> dict:
    """Give a quote or invoice started in this email its number, and attach it."""
    session = _open(session_id)
    issue_draft_in(session, invoice_id)
    _say(session, "agent", _issued_note(invoice_id))
    _store(session)
    _sync(session)
    return session


def drafts(session: dict) -> list[dict]:
    """The quotes, invoices and contracts started in this email, with what is still missing."""
    from gmail_auto import contracts, invoices

    out = []
    for made in session.get("made") or []:
        if made.get("kind") in ("quote", "invoice"):
            item = invoices.get(made["id"])
            if item is None:
                continue
            sums = invoices.totals(item)
            out.append(
                {
                    **made, "ref": f"invoice:{item['id']}", "status": item.get("status"), "number": item.get("number") or "",
                    "total": invoices.money(sums["total"], sums["currency"]),
                    "missing": invoices.missing_for_issue(item, invoices.load_template()) if item.get("status") == "draft" else [],
                    "attached": any(entry.get("ref") == f"invoice:{item['id']}" for entry in session["attachments"]),
                }
            )
        elif made.get("kind") == "contract":
            item = contracts.get(made["id"])
            if item is None:
                continue
            out.append(
                {
                    **made, "ref": f"contract:{item['id']}", "status": "draft", "number": "", "total": "",
                    "missing": contracts.empty_fields(item),
                    "attached": any(entry.get("ref") == f"contract:{item['id']}" for entry in session["attachments"]),
                }
            )
    return out


def relanguage(session_id: str, profile: dict, complete=None) -> dict:
    """The email language was switched: rewrite the draft in it, keeping what it says."""
    session = _open(session_id)
    if not session["body"].strip():
        return session
    label = LANGUAGES.get(session.get("language") or "auto", "")
    wanted = "the language that fits the person" if session.get("language") == "auto" else label
    return say(session_id, f"Rewrite the whole email in {wanted}. Keep the content the same.", profile, complete=complete)


def queue(session_id: str, provider: str = "") -> dict:
    """Put the finished email in the approvals queue (or update the one already there). Returns the item."""
    session = _open(session_id)
    to = outgoing.clean_recipients(_resolve_to(session["to"]))
    cc = outgoing.clean_recipients(_resolve_to(session.get("cc") or "")) if (session.get("cc") or "").strip() else ""
    if not session["body"].strip():
        raise ConfigError("The email is empty.")
    subject = session["subject"] or "Hello"
    item = approvals.get(session["approval_id"]) if session.get("approval_id") else None
    if item is None or item.get("status") != "pending":
        item = approvals.create_new(to, subject, session["body"], provider=provider or outgoing.default_provider(), cc=cc)
        session["approval_id"] = item["id"]
    session["to"] = to
    session["cc"] = cc
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
    lines = [f"To: {session['to'] or '(who?)'}"]
    if session.get("cc"):
        lines.append(f"Cc: {session['cc']}")
    lines += [f"Subject: {session['subject'] or '(none yet)'}", "", session["body"] or "(no text yet)"]
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
    said = " ".join(message["text"] for message in session["messages"] if message["who"] == "you")[-1500:]
    files = knowledge.attachable_documents()
    if files:
        relevant = {hit["doc_id"] for hit in knowledge.search(f"{session['subject']}\n{said}", 8)}
        files.sort(key=lambda doc: 0 if doc["id"] in relevant else 1)
        parts.append(
            "MY FILES (attach by ref):\n"
            + "\n".join(
                f"- doc:{doc['id']} | {doc['title']} ({knowledge.ROLES[knowledge.role_of(doc)]})"
                + (" (relevant)" if doc["id"] in relevant else "")
                + (f" | about: {about}" if (about := _about(doc, doc["id"] in relevant)) else "")
                for doc in files[:25]
            )
        )
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
    started = drafts(session)
    if started:
        rows = []
        for made in started:
            if made["kind"] == "contract":
                item = contracts.get(made["id"])
                values = ", ".join(f"{name}={value or '(missing)'}" for name, value in (item or {}).get("values", {}).items())
                rows.append(f"- {made['ref']} | contract {item.get('template_title') if item else ''}: {values}" + (" | ATTACHED" if made["attached"] else ""))
            else:
                item = invoices.get(made["id"])
                lines = "; ".join(
                    f"{line.get('description')} x{line.get('qty')} @ {line.get('unit_price') if line.get('unit_price') is not None else '(price missing)'}"
                    for line in (item or {}).get("items") or []
                ) or "(no lines yet)"
                state = f"issued as {made['number']}" if made["status"] == "issued" else ("ready to issue" if not made["missing"] else "missing: " + "; ".join(made["missing"]))
                rows.append(f"- {made['ref']} | {made['kind']} for {item.get('client_name') if item else ''}: {lines} | total {made['total']} | {state}" + (" | ATTACHED" if made["attached"] else ""))
        parts.append("DRAFTS IN THIS EMAIL:\n" + "\n".join(rows))
    hits = knowledge.search(f"{session['subject']}\n{said}", 4)
    for hit in hits:
        parts.append(f"KNOWLEDGE \"{hit['title']}\":\n{hit['text']}")
    person = contacts.find(session["to"].split(",")[0].strip()) if "@" in session["to"] else None
    if person:
        parts.append("ABOUT THE RECIPIENT:\n" + contacts.card(person))
    parts.append("EMAIL LANGUAGE: " + language_rule(session))
    parts.append(
        "CURRENT DRAFT:\n"
        f"To: {session['to'] or '(not set)'}\nCc: {session.get('cc') or '(none)'}\nSubject: {session['subject'] or '(not set)'}\n"
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
    cc = " ".join(str(data.get("cc") or "").split())
    if cc.lower() in ("none", "remove", "无"):
        session["cc"] = ""
    elif cc:
        session["cc"] = _resolve_to(cc) or cc
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
    fill = data.get("fill")
    if isinstance(fill, dict) and isinstance(fill.get("values"), dict):
        try:
            notes.append(_fill(session, str(fill.get("ref") or ""), fill["values"]))
        except ConfigError as exc:
            notes.append(f"(Could not fill it in: {exc})")
    lines = data.get("lines")
    if isinstance(lines, dict) and isinstance(lines.get("items"), list):
        try:
            notes.append(_set_lines(session, str(lines.get("ref") or ""), lines["items"]))
        except ConfigError as exc:
            notes.append(f"(Could not change the lines: {exc})")
    issue = str(data.get("issue") or "").strip()
    if issue.startswith("invoice:"):
        said = next((message["text"] for message in reversed(session["messages"]) if message["who"] == "you"), "")
        if _AGREE.search(said):
            try:
                issue_draft_in(session, issue.split(":", 1)[1])
                notes.append(f"({_issued_note(issue.split(':', 1)[1])})")
            except ConfigError as exc:
                notes.append(f"(Not issued: {exc})")
        else:
            notes.append("(Not issued yet: say yes to give it its number.)")
    return [note for note in notes if note]


def issue_draft_in(session: dict, invoice_id: str) -> None:
    from gmail_auto import invoices

    if not any(made.get("id") == invoice_id for made in session.get("made") or []):
        raise ConfigError("that quote or invoice was not started in this email")
    invoice = invoices.get(invoice_id)
    if invoice is None:
        raise ConfigError("that quote or invoice is gone")
    if invoice.get("status") != "issued":
        invoices.issue(invoice_id)
    _attach(session, f"invoice:{invoice_id}")


def _issued_note(invoice_id: str) -> str:
    from gmail_auto import invoices

    item = invoices.get(invoice_id) or {}
    sums = invoices.totals(item) if item else {"total": 0, "currency": ""}
    word = "Quote" if invoices.kind_of(item) == "quote" else "Invoice"
    return f"{word} {item.get('number')} for {invoices.money(sums['total'], sums['currency'])} is issued and attached."


def _fill(session: dict, ref: str, values: dict) -> str:
    from gmail_auto import contracts

    if not ref.startswith("contract:"):
        raise ConfigError("only a contract's parts can be filled in")
    contract_id = ref.split(":", 1)[1]
    if not any(made.get("id") == contract_id for made in session.get("made") or []):
        raise ConfigError("that contract was not started in this email")
    contract = contracts.get(contract_id)
    if contract is None:
        raise ConfigError("that contract is gone")
    known = {field["name"] for field in contract["fields"]}
    contracts.update(contract_id, {name: value for name, value in values.items() if name in known})
    return _contract_status(session, contract_id)


def _contract_status(session: dict, contract_id: str) -> str:
    """Attach the contract once every part is filled; otherwise say what is still missing."""
    from gmail_auto import contracts

    contract = contracts.get(contract_id)
    empty = contracts.empty_fields(contract) if contract else []
    if empty:
        return "(Contract still needs: " + ", ".join(empty) + ".)"
    session["attachments"] = [item for item in session["attachments"] if item.get("ref") != f"contract:{contract_id}"]
    _attach(session, f"contract:{contract_id}")
    return "(Contract filled in and attached.)"


def _set_lines(session: dict, ref: str, items: list) -> str:
    from gmail_auto import invoices

    if not ref.startswith("invoice:"):
        raise ConfigError("lines belong to a quote or invoice")
    invoice_id = ref.split(":", 1)[1]
    if not any(made.get("id") == invoice_id for made in session.get("made") or []):
        raise ConfigError("that quote or invoice was not started in this email")
    invoices.update(invoice_id, {"items": items})
    item = invoices.get(invoice_id)
    missing = invoices.missing_for_issue(item, invoices.load_template())
    sums = invoices.totals(item)
    if missing:
        return "(Still needed before it can be issued: " + "; ".join(missing) + ".)"
    return f"(Ready to issue: total {invoices.money(sums['total'], sums['currency'])}.)"


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
        missing = invoices.missing_for_issue(item, invoices.load_template())
        sums = invoices.totals(item)
        if missing:
            return f"(Draft {kind} started. Still needed: " + "; ".join(missing) + ".)"
        return f"(Draft {kind} ready: total {invoices.money(sums['total'], sums['currency'])}. Say yes to issue and attach it.)"
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
        return _contract_status(session, item["id"])
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


def _about(doc: dict, deep: bool = False) -> str:
    """A short line on what a document says: its summary, or (for a relevant one) the start of its text."""
    summary = " ".join(str(doc.get("summary") or "").split())
    if not summary and deep:
        full = knowledge.get_document(doc["id"]) or {}
        summary = " ".join(str(full.get("text") or "").split())
    return summary[:140]


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
    try:
        cc = outgoing.clean_recipients(_resolve_to(session.get("cc") or "")) if (session.get("cc") or "").strip() else ""
    except ConfigError:
        cc = item.get("cc") or ""
    approvals.set_new_fields(item_id, to, session["subject"] or item.get("subject") or "Hello", cc=cc)
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
