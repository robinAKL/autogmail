"""Daily counters behind the dashboard.

Every handled letter adds to the counters of the day it was handled, so the
history stays small and survives even after old mail records are trimmed.
"""

import time
from datetime import date, datetime, timedelta

from gmail_auto.storage import LOCK, load_doc, save_doc

OUTCOMES = ("auto_sent", "approved_sent", "drafted", "asked", "skipped", "failed", "rejected", "expired")
SKIP_LABELS = {
    "sensitive": "Passwords, codes, ID or bank details",
    "system": "System or no-reply address",
    "list": "Mailing list",
    "auto": "Automatic reply",
    "promotions": "Promotion",
    "social": "Social notification",
    "forums": "Forum",
    "self": "From yourself",
    "empty": "No text to answer",
    "no_reply_needed": "No reply needed",
    "error": "Could not write a reply",
    "other": "Other",
}
MINUTES_SAVED_PER_REPLY = 4
_MAX_SENDERS_PER_DAY = 60


def note_received(mail) -> None:
    with LOCK:
        data, day = _today()
        day["received"] = day.get("received", 0) + 1
        hour = _hour(getattr(mail, "received_ts", 0))
        _bump(day.setdefault("by_hour", {}), hour)
        _bump(day.setdefault("by_provider", {}), getattr(mail, "provider", "") or "gmail")
        sender = (getattr(mail, "from_email", "") or "").lower()
        senders = day.setdefault("senders", {})
        if sender and (sender in senders or len(senders) < _MAX_SENDERS_PER_DAY):
            _bump(senders, sender)
        save_doc("stats", data)


def note_outcome(kind: str, mail=None, skip_code: str = "", sources: list[str] | None = None) -> None:
    if kind not in OUTCOMES:
        return
    with LOCK:
        data, day = _today()
        day[kind] = day.get(kind, 0) + 1
        if kind == "skipped":
            _bump(day.setdefault("skip_reasons", {}), skip_code or "other")
        if kind in ("auto_sent", "approved_sent"):
            received = float(getattr(mail, "received_ts", 0) or 0) if mail is not None else 0
            if received:
                waited = max(0.0, time.time() - received)
                if waited < 14 * 24 * 3600:
                    day["response_secs"] = day.get("response_secs", 0) + waited
                    day["response_n"] = day.get("response_n", 0) + 1
        if sources and kind in ("auto_sent", "approved_sent", "drafted", "asked"):
            day["kb_replies"] = day.get("kb_replies", 0) + 1
            docs = day.setdefault("kb_docs", {})
            for title in sources[:5]:
                _bump(docs, title[:80])
        save_doc("stats", data)


def note_decision(seconds: float, edited: bool = False) -> None:
    """How long an approval waited for the user, and whether the draft was changed first."""
    with LOCK:
        data, day = _today()
        day["decision_secs"] = day.get("decision_secs", 0) + max(0.0, seconds)
        day["decision_n"] = day.get("decision_n", 0) + 1
        if edited:
            day["edited"] = day.get("edited", 0) + 1
        save_doc("stats", data)


def dashboard(span: int = 30, today: date | None = None) -> dict:
    today = today or date.today()
    days = _load().get("days", {})
    yesterday = today - timedelta(days=1)
    y = _day(days, yesterday)
    before = _day(days, yesterday - timedelta(days=1))

    series = []
    for offset in range(span - 1, -1, -1):
        current = today - timedelta(days=offset)
        row = _day(days, current)
        series.append(
            {
                "date": current.isoformat(),
                "label": current.strftime("%d %b").lstrip("0"),
                "weekday": current.strftime("%a"),
                "auto_sent": row.get("auto_sent", 0),
                "approved_sent": row.get("approved_sent", 0),
                "drafted": row.get("drafted", 0),
                "asked": row.get("asked", 0),
                "skipped": row.get("skipped", 0),
                "failed": row.get("failed", 0),
                "received": row.get("received", 0),
            }
        )
    window = [_day(days, today - timedelta(days=offset)) for offset in range(span)]
    everything = [row for row in days.values() if isinstance(row, dict)]

    return {
        "span": span,
        "yesterday_label": yesterday.strftime("%A %d %B %Y").replace(" 0", " "),
        "has_history": bool(everything),
        "yesterday": _summary([y]),
        "previous": _summary([before]),
        "window": _summary(window),
        "all_time": _summary(everything),
        "first_day": min(days) if days else "",
        "series": series,
        "hours": _hours(window),
        "skip_reasons": _ranked(_merge(window, "skip_reasons"), SKIP_LABELS),
        "senders": _ranked(_merge(window, "senders"), limit=8),
        "providers": _ranked(_merge(window, "by_provider"), {"gmail": "Gmail", "outlook": "Outlook"}),
        "kb_docs": _ranked(_merge(window, "kb_docs"), limit=6),
    }


