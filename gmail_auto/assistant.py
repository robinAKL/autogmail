"""What the owner can say over WhatsApp, and what happens when they do."""

import re
from datetime import datetime

from gmail_auto import approvals, stats, whatsapp
from gmail_auto import llm as llm_mod
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.llm import complete, parse_json
from gmail_auto.profile_store import load_profile
from gmail_auto.store import get_state, is_live, list_processed, set_live, set_state

_YES = {
    "yes", "y", "yep", "yeah", "ok", "okay", "approve", "approved", "send", "send it", "go", "go ahead", "sure", "confirm",
    "同意", "可以", "好", "好的", "发", "发送", "发吧", "行", "确认", "是", "是的", "批准",
}
_NO = {
    "no", "n", "nope", "skip", "reject", "cancel", "dont send", "do not send", "don't send", "discard", "drop",
    "ignore", "junk", "spam",
    "不同意", "不", "不用", "不发", "不要发", "拒绝", "取消", "跳过", "不行", "丢掉", "丢弃", "忽略", "删除草稿",
}
_NEW_TASK = re.compile(
    r"(?i)\b(search|unread|archive|pause|summar|meeting|invite|email|tomorrow|monday|tuesday|"
    r"找|搜索|未读|删|会议|邮件|日程)\b"
)
_HELP = (
    "Tell me what you want in ordinary language, for example:\n"
    "- Search / summarise unread / show details / is this spam\n"
    "- Reply to #12, drop 145 139 138, or send the waiting draft\n"
    "- Write a new email to someone\n"
    "- Create a meeting or invite\n"
    "- LIST / STATUS / PAUSE still work as shortcuts"
)
_INTENTS = (
    "approve", "reject", "revise", "list", "status", "pause", "help",
    "summarize", "search", "read", "analyze", "draft_reply", "compose",
    "archive", "mark_read", "create_event", "invite", "calendar", "answer",
)
_INTENT_SYSTEM = """You are the owner's email and calendar assistant on WhatsApp.
Read the owner's message and decide what they want done. Do not treat this as a keyword command list.
Reply in the language the owner wrote in (Chinese or English).
Use ONLY the knowledge base, profile, waiting items, listed emails, and recent mail given below. Never invent emails, senders, prices, or dates.

Return ONE JSON object:
- intent: one of
  approve (send waiting draft(s)),
  reject (drop waiting draft(s), or cancel a pending confirm),
  revise (rewrite a waiting draft using instruction),
  list, status, pause, help,
  summarize (unread / important mail),
  search (query is the keyword),
  read (full details of a letter),
  analyze (what it is, spam/phishing/ad vs real, what they want, whether to reply),
  draft_reply (write a reply to a listed or waiting letter),
  compose (new email; fill to, subject, body),
  archive, mark_read,
  create_event (personal reminder/meeting),
  invite (meeting with attendees),
  calendar (what's on the calendar),
  answer (a question; put the answer in reply)
- numbers: array of waiting-item or listed-email ids mentioned (e.g. ["145","139","138"]). Empty if none.
- number: first number as a string, or ""
- query: search keyword, sender, or calendar range, else ""
- to, subject, body: for compose
- instruction: how to reply or revise
- title, start, end, location: for calendar (start/end ISO 8601 in Pacific/Auckland if known, else "")
- attendees: array of email addresses for invite
- reply: short answer for intent=answer or analyze, else ""

Examples:
- "Drop 145 139 138" or "这三封不用回" with those ids → reject, numbers ["145","139","138"]
- "NO" while several wait and last_focus is set → reject those last_focus ids
- Bare YES / OK / 确认 while PENDING CONFIRM is set → approve with empty numbers (confirm that pending action only; do not invent waiting-draft ids)
- Never copy WAITING APPROVALS ids into numbers unless the owner typed those ids or clearly said the first/second letter
- "帮我看看第一封是不是广告" → analyze, number "1"
- "明天下午3点和 lee@x.com 开会" → invite
- "给 lee@x.com 发邮件说明报价" → compose
"""


def handle(incoming: whatsapp.Incoming, clients: list | None = None) -> str:
    """Act on one message from the owner and return what to answer."""
    text = incoming.text.strip()
    action, target = _button(incoming.button)
    waiting = approvals.open_items()
    if action == "approve":
        return _apply_many("approve", [target] if target else [], waiting, clients)
    if action == "reject":
        return _apply_many("reject", [target] if target else [], waiting, clients)
    if not text:
        return _HELP
    pending = get_state("wa_pending_action")
    if isinstance(pending, dict) and pending.get("kind"):
        stance = _confirm_stance(text)
        if stance == "yes":
            return _execute_pending(pending, clients)
        if stance == "no":
            set_state("wa_pending_action", None)
            return "Cancelled. Nothing was sent or deleted."
    parsed = _interpret(text, waiting, incoming.context_id, clients)
    return _dispatch(parsed, text, waiting, clients)


def _confirm_stance(text: str) -> str:
    """yes/no when the owner is answering the last confirm prompt — not starting a new task."""
    word = _word(text)
    if word in _YES:
        return "yes"
    if word in _NO:
        return "no"
    if _numbers(text) or _NEW_TASK.search(text or ""):
        return ""
    folded = word or (text or "").strip().lower()
    if len(folded) > 40:
        return ""
    if folded.startswith(("yes", "yep", "yeah", "ok", "okay", "sure", "confirm", "please do", "add it", "create it", "do it", "go ahead")):
        return "yes"
    if folded.startswith(("好", "可以", "行", "确认", "是", "同意", "加", "就这样")):
        return "yes"
    if folded.startswith(("no", "cancel", "stop", "don't", "dont", "不用", "不要", "取消", "算了", "先别")):
        return "no"
    return ""


def _ground_ids(text: str, numbers: list[str]) -> list[str]:
    """Only keep ids the owner actually pointed at. Never approve a pile the model invented."""
    typed = _numbers(text)
    clean = [str(item).strip().lstrip("#") for item in (numbers or []) if str(item).strip()]
    if typed:
        return [item for item in clean if item in typed] or typed
    if len(clean) > 1:
        return []
    return clean


