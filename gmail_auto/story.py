"""Group letters into a story: one thread, plus related mail with the same person and subject."""

from __future__ import annotations

import re

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
        open_story = size >= 2 and status != "settled"
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
    out = []
    for index, row in enumerate(rows):
        root = find(index)
        provider = (rows[root].get("provider") or row.get("provider") or "gmail").strip()
        out.append(f"{provider}:g{root}")
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
