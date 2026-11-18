"""Invoices made from the owner's template, filled from a letter and the knowledge base.

The model only fills in who and what. Amounts, GST, dates and invoice numbers are worked out here, and a
number is given out only when the invoice is issued, so drafts that are thrown away leave no gaps.
"""

import re
import secrets
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.storage import LOCK, load_doc, save_doc

TEMPLATE_FIELDS = (
    "business_name", "address", "email", "phone", "gst_number", "bank_name", "bank_account",
    "payment_terms_days", "prefix", "next_number", "gst_rate", "gst_registered", "prices_include_gst",
    "currency", "notes", "quote_prefix", "quote_next_number", "quote_valid_days",
)
KINDS = {"invoice": "Invoice", "quote": "Quote"}
DEFAULT_TEMPLATE = {
    "business_name": "",
    "address": "",
    "email": "",
    "phone": "",
    "gst_number": "",
    "bank_name": "",
    "bank_account": "",
    "payment_terms_days": 7,
    "prefix": "INV-",
    "next_number": 1,
    "gst_rate": 15.0,
    "gst_registered": True,
    "prices_include_gst": False,
    "currency": "NZD",
    "notes": "",
    "quote_prefix": "Q-",
    "quote_next_number": 1,
    "quote_valid_days": 30,
}
MAX_ITEMS = 30
_ID = re.compile(r"^[a-f0-9]{12}$")
_CENT = Decimal("0.01")


# ---- template -------------------------------------------------------------------


def load_template() -> dict:
    saved = load_doc("invoice_template", {})
    template = dict(DEFAULT_TEMPLATE)
    if isinstance(saved, dict):
        template.update({key: saved[key] for key in TEMPLATE_FIELDS if key in saved})
    return template


def save_template(values: dict) -> dict:
    with LOCK:
        template = load_template()
        template.update(_clean_template(values, template))
        save_doc("invoice_template", template)
        return template


def template_ready(template: dict | None = None) -> bool:
    template = template or load_template()
    return bool(template.get("business_name", "").strip())


def _clean_template(values: dict, current: dict) -> dict:
    out = {}
    for key in ("business_name", "email", "phone", "gst_number", "bank_name", "bank_account", "currency"):
        if key in values:
            out[key] = _line(values.get(key), 120)
    for key in ("address", "notes"):
        if key in values:
            out[key] = _text(values.get(key), 600)
    for key in ("prefix", "quote_prefix"):
        if key in values:
            out[key] = re.sub(r"[^A-Za-z0-9/_-]", "", str(values.get(key) or ""))[:12]
    if "quote_next_number" in values:
        out["quote_next_number"] = _whole(values.get("quote_next_number"), current["quote_next_number"], 1, 9_999_999)
    if "quote_valid_days" in values:
        out["quote_valid_days"] = _whole(values.get("quote_valid_days"), current["quote_valid_days"], 1, 365)
    if "payment_terms_days" in values:
        out["payment_terms_days"] = _whole(values.get("payment_terms_days"), current["payment_terms_days"], 0, 365)
    if "next_number" in values:
        out["next_number"] = _whole(values.get("next_number"), current["next_number"], 1, 9_999_999)
    if "gst_rate" in values:
        rate = _number(values.get("gst_rate"))
        out["gst_rate"] = float(rate) if rate is not None and 0 <= rate <= 50 else current["gst_rate"]
    for key in ("gst_registered", "prices_include_gst"):
        if key in values:
            out[key] = _flag(values.get(key))
    out["currency"] = (out.get("currency") or current.get("currency") or "NZD").upper()[:3]
    return out


def template_from_text(text: str, complete=None) -> dict:
    """Read the business details off an old invoice so the owner does not type them in."""
    from gmail_auto.llm import parse_json

    if complete is None:
        from gmail_auto.llm import complete
    raw = complete(
        "You read an invoice the owner sent before and pull out the SENDER's (the owner's) business details, "
        "not the customer's. Return JSON only with these keys, empty string when not shown: business_name, "
        "address (lines joined with newlines), email, phone, gst_number, bank_name, bank_account, "
        "payment_terms_days (number of days or empty), prefix (the invoice number without its digits, e.g. "
        "\"INV-\"), last_number (the digits of the invoice number, or empty), currency (3 letters), "
        "notes (payment or footer notes worth repeating).",
        (text or "")[:12000],
        max_tokens=800,
        temperature=0,
    )
    data = parse_json(raw) or {}
    values = {key: data[key] for key in TEMPLATE_FIELDS if data.get(key) not in (None, "")}
    last = re.sub(r"\D", "", str(data.get("last_number") or ""))
    if last:
        values["next_number"] = int(last) + 1
    return values


