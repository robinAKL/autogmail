"""Group letters into a story: one thread, plus related mail with the same person and subject."""

from __future__ import annotations

import re
import time

from gmail_auto.filters import is_system_address

_PREFIX = re.compile(r"^(?:re|fw|fwd|回复|转发)\s*:\s*", re.I)
_GENERIC = re.compile(
    r"delivery status notification|undeliverable|mail delivery subsystem|"
    r"out of office|automatic reply|auto[- ]reply|newsletter|unsubscribe",
    re.I,
)
_STATUS = {
    "waiting_on_me": "Waiting on you",
    "waiting_on_them": "Waiting on them",
    "in_progress": "In progress",
    "settled": "",
}


def stem_subject(subject: str) -> str:
    text = " ".join((subject or "").split())
    while True:
        cleaned = _PREFIX.sub("", text, count=1).strip()
        if cleaned == text:
            return cleaned.casefold()
        text = cleaned


def attach_stories(rows: list[dict]) -> list[dict]:
    """Stamp each row with story_id, size, status. Returns the same list."""
    if not rows:
        return rows
    groups = _groups(rows)
    by_id: dict[str, list[dict]] = {}
    for row, story_id in zip(rows, groups):
        row["story_id"] = story_id
        by_id.setdefault(story_id, []).append(row)
    for members in by_id.values():
        members.sort(key=_when)
        status = story_status(members)
        size = len(members)
        system = all(is_system_address((item.get("email") or "").strip()) for item in members)
        open_story = size >= 2 and status != "settled" and not system
        for row in members:
            row["story_size"] = size
            row["story_status"] = status
            row["story_open"] = open_story
    return rows


def story_view(row: dict | None, rows: list[dict]) -> dict | None:
    """Timeline for the opened letter. Hidden when the story is still a single letter."""
    if not row:
        return None
    story_id = row.get("story_id") or ""
    if not story_id or story_id in hidden_ids():
        return None
    members = [item for item in rows if item.get("story_id") == story_id]
    members.sort(key=_when)
    if len(members) < 2:
        return None
    status = row.get("story_status") or story_status(members)
    sources: list[str] = []
    seen = set()
    steps = []
    for item in members:
        steps.append(_step(item, current=item.get("key") == row.get("key")))
        if item.get("reply") and item.get("kind") in ("sent", "drafted"):
            steps.append(_outbound(item))
        for title in item.get("sources") or []:
            if title and title not in seen:
                seen.add(title)
                sources.append(title)
    return {
        "id": story_id,
        "status": status,
        "label": _STATUS.get(status, ""),
        "size": len(members),
        "steps": steps,
        "sources": sources,
    }


def story_status(members: list[dict]) -> str:
    if not members:
        return "settled"
    kinds = {item.get("kind") for item in members}
    groups = {item.get("group") for item in members}
    if "waiting" in kinds or "drafted" in kinds or "needs" in groups:
        return "waiting_on_me"
    newest = members[-1]
    if newest.get("kind") == "sent":
        return "waiting_on_them"
    if newest.get("kind") == "pending":
        return "waiting_on_me"
    if len(members) < 2:
        return "settled"
    if all(item.get("group") == "skipped" for item in members):
        return "settled"
    return "in_progress"


def _groups(rows: list[dict]) -> list[str]:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    by_thread: dict[tuple[str, str], int] = {}
    by_topic: dict[tuple[str, str, str], int] = {}
    for index, row in enumerate(rows):
        provider = (row.get("provider") or "gmail").strip()
        thread = (row.get("thread_id") or "").strip()
        if thread:
            key = (provider, thread)
            if key in by_thread:
                union(index, by_thread[key])
            else:
                by_thread[key] = index
        if not _topic_row(row):
            continue
        email = (row.get("email") or "").strip().casefold()
        stem = stem_subject(row.get("subject") or "")
        topic = (provider, email, stem)
        if topic in by_topic:
            other = by_topic[topic]
            other_thread = (rows[other].get("thread_id") or "").strip()
            if not thread or not other_thread or thread == other_thread:
                union(index, other)
        else:
            by_topic[topic] = index
    canon: dict[int, dict] = {}
    for index, row in enumerate(rows):
        root = find(index)
        info = canon.setdefault(
            root,
            {"provider": (row.get("provider") or "gmail").strip(), "thread": "", "email": "", "stem": "", "key": row.get("key") or index},
        )
        thread = (row.get("thread_id") or "").strip()
        if thread and not info["thread"]:
            info["thread"] = thread
        email = (row.get("email") or "").strip().casefold()
        stem = stem_subject(row.get("subject") or "")
        if email and stem and not info["stem"]:
            info["email"] = email
            info["stem"] = stem
    out = []
    for index, row in enumerate(rows):
        info = canon[find(index)]
        if info["thread"]:
            out.append(f"{info['provider']}:t:{info['thread']}")
        elif info["email"] and info["stem"]:
            out.append(f"{info['provider']}:s:{info['email']}:{info['stem']}")
        else:
            out.append(f"{info['provider']}:m:{info['key']}")
    return out


