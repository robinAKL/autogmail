"""The morning report on WhatsApp.

It covers everything since the last report (not only unread mail, since letters are marked read once they
are handled), plus what is still open: replies waiting for the owner's OK and bills or actions not yet
marked handled. Letters are sorted into sections and numbered, so the owner can answer "1" to open one.
Times are New Zealand time; the server clock may be UTC.
"""

import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from gmail_auto.errors import ConfigError, ReplyError

ZONE = ZoneInfo("Pacific/Auckland")
SECTIONS = [
    ("action", "🔴", "Needs you", "需要你处理"),
    ("people", "👤", "From people", "真人来信"),
    ("calendar", "📅", "Today and tomorrow", "今明两天"),
    ("institution", "🏛", "Institutions and insurance", "机构和保险"),
    ("booking", "📦", "Bookings and deliveries", "预订和快递"),
    ("job", "💼", "Job applications", "求职进展"),
]
_KIND_SECTION = {
    "fine": "action", "invoice": "action", "action": "action", "account": "action",
    "enquiry": "people", "person": "people", "company": "people", "meeting": "people",
    "school": "institution", "institution": "institution", "health": "institution", "housing": "institution",
    "insurance": "institution", "booking": "booking", "job": "job",
}
_STILL_OPEN = {"fine", "invoice", "action", "account"}
_OPEN_DAYS = 14
_PER_SECTION = 6
_OPEN_SHOWN = 5
_PERSON_NAME = re.compile(r"^(?:(?:mrs?|ms|miss|dr|prof)\.?\s+)?[A-Za-z\u00c0-\u024f'’-]{2,}(?:\s+[A-Za-z\u00c0-\u024f'’-]{1,}){1,3}$|^[\u4e00-\u9fff]{2,4}$")
_NOT_A_PERSON = re.compile(r"(?i)\b(team|support|service|services|ltd|limited|inc|llc|group|company|news|notifications?|admin|office|school|council|bank|insurance|care|desk|store|shop|via|jobs?|careers|alerts?|reception|accounts?|info|for|of|the|and|at|from|partners?|life|business|opportunities|association|club|foundation|university|college)\b|\.com|\.co\b")
_DELIVERY = re.compile(r"(?i)\b(parcel|delivered|out for delivery|has shipped|tracking number|collected your|courier)\b|快递|包裹")


def now_local() -> datetime:
    return datetime.now(ZONE)


def due(settings: dict, sent_day: str, now: datetime | None = None) -> bool:
    """True once a day, at or after the chosen hour, New Zealand time."""
    now = now or now_local()
    return bool(settings.get("daily_digest")) and now.hour >= int(settings.get("digest_hour") or 8) and sent_day != now.date().isoformat()