# ---- invoices ----------------------------------------------------------------------


def list_invoices(kind: str | None = None) -> list[dict]:
    """Invoices and quotes, newest first. kind="invoice" or "quote" keeps only that kind."""
    items = [item for item in _all().values() if kind is None or kind_of(item) == kind]
    items.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return items


def kind_of(invoice: dict) -> str:
    return "quote" if invoice.get("kind") == "quote" else "invoice"


def get(invoice_id: str) -> dict | None:
    return _all().get(invoice_id) if _ID.match(invoice_id or "") else None


def create(fields: dict, mail_key: str = "", kind: str = "invoice") -> dict:
    invoice = {
        "id": secrets.token_hex(6),
        "kind": "quote" if kind == "quote" else "invoice",
        "status": "draft",
        "number": "",
        "created_at": time.time(),
        "issued_at": "",
        "mail_key": mail_key,
        "file_id": "",
        "issue_date": "",
        "due_date": "",
        **_clean_fields(fields),
    }
    _store(invoice)
    return invoice


def update(invoice_id: str, fields: dict) -> dict:
    with LOCK:
        invoice = _draft(invoice_id)
        invoice.update(_clean_fields(fields))
        _store(invoice)
        return invoice


def delete(invoice_id: str) -> None:
    with LOCK:
        data = _all()
        invoice = data.get(invoice_id)
        if invoice is None:
            return
        if invoice.get("status") != "draft":
            raise ConfigError(f"An issued {kind_of(invoice)} is kept for your records. It cannot be deleted.")
        data.pop(invoice_id)
        save_doc("invoices", data)