def _interpret(text: str, waiting: list, context_id: str, clients: list | None) -> dict:
    """LLM first when a real model is wired; otherwise a fallback that still understands drop/YES/search."""
    fallback = _fallback_intent(text, waiting, context_id)
    try:
        llm_data = _llm_intent(text, waiting, clients)
    except (ConfigError, ReplyError):
        llm_data = None
    if llm_data and llm_data.get("intent") in _INTENTS:
        if not llm_data.get("numbers") and fallback.get("numbers"):
            llm_data["numbers"] = fallback["numbers"]
        llm_data["numbers"] = _ground_ids(text, llm_data.get("numbers") or [])
        if llm_data["numbers"]:
            llm_data["number"] = llm_data["numbers"][0]
        else:
            llm_data["number"] = ""
        return llm_data
    fallback["numbers"] = _ground_ids(text, fallback.get("numbers") or [])
    return fallback


def _llm_intent(text: str, waiting: list, clients: list | None) -> dict | None:
    raw = _complete_intent(_INTENT_SYSTEM, _intent_user_prompt(text, waiting, clients), max_tokens=700)
    if not raw:
        return None
    data = parse_json(raw)
    if not isinstance(data, dict):
        return {"intent": "answer", "reply": raw, "numbers": []}
    numbers = _as_numbers(data.get("numbers") or data.get("number"))
    data["numbers"] = numbers
    data["number"] = numbers[0] if numbers else str(data.get("number") or "").strip().lstrip("#")
    data["intent"] = str(data.get("intent") or "answer").strip().lower()
    if data["intent"] not in _INTENTS:
        data["intent"] = "answer"
    return data


def _complete_intent(system: str, user: str, max_tokens: int = 700) -> str:
    """Call Super, but skip the network with dummy test keys unless complete() is mocked."""
    from gmail_auto.llm import model_ready

    if complete is llm_mod.complete and not model_ready():
        raise ConfigError("no AI key")
    return complete(system, user, max_tokens=max_tokens, role="super").strip()


def _intent_user_prompt(text: str, waiting: list, clients: list | None) -> str:
    waiting_lines = []
    for item in waiting[:20]:
        waiting_lines.append(
            f"#{item.get('id')} [{item.get('kind') or item.get('role') or ''}] "
            f"{item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))}"
        )
    listed = get_state("wa_mail_list", [])
    listed_lines = []
    if isinstance(listed, list):
        for entry in listed[:10]:
            listed_lines.append(
                f"#{entry.get('num')} {entry.get('from_name') or entry.get('from_email')} — {_short(entry.get('subject'))}"
            )
    focus = _last_focus()
    pending = get_state("wa_pending_action")
    pending_line = ""
    if isinstance(pending, dict) and pending.get("kind"):
        pending_line = f"{pending.get('kind')} {pending.get('label') or pending.get('to') or ''}"
    recent = []
    for record in list_processed(8):
        if record.get("sensitive"):
            continue
        recent.append(
            f"- {record.get('from_name') or record.get('from_email') or 'unknown'} | {_short(record.get('subject'))} | {record.get('action')}"
        )
    return (
        _kb_and_profile_context(text) + "\n\n"
        "WAITING APPROVALS:\n" + ("\n".join(waiting_lines) or "(none)") + "\n\n"
        "LISTED EMAILS:\n" + ("\n".join(listed_lines) or "(none)") + "\n\n"
        f"LAST FOCUS IDS: {', '.join(focus) if focus else '(none)'}\n"
        f"PENDING CONFIRM: {pending_line or '(none)'}\n\n"
        "RECENTLY HANDLED:\n" + ("\n".join(recent) or "(none)") + "\n\n"
        "NUMBERS:\n" + stats.digest_text() + "\n\n"
        f"OWNER'S MESSAGE:\n{text}"
    )


