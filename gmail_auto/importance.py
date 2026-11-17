"""Which letters are worth a WhatsApp ping, even when no reply is drafted yet.

Only things the owner would want on their phone get through: real people,
clients and companies they deal with, appointments, bills, and letters from
schools, government or other institutions (fines included). Notifications,
receipts, newsletters and marketing never ping, even when they mention a
meeting or a payment.
"""

import re

from gmail_auto.models import Mail

PING_SKIPS = {"system", "empty", "auto"}

# What is sent to WhatsApp, in the order it is checked. Shown on Settings, each one can be turned off.
RULES = [
    ("fine", "Fines and legal", "Infringement and parking/speed fines, tolls, final notices, debt collectors, court or summons."),
    ("invoice", "Bills to pay", "Invoices, power/phone/rates bills, statements and anything overdue. Receipts for things already paid are not sent."),
    ("action", "Needs you to act", "Documents to sign, failed or declined payments, customs fees, missed deliveries, renewals and expiring policies, registration or WOF due."),
    ("school", "School and children", "Your children's schools and teachers."),
    ("institution", "Government and institutions", "IRD, ACC, NZTA, councils, police, courts, immigration, universities and other .govt.nz / .gov mail."),
    ("health", "Health", "Test results, prescriptions, referrals and clinic or hospital mail."),
    ("housing", "Home and tenancy", "Landlord, property manager, lease, rent, bond, body corporate, rates and inspections."),
    ("enquiry", "Clients and enquiries", "Quotes, pricing questions and new work from real people."),
    ("meeting", "Appointments", "A real person inviting you to a meeting or appointment with a date that is still ahead."),
    ("person", "People you know", "Someone in your knowledge base or someone you have replied to before."),
    ("company", "Your company", "Mail about your own company and the organisations you work with."),
]
# Never sent, whatever it mentions, unless it falls in one of the first rules above.
QUIET = [
    "No-reply and notification senders, mailing lists and bulk mail",
    "Newsletters, marketing, sales, coupons and webinars",
    "Receipts, order and delivery updates, sign-in and security alerts",
    "Social media, forums, job alerts and product updates",
]
KINDS = [key for key, _, _ in RULES]
# Phone pings for a busy owner. Quotes, people and company mail go to Inbox / the morning digest.
INTERRUPT = ("fine", "invoice", "action", "school", "institution", "health", "housing")
DIGEST = ("enquiry", "meeting", "person", "company")
PRESETS = ("urgent", "busy", "custom")
_ANY_SENDER = {"fine", "invoice", "action", "school", "institution", "health", "housing"}
LABELS = {
    "fine": "fine",
    "invoice": "bill",
    "action": "action needed",
    "institution": "institution",
    "health": "health",
    "housing": "home",
    "enquiry": "enquiry",
    "meeting": "appointment",
    "school": "school",
    "person": "person",
    "company": "company",
}