def issue(invoice_id: str, today: date | None = None) -> dict:
    """Give the invoice its number and dates. Checks that it is complete first."""
    template = load_template()
    with LOCK:
        invoice = _draft(invoice_id)
        problems = missing_for_issue(invoice, template)
        if problems:
            raise ConfigError("Before issuing: " + "; ".join(problems) + ".")
        template = load_template()
        quote = kind_of(invoice) == "quote"
        counter, prefix, days = ("quote_next_number", "quote_prefix", "quote_valid_days") if quote else ("next_number", "prefix", "payment_terms_days")
        number = int(template[counter])
        template[counter] = number + 1
        save_doc("invoice_template", template)
        day = today or _today()
        invoice["number"] = f"{template[prefix]}{number:04d}"
        invoice["status"] = "issued"
        invoice["issue_date"] = invoice.get("issue_date") or day.isoformat()
        start = date.fromisoformat(invoice["issue_date"])
        # For a quote, due_date is the date the price is valid until.
        invoice["due_date"] = invoice.get("due_date") or (start + timedelta(days=int(template[days]))).isoformat()
        invoice["issued_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        invoice["template"] = {key: template[key] for key in TEMPLATE_FIELDS}
        _store(invoice)
        return invoice


def quote_to_invoice(quote_id: str) -> dict:
    """The client said yes: a draft invoice with the quote's client and lines."""
    quote = get(quote_id)
    if quote is None or kind_of(quote) != "quote":
        raise ConfigError("That quote was not found.")
    if quote.get("status") != "issued":
        raise ConfigError("Issue the quote first.")
    fields = {key: quote.get(key) for key in ("client_name", "client_email", "client_address", "notes")}
    fields["reference"] = f"Quote {quote['number']}" + (f" - {quote['reference']}" if quote.get("reference") else "")
    fields["items"] = quote.get("items") or []
    invoice = create(fields, mail_key=quote.get("mail_key") or "", kind="invoice")
    with LOCK:
        data = _all()
        if quote_id in data:
            data[quote_id]["invoice_id"] = invoice["id"]
            save_doc("invoices", data)
    return invoice


def set_file(invoice_id: str, file_id: str) -> None:
    with LOCK:
        data = _all()
        if invoice_id in data:
            data[invoice_id]["file_id"] = file_id
            save_doc("invoices", data)


def missing_for_issue(invoice: dict, template: dict) -> list[str]:
    problems = []
    if not template_ready(template):
        problems.append("fill in your business details on the Invoices page")
    if template.get("gst_registered") and not (template.get("gst_number") or "").strip():
        problems.append("add your GST number (or untick GST registered)")
    if not (invoice.get("client_name") or "").strip():
        problems.append(f"add who the {kind_of(invoice)} is for")
    lines = [item for item in invoice.get("items") or [] if (item.get("description") or "").strip()]
    if not lines:
        problems.append("add at least one line")
    if any(item.get("unit_price") is None for item in lines):
        problems.append("fill in every price")
    return problems


def totals(invoice: dict, template: dict | None = None) -> dict:
    """Amounts for each line and the invoice, rounded to the cent. GST is worked out here, never by the model."""
    template = invoice.get("template") or template or load_template()
    rate = Decimal(str(template.get("gst_rate") or 0)) if template.get("gst_registered") else Decimal(0)
    lines = []
    total_of_lines = Decimal(0)
    for item in invoice.get("items") or []:
        qty = _number(item.get("qty"))
        price = _number(item.get("unit_price"))
        amount = (qty * price).quantize(_CENT, ROUND_HALF_UP) if qty is not None and price is not None else None
        if amount is not None:
            total_of_lines += amount
        lines.append({**item, "amount": amount})
    if template.get("prices_include_gst"):
        total = total_of_lines
        gst = (total * rate / (Decimal(100) + rate)).quantize(_CENT, ROUND_HALF_UP)
        subtotal = total - gst
    else:
        subtotal = total_of_lines
        gst = (subtotal * rate / Decimal(100)).quantize(_CENT, ROUND_HALF_UP)
        total = subtotal + gst
    return {"lines": lines, "subtotal": subtotal, "gst": gst, "total": total, "rate": rate, "currency": template.get("currency") or "NZD"}


def money(value, currency: str = "") -> str:
    if value is None:
        return "—"
    text = f"{Decimal(value):,.2f}"
    return f"{currency} {text}".strip()


# ---- filling one in from a letter ----------------------------------------------------

_FILL_SYSTEM = """You prepare an invoice for the owner (the sender of the invoice).
Fill in who it is for and what it is for, using ONLY the letter, the owner's request and the knowledge base.
Prices: copy a unit price only when the knowledge base or the letter states it. Never guess a price; use
null and say so in "missing". Quantities default to 1 unless hours, sessions or people are stated.
Do not add GST or totals; they are worked out separately. Write descriptions the way they would appear on an
invoice, in English unless the owner asks otherwise.
Return JSON only:
{"client_name": "", "client_email": "", "client_address": "", "reference": "",
 "items": [{"description": "", "qty": 1, "unit_price": 0.0 or null}],
 "notes": "", "missing": ["short notes on anything you could not find"]}"""


def fill_from(request: str, mail=None, complete=None) -> dict:
    """Ask the model for the client and lines. Falls back to the letter's sender when no model is set."""
    from gmail_auto.llm import model_ready, parse_json

    fields = {"client_name": "", "client_email": "", "items": [], "missing": []}
    if mail is not None:
        fields["client_name"] = mail.from_name or ""
        fields["client_email"] = (mail.reply_email or mail.from_email or "").strip()
        fields["reference"] = (mail.subject or "")[:80]
    if complete is None:
        from gmail_auto.llm import complete

        if not model_ready():
            fields["missing"] = ["No AI model is set, so fill in the lines yourself."]
            _from_contact(fields, mail)
            return fields
    prompt = _fill_prompt(request, mail)
    try:
        data = parse_json(complete(_FILL_SYSTEM, prompt, max_tokens=1200, temperature=0.1)) or {}
    except ReplyError as exc:
        fields["missing"] = [f"The model could not help: {exc}"]
        _from_contact(fields, mail)
        return fields
    for key in ("client_name", "client_email", "client_address", "reference", "notes"):
        if str(data.get(key) or "").strip():
            fields[key] = data[key]
    fields["items"] = data.get("items") or []
    fields["missing"] = data.get("missing") or []
    _from_contact(fields, mail)
    return fields


def _from_contact(fields: dict, mail) -> None:
    """The client's company and address from Contacts beat what the model read off one letter."""
    if mail is None:
        return
    try:
        from gmail_auto.contacts import find

        person = find(mail.reply_email or mail.from_email)
    except ConfigError:
        person = None
    if not person:
        return
    if person.get("company") and not person.get("company_guessed"):
        fields["client_name"] = person["company"] + (f" (attn. {person['name']})" if person.get("name") else "")
    elif person.get("name") and not fields.get("client_name"):
        fields["client_name"] = person["name"]
    if person.get("address"):
        fields["client_address"] = person["address"]
    fields["client_email"] = fields.get("client_email") or person["emails"][0]


def _fill_prompt(request: str, mail) -> str:
    from gmail_auto import knowledge

    parts = [f"OWNER'S REQUEST:\n{(request or 'Make an invoice for this letter.').strip()}"]
    if mail is not None:
        context = knowledge.context_for(mail)
        parts.append(
            f"THE LETTER from {mail.from_name or ''} <{mail.from_email or ''}>\nSubject: {mail.subject or ''}\n"
            f"{(mail.body or mail.snippet or '')[:6000]}"
        )
    else:
        passages = knowledge.search(request or "", 5)
        context = {"brief": knowledge.load_index()["brief"]["text"], "rules": [], "passages": passages}
    rendered = knowledge.render_context(context)
    if rendered:
        parts.append(rendered)
    template = load_template()
    parts.append(f"OWNER'S BUSINESS: {template.get('business_name') or '(not set)'}; currency {template.get('currency')}")
    return "\n\n".join(parts)


# ---- the PDF -------------------------------------------------------------------------


def render_pdf(invoice: dict) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    template = invoice.get("template") or load_template()
    sums = totals(invoice, template)
    currency = sums["currency"]
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    pdf.set_title(_latin(f"Invoice {invoice.get('number') or 'draft'}"))
    width = pdf.w - 36
    ink = (34, 30, 26)
    muted = (110, 102, 92)

    def write(text, size=10, style="", color=ink, align="L", w=0, h=5):
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(w or width, h, _latin(text), align=align, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    top = pdf.get_y()
    write(template.get("business_name") or "Your business", 17, "B", h=8, w=width * 0.58)
    details = [template.get("address") or ""]
    details += [value for value in (template.get("email"), template.get("phone")) if value]
    if template.get("gst_registered") and template.get("gst_number"):
        details.append(f"GST No. {template['gst_number']}")
    write("\n".join(line for line in details if line), 9.5, color=muted, w=width * 0.58)
    left_bottom = pdf.get_y()

    pdf.set_xy(18 + width * 0.6, top)
    quote = kind_of(invoice) == "quote"
    title = "QUOTE" if quote else ("TAX INVOICE" if template.get("gst_registered") else "INVOICE")
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*ink)
    pdf.cell(width * 0.4, 8, title, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    facts = [
        ("Quote no." if quote else "Invoice no.", invoice.get("number") or "DRAFT"),
        ("Date", _nice_date(invoice.get("issue_date")) or "on issue"),
        ("Valid until", _nice_date(invoice.get("due_date")) or f"{template.get('quote_valid_days', 30)} days after issue")
        if quote
        else ("Due", _nice_date(invoice.get("due_date")) or f"{template.get('payment_terms_days')} days after issue"),
    ]
    if invoice.get("reference"):
        facts.append(("Reference", invoice["reference"]))
    for label, value in facts:
        pdf.set_x(18 + width * 0.6)
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*muted)
        pdf.cell(width * 0.16, 5.5, label)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*ink)
        pdf.multi_cell(width * 0.24, 5.5, _latin(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(max(left_bottom, pdf.get_y()) + 8)

    write("BILL TO", 8.5, "B", muted)
    client = [invoice.get("client_name") or "", invoice.get("client_address") or "", invoice.get("client_email") or ""]
    write("\n".join(line for line in client if line), 10.5)
    pdf.ln(6)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*ink)
    price_label = "Unit price" + (" (incl. GST)" if template.get("prices_include_gst") and template.get("gst_registered") else "")
    with pdf.table(
        col_widths=(56, 10, 17, 17),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
        borders_layout="HORIZONTAL_LINES",
        line_height=6.5,
        padding=1.6,
        first_row_as_headings=True,
        headings_style=_heading_style(),
    ) as table:
        head = table.row()
        for label in ("Description", "Qty", price_label, "Amount"):
            head.cell(label)
        for line in sums["lines"]:
            if not (line.get("description") or "").strip():
                continue
            row = table.row()
            row.cell(_latin(line["description"]))
            row.cell(_qty(line.get("qty")))
            row.cell(_latin(money(_number(line.get("unit_price")))))
            row.cell(_latin(money(line["amount"])))
    pdf.ln(4)

    rows = []
    if template.get("gst_registered"):
        rows.append(("Subtotal (excl. GST)", money(sums["subtotal"])))
        rows.append((f"GST {_rate(sums['rate'])}%", money(sums["gst"])))
    rows.append((f"Total {currency}", money(sums["total"])))
    for index, (label, value) in enumerate(rows):
        last = index == len(rows) - 1
        pdf.set_x(18 + width * 0.55)
        pdf.set_font("Helvetica", "B" if last else "", 11.5 if last else 10)
        pdf.set_text_color(*ink if last else muted)
        pdf.cell(width * 0.25, 7, label)
        pdf.set_text_color(*ink)
        pdf.cell(width * 0.2, 7, value, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(8)

    pay = []
    if quote:
        write("ACCEPTING THIS QUOTE", 8.5, "B", (110, 102, 92))
        write(
            f"To go ahead, reply to accept quote {invoice.get('number') or ''}"
            + (f" before {_nice_date(invoice['due_date'])}." if invoice.get("due_date") else "."),
            10,
        )
        pdf.ln(4)
    if template.get("bank_account"):
        account = template["bank_account"] + (f" ({template['bank_name']})" if template.get("bank_name") else "")
        pay.append(f"Please pay to {account}")
        pay.append(f"Use {invoice.get('number') or 'the invoice number'} as the reference.")
    if invoice.get("due_date"):
        pay.append(f"Payment due by {_nice_date(invoice['due_date'])}.")
    if pay and not quote:
        write("PAYMENT", 8.5, "B", muted)
        write("\n".join(pay), 10)
        pdf.ln(4)
    for note in (invoice.get("notes"), template.get("notes")):
        if (note or "").strip():
            write(note, 9.5, color=muted)
            pdf.ln(2)
    return bytes(pdf.output())


def file_name(invoice: dict) -> str:
    who = re.sub(r"[^A-Za-z0-9]+", "-", invoice.get("client_name") or "").strip("-")[:30]
    return f"{invoice.get('number') or 'draft-' + kind_of(invoice)}{'-' + who if who else ''}.pdf"


# ---- helpers --------------------------------------------------------------------------


def _heading_style():
    from fpdf.fonts import FontFace

    return FontFace(emphasis="BOLD", color=(110, 102, 92), size_pt=9)


def _all() -> dict:
    data = load_doc("invoices", {})
    return data if isinstance(data, dict) else {}


def _store(invoice: dict) -> None:
    with LOCK:
        data = _all()
        data[invoice["id"]] = invoice
        save_doc("invoices", data)


def _draft(invoice_id: str) -> dict:
    invoice = get(invoice_id)
    if invoice is None:
        raise ConfigError("That invoice was not found.")
    if invoice.get("status") != "draft":
        raise ConfigError(f"Invoice {invoice.get('number')} is already issued and cannot be changed.")
    return invoice


def _clean_fields(fields: dict) -> dict:
    out = {}
    for key, limit in (("client_name", 120), ("client_email", 160), ("reference", 120)):
        if key in fields:
            out[key] = _line(fields.get(key), limit)
    for key in ("client_address", "notes"):
        if key in fields:
            out[key] = _text(fields.get(key), 600)
    for key in ("issue_date", "due_date"):
        if key in fields:
            out[key] = _iso_date(fields.get(key))
    if "items" in fields:
        items = []
        for raw in list(fields.get("items") or [])[:MAX_ITEMS]:
            if not isinstance(raw, dict):
                continue
            description = _text(raw.get("description"), 300)
            qty = _number(raw.get("qty"))
            price = _number(raw.get("unit_price"))
            if not description and price is None:
                continue
            items.append(
                {
                    "description": description,
                    "qty": float(qty) if qty is not None else 1.0,
                    "unit_price": float(price) if price is not None else None,
                }
            )
        out["items"] = items
    if "missing" in fields:
        out["missing"] = [_line(item, 200) for item in list(fields.get("missing") or [])[:8] if str(item or "").strip()]
    return out


def _number(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = re.sub(r"[,$\s]|NZD|USD|AUD", "", str(value), flags=re.IGNORECASE)
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _whole(value, fallback: int, low: int, high: int) -> int:
    number = _number(value)
    if number is None:
        return fallback
    return max(low, min(high, int(number)))


def _flag(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _text(value, limit: int) -> str:
    lines = [" ".join(line.split()) for line in str(value or "").replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)[:limit]


def _iso_date(value) -> str:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _today() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Pacific/Auckland")).date()
    except Exception:
        return date.today()


def _nice_date(value: str) -> str:
    try:
        day = date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return ""
    return f"{day.day} {day.strftime('%B %Y')}"


def _qty(value) -> str:
    number = _number(value)
    if number is None:
        return "1"
    return f"{number:f}".rstrip("0").rstrip(".") or "0"


def _rate(rate: Decimal) -> str:
    return f"{rate:f}".rstrip("0").rstrip(".")


_LATIN = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "•": "-", " ": " "})


def _latin(text) -> str:
    """The built-in PDF font covers Western European letters only; anything else becomes '?'."""
    return str(text or "").translate(_LATIN).encode("latin-1", "replace").decode("latin-1")