def build(clients: list | None = None, since: str = "", language: str = "en", jobs: bool = False, complete=None) -> tuple[str, list]:
    """The report text and the letters behind its numbers (for the WhatsApp number list)."""
    from gmail_auto import approvals, contacts
    from gmail_auto.importance import assess, from_dev_tool
    from gmail_auto.models import Mail
    from gmail_auto.store import list_processed

    since = since or (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    open_since = (datetime.now() - timedelta(days=_OPEN_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        records = list_processed(3000)
    except ConfigError:
        records = []
    waiting = {}
    try:
        for item in approvals.pending():
            waiting[item.get("key")] = item
    except ConfigError:
        pass

    items: list[dict] = []
    ignored: dict[str, int] = {}
    new_count = 0
    for record in records:
        stamp = str(record.get("processed_at") or "")
        fresh = stamp >= since
        if fresh:
            new_count += 1
        if record.get("sensitive") or record.get("handled"):
            continue
        if not fresh and not (stamp >= open_since):
            continue
        address = (record.get("from_email") or "").lower()
        mail = Mail(
            id=record.get("mail_id") or record.get("id") or "", thread_id=record.get("thread_id") or "",
            from_name=record.get("from_name") or "", from_email=address, reply_email="", subject=record.get("subject") or "",
            date=record.get("date") or "", body=(record.get("summary") or "")[:600],
        )
        tag = assess(mail, lookups=False)
        person = contacts.find(address)
        human = bool(person) or (
            not contacts.looks_automated(address)
            and (record.get("action") in ("sent", "waiting", "drafted") or _looks_like_a_person(record.get("from_name") or ""))
        )
        kind = tag.get("kind") if tag.get("important") else ""
        if not fresh:
            # Older letters only come back while they are still something to do.
            if kind in _STILL_OPEN or record.get("key") in waiting or record.get("id") in waiting:
                items.append(_item(record, "action" if kind in _STILL_OPEN else "people", kind, person, waiting, still_open=True))
            continue
        if kind in _STILL_OPEN:
            section = "action"
        elif human:
            section = "people"
        elif kind:
            section = _KIND_SECTION.get(kind, "")
        elif _DELIVERY.search(mail.subject or ""):
            section = "booking"
        else:
            section = ""
        if section == "job" and not jobs:
            section = ""
        if not section:
            reason = _ignored_reason(mail, record, from_dev_tool(mail))
            ignored[reason] = ignored.get(reason, 0) + 1
            continue
        items.append(_item(record, section, kind, person, waiting))

    # Older open items: one per sender (the newest), and only a few, so a run of reminders does not flood the report.
    seen_senders: set[str] = set()
    kept, extra_open = [], 0
    for item in items:
        if not item.get("still_open"):
            kept.append(item)
            continue
        sender = (item["record"].get("from_email") or item["who"]).lower()
        if sender in seen_senders:
            continue
        seen_senders.add(sender)
        if sum(1 for other in kept if other.get("still_open")) >= _OPEN_SHOWN:
            extra_open += 1
            continue
        kept.append(item)
    items = kept
    # A booking usually comes as a confirmation plus a summary: one line per sender is enough.
    booked: set[str] = set()
    items = [
        item
        for item in items
        if item["section"] != "booking"
        or not ((item["record"].get("from_email") or item["who"]).lower() in booked or booked.add((item["record"].get("from_email") or item["who"]).lower()))
    ]

    # Replies still waiting for the owner, even when their letter came earlier.
    listed = {item["key"] for item in items}
    for key, approval in waiting.items():
        if key in listed or approval.get("role") == "notice":
            continue
        items.append(
            {
                "key": key, "section": "people", "who": approval.get("from_name") or approval.get("to") or approval.get("from_email") or "",
                "text": approval.get("summary") or approval.get("subject") or "", "status": "waiting", "still_open": False,
                "record": {"id": key, "mail_id": approval.get("mail_id") or "", "provider": approval.get("provider") or "gmail",
                           "from_name": approval.get("from_name") or "", "from_email": approval.get("from_email") or "",
                           "subject": approval.get("subject") or "", "summary": approval.get("summary") or "",
                           "thread_id": approval.get("thread_id") or "", "date": approval.get("date") or ""},
            }
        )

    items = _sharpen(items, language, complete)
    events = _upcoming(clients)
    return _write(items, events, ignored, new_count, language, extra_open)


def _item(record: dict, section: str, kind: str, person: dict | None, waiting: dict, still_open: bool = False) -> dict:
    key = record.get("id") or ""
    status = {"sent": "replied", "waiting": "waiting", "drafted": "waiting"}.get(record.get("action") or "", "")
    if key in waiting:
        status = "waiting"
    who = record.get("from_name") or record.get("from_email") or ""
    if person and person.get("company") and person.get("company") not in who:
        who = f"{who} ({person['company']})"
    text = (record.get("summary") or record.get("subject") or "").strip()
    return {
        "key": key, "section": section, "kind": kind, "who": who, "text": text, "status": status, "still_open": still_open,
        "date": str(record.get("processed_at") or "")[:10], "record": record,
    }


def _looks_like_a_person(name: str) -> bool:
    """"Mrs Anna Cooper" or "李明" yes; "Kogan.com Customer Care" or "Westlake Girls High School" no."""
    name = " ".join((name or "").split())
    return bool(name) and bool(_PERSON_NAME.match(name)) and not _NOT_A_PERSON.search(name)


def _ignored_reason(mail, record: dict, dev_tool: bool) -> str:
    from gmail_auto.importance import _JOB_ALERT

    code = record.get("skip_code") or ""
    if dev_tool:
        return "builds"
    if _JOB_ALERT.search(f"{mail.subject} {mail.from_name} {mail.from_email}") or "job" in (mail.from_name or "").lower():
        return "jobs"
    if code in ("ad", "promotions", "list", "social", "forums") or re.search(r"(?i)newsletter|daily shot|news\b", mail.subject or mail.from_name or ""):
        return "news"
    if re.search(r"(?i)receipt|order|your purchase", mail.subject or ""):
        return "receipts"
    return "other"


_SHARPEN = """You prepare the owner's morning email report. For each numbered letter decide the section and
write one short line saying what it is and what, if anything, the owner must do (with any amount and date).
Sections: action (the owner must pay, sign, verify, reply or decide), people (a real person wrote), institution
(school, government, bank, insurance), booking (bookings, travel, deliveries), job (the owner's own job
applications), ignore (nothing to do and not worth reading). Keep a section given unless it is clearly wrong.
Never invent facts. Write the lines in {language}.
Return JSON only: {{"items": [{{"n": 1, "section": "...", "line": "..."}}]}}"""


def _sharpen(items: list[dict], language: str, complete=None) -> list[dict]:
    """Let the model check sections and write clearer lines. Without a model the rules' result stands."""
    from gmail_auto.llm import model_ready, parse_json

    if not items:
        return items
    if complete is None:
        if not model_ready():
            return items
        from gmail_auto.llm import complete
    rows = "\n".join(
        f"{index}. section={item['section']} | from {item['who']} | {(item['record'].get('subject') or '')[:120]} | {item['text'][:240]}"
        for index, item in enumerate(items, 1)
    )
    try:
        data = parse_json(complete(_SHARPEN.format(language="Chinese" if language == "zh" else "English"), rows, max_tokens=2500, temperature=0)) or {}
    except (ReplyError, ConfigError):
        return items
    known = {key for key, *_ in SECTIONS} | {"ignore"}
    for row in data.get("items") or []:
        try:
            item = items[int(row.get("n")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        section = str(row.get("section") or "")
        if section in known and section != "calendar":
            item["section"] = section
        line = " ".join(str(row.get("line") or "").split())
        if line:
            item["text"] = line[:160]
            item["sharpened"] = True
    return [item for item in items if item["section"] != "ignore"]


def _upcoming(clients) -> list[dict]:
    try:
        from gmail_auto.calendars import list_upcoming

        events = list_upcoming(2).get("events") or []
    except Exception:
        return []
    today = now_local().date()
    out = []
    for event in events:
        start = event.get("start")
        day = start.astimezone(ZONE).date() if hasattr(start, "astimezone") else today
        if (day - today).days in (0, 1):
            out.append({"title": event.get("title") or "", "hour": event.get("hour") or "", "tomorrow": day != today, "location": event.get("location") or ""})
    return out


_WORDS = {
    "en": {
        "hello": "Good morning.", "came": "Since the last report: {n} emails came in.",
        "quiet": "Good morning. Nothing needs you today. {n} emails came in since the last report; none need you.",
        "replied": "replied automatically", "waiting": "draft waiting for your OK", "not_replied": "not replied",
        "still": "still open since {date}", "today": "today", "tomorrow": "tomorrow",
        "ignored": "Left out {n}: {parts}.", "footer": 'Reply with a number to read one, e.g. "1".',
        "builds": "builds and code", "jobs": "job ads", "news": "newsletters and ads", "receipts": "receipts", "other": "other notices",
        "more": "...and {n} more",
        "open_more": "...and {n} more open items: see Notices on the web page.",
        "join": " ",
    },
    "zh": {
        "hello": "早上好。", "came": "自上次早报以来收到 {n} 封邮件。",
        "quiet": "早上好。今天没有需要你处理的事。自上次早报以来收到 {n} 封邮件，都不用你操心。",
        "replied": "已自动回复", "waiting": "草稿等你确认", "not_replied": "未回复",
        "still": "自 {date} 起仍未处理", "today": "今天", "tomorrow": "明天",
        "ignored": "已自动忽略 {n} 封：{parts}。", "footer": "回复编号查看详情，例如“1”或“看第 4 封”。",
        "builds": "开发和构建通知", "jobs": "职位推送", "news": "简报和广告", "receipts": "收据", "other": "其他通知",
        "more": "……还有 {n} 封",
        "open_more": "……还有 {n} 项旧事项未处理，请到网页的“提醒”里查看。",
        "join": "",
    },
}


def _write(items: list[dict], events: list[dict], ignored: dict, new_count: int, language: str, extra_open: int = 0) -> tuple[str, list]:
    words = _WORDS["zh" if language == "zh" else "en"]
    order = [key for key, *_ in SECTIONS]
    numbered: list[dict] = []
    blocks = []
    for key, icon, title_en, title_zh in SECTIONS:
        title = title_zh if language == "zh" else title_en
        lines = []
        if key == "calendar":
            for event in events:
                when = words["tomorrow"] if event["tomorrow"] else words["today"]
                lines.append(f"- {when} {event['hour']} {event['title']}".replace("  ", " ").strip())
        else:
            chosen = [item for item in items if item["section"] == key]
            for item in chosen[:_PER_SECTION]:
                numbered.append(item)
                status = ""
                if key == "people":
                    status = words.get({"replied": "replied", "waiting": "waiting"}.get(item["status"], "not_replied"), "")
                if item.get("still_open"):
                    status = words["still"].format(date=item.get("date") or "")
                line = f"{len(numbered)}. {item['who']}: {_short(item['text'])}"
                lines.append(line + (f" —— {status}" if language == "zh" and status else f" ({status})" if status else ""))
            if len(chosen) > _PER_SECTION:
                lines.append(words["more"].format(n=len(chosen) - _PER_SECTION))
            if key == "action" and extra_open:
                lines.append(words["open_more"].format(n=extra_open))
        if lines:
            blocks.append(f"{icon} {title} ({len([l for l in lines if l[:1].isdigit()]) or len(lines)})\n" + "\n".join(lines))
    left_out = sum(ignored.values())
    if not blocks:
        return words["quiet"].format(n=new_count), []
    parts = ", ".join(f"{words.get(reason, reason)} {count}" for reason, count in sorted(ignored.items(), key=lambda pair: -pair[1]))
    text = words["hello"] + words["join"] + words["came"].format(n=new_count) + "\n\n" + "\n\n".join(blocks)
    if left_out:
        text += "\n\n✅ " + words["ignored"].format(n=left_out, parts=parts)
    if numbered:
        text += "\n\n" + words["footer"]
    return text, [item["record"] for item in numbered]


def _short(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def as_mail_list(records: list[dict]) -> list:
    """The numbered letters, in the shape the WhatsApp assistant keeps for "1", "2"..."""
    out = []
    for record in records:
        out.append(
            SimpleNamespace(
                key=record.get("id") or record.get("mail_id") or "", id=record.get("mail_id") or record.get("id") or "",
                provider=record.get("provider") or "gmail", thread_id=record.get("thread_id") or "",
                from_name=record.get("from_name") or "", from_email=record.get("from_email") or "", reply_email=record.get("from_email") or "",
                subject=record.get("subject") or "", date=record.get("date") or "", snippet=record.get("summary") or "", body="",
                received_ts=time.time(),
            )
        )
    return out