_FINE = re.compile(
    r"\b(infringement|penalty notice|parking (?:ticket|fine|notice)|speed(?:ing)? (?:camera|ticket|fine)|"
    r"traffic (?:fine|offence)|toll (?:notice|charge)|fine of|you have been fined|court|summons|"
    r"final notice|debt collect\w*)\b|罚单|罚款|违章",
    re.IGNORECASE,
)
_INVOICE = re.compile(
    r"\b(invoice|tax invoice|payment due|payment overdue|statement of account|amount due|"
    r"your (?:\w+ )?bill\b|bill is (?:ready|due)|overdue|due date)\b|账单|发票|缴费",
    re.IGNORECASE,
)
_ENQUIRY = re.compile(
    r"\b(quot(?:e|ation)|enquir(?:y|ies)|inquir(?:y|ies)|\brfq\b|pricing|how much|day rate)\b",
    re.IGNORECASE,
)
_ACTION = re.compile(
    r"\b(please sign|signature (?:required|requested)|docusign|adobe sign|"
    r"sign (?:the|this|your) (?:document|contract|agreement|lease|form)|"
    r"payment (?:failed|declined|unsuccessful)|card (?:was )?declined|unable to process (?:your )?payment|"
    r"direct debit (?:failed|returned|dishonou?red)|account (?:suspended|on hold)|"
    r"customs (?:fees?|charges?|duty|clearance)|delivery (?:failed|attempted|unsuccessful)|missed delivery|"
    r"renewal (?:due|notice|reminder)|due for renewal|policy (?:renewal|expir\w*)|expir(?:es|ing) (?:soon|on)|"
    r"warrant of fitness|\bwof\b|registration (?:due|expir\w*)|licen[cs]e (?:renewal|expir\w*))\b|"
    r"请签署|签字|付款失败|扣款失败|即将到期|续保|需要处理",
    re.IGNORECASE,
)
_HEALTH = re.compile(
    r"\b(test results?|lab results?|blood test|prescriptions?|referral|specialist|medical cent(?:re|er)|"
    r"health centre|hospital|clinic|vaccinations?|immuni[sz]ations?|health ?nz|te whatu ora)\b|"
    r"体检|检查结果|化验|处方|医院|诊所",
    re.IGNORECASE,
)
_HOUSING = re.compile(
    r"\b(tenancy|tenant|landlord|property manager|lease|rent (?:due|increase|arrears|review|payment)|"
    r"bond (?:lodgement|refund)|body corporate|rates (?:notice|invoice|instal?ments?)|"
    r"(?:property|house|routine) inspection)\b|租约|房租|房东|物业",
    re.IGNORECASE,
)
_MARKETING = re.compile(
    r"\b(newsletter|webinar|unsubscribe|view (?:this|it) in (?:your|a) browser|manage (?:your )?preferences|"
    r"% off|sale ends|limited time|special offer|deal of|promo code|coupon|free trial|shop now|book now)\b",
    re.IGNORECASE,
)
# Job boards and recruiter digests list jobs at clinics, landlords and councils; none of it is the owner's own business.
_JOB_ALERT = re.compile(
    r"\b(\d+\s+(?:new\s+)?jobs?\b|new jobs?|jobs? (?:this|of the) week|job (?:alerts?|digest|listings?|matches|board|openings?)|"
    r"jobs? (?:for|matching) you|remote jobs?|apply now|now hiring|we'?re hiring|is hiring|open (?:roles|positions)|"
    r"recommended jobs?|similar jobs?)\b|招聘|职位推荐|新职位",
    re.IGNORECASE,
)
# A paid receipt, an order update or an account alert is a notification, not a bill.
_NOTICE_WORDS = re.compile(
    r"\b(receipt|your order|order (?:confirmed|confirmation|shipped|#)|has shipped|out for delivery|"
    r"tracking number|sign[- ]in|new login|security alert|verify your|confirm your email|"
    r"newsletter|webinar|unsubscribe|view (?:this|it) in (?:your|a) browser|manage (?:your )?preferences|"
    r"% off|sale ends|limited time|special offer|deal of|promo code|coupon|free trial|"
    r"job alert|jobs? (?:for|matching) you|you may be a fit|recommended for you|"
    r"liked your|commented on|mentioned you|new follower|digest|weekly update|monthly update|"
    r"release notes|product update|what'?s new)\b",
    re.IGNORECASE,
)
_INSTITUTION_DOMAINS = (
    ".govt.nz",
    ".gov.au",
    ".gov.uk",
    ".gov",
    ".ac.nz",
    ".school.nz",
    ".health.nz",
    "ird.govt.nz",
    "nzta.govt.nz",
    "acc.co.nz",
    "police.govt.nz",
    "justice.govt.nz",
    "council",
    "aucklandcouncil",
    "at.govt.nz",
)
_SCHOOL = ("westlake", "wairau", "etap.co.nz", "school.nz")
_COMPANY = ("kiwiknowledge", "kiwi knowledge", "innopro")
_ROBOT_NAMES = {
    "alert", "alerts", "news", "info", "hello", "hi", "offers", "offer", "deals", "updates", "update",
    "support", "help", "team", "billing", "accounts", "account", "service", "services", "mail", "mailer",
    "reply", "store", "shop", "orders", "order", "receipts", "rewards", "members", "community", "events",
}
_NOT_IMPORTANT = {"kind": "other", "important": False, "why": "", "label": ""}