def _topic_row(row: dict) -> bool:
    """Same person + same subject can join a story, but not bounces or no-reply blasts."""
    email = (row.get("email") or "").strip()
    stem = stem_subject(row.get("subject") or "")
    if not email or len(stem) < 8:
        return False
    if is_system_address(email):
        return False
    if _GENERIC.search(stem) or _GENERIC.search(row.get("subject") or ""):
        return False
    return True


def story_board(rows: list[dict], hidden: set[str] | None = None) -> list[dict]:
    """One card per open matter, newest activity first. Hidden ids stay off the rail."""
    skip = hidden if hidden is not None else hidden_ids()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        story_id = row.get("story_id") or ""
        if not story_id or story_id in skip or not row.get("story_open"):
            continue
        groups.setdefault(story_id, []).append(row)
    cards = []
    for story_id, members in groups.items():
        members.sort(key=_when)
        newest = members[-1]
        first = members[0]
        status = newest.get("story_status") or story_status(members)
        cards.append(
            {
                "id": story_id,
                "title": display_title(first.get("subject") or newest.get("subject") or "This matter"),
                "who": newest.get("who") or first.get("who") or "Unknown",
                "blurb": (newest.get("summary") or newest.get("snippet") or newest.get("subject") or "")[:160],
                "status": status,
                "label": _STATUS.get(status, "In progress") or "In progress",
                "size": len(members),
                "date": newest.get("short_date") or newest.get("date") or "",
                "open_key": newest.get("key") or "",
                "letters": [
                    {
                        "key": item.get("key") or "",
                        "who": item.get("who") or "",
                        "subject": item.get("subject") or "",
                        "date": item.get("short_date") or item.get("date") or "",
                        "status": item.get("status") or "",
                    }
                    for item in members
                ],
            }
        )
    waiting = [card for card in cards if card["status"] == "waiting_on_me"]
    rest = [card for card in cards if card["status"] != "waiting_on_me"]
    rest.sort(key=lambda card: card.get("date") or "", reverse=True)
    return waiting + rest


def story_detail(story_id: str, rows: list[dict]) -> dict | None:
    story_id = (story_id or "").strip()
    if not story_id or story_id in hidden_ids():
        return None
    members = [row for row in rows if row.get("story_id") == story_id]
    if not members:
        return None
    members.sort(key=_when)
    view = story_view(members[-1], rows)
    if view is None:
        return None
    view["title"] = display_title(members[0].get("subject") or "")
    view["who"] = members[-1].get("who") or ""
    view["open_key"] = members[-1].get("key") or ""
    view["letters"] = [
        {
            "key": item.get("key") or "",
            "who": item.get("who") or "",
            "subject": item.get("subject") or "",
            "date": item.get("short_date") or item.get("date") or "",
            "status": item.get("status") or "",
            "kind": item.get("kind") or "",
        }
        for item in members
    ]
    return view


def display_title(subject: str) -> str:
    text = " ".join((subject or "").split())
    while True:
        cleaned = _PREFIX.sub("", text, count=1).strip()
        if cleaned == text:
            return cleaned or "This matter"
        text = cleaned


def hidden_ids() -> set[str]:
    from gmail_auto.storage import load_doc

    try:
        saved = load_doc("story_board", {})
    except Exception:
        return set()
    if not isinstance(saved, dict):
        return set()
    hidden = saved.get("hidden") or {}
    if not isinstance(hidden, dict):
        return set()
    return set(hidden)