def _fallback_intent(text: str, waiting: list, context_id: str) -> dict:
    numbers = _numbers(text)
    word = _word(text)
    item = _resolve_waiting_item(text, numbers, context_id, waiting)
    empty = {"intent": "answer", "numbers": numbers, "instruction": text, "query": "", "reply": ""}

    if word in _YES:
        return {"intent": "approve", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in _NO:
        return {"intent": "reject", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in {"help", "?", "帮助", "menu"}:
        return {"intent": "help", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in {"list", "pending", "waiting", "待处理", "列表"}:
        return {"intent": "list", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in {"status", "stats", "today", "yesterday", "summary", "统计", "状态"}:
        return {"intent": "status", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in {"pause", "stop", "暂停"}:
        return {"intent": "pause", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    if word in {"unread", "总结未读", "未读", "未读邮件", "summary unread", "summarize unread"}:
        return {"intent": "summarize", "numbers": numbers, "instruction": "", "query": "", "reply": ""}
    search_query = _search_query(text)
    if search_query is not None:
        return {"intent": "search", "numbers": numbers, "instruction": "", "query": search_query, "reply": ""}
    archive_num = _archive_number(text)
    if archive_num:
        return {"intent": "archive", "numbers": [archive_num], "instruction": "", "query": "", "reply": "", "immediate": True}

    number = numbers[0] if numbers else ""
    if _shown_mail_entry(numbers) is not None:
        item = None
    if item is None and number:
        listed = _listed_entry(number)
        if listed is not None:
            instruction = _instruction(text)
            if _wants_reading(instruction):
                return {"intent": "read", "numbers": [number], "instruction": "", "query": "", "reply": ""}
            return {"intent": "draft_reply", "numbers": [number], "instruction": instruction, "query": "", "reply": ""}

    if item is None:
        hinted = approvals.find_by_hint(text)
        if len(hinted) == 1:
            item = hinted[0]
            numbers = [str(item["id"])]
        elif len(hinted) > 1:
            _remember_focus([str(row["id"]) for row in hinted])
            return {
                "intent": "list",
                "numbers": [str(row["id"]) for row in hinted],
                "instruction": "",
                "query": "",
                "reply": "Which letter?\n\n" + _list(hinted),
                "subset": hinted,
            }

    chosen = item if item is not None and item.get("status") == "pending" else (waiting[0] if len(waiting) == 1 else None)
    if chosen is not None:
        instruction = _instruction(text)
        if not instruction:
            return {"intent": "read", "numbers": [str(chosen["id"])], "instruction": "", "query": "", "reply": "", "approval": chosen}
        return {"intent": "revise", "numbers": [str(chosen["id"])], "instruction": instruction, "query": "", "reply": "", "approval": chosen}
    if len(waiting) > 1:
        return {
            "intent": "list",
            "numbers": [],
            "instruction": "",
            "query": "",
            "reply": "Several letters are waiting. Tell me the number, or what you want done (search, reply, drop, meeting…)\n\n" + _list(waiting),
        }
    return empty


def _dispatch(parsed: dict, text: str, waiting: list, clients: list | None) -> str:
    intent = parsed.get("intent") or "answer"
    numbers = parsed.get("numbers") or []
    if parsed.get("number") and not numbers:
        numbers = [str(parsed["number"]).lstrip("#")]
    if numbers:
        _remember_focus(numbers)
    if intent == "revise" and _shown_mail_entry(numbers) is not None:
        intent = "read" if _wants_reading(parsed.get("instruction") or "") else "draft_reply"

    if intent == "help":
        return _HELP
    if intent == "list":
        if parsed.get("reply"):
            return parsed["reply"]
        subset = parsed.get("subset")
        if subset:
            _remember_focus([str(row["id"]) for row in subset])
            return "Which letter?\n\n" + _list(subset)
        return _list(waiting)
    if intent == "status":
        return _status(waiting)
    if intent == "pause":
        return _pause()
    if intent == "approve":
        pending_reply = _confirm_pending(text, "approve", clients)
        if pending_reply is not None:
            return pending_reply
        return _apply_many("approve", numbers, waiting, clients)
    if intent == "reject":
        pending_reply = _confirm_pending(text, "reject", clients)
        if pending_reply is not None:
            return pending_reply
        return _apply_many("reject", numbers, waiting, clients)
    if intent == "revise":
        item = parsed.get("approval") or _item_from_numbers(numbers, waiting)
        if item is None:
            return _which(waiting, "change")
        return _revise(item, parsed.get("instruction") or text)
    if intent == "summarize":
        if parsed.get("query"):
            return _search_mail(parsed["query"], clients)
        return _summarize_unread(clients)
    if intent == "search":
        query = (parsed.get("query") or _search_query(text) or text).strip()
        if query.lower() in {"search", "find", "搜索", "查找", "找"}:
            return "What should I search for?"
        return _search_mail(query, clients)
    if intent == "read":
        shown = _shown_mail_entry(numbers)
        if shown is not None:
            return _read_entry(shown, clients)
        approval = parsed.get("approval") or _item_from_numbers(numbers, waiting)
        if approval is not None and approval.get("draft"):
            return (
                f"Draft for #{approval['id']} (to {approval.get('from_name') or approval.get('from_email')}):\n\n"
                f"{approval['draft']}\n\nReply YES to send, NO to drop it, or tell me what to change."
            )
        if approval is not None and not (approval.get("draft") or "").strip():
            return f"#{approval['id']} has no draft yet. Send: {approval['id']} <what you want to say>"
        entry = _resolve_mail_entry(numbers[0] if numbers else "", parsed.get("query") or "", clients)
        if entry is None:
            return parsed.get("reply") or "Could not find that email. Ask me to summarise unread or search first."
        return _read_entry(entry, clients)
    if intent == "analyze":
        return _analyze(numbers, parsed, clients)
    if intent == "draft_reply":
        number = numbers[0] if numbers else ""
        entry = _listed_entry(number) if number else _resolve_mail_entry(number, parsed.get("query") or "", clients)
        if entry is None:
            item = _item_from_numbers(numbers, waiting)
            if item is not None:
                return _revise(item, parsed.get("instruction") or text)
            return "Which email should I reply to? Ask me to summarise unread or search first."
        return _reply_listed(str(entry.get("num") or number or "1"), entry, parsed.get("instruction") or text, clients)
    if intent == "compose":
        return _compose_preview(parsed)
    if intent == "archive":
        number = numbers[0] if numbers else ""
        if parsed.get("immediate") and number:
            return _archive_by_number(number, clients)
        return _ask_archive(number, parsed.get("query") or "", clients)
    if intent == "mark_read":
        return _mark_read_listed(numbers, parsed.get("query") or "", clients)
    if intent in {"create_event", "invite"}:
        return _ask_calendar(parsed, invite=intent == "invite")
    if intent == "calendar":
        return _calendar_brief(parsed.get("query") or "")
    reply = str(parsed.get("reply") or "").strip()
    if reply:
        return reply
    return _answer(text, clients)


def _confirm_pending(text: str, verb: str, clients: list | None) -> str | None:
    """A bare YES/NO confirms the last calendar/send/archive preview, not a pile of waiting notices."""
    pending = get_state("wa_pending_action")
    if not (isinstance(pending, dict) and pending.get("kind")):
        return None
    if _numbers(text):
        return None
    if verb == "reject":
        set_state("wa_pending_action", None)
        return "Cancelled. Nothing was sent or deleted."
    return _execute_pending(pending, clients)


def _apply_many(verb: str, numbers: list[str], waiting: list, clients: list | None) -> str:
    pending = get_state("wa_pending_action")
    if verb == "approve" and not numbers and isinstance(pending, dict) and pending.get("kind"):
        return _execute_pending(pending, clients)
    if verb == "reject" and not numbers and isinstance(pending, dict) and pending.get("kind"):
        set_state("wa_pending_action", None)
        return "Cancelled. Nothing was sent or deleted."
    ids = [str(n).lstrip("#") for n in numbers if str(n).strip()]
    if not ids:
        ids = list(_last_focus()) if verb == "reject" else []
    items = []
    for item_id in ids:
        found = approvals.get(item_id)
        if found is not None and found.get("status") == "pending":
            items.append(found)
    if not items:
        if len(ids) == 1:
            listed = _listed_entry(ids[0])
            if listed is not None and verb == "reject":
                return _ask_archive(ids[0], "", clients)
        picked = _pick(None, waiting)
        if picked is None:
            return _which(waiting, "send" if verb == "approve" else "drop")
        items = [picked]
    lines = []
    current = waiting
    for item in items:
        if verb == "approve":
            lines.append(_approve(item, current))
        else:
            lines.append(_reject(item, current))
        current = approvals.open_items()
    set_state("wa_last_focus", [])
    return "\n".join(lines)


def _analyze(numbers: list[str], parsed: dict, clients: list | None) -> str:
    canned = str(parsed.get("reply") or "").strip()
    number = numbers[0] if numbers else str(parsed.get("number") or "").lstrip("#")
    entry = _resolve_mail_entry(number, parsed.get("query") or "", clients)
    item = None
    if entry is None and number:
        item = approvals.get(number)
    if entry is None and item is None:
        return canned or "Which letter should I look at? Give a number, or ask me to summarise unread first."
    blob = ""
    header = ""
    if item is not None:
        header = f"#{item.get('id')} {item.get('from_name') or item.get('from_email')} — {item.get('subject')}"
        blob = f"From: {item.get('from_name')} <{item.get('from_email')}>\nSubject: {item.get('subject')}\nDate: {item.get('date')}\n\n{item.get('body') or item.get('draft') or ''}"
    else:
        header = f"#{entry.get('num')} {entry.get('from_name') or entry.get('from_email')} — {entry.get('subject')}"
        details = _read_entry(entry, clients)
        blob = details
    if canned:
        return f"{header}\n\n{canned}"
    from gmail_auto.models import Mail
    from gmail_auto.screen import screen_mail
    from gmail_auto.worker import brief_mail, format_brief

    source = item or entry or {}
    mail = Mail(
        id=source.get("mail_id") or number or "",
        thread_id=source.get("thread_id") or "",
        from_name=source.get("from_name") or "",
        from_email=source.get("from_email") or "",
        reply_email=source.get("reply_email") or "",
        subject=source.get("subject") or "",
        date=source.get("date") or "",
        body=blob,
        snippet=source.get("snippet") or "",
        provider=source.get("provider") or "gmail",
    )
    brief = brief_mail(mail)
    verdict = screen_mail(mail, brief)
    parts = [header, format_brief(brief)]
    if verdict is not None:
        parts.append(f"Decision: {verdict.disposition} — {verdict.reason}")
    return "\n\n".join(part for part in parts if part)


def _compose_preview(parsed: dict) -> str:
    to_addr = str(parsed.get("to") or "").strip()
    subject = str(parsed.get("subject") or "").strip() or "Hello"
    body = str(parsed.get("body") or "").strip()
    if not to_addr or not body:
        return str(parsed.get("reply") or "Please tell me the recipient email address and what you want to say.")
    set_state("wa_pending_action", {"kind": "send_new", "to": to_addr, "subject": subject, "body": body})
    return f"Ready to send:\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n\nReply YES to send, or NO to cancel."


def _ask_archive(number: str, query: str, clients: list | None) -> str:
    entry = _resolve_mail_entry(number, query, clients)
    if entry is None or not entry.get("mail_id"):
        return "Could not find which email to archive. Ask me to summarise unread or search first."
    who = entry.get("from_name") or entry.get("from_email") or "Unknown"
    label = f"{who} — {_short(entry.get('subject'))}"
    set_state(
        "wa_pending_action",
        {"kind": "archive", "mail_id": entry["mail_id"], "provider": entry.get("provider") or "gmail", "label": label},
    )
    return f"Archive/delete \"{label}\"? Reply YES to confirm, or NO to cancel."


def _mark_read_listed(numbers: list[str], query: str, clients: list | None) -> str:
    entry = _resolve_mail_entry(numbers[0] if numbers else "", query, clients)
    if entry is None or not entry.get("mail_id"):
        return "Could not find which email to mark as read."
    client = _client_for(entry.get("provider") or "gmail", clients)
    if client is None:
        return "Mailbox is not connected."
    try:
        client.mark_read(entry["mail_id"])
    except ConfigError as exc:
        return f"Could not mark as read: {exc}"
    return f"Marked as read: {_short(entry.get('subject'))}."


def _ask_calendar(parsed: dict, invite: bool) -> str:
    title = str(parsed.get("title") or parsed.get("subject") or "").strip() or "Meeting"
    start = _parse_when(parsed.get("start"))
    end = _parse_when(parsed.get("end"))
    if start is None:
        return str(parsed.get("reply") or "When should I put this on the calendar? Give a day and time.")
    if end is None:
        from datetime import timedelta

        end = start + timedelta(hours=1)
    attendees = parsed.get("attendees") or []
    if isinstance(attendees, str):
        attendees = [part.strip() for part in attendees.split(",") if part.strip()]
    if invite and not attendees:
        to_addr = str(parsed.get("to") or "").strip()
        if to_addr:
            attendees = [to_addr]
    draft = {
        "title": title[:160],
        "start": start,
        "end": end,
        "location": str(parsed.get("location") or "").strip(),
        "attendees": attendees,
        "description": str(parsed.get("body") or parsed.get("instruction") or "").strip(),
        "kind": "invite" if attendees else "meeting",
    }
    set_state("wa_pending_action", {"kind": "create_event", "provider": "", "draft": _event_draft_payload(draft)})
    when = start.strftime("%a %d %b %Y %H:%M") + "–" + end.strftime("%H:%M")
    who = (", ".join(attendees) if attendees else "just you")
    return f"Add to calendar:\n{title}\n{when}\nWho: {who}\n\nReply YES to create, or NO to cancel."


def _event_draft_payload(draft: dict) -> dict:
    payload = dict(draft)
    start, end = payload.get("start"), payload.get("end")
    payload["start"] = start.isoformat() if hasattr(start, "isoformat") else str(start or "")
    payload["end"] = end.isoformat() if hasattr(end, "isoformat") else str(end or "")
    return payload


def _parse_when(value) -> object | None:
    if value is None or value == "":
        return None
    if hasattr(value, "year"):
        return value
    text = str(value).strip()
    if not text:
        return None
    from datetime import datetime
    from gmail_auto.calendars import ZONE

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text.replace("Z", "")[:19] if "T" in text else text[:16], fmt)
            return parsed.replace(tzinfo=ZONE)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZONE)
    except ValueError:
        return None


def _calendar_brief(query: str) -> str:
    from gmail_auto.calendars import list_upcoming

    try:
        data = list_upcoming(14)
    except ConfigError as exc:
        return f"Could not read the calendar: {exc}"
    events = data.get("events") or []
    if query:
        needle = query.lower()
        events = [event for event in events if needle in f"{event.get('title')} {event.get('location')}".lower()]
    if not events:
        return "Nothing on the calendar in the next two weeks."
    lines = ["Upcoming:"]
    for event in events[:12]:
        lines.append(f"- {event.get('when')}: {event.get('title')}")
    return "\n".join(lines)


def _resolve_waiting_item(text: str, numbers: list[str], context_id: str, waiting: list):
    item = approvals.find_by_whatsapp(context_id)
    if item is not None:
        return item
    number = numbers[0] if numbers else _number(text)
    if not number:
        return None
    found = approvals.get(number)
    if found is not None and found.get("status") == "pending":
        return found
    if _listed_entry(number) is None:
        return found
    return None


def _item_from_numbers(numbers: list[str], waiting: list):
    for item_id in numbers:
        found = approvals.get(str(item_id).lstrip("#"))
        if found is not None and found.get("status") == "pending":
            return found
    return _pick(None, waiting) if len(numbers) == 0 else None


def _as_numbers(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip().lstrip("#") for item in value if str(item).strip()]
    text = str(value).strip()
    if "," in text or " " in text:
        return _numbers(text)
    return [text.lstrip("#")] if text else []


def _remember_focus(ids: list[str]) -> None:
    clean = [str(item).lstrip("#") for item in ids if str(item).strip()]
    set_state("wa_last_focus", clean[:20])


def _last_focus() -> list[str]:
    saved = get_state("wa_last_focus", [])
    if isinstance(saved, list):
        return [str(item) for item in saved if str(item).strip()]
    return []


def seen_before(message_id: str) -> bool:
    """WhatsApp delivers a message again when it is unsure the first delivery landed."""
    if not message_id:
        return False
    seen = get_state("wa_seen", [])
    if not isinstance(seen, list):
        seen = []
    if message_id in seen:
        return True
    set_state("wa_seen", (seen + [message_id])[-100:])
    return False


def _approve(item, waiting) -> str:
    if item is None:
        return _which(waiting, "send")
    if item.get("role") == "notice" and not (item.get("draft") or "").strip():
        return f"#{item['id']} has no draft yet. Send: {item['id']} <what you want to say>"
    if item.get("status") != "pending":
        return f"#{item['id']} was already handled ({item.get('status')})."
    try:
        approvals.approve(item["id"], "whatsapp")
    except ConfigError as exc:
        return f"I could not send #{item['id']}: {exc}"
    left = len(waiting) - 1
    more = f" {left} more waiting." if left > 0 else ""
    return f"Sent to {item.get('from_name') or item.get('from_email')} — \"{_short(item.get('subject'))}\".{more}"


def _reject(item, waiting) -> str:
    if item is None:
        return _which(waiting, "drop")
    if item.get("status") != "pending":
        return f"#{item['id']} was already handled ({item.get('status')})."
    try:
        approvals.reject(item["id"], "whatsapp")
    except ConfigError as exc:
        return str(exc)
    return f"OK, nothing sent for \"{_short(item.get('subject'))}\". The email stays in your inbox."


def _revise(item, instruction: str) -> str:
    try:
        updated = approvals.revise(item["id"], instruction, load_profile())
    except ConfigError as exc:
        return f"I could not change the draft: {exc}"
    return (
        f"Updated draft for #{updated['id']} (to {updated.get('from_name') or updated.get('from_email')}):\n\n"
        f"{updated['draft']}\n\nReply YES to send, NO to drop it, or tell me what else to change."
    )


def _list(waiting) -> str:
    set_state("wa_last_list", "approvals")
    if not waiting:
        return "Nothing is waiting for you."
    lines = [f"{len(waiting)} waiting for you:"]
    for item in waiting[:10]:
        mark = f" [{item.get('kind')}]" if item.get("kind") else ""
        if item.get("role") == "notice" and not (item.get("draft") or "").strip():
            lines.append(f"#{item['id']}{mark} {item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))} (reply {item['id']} <what to say>)")
        else:
            lines.append(f"#{item['id']}{mark} {item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))}")
    lines.append("Say what you want done, e.g. drop 12 15, reply 12 I'll call tomorrow, or YES 12 to send.")
    return "\n".join(lines)


def _status(waiting) -> str:
    data = stats.dashboard(7)
    today = data["series"][-1]
    sent_today = today["auto_sent"] + today["approved_sent"]
    mode = "sending is ON" if is_live() else "draft only (nothing is sent)"
    return (
        f"Today so far: {today['received']} checked, {sent_today} sent, {today['skipped']} skipped.\n"
        f"{stats.digest_text()}\n"
        f"Waiting for you: {len(waiting)}. Mode: {mode}."
    )


def _pause() -> str:
    set_live(False)
    return "Paused. I will only save drafts until you turn sending back on in the web page."


def _kb_and_profile_context(query: str) -> str:
    from gmail_auto.knowledge import get_document, list_documents, load_index, search as kb_search

    parts = []
    try:
        profile = load_profile()
        parts.append(
            f"OWNER PROFILE:\nName: {profile.get('name', '')}\nRole: {profile.get('role', '')}\n"
            f"Tone: {profile.get('tone', '')}\n"
            f"Cannot promise: {'; '.join(profile.get('cannot_promise', []))}"
        )
    except ConfigError:
        pass
    brief = (load_index().get("brief") or {}).get("text") or ""
    if brief:
        parts.append("KNOWLEDGE BRIEF:\n" + brief)
    hits = kb_search(query, limit=4)
    if hits:
        snippets = "\n".join(f"- [{h.get('title', 'Note')}] {h.get('snippet', '')}" for h in hits)
        parts.append("RELEVANT KNOWLEDGE:\n" + snippets)
    else:
        doc_lines = []
        for doc in list_documents()[:4]:
            full = get_document(doc["id"]) if doc.get("id") else None
            text = (full or {}).get("summary") or (full or {}).get("text") or ""
            if text:
                doc_lines.append(f"- [{doc.get('title', 'Note')}] {text[:300]}")
        if doc_lines:
            parts.append("KNOWLEDGE NOTES:\n" + "\n".join(doc_lines))
    return "\n\n".join(parts)


def _resolve_mail_entry(number: str, query: str, clients: list | None) -> dict | None:
    if number:
        entry = _listed_entry(number)
        if entry is not None:
            return entry
    saved = get_state("wa_mail_list", [])
    if isinstance(saved, list) and saved:
        if query:
            needle = query.lower()
            for entry in saved:
                blob = " ".join(str(entry.get(k) or "") for k in ("from_name", "from_email", "subject", "snippet")).lower()
                if needle in blob:
                    return entry
        if not query and not number:
            return saved[0]
    if query:
        _search_mail(query, clients)
        refreshed = get_state("wa_mail_list", [])
        if isinstance(refreshed, list) and refreshed:
            return refreshed[0]
    return None


def _explain_mail(who: str, subject: str, body: str) -> str:
    """Two or three plain sentences on what the letter says and whether anything needs doing."""
    from gmail_auto.llm import model_ready

    if not body.strip() or (complete is llm_mod.complete and not model_ready()):
        return ""
    try:
        text = complete(
            "Explain this email to its owner in two or three short, plain sentences: what it says, "
            "and whether they need to do anything (and by when). No greeting, no lists.",
            f"From: {who}\nSubject: {subject}\n\n{body[:3000]}",
            max_tokens=220,
        )
    except ConfigError:
        return ""
    return " ".join(text.split())[:600]


def _read_entry(entry: dict, clients: list | None) -> str:
    provider = entry.get("provider") or "gmail"
    mail_id = entry.get("mail_id") or ""
    client = _client_for(provider, clients)
    body = entry.get("snippet") or ""
    who = entry.get("from_name") or entry.get("from_email") or "Unknown"
    subject = entry.get("subject") or "(no subject)"
    when = entry.get("date") or ""
    if client is not None and mail_id:
        get_msg = getattr(client, "get_message", None)
        if get_msg is not None:
            try:
                mail = get_msg(mail_id)
                body = (mail.body or mail.snippet or "")[:1000]
                when = mail.date or when
            except ConfigError:
                pass
        else:
            for fn_name in ("list_unread", "list_recent"):
                fn = getattr(client, fn_name, None)
                if fn is None:
                    continue
                try:
                    candidates = fn(50)
                except ConfigError:
                    continue
                found = next((item for item in candidates if getattr(item, "id", "") == mail_id), None)
                if found is not None:
                    body = (found.body or found.snippet or "")[:1000]
                    when = found.date or when
                    break
    num = entry.get("num") or ""
    prefix = f"#{num} " if num else ""
    meaning = _explain_mail(who, subject, body)
    return (
        f"{prefix}From: {who}\nSubject: {subject}"
        + (f"\nDate: {when}" if when else "")
        + (f"\n\nWhat it means: {meaning}" if meaning else "")
        + (f"\n\n{body.strip()}" if body.strip() else "")
        + (f"\n\nReply: REPLY {num} <what to say> | Archive: ARCHIVE {num}" if num else "")
    )


def _execute_pending(pending: dict, clients: list | None) -> str:
    kind = pending.get("kind")
    provider = pending.get("provider") or ""
    found = _mail_clients(clients)
    client = _client_for(provider, clients) if provider else (found[0] if found else None)
    try:
        if kind == "create_event":
            from gmail_auto.calendars import create_event

            raw_draft = dict(pending.get("draft") or {})
            raw_draft["start"] = _parse_when(raw_draft.get("start"))
            raw_draft["end"] = _parse_when(raw_draft.get("end"))
            if raw_draft["start"] is None or raw_draft["end"] is None:
                set_state("wa_pending_action", None)
                return "That calendar draft had no valid time, so I cancelled it."
            event = create_event(
                provider or getattr(client, "provider", None) or "gmail",
                raw_draft,
            )
            set_state("wa_pending_action", None)
            return f"Added to calendar: {event.get('title') or raw_draft.get('title')} — {event.get('when') or ''}."
        if client is None:
            return "Could not run that action: no mailbox is connected."
        if kind == "send_new":
            client.send_notice(pending["to"], pending["subject"], pending["body"])
            set_state("wa_pending_action", None)
            return f"Sent email to {pending['to']} — \"{pending['subject']}\"."
        if kind == "archive":
            client.archive(pending["mail_id"])
            set_state("wa_pending_action", None)
            return f"Archived: {pending.get('label') or pending['mail_id']}."
    except ConfigError as exc:
        return f"Action failed: {exc}"
    set_state("wa_pending_action", None)
    return _HELP


def _answer(question: str, clients: list | None = None) -> str:
    try:
        raw = _complete_intent(_INTENT_SYSTEM, _intent_user_prompt(question, approvals.open_items(), clients), max_tokens=600)
    except (ConfigError, ReplyError) as exc:
        return f"I could not answer that right now: {exc}\n\n{_HELP}"
    if not raw:
        return _HELP
    data = parse_json(raw)
    if isinstance(data, dict):
        reply = str(data.get("reply") or "").strip()
        if reply:
            return reply
        if str(data.get("intent") or "") in _INTENTS and data.get("intent") != "answer":
            data["numbers"] = _as_numbers(data.get("numbers") or data.get("number"))
            return _dispatch(data, question, approvals.open_items(), clients)
    return raw


def _pick(item, waiting):
    if item is not None:
        return item
    return waiting[0] if len(waiting) == 1 else None


def _which(waiting, verb: str) -> str:
    if not waiting:
        return "Nothing is waiting for you right now."
    return f"Which one should I {verb}? Answer with the number.\n\n" + _list(waiting)


def _button(value: str) -> tuple[str, str]:
    action, _, target = (value or "").partition(":")
    if action in {"approve", "reject"} and target.isdigit():
        return action, target
    return "", ""


def _number(text: str) -> str:
    found = _numbers(text)
    return found[0] if found else ""


def _numbers(text: str) -> list[str]:
    cleaned = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b", " ", text or "")
    hashed = re.findall(r"#\s*(\d{1,6})\b", cleaned)
    if hashed:
        return list(dict.fromkeys(hashed))
    return list(dict.fromkeys(re.findall(r"\b(\d{1,6})\b", cleaned)))


def _strip_number(text: str) -> str:
    return re.sub(r"^\s*#?\s*\d{1,6}\s*[:：,.-]?\s*", "", text).strip() or text


def _instruction(text: str) -> str:
    cleaned = re.sub(r"^\s*(?:reply|re|回[复覆]?)\s+#?\d{1,6}\s*[:：,.-]?\s*", "", text.strip(), flags=re.IGNORECASE)
    if cleaned != text.strip():
        return cleaned.strip()
    cleaned = _strip_number(text)
    cleaned = re.sub(r"^(reply|re|回[复覆]?)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


_READ_WORDS = re.compile(
    r"\b(details?|tell me|what(?:'s| is| does)?|mean(?:ing|s)?|read|show|open|explain|summar(?:y|ise|ize)|about)\b"
    r"|详情|详细|什么意思|啥意思|看看|看一下|说说|讲讲|内容|解释",
    re.IGNORECASE,
)


def _wants_reading(instruction: str) -> bool:
    """'Tell me #1 details' asks to see the letter; 'reply 1 thanks' asks to answer it."""
    cleaned = (instruction or "").strip()
    if not cleaned:
        return True
    if re.match(r"^\s*(?:reply|re|回[复覆]?)\b", cleaned, flags=re.IGNORECASE):
        return False
    return bool(_READ_WORDS.search(cleaned)) and len(cleaned) <= 80


def _word(text: str) -> str:
    cleaned = re.sub(r"#?\s*\d{1,6}", " ", text.lower())
    cleaned = re.sub(r"[.!?。！？,，'’]", "", cleaned)
    return " ".join(cleaned.split())


def _short(text, limit: int = 60) -> str:
    text = " ".join(str(text or "(no subject)").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- mailbox commands (search / unread / reply / archive) -------------------


def _mail_clients(clients: list | None) -> list:
    if clients is not None:
        return list(clients)
    from gmail_auto.errors import NotConnected
    from gmail_auto.mailboxes import connected_clients

    try:
        found, _ = connected_clients()
        return found
    except (ConfigError, NotConnected):
        return []


def _client_for(provider: str, clients: list | None):
    found = _mail_clients(clients)
    for client in found:
        if (getattr(client, "provider", "gmail") or "gmail") == provider:
            return client
    return found[0] if len(found) == 1 else None


def _save_mail_list(mails: list) -> list[dict]:
    entries = []
    for idx, mail in enumerate(mails[:10], start=1):
        entries.append(
            {
                "num": str(idx),
                "provider": getattr(mail, "provider", "gmail") or "gmail",
                "mail_id": mail.id,
                "thread_id": mail.thread_id,
                "from_name": mail.from_name,
                "from_email": mail.from_email,
                "reply_email": mail.reply_email,
                "subject": mail.subject,
                "date": mail.date,
                "snippet": (mail.snippet or mail.body or "")[:300],
                "received_ts": float(getattr(mail, "received_ts", 0) or 0),
            }
        )
    set_state("wa_mail_list", entries)
    set_state("wa_last_list", "mail")
    return entries


def _shown_mail_entry(numbers: list[str]) -> dict | None:
    """A number right after a search or unread list means that list, not an older approval with the same number."""
    if not numbers or get_state("wa_last_list") != "mail":
        return None
    return _listed_entry(str(numbers[0]).lstrip("#"))


def _listed_entry(number: str) -> dict | None:
    entries = get_state("wa_mail_list", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("num")) == str(number):
            return entry
    return None


def _format_mail_list(header: str, entries: list[dict]) -> str:
    lines = [header]
    for entry in entries:
        who = entry.get("from_name") or entry.get("from_email") or "Unknown"
        lines.append(f"{entry['num']}. {who} — {_short(entry.get('subject'))}")
    lines.append("Reply with: REPLY <n> <message> or ARCHIVE <n>")
    return "\n".join(lines)


def _summarize_unread(clients: list | None = None) -> str:
    from gmail_auto.importance import assess

    found_clients = _mail_clients(clients)
    if not found_clients:
        return "No mailbox is connected yet."
    unread = []
    for client in found_clients:
        try:
            unread.extend(client.list_unread(50))
        except ConfigError:
            continue
    if not unread:
        set_state("wa_mail_list", [])
        return "No unread emails right now."
    unread.sort(key=lambda m: float(getattr(m, "received_ts", 0) or 0), reverse=True)
    important = [m for m in unread if assess(m).get("important")]
    picked = important[:10] if important else unread[:10]
    entries = _save_mail_list(picked)
    label = f"Important unread ({len(important)} of {len(unread)}):" if important else f"Latest unread ({len(unread)}):"
    return _format_mail_list(label, entries)


def _search_query(text: str) -> str | None:
    match = re.match(r"^(?:search|find|搜索|查找|找)\s+(.+)$", text.strip(), flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _search_mail(query: str, clients: list | None = None) -> str:
    found_clients = _mail_clients(clients)
    if not found_clients:
        return "No mailbox is connected yet."
    words = _search_words(query)
    seen_keys: set[str] = set()
    matches = []

    def consider(mail) -> None:
        if mail.key in seen_keys:
            return
        blob = " ".join(
            [mail.from_name or "", mail.from_email or "", mail.subject or "", mail.snippet or "", mail.body or ""]
        ).lower()
        if all(word in blob for word in words):
            seen_keys.add(mail.key)
            matches.append(mail)

    for client in found_clients:
        candidates = []
        # The mailbox's own search reaches older and archived letters, not just the newest page of the inbox.
        lookups = [("search", (query, 25)), ("list_unread", (50,)), ("list_recent", (50,))]
        for method_name, args in lookups:
            fn = getattr(client, method_name, None)
            if fn is None:
                continue
            try:
                candidates.extend(fn(*args))
            except ConfigError:
                continue
        for mail in candidates:
            consider(mail)
    # Letters the app already handled, as listed on the dashboard, even after they left the inbox.
    for mail in _processed_as_mail():
        consider(mail)
    if not matches:
        return f"No recent emails matching \"{query}\"."
    matches.sort(key=lambda m: float(getattr(m, "received_ts", 0) or 0), reverse=True)
    entries = _save_mail_list(matches)
    return _format_mail_list(f"Search \"{query}\" ({len(entries)}):", entries)


def _search_words(query: str) -> list[str]:
    cleaned = query.strip().strip("\"'“”‘’").lower()
    words = [word.strip("\"'“”‘’,.;:!?") for word in cleaned.split()]
    return [word for word in words if word] or [cleaned]


def _processed_as_mail() -> list:
    from types import SimpleNamespace

    try:
        records = list_processed(3000)
    except ConfigError:
        return []
    out = []
    for record in records:
        if record.get("sensitive") or not record.get("mail_id"):
            continue
        provider = record.get("provider") or "gmail"
        try:
            stamp = datetime.strptime(str(record.get("processed_at") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            stamp = 0.0
        out.append(
            SimpleNamespace(
                key=record.get("id") or record["mail_id"],
                id=record["mail_id"],
                provider=provider,
                thread_id=record.get("thread_id") or "",
                from_name=record.get("from_name") or "",
                from_email=record.get("from_email") or "",
                reply_email=record.get("from_email") or "",
                subject=record.get("subject") or "",
                date=record.get("date") or "",
                snippet=record.get("summary") or "",
                body="",
                received_ts=stamp,
            )
        )
    return out


def _archive_number(text: str) -> str:
    match = re.match(r"^(?:archive|arch|归档)\s+#?(\d{1,6})\b", text.strip(), flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _archive_by_number(number: str, clients: list | None = None) -> str:
    entry = _listed_entry(number)
    approval = approvals.get(number) if entry is None else None
    target = entry or approval
    if target is None:
        return f"Could not find #{number}. Send UNREAD or SEARCH <keyword> first."
    provider = target.get("provider") or "gmail"
    mail_id = target.get("mail_id") or ""
    client = _client_for(provider, clients)
    if client is None or not mail_id:
        return f"Could not archive #{number}: mailbox is not connected."
    try:
        client.archive(mail_id)
    except ConfigError as exc:
        return f"Could not archive #{number}: {exc}"
    if approval is not None and approval.get("status") == "pending":
        try:
            approvals.reject(approval["id"], "whatsapp")
        except ConfigError:
            pass
    who = target.get("from_name") or target.get("from_email") or "Unknown"
    return f"Archived #{number}: {who} — {_short(target.get('subject'))}."


def _reply_listed(number: str, entry: dict, instruction: str, clients: list | None = None) -> str:
    from gmail_auto.models import Draft, Mail

    provider = entry.get("provider") or "gmail"
    mail_id = entry.get("mail_id") or ""
    client = _client_for(provider, clients)
    mail = None
    if client is not None and mail_id:
        try:
            mail = client.get_message(mail_id)
        except ConfigError:
            mail = None
    if mail is None:
        mail = Mail(
            id=mail_id or number,
            thread_id=entry.get("thread_id") or "",
            from_name=entry.get("from_name") or "",
            from_email=entry.get("from_email") or "",
            reply_email=entry.get("reply_email") or "",
            subject=entry.get("subject") or "",
            date=entry.get("date") or "",
            body=entry.get("snippet") or "",
            snippet=entry.get("snippet") or "",
            provider=provider,
            received_ts=float(entry.get("received_ts") or 0),
        )
    created = approvals.create(
        mail,
        Draft(action="ask", reply="", reason="Requested from WhatsApp", summary=_short(mail.subject)),
        "en",
        role="ask",
    )
    try:
        updated = approvals.revise(created["id"], instruction, load_profile())
        draft_text = updated.get("draft") or ""
    except ConfigError as exc:
        try:
            approvals.reject(created["id"], "whatsapp")
        except ConfigError:
            pass
        return f"I could not draft a reply for #{number}: {exc}"
    if not draft_text.strip():
        try:
            approvals.reject(created["id"], "whatsapp")
        except ConfigError:
            pass
        return f"I could not draft a reply for #{number}: empty draft."
    who = mail.from_name or mail.from_email or "Unknown"
    return (
        f"Drafted reply #{created['id']} (to {who}):\n\n"
        f"{draft_text}\n\nReply YES {created['id']} to send, NO {created['id']} to drop it, or tell me what to change."
    )