def assess(mail: Mail, lookups: bool = True) -> dict:
    """Return kind, whether it is important, and a short English why.

    lookups=False skips the knowledge base and mail-history checks, so it is cheap enough to run on every
    row of the inbox list.
    """
    from gmail_auto.filters import obvious_junk

    sender = f"{mail.from_email or ''} {mail.from_name or ''}".lower()
    text = f"{mail.subject or ''}\n{mail.body or mail.snippet or ''}"
    blob = f"{sender}\n{text}"
    lowered = blob.lower()
    if obvious_junk(mail):
        return dict(_NOT_IMPORTANT)
    notice = is_notification(mail)
    selling = bool(_MARKETING.search(text) or re.search(r"退订|限时优惠|立即抢购|优惠券", text))
    if not _institution(mail) and (_JOB_ALERT.search(mail.subject or "") or (notice and _JOB_ALERT.search(text))):
        return dict(_NOT_IMPORTANT)
    # Bulk and automated mail only counts as health or home when the subject or sender says so,
    # not because a word like "clinic" or "lease" appears somewhere in a long digest.
    headline = f"{mail.subject or ''}\n{sender}"
    topic_text = headline if notice else text

    # These matter even when an automated system sends them, as long as it is not selling something.
    if not selling:
        if _FINE.search(text):
            return _tag("fine", "Looks like a fine, penalty or final notice.")
        if _INVOICE.search(text) and not _receipt_only(text):
            return _tag("invoice", "Looks like a bill or invoice to pay.")
        if _ACTION.search(text):
            return _tag("action", "Needs you to sign, pay, renew or sort something out.")
    if any(token in lowered for token in _SCHOOL):
        return _tag("school", "School or parent mail.")
    if _institution(mail) and not selling:
        return _tag("institution", "From a government office or other institution.")
    if not selling and _HEALTH.search(topic_text):
        return _tag("health", "Health: results, prescriptions, referrals or clinic mail.")
    if not selling and _HOUSING.search(topic_text):
        return _tag("housing", "About your home, tenancy, rent or rates.")

    if notice:
        return dict(_NOT_IMPORTANT)

    if _ENQUIRY.search(text):
        return _tag("enquiry", "Looks like a quote or enquiry.")
    if _is_meeting(mail):
        return _tag("meeting", "Looks like an appointment or invitation with a date.")
    if lookups and _known_person(mail, blob):
        return _tag("person", "Someone already in your knowledge or mail history.")
    if any(token in lowered for token in _COMPANY):
        return _tag("company", "Your company or a known organisation.")
    return dict(_NOT_IMPORTANT)


def is_notification(mail: Mail) -> bool:
    """True for automated mail: no-reply senders, mailing lists, bulk mail, receipts, alerts and marketing."""
    from gmail_auto.filters import is_system_address

    if is_system_address(mail.from_email or "") or is_system_address(mail.reply_email or ""):
        return True
    headers = {key.lower(): str(value or "") for key, value in (mail.headers or {}).items()}
    if headers.get("list-unsubscribe") or headers.get("list-id") or headers.get("x-mailing-list"):
        return True
    precedence = headers.get("precedence", "").lower()
    if any(word in precedence for word in ("bulk", "list", "junk")):
        return True
    submitted = headers.get("auto-submitted", "").strip().lower()
    if submitted and submitted != "no":
        return True
    labels = set(mail.label_ids or [])
    if labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "CATEGORY_UPDATES"}:
        return True
    local = (mail.from_email or "").split("@", 1)[0].lower()
    for part in re.split(r"[._+-]+", local):
        if part in _ROBOT_NAMES or part.startswith(("notif", "newsletter", "promo", "marketing")):
            return True
    text = f"{mail.subject or ''}\n{mail.body or mail.snippet or ''}"
    return bool(_NOTICE_WORDS.search(text))