def hide_story(story_id: str) -> None:
    from gmail_auto.storage import LOCK, load_doc, save_doc

    story_id = (story_id or "").strip()[:400]
    if not story_id:
        return
    with LOCK:
        saved = load_doc("story_board", {})
        if not isinstance(saved, dict):
            saved = {}
        hidden = dict(saved.get("hidden") or {})
        hidden[story_id] = {"at": time.time()}
        saved["hidden"] = hidden
        save_doc("story_board", saved)


def history_for_mail(mail) -> list[dict]:
    """Earlier letters in this matter, from saved records. Used when drafting a reply."""
    from gmail_auto.store import list_processed

    try:
        records = list_processed(400)
    except Exception:
        return []
    rows = []
    for record in records:
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "key": record.get("id") or "",
                "provider": record.get("provider") or getattr(mail, "provider", "gmail") or "gmail",
                "thread_id": record.get("thread_id") or "",
                "email": record.get("from_email") or "",
                "subject": record.get("subject") or "",
                "date": record.get("processed_at") or record.get("date") or "",
                "received_ts": 0,
                "kind": record.get("action") or "",
                "group": record.get("action") or "",
                "status": record.get("action") or "",
                "summary": record.get("summary") or "",
                "snippet": record.get("summary") or "",
                "reply": record.get("reply") or "",
                "short_date": record.get("date") or record.get("processed_at") or "",
                "sources": record.get("sources") or [],
                "who": record.get("from_name") or record.get("from_email") or "",
            }
        )
    current = {
        "key": getattr(mail, "key", "") or getattr(mail, "id", ""),
        "provider": getattr(mail, "provider", "gmail") or "gmail",
        "thread_id": getattr(mail, "thread_id", "") or "",
        "email": getattr(mail, "from_email", "") or "",
        "subject": getattr(mail, "subject", "") or "",
        "date": getattr(mail, "date", "") or "",
        "received_ts": float(getattr(mail, "received_ts", 0) or 0),
        "kind": "pending",
        "group": "needs",
        "status": "current",
        "summary": getattr(mail, "snippet", "") or "",
        "snippet": getattr(mail, "snippet", "") or "",
        "reply": "",
        "short_date": getattr(mail, "date", "") or "",
        "sources": [],
        "who": getattr(mail, "from_name", "") or getattr(mail, "from_email", "") or "",
    }
    attach_stories(rows + [current])
    story_id = current.get("story_id")
    members = [item for item in rows if item.get("story_id") == story_id and item.get("key") != current.get("key")]
    members.sort(key=_when)
    return members[-8:]


def format_history(members: list[dict]) -> str:
    if not members:
        return ""
    lines = ["THIS MATTER (earlier letters — do not invent facts that are not here):"]
    for item in members:
        who = item.get("who") or "Unknown"
        when = item.get("short_date") or item.get("date") or ""
        summary = (item.get("summary") or item.get("snippet") or item.get("subject") or "").strip()
        lines.append(f"- {when} {who}: {summary[:180]}")
        reply = (item.get("reply") or "").strip()
        if reply:
            lines.append(f"  You said: {reply[:180]}")
    return "\n".join(lines)


def _when(row: dict) -> tuple:
    return (float(row.get("received_ts") or 0), str(row.get("date") or ""))


def _step(row: dict, current: bool) -> dict:
    return {
        "key": row.get("key") or "",
        "who": row.get("who") or "Unknown sender",
        "date": row.get("short_date") or row.get("date") or "",
        "summary": (row.get("summary") or row.get("snippet") or row.get("subject") or "")[:180],
        "status": row.get("status") or "",
        "kind": row.get("kind") or "",
        "current": current,
        "outbound": False,
    }


def _outbound(row: dict) -> dict:
    kind = row.get("kind") or "sent"
    label = "You replied" if kind == "sent" else "Your draft"
    return {
        "key": row.get("key") or "",
        "who": label,
        "date": row.get("short_date") or row.get("date") or "",
        "summary": (row.get("reply") or "")[:180],
        "status": row.get("status") or "",
        "kind": "outbound",
        "current": False,
        "outbound": True,
    }