def digest_text(today: date | None = None) -> str:
    """Yesterday in a few lines, for the morning WhatsApp message."""
    data = dashboard(7, today)
    y = data["yesterday"]
    lines = [
        f"Yesterday ({data['yesterday_label']})",
        f"- {y['received']} new emails checked",
        f"- {y['sent']} replies sent ({y['auto_sent']} automatic, {y['approved_sent']} after your OK)",
        f"- {y['skipped']} skipped, {y['failed']} failed",
    ]
    if y["drafted"]:
        lines.append(f"- {y['drafted']} drafts saved without sending")
    if y["response_text"]:
        lines.append(f"- Typical reply time: {y['response_text']}")
    if y["minutes_saved"]:
        lines.append(f"- About {y['minutes_saved']} minutes of writing saved")
    return "\n".join(lines)


def rebuild_from_records(records: list[dict]) -> None:
    """First run after an upgrade: fill the counters from mail records that already exist."""
    with LOCK:
        if _load().get("days"):
            return
        days: dict = {}
        for record in records:
            stamp = str(record.get("processed_at") or "")[:10]
            if len(stamp) != 10:
                continue
            day = days.setdefault(stamp, {})
            day["received"] = day.get("received", 0) + 1
            action = record.get("action")
            if action == "sent":
                day["auto_sent"] = day.get("auto_sent", 0) + 1
            elif action == "drafted":
                day["drafted"] = day.get("drafted", 0) + 1
            elif action == "skipped":
                day["skipped"] = day.get("skipped", 0) + 1
                _bump(day.setdefault("skip_reasons", {}), record.get("skip_code") or _guess_code(record))
            hour = str(record.get("processed_at") or "")[11:13]
            if hour.isdigit():
                _bump(day.setdefault("by_hour", {}), hour)
            _bump(day.setdefault("by_provider", {}), record.get("provider") or "gmail")
            sender = str(record.get("from_email") or "").lower()
            if sender:
                _bump(day.setdefault("senders", {}), sender)
        if days:
            save_doc("stats", {"days": days})


def duration_text(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds < 90:
        return f"{int(round(seconds))} sec"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(round(minutes))} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} h"
    return f"{hours / 24:.1f} days"


def _summary(rows: list[dict]) -> dict:
    total = {key: sum(int(row.get(key, 0) or 0) for row in rows) for key in OUTCOMES + ("received", "kb_replies", "edited")}
    sent = total["auto_sent"] + total["approved_sent"]
    response_n = sum(int(row.get("response_n", 0) or 0) for row in rows)
    response_secs = sum(float(row.get("response_secs", 0) or 0) for row in rows)
    decision_n = sum(int(row.get("decision_n", 0) or 0) for row in rows)
    decision_secs = sum(float(row.get("decision_secs", 0) or 0) for row in rows)
    decided = total["approved_sent"] + total["rejected"]
    total.update(
        {
            "sent": sent,
            "minutes_saved": sent * MINUTES_SAVED_PER_REPLY,
            "response_text": duration_text(response_secs / response_n) if response_n else "",
            "decision_text": duration_text(decision_secs / decision_n) if decision_n else "",
            "approval_rate": round(100 * total["approved_sent"] / decided) if decided else None,
            "reply_rate": round(100 * sent / total["received"]) if total["received"] else None,
            "kb_rate": round(100 * total["kb_replies"] / (sent + total["drafted"])) if (sent + total["drafted"]) else None,
        }
    )
    return total


def _hours(rows: list[dict]) -> list[dict]:
    merged = _merge(rows, "by_hour")
    return [{"hour": hour, "count": int(merged.get(f"{hour:02d}", 0))} for hour in range(24)]


def _merge(rows: list[dict], key: str) -> dict:
    merged: dict = {}
    for row in rows:
        part = row.get(key)
        if isinstance(part, dict):
            for name, count in part.items():
                merged[name] = merged.get(name, 0) + int(count or 0)
    return merged


def _ranked(counts: dict, labels: dict | None = None, limit: int = 12) -> list[dict]:
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    return [{"key": name, "label": (labels or {}).get(name, name), "count": count} for name, count in ordered if count]


def _guess_code(record: dict) -> str:
    if record.get("sensitive"):
        return "sensitive"
    reason = str(record.get("reason") or "")
    lowered = reason.lower()
    for marks, code in (
        (("系统", "system"), "system"),
        (("邮件列表", "mailing list"), "list"),
        (("自动回复", "automatic reply"), "auto"),
        (("促销", "promotion"), "promotions"),
        (("社交", "social"), "social"),
        (("论坛", "forum"), "forums"),
        (("自己", "yourself"), "self"),
        (("没有可回复", "no text"), "empty"),
        (("openai", "停止", "stopped"), "error"),
    ):
        if any(mark in lowered or mark in reason for mark in marks):
            return code
    return "other"


def _load() -> dict:
    data = load_doc("stats", {})
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        return {"days": {}}
    return data


def _today() -> tuple[dict, dict]:
    data = _load()
    day = data["days"].setdefault(date.today().isoformat(), {})
    return data, day


def _day(days: dict, when: date) -> dict:
    row = days.get(when.isoformat())
    return row if isinstance(row, dict) else {}


def _hour(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp) if timestamp else datetime.now()
    return f"{moment.hour:02d}"


def _bump(counts: dict, key: str) -> None:
    counts[key] = int(counts.get(key, 0) or 0) + 1