def _receipt_only(text: str) -> bool:
    """A receipt for something already paid, with nothing left to pay."""
    lowered = text.lower()
    if not re.search(r"\b(receipt|payment received|thank you for your payment|paid in full)\b", lowered):
        return False
    return not re.search(r"\b(due|overdue|outstanding|amount owing|please pay)\b", lowered)


def _institution(mail: Mail) -> bool:
    domain = (mail.from_email or "").split("@", 1)[-1].lower()
    if not domain:
        return False
    return any(domain.endswith(mark) or mark in domain for mark in _INSTITUTION_DOMAINS)


def _tag(kind: str, why: str) -> dict:
    return {"kind": kind, "important": True, "why": why, "label": LABELS.get(kind, kind)}


def _is_meeting(mail: Mail) -> bool:
    try:
        from gmail_auto.calendars import suggest_event

        return suggest_event(mail, strict=True) is not None
    except Exception:
        return False


def _known_person(mail: Mail, blob: str) -> bool:
    email = (mail.from_email or "").strip().lower()
    name = (mail.from_name or "").strip().lower()
    if _from_knowledge(email, name, blob):
        return True
    if not email:
        return False
    try:
        from gmail_auto.store import list_processed

        for record in list_processed(80):
            if (record.get("from_email") or "").strip().lower() == email and record.get("action") in {"sent", "approved_sent"}:
                return True
    except Exception:
        return False
    return False


def ping_kinds(settings: dict | None = None) -> list[str]:
    """Kinds that may WhatsApp the owner. Not the same as the Inbox Important tab."""
    from gmail_auto.settings import load_settings

    data = settings if isinstance(settings, dict) else load_settings()
    preset = preset_of(data)
    if preset in ("urgent", "busy"):
        return list(INTERRUPT)
    return list(data.get("notify_kinds") or [])


def inbox_kinds(settings: dict | None = None) -> list[str]:
    """Kinds that belong on the Inbox Important tab."""
    from gmail_auto.settings import load_settings

    data = settings if isinstance(settings, dict) else load_settings()
    preset = preset_of(data)
    if preset == "busy":
        return list(INTERRUPT) + list(DIGEST)
    if preset == "urgent":
        return list(INTERRUPT)
    return list(data.get("notify_kinds") or [])


def preset_of(settings: dict | None) -> str:
    data = settings or {}
    preset = str(data.get("notify_preset") or "").strip()
    if preset in PRESETS:
        return preset
    stored = data.get("notify_kinds")
    kinds = set(stored or [])
    if stored is None or kinds == set(KINDS):
        return "busy"
    if kinds == set(INTERRUPT):
        return "urgent"
    return "custom"


def known_from_knowledge(limit: int = 8) -> dict:
    """Short lists so Settings can show *your* people and topics, not a generic checklist."""
    people: list[str] = []
    topics: list[str] = []
    try:
        from gmail_auto.knowledge import list_items

        for item in list_items("person", active_only=True):
            text = _short_fact(item.get("text") or "")
            if text and text not in people:
                people.append(text)
            if len(people) >= limit:
                break
        for item in list_items("topic", active_only=True):
            text = _short_fact(item.get("text") or "")
            if text and text not in topics:
                topics.append(text)
            if len(topics) >= limit:
                break
    except Exception:
        pass
    return {"people": people, "topics": topics}


def _short_fact(value: str) -> str:
    text = re.sub(r"[*_`]+", "", " ".join((value or "").split()))
    if ":" in text:
        head = text.split(":", 1)[0].strip()
        if 1 < len(head) <= 48:
            text = head
    return text[:80]


def _from_knowledge(email: str, name: str, blob: str) -> bool:
    try:
        from gmail_auto.knowledge import load_index

        items = load_index()["model"]["items"]
    except Exception:
        return False
    for item in items:
        if item.get("kind") not in {"person", "fact"}:
            continue
        card = f"{item.get('text') or ''} {' '.join(item.get('related') or [])}".lower()
        if email and email in card:
            return True
        if name and len(name) > 3 and name in card:
            return True
    return False
