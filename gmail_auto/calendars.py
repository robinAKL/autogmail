"""Mailbox calendars: upcoming events, and reminders or meetings created from email."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gmail_auto.errors import ConfigError, NotConnected
from gmail_auto.filters import Skip, classify
from gmail_auto.mailboxes import PROVIDERS
from gmail_auto.models import Mail
from gmail_auto.settings import load_settings
from gmail_auto.storage import load_doc, save_doc

ZONE = ZoneInfo("Pacific/Auckland")
_DAYS = 14
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HINT = re.compile(
    r"\b(meeting|invitation|invite|appointment|interview|reminder|catch[- ]?up|"
    r"zoom|teams|calendar|parent.?teacher|call at|see you)\b",
    re.IGNORECASE,
)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?\b")
_DMY = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_AHEAD_DAYS = 180
_NEAR = 160


def _hint_near_date(text: str) -> bool:
    """The meeting word sits close to the date, so a date elsewhere in a long letter does not count."""
    spans = [m.span() for m in _ISO.finditer(text)] + [m.span() for m in _DMY.finditer(text)]
    hints = [m.start() for m in _HINT.finditer(text)]
    return any(start - _NEAR <= spot <= end + _NEAR for start, end in spans for spot in hints)


def list_upcoming(days: int = _DAYS) -> dict:
    """Events from connected Outlook and Gmail calendars, next two weeks."""
    start = datetime.now(ZONE).replace(second=0, microsecond=0)
    end = start + timedelta(days=max(1, min(int(days), 60)))
    events, accounts, errors = _collect(start, end)
    days_out = []
    current = ""
    bucket = None
    for event in events:
        day = event["day"]
        if day != current:
            bucket = {"day": day, "events": []}
            days_out.append(bucket)
            current = day
        bucket["events"].append(event)
    return {"days": days_out, "events": events, "accounts": accounts, "errors": errors, "from": start, "until": end}


def ping_due_whatsapp() -> int:
    """WhatsApp ping for events starting in the next 20 minutes. Does not change calendar-app reminders."""
    settings = load_settings()
    if not settings.get("notify_calendar"):
        return 0
    from gmail_auto import whatsapp

    if not whatsapp.ready():
        return 0
    now = datetime.now(ZONE)
    until = now + timedelta(minutes=20)
    data = list_upcoming(1)
    saved = load_doc("calendar_pings", {})
    if not isinstance(saved, dict):
        saved = {}
    sent = 0
    for event in data.get("events") or []:
        start = event.get("start")
        event_id = event.get("id") or ""
        if not event_id or start is None or start <= now or start > until:
            continue
        if saved.get(event_id):
            continue
        minutes = max(1, int((start - now).total_seconds() // 60))
        line = f"Calendar in {minutes} min: {event.get('title') or 'Event'}"
        extra = event.get("location") or event.get("when") or ""
        if extra:
            line += f"\n{extra}"
        try:
            whatsapp.send_text(line)
        except ConfigError:
            break
        saved[event_id] = now.strftime("%Y-%m-%d %H:%M")
        sent += 1
    if sent:
        if len(saved) > 200:
            ordered = sorted(saved.items(), key=lambda pair: str(pair[1]))
            saved = dict(ordered[-200:])
        save_doc("calendar_pings", saved)
    return sent


def month_board(year: int | None = None, month: int | None = None, day: str = "") -> dict:
    """A Monday–Sunday month grid with events placed on their dates."""
    now = datetime.now(ZONE)
    try:
        year = int(year or now.year)
        month = int(month or now.month)
    except (TypeError, ValueError):
        year, month = now.year, now.month
    if year < 1970 or year > 2100 or month < 1 or month > 12:
        year, month = now.year, now.month
    first = datetime(year, month, 1, tzinfo=ZONE)
    next_first = datetime(year + 1, 1, 1, tzinfo=ZONE) if month == 12 else datetime(year, month + 1, 1, tzinfo=ZONE)
    last = next_first - timedelta(seconds=1)
    grid_start = first - timedelta(days=first.weekday())
    grid_end = (last + timedelta(days=6 - last.weekday())).replace(hour=23, minute=59, second=59, microsecond=0)
    events, accounts, errors = _collect(grid_start, grid_end + timedelta(seconds=1))
    by_date: dict[str, list] = {}
    for event in events:
        by_date.setdefault(event["date"], []).append(event)
    weeks = []
    cursor = grid_start.date()
    end_date = grid_end.date()
    while cursor <= end_date:
        week = []
        for _ in range(7):
            key = cursor.isoformat()
            week.append(
                {
                    "date": key,
                    "num": cursor.day,
                    "in_month": cursor.month == month,
                    "today": cursor == now.date(),
                    "events": by_date.get(key) or [],
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)
    selected = _selected_day(day, year, month, now)
    selected_events = by_date.get(selected) or []
    prev_month = 12 if month == 1 else month - 1
    prev_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year
    return {
        "year": year,
        "month": month,
        "title": first.strftime("%B %Y"),
        "weekdays": list(_WEEKDAYS),
        "weeks": weeks,
        "accounts": accounts,
        "errors": errors,
        "today": now.date().isoformat(),
        "selected": selected,
        "selected_label": _day_heading(selected),
        "selected_events": selected_events,
        "prev": {"year": prev_year, "month": prev_month},
        "next": {"year": next_year, "month": next_month},
        "this": {"year": now.year, "month": now.month, "day": now.date().isoformat()},
        "count": len(events),
    }


def suggest_event(mail: Mail, user_email: str = "", strict: bool = False) -> dict | None:
    """Turn a meeting or reminder letter into a calendar draft, or None if it is not that kind of mail.

    strict is for anything automatic (creating events while checking mail, WhatsApp pings): the letter
    must come from a person rather than a notification system, name the meeting in its subject or next to
    the date, and give a real date that is still ahead. Without strict, the owner asked for it by hand, so
    a letter without a date still gets a draft for tomorrow 09:00 that they can move.
    """
    if user_email:
        decision = classify(mail, user_email)
        if isinstance(decision, Skip) and decision.code in {"system", "promotions", "social", "list", "forums", "auto", "sensitive"}:
            return None
    text = f"{mail.subject or ''}\n{mail.body or mail.snippet or ''}"
    if not _HINT.search(text):
        return None
    found = _when(text)
    if strict:
        from gmail_auto.importance import is_notification

        if found is None or is_notification(mail):
            return None
        now = datetime.now(ZONE)
        if found < now or found > now + timedelta(days=_AHEAD_DAYS):
            return None
        if not _HINT.search(mail.subject or "") and not _hint_near_date(text):
            return None
    start = found or (datetime.now(ZONE) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    attendees = []
    for address in _EMAIL.findall(text):
        if address.lower() not in {(mail.from_email or "").lower(), user_email.lower()} and address not in attendees:
            attendees.append(address)
        if len(attendees) >= 8:
            break
    kind = "invite" if attendees else ("reminder" if "reminder" in text.lower() else "meeting")
    return {
        "title": (mail.subject or "Calendar item").strip()[:160],
        "start": start,
        "end": end,
        "location": "",
        "attendees": attendees,
        "kind": kind,
        "description": f"From email: {mail.from_name or mail.from_email}\n{mail.subject}",
        "mail_key": mail.key,
        "provider": mail.provider or "gmail",
    }


def create_from_mail(mail: Mail, user_email: str = "") -> dict:
    draft = suggest_event(mail, user_email)
    if draft is None:
        raise ConfigError("This letter does not look like a meeting, invitation or reminder.")
    if already_created(mail.key):
        raise ConfigError("A calendar item was already created from this letter.")
    event = create_event(mail.provider or "gmail", draft)
    _remember(mail.key, event.get("id") or "", auto=False, provider=mail.provider or "gmail")
    return event


def maybe_create_from_mail(mail: Mail, user_email: str) -> str:
    """Used while checking mail. Never raises: missing calendar access is skipped."""
    try:
        if not load_settings().get("auto_calendar"):
            return ""
        if already_created(mail.key):
            return ""
        draft = suggest_event(mail, user_email, strict=True)
        if draft is None:
            return ""
        event = create_event(mail.provider or "gmail", draft)
        _remember(mail.key, event.get("id") or "", auto=True, provider=mail.provider or "gmail")
        return event.get("title") or draft["title"]
    except ConfigError:
        return ""


def create_event(provider: str, draft: dict) -> dict:
    if provider == "outlook":
        return _outlook_create(draft)
    return _gmail_create(draft)


def _collect(start: datetime, end: datetime) -> tuple[list[dict], list[dict], list[str]]:
    events = []
    errors = []
    accounts = []
    for provider, fetch in (("outlook", _outlook_events), ("gmail", _gmail_events)):
        try:
            found, account = fetch(start, end)
        except NotConnected:
            continue
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        events.extend(found)
        if account:
            accounts.append({"provider": provider, "label": PROVIDERS.get(provider, provider.title()), "email": account})
    events.sort(key=lambda item: item["start"] or datetime.max.replace(tzinfo=ZONE))
    return events, accounts, errors


def _selected_day(day: str, year: int, month: int, now: datetime) -> str:
    if day:
        try:
            picked = datetime.strptime(day[:10], "%Y-%m-%d").date()
            return picked.isoformat()
        except ValueError:
            pass
    if now.year == year and now.month == month:
        return now.date().isoformat()
    return datetime(year, month, 1, tzinfo=ZONE).date().isoformat()


def _day_heading(iso: str) -> str:
    moment = datetime.strptime(iso, "%Y-%m-%d")
    return f"{moment.strftime('%A')} {moment.day} {moment.strftime('%B')}"


def already_created(mail_key: str) -> bool:
    saved = load_doc("calendar_mail", {}) or {}
    return bool(isinstance(saved, dict) and saved.get(mail_key))


def added_from_mail() -> int:
    """How many calendar items this app added while checking mail and that are still there to remove."""
    return len(_removable(_saved_mail_events()))


def remove_added_from_mail(limit: int = 40) -> dict:
    """Delete the calendar items this app added while checking mail. Nothing else in the calendar is touched.

    Only event ids this app wrote down when it created them are deleted. Items the owner added by hand with
    the button on a letter are kept. A removed letter stays on record, so it is never added again.
    """
    saved = _saved_mail_events()
    todo = _removable(saved)
    removed = 0
    failed = 0
    error = ""
    for mail_key, record in todo[: max(1, int(limit))]:
        provider = record.get("provider") or ("outlook" if mail_key.startswith("outlook:") else "gmail")
        try:
            _delete_event(provider, record["event_id"])
        except ConfigError as exc:
            failed += 1
            error = error or str(exc)
            continue
        saved[mail_key] = {**record, "event_id": "", "removed": True}
        removed += 1
    if removed:
        save_doc("calendar_mail", saved)
    return {"removed": removed, "failed": failed, "left": len(todo) - removed, "error": error}


def _saved_mail_events() -> dict:
    saved = load_doc("calendar_mail", {}) or {}
    return saved if isinstance(saved, dict) else {}


def _removable(saved: dict) -> list[tuple[str, dict]]:
    found = []
    for mail_key, record in saved.items():
        if not isinstance(record, dict) or not record.get("event_id") or record.get("removed"):
            continue
        if record.get("auto") is False:
            continue
        found.append((mail_key, record))
    return found


def _delete_event(provider: str, event_id: str) -> None:
    """Delete one event. One that is already gone counts as deleted."""
    if provider == "outlook":
        from gmail_auto import microsoft

        try:
            microsoft.graph("outlook", "DELETE", f"/me/events/{event_id}", timeout=15, attempts=1)
        except microsoft.GraphError as exc:
            if exc.status in (404, 410):
                return
            raise
        return
    service = _google_calendar()
    try:
        service.events().delete(calendarId="primary", eventId=event_id, sendUpdates="none").execute()
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if str(status) in ("404", "410"):
            return
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("Gmail calendar did not remove the item. Reconnect Gmail on Connections and try again.") from exc


def _remember(mail_key: str, event_id: str, auto: bool = True, provider: str = "") -> None:
    saved = load_doc("calendar_mail", {}) or {}
    if not isinstance(saved, dict):
        saved = {}
    saved[mail_key] = {
        "event_id": event_id,
        "at": datetime.now(ZONE).strftime("%Y-%m-%d %H:%M"),
        "auto": bool(auto),
        "provider": provider or ("outlook" if mail_key.startswith("outlook:") else "gmail"),
    }
    if len(saved) > 400:
        ordered = sorted(saved.items(), key=lambda pair: str((pair[1] or {}).get("at") if isinstance(pair[1], dict) else ""))
        saved = dict(ordered[-400:])
    save_doc("calendar_mail", saved)


def _outlook_events(start: datetime, end: datetime) -> tuple[list[dict], str]:
    from gmail_auto import microsoft
    from gmail_auto.outlook_client import OutlookClient

    account = OutlookClient().get_user_email()
    data = microsoft.graph(
        "outlook",
        "GET",
        "/me/calendarView",
        params={
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": "id,subject,start,end,location,organizer,isReminderOn,webLink,attendees",
            "$orderby": "start/dateTime",
            "$top": 50,
        },
        timeout=12,
        attempts=1,
    )
    events = [_view(item, "outlook") for item in data.get("value") or []]
    return [item for item in events if item], account


def _gmail_events(start: datetime, end: datetime) -> tuple[list[dict], str]:
    from gmail_auto.gmail_client import GmailClient

    account = GmailClient(allow_browser=False).get_user_email()
    service = _google_calendar()
    listed = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=_utc(start),
            timeMax=_utc(end),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    events = [_google_view(item) for item in listed.get("items") or []]
    return [item for item in events if item], account


def _outlook_create(draft: dict) -> dict:
    from gmail_auto import microsoft

    body = {
        "subject": draft["title"],
        "body": {"contentType": "text", "content": draft.get("description") or ""},
        "start": {"dateTime": draft["start"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Pacific/Auckland"},
        "end": {"dateTime": draft["end"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Pacific/Auckland"},
        "isReminderOn": True,
        "reminderMinutesBeforeStart": 30,
    }
    attendees = draft.get("attendees") or []
    if attendees:
        body["attendees"] = [{"emailAddress": {"address": item}, "type": "required"} for item in attendees]
        body["responseRequested"] = True
    created = microsoft.graph("outlook", "POST", "/me/events", body=body, timeout=15, attempts=1)
    viewed = _view(created, "outlook")
    if not viewed:
        raise ConfigError("Outlook did not create the calendar item.")
    return viewed


def _gmail_create(draft: dict) -> dict:
    service = _google_calendar()
    body = {
        "summary": draft["title"],
        "description": draft.get("description") or "",
        "start": {"dateTime": draft["start"].isoformat(), "timeZone": "Pacific/Auckland"},
        "end": {"dateTime": draft["end"].isoformat(), "timeZone": "Pacific/Auckland"},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
    }
    attendees = draft.get("attendees") or []
    if attendees:
        body["attendees"] = [{"email": item} for item in attendees]
    created = service.events().insert(calendarId="primary", body=body, sendUpdates="all" if attendees else "none").execute()
    viewed = _google_view(created)
    if not viewed:
        raise ConfigError("Gmail did not create the calendar item.")
    return viewed


def _google_calendar():
    from gmail_auto.gmail_client import load_credentials

    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ConfigError("Dependencies are not installed. Run: pip install -r requirements.txt") from exc
    creds = load_credentials(allow_browser=False)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _view(raw: dict, provider: str) -> dict | None:
    start = _graph_time(raw.get("start") or {})
    end = _graph_time(raw.get("end") or {}) or start
    if start is None:
        return None
    location = ((raw.get("location") or {}).get("displayName") or "").strip()
    attendees = []
    for item in raw.get("attendees") or []:
        address = ((item or {}).get("emailAddress") or {}).get("address") or ""
        if address:
            attendees.append(address)
    return _item(raw.get("id") or "", provider, raw.get("subject") or "(no title)", start, end, location, attendees, raw.get("webLink") or "")


def _google_view(raw: dict) -> dict | None:
    start = _google_time(raw.get("start") or {})
    end = _google_time(raw.get("end") or {}) or start
    if start is None:
        return None
    attendees = [item.get("email") or "" for item in raw.get("attendees") or [] if item.get("email")]
    return _item(raw.get("id") or "", "gmail", raw.get("summary") or "(no title)", start, end, raw.get("location") or "", attendees, raw.get("htmlLink") or "")


def _item(event_id: str, provider: str, title: str, start: datetime, end: datetime, location: str, attendees: list[str], link: str) -> dict:
    kind = "invite" if attendees else "reminder"
    if attendees or (end - start) >= timedelta(minutes=25):
        kind = "meeting" if not attendees else "invite"
    all_day = (end - start) >= timedelta(hours=20) or (start.hour == 0 and start.minute == 0 and end.hour == 0)
    return {
        "id": event_id,
        "provider": provider,
        "provider_label": PROVIDERS.get(provider, "Mail"),
        "title": title,
        "start": start,
        "end": end,
        "when": "All day" if all_day and start.date() == end.date() else _when_text(start, end),
        "hour": "" if all_day else start.strftime("%H:%M"),
        "date": start.date().isoformat(),
        "day": start.strftime("%A %d %B %Y"),
        "location": location,
        "attendees": attendees,
        "kind": kind,
        "all_day": all_day,
        "link": link,
    }


_WINDOWS_ZONES = {
    "new zealand standard time": "Pacific/Auckland",
    "nz standard time": "Pacific/Auckland",
    "aus eastern standard time": "Australia/Sydney",
    "e. australia standard time": "Australia/Brisbane",
    "china standard time": "Asia/Shanghai",
    "gmt standard time": "Europe/London",
    "pacific standard time": "America/Los_Angeles",
    "eastern standard time": "America/New_York",
}


def _graph_time(value: dict) -> datetime | None:
    """Microsoft Graph sends a wall-clock time plus its own timeZone field, which is UTC unless asked otherwise."""
    stamp = (value.get("dateTime") or value.get("date") or "").strip()
    if not stamp:
        return None
    stamp = stamp.replace("Z", "+00:00")
    # Graph writes seven fractional digits ("…T17:45:00.0000000"); older Pythons only read six.
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", stamp)
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_graph_zone(value.get("timeZone")))
    return moment.astimezone(ZONE)


def _graph_zone(name):
    text = (name or "").strip()
    if not text or text.upper() in {"UTC", "GMT", "Z", "ETC/UTC", "COORDINATED UNIVERSAL TIME", "TZONE://MICROSOFT/UTC"}:
        return timezone.utc
    mapped = _WINDOWS_ZONES.get(text.lower(), text)
    try:
        return ZoneInfo(mapped)
    except Exception:
        return timezone.utc


def _google_time(value: dict) -> datetime | None:
    stamp = (value.get("dateTime") or value.get("date") or "").strip()
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZONE)
    return moment.astimezone(ZONE)


def _utc(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when_text(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%d %b %H:%M')} – {end.strftime('%d %b %H:%M')}"


def _when(text: str) -> datetime | None:
    found = _ISO.search(text)
    if found:
        year, month, day = int(found.group(1)), int(found.group(2)), int(found.group(3))
        hour = int(found.group(4) or 9)
        minute = int(found.group(5) or 0)
        return _aware(year, month, day, hour, minute)
    found = _DMY.search(text)
    if found:
        day = int(found.group(1))
        month = _MONTHS[found.group(2).lower()]
        year = int(found.group(3))
        hour = int(found.group(4) or 9)
        minute = int(found.group(5) or 0)
        clock = _CLOCK.search(text[found.end() : found.end() + 24])
        if clock:
            hour, minute = _clock_parts(clock)
        return _aware(year, month, day, hour, minute)
    return None


def _clock_parts(match) -> tuple[int, int]:
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm" and hour < 12:
        hour += 12
    if match.group(3).lower() == "am" and hour == 12:
        hour = 0
    return hour, minute


def _aware(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZONE)
