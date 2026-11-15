"""What the owner can say over WhatsApp, and what happens when they do."""

import re

from gmail_auto import approvals, stats, whatsapp
from gmail_auto.errors import ConfigError
from gmail_auto.llm import complete, parse_json
from gmail_auto.profile_store import load_profile
from gmail_auto.store import get_state, is_live, list_processed, set_live, set_state

_YES = {
    "yes", "y", "yep", "yeah", "ok", "okay", "approve", "approved", "send", "send it", "go", "go ahead", "sure", "confirm",
    "同意", "可以", "好", "好的", "发", "发送", "发吧", "行", "确认", "是", "是的", "批准",
}
_NO = {
    "no", "n", "nope", "skip", "reject", "cancel", "dont send", "do not send", "don't send", "discard",
    "不同意", "不", "不用", "不发", "不要发", "拒绝", "取消", "跳过", "不行",
}
_HELP = (
    "I can do these from here:\n"
    "- YES / NO — send or drop the reply that is waiting (add its number, e.g. YES 12, when several wait)\n"
    "- 12 <what to say> — draft a reply to that letter, then YES to send\n"
    "- Reply lee <what to say> — find the open letter from that person and draft it\n"
    "- UNREAD — summarize important unread emails with numbers\n"
    "- SEARCH <keyword> — search recent emails (last 50 unread/recent) with numbers\n"
    "- REPLY <n> <what to say> / ARCHIVE <n> — draft a reply or archive by number\n"
    "- Any other text while a reply is waiting — I rewrite the draft that way and ask again\n"
    "- LIST — letters waiting for you\n"
    "- STATUS — today's and yesterday's numbers\n"
    "- PAUSE — stop sending anything until you turn it back on in the web page\n"
    "- Anything else — ask me about your mail"
)
_CHAT_SYSTEM = """You are the owner's email assistant, chatting with the owner on WhatsApp.
Reply in the language the owner wrote in (Chinese or English).
Use ONLY the knowledge base, profile, and email context given below. Never invent emails, senders, prices, or dates.
Return ONE JSON object with these fields:
- intent: one of "answer", "summarize", "read", "draft_reply", "compose", "archive", "mark_read"
- number: listed email number as string (e.g. "1") if referring to a listed email, else ""
- query: search keyword or sender name if looking up an email, else ""
- to: recipient email address for "compose", else ""
- subject: email subject for "compose", else ""
- body: full email body for "compose" (written in the owner's tone and grounded in the knowledge base), else ""
- instruction: editing/reply instruction for "draft_reply", else ""
- reply: concise plain-text answer for "answer" (or short summary for "read"), else ""
"""


def handle(incoming: whatsapp.Incoming, clients: list | None = None) -> str:
    """Act on one message from the owner and return what to answer."""
    text = incoming.text.strip()
    action, target = _button(incoming.button)
    waiting = approvals.open_items()
    item = None
    if target:
        item = approvals.get(target)
    if item is None:
        item = approvals.find_by_whatsapp(incoming.context_id)
    number = _number(text)
    if item is None and number:
        found = approvals.get(number)
        if found is not None and found.get("status") == "pending":
            item = found
        elif _listed_entry(number) is None:
            item = found
    word = _word(text)

    if action == "approve" or (not action and word in _YES):
        if item is None and not target and not number:
            pending = get_state("wa_pending_action")
            if isinstance(pending, dict) and pending.get("kind"):
                return _execute_pending(pending, clients)
        return _approve(_pick(item, waiting), waiting)
    if action == "reject" or (not action and word in _NO):
        if item is None and not target and not number:
            pending = get_state("wa_pending_action")
            if isinstance(pending, dict) and pending.get("kind"):
                set_state("wa_pending_action", None)
                return "Cancelled. Nothing was sent or deleted."
        return _reject(_pick(item, waiting), waiting)
    if word in {"help", "?", "帮助", "menu"}:
        return _HELP
    if word in {"list", "pending", "waiting", "待处理", "列表"}:
        return _list(waiting)
    if word in {"status", "stats", "today", "yesterday", "summary", "统计", "状态"}:
        return _status(waiting)
    if word in {"pause", "stop", "暂停"}:
        return _pause()
    if word in {"unread", "总结未读", "未读", "未读邮件", "summary unread", "summarize unread"}:
        return _summarize_unread(clients)
    search_query = _search_query(text)
    if search_query is not None:
        return _search_mail(search_query, clients)
    archive_num = _archive_number(text)
    if archive_num:
        return _archive_by_number(archive_num, clients)
    if not text:
        return _HELP
    if item is None and number:
        listed = _listed_entry(number)
        if listed is not None:
            instruction = _instruction(text)
            if not instruction:
                who = listed.get("from_name") or listed.get("from_email") or "Unknown"
                return f"#{number}: {who} — {_short(listed.get('subject'))}\nSend: REPLY {number} <what to say> or ARCHIVE {number}"
            return _reply_listed(number, listed, instruction, clients)
    if item is None:
        hinted = approvals.find_by_hint(text)
        if len(hinted) == 1:
            item = hinted[0]
        elif len(hinted) > 1:
            return "Which letter?\n\n" + _list(hinted)

    chosen = item if item is not None and item.get("status") == "pending" else (waiting[0] if len(waiting) == 1 else None)
    if chosen is not None:
        instruction = _instruction(text)
        if not instruction:
            if chosen.get("draft"):
                return (
                    f"Draft for #{chosen['id']} (to {chosen.get('from_name') or chosen.get('from_email')}):\n\n"
                    f"{chosen['draft']}\n\nReply YES to send, NO to drop it, or tell me what to change."
                )
            return f"#{chosen['id']} has no draft yet. Send: {chosen['id']} <what you want to say>"
        return _revise(chosen, instruction)
    if len(waiting) > 1:
        return "Several letters are waiting. Start with the number, for example: 12 make it shorter\n\n" + _list(waiting)
    return _answer(text, clients)


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
    if not waiting:
        return "Nothing is waiting for you."
    lines = [f"{len(waiting)} waiting for you:"]
    for item in waiting[:10]:
        mark = f" [{item.get('kind')}]" if item.get("kind") else ""
        if item.get("role") == "notice" and not (item.get("draft") or "").strip():
            lines.append(f"#{item['id']}{mark} {item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))} (reply {item['id']} <what to say>)")
        else:
            lines.append(f"#{item['id']}{mark} {item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))}")
    lines.append("Answer with YES or NO and the number, or 12 <what to say>.")
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
    return (
        f"{prefix}From: {who}\nSubject: {subject}"
        + (f"\nDate: {when}" if when else "")
        + (f"\n\n{body.strip()}" if body.strip() else "")
        + (f"\n\nReply: REPLY {num} <what to say> | Archive: ARCHIVE {num}" if num else "")
    )


def _execute_pending(pending: dict, clients: list | None) -> str:
    kind = pending.get("kind")
    provider = pending.get("provider") or ""
    found = _mail_clients(clients)
    client = _client_for(provider, clients) if provider else (found[0] if found else None)
    if client is None:
        return "Could not run that action: no mailbox is connected."
    try:
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
    recent = []
    for record in list_processed(12):
        if record.get("sensitive"):
            continue
        recent.append(
            f"- {record.get('processed_at', '')[:16]} | {record.get('from_name') or record.get('from_email') or 'unknown'} | "
            f"{_short(record.get('subject'))} | {record.get('action')}"
        )
    listed = get_state("wa_mail_list", [])
    listed_lines = []
    if isinstance(listed, list):
        for entry in listed[:10]:
            listed_lines.append(f"#{entry.get('num')} {entry.get('from_name') or entry.get('from_email')} — {_short(entry.get('subject'))}")

    context = (
        _kb_and_profile_context(question) + "\n\n"
        "LISTED EMAILS (numbered for quick actions):\n" + ("\n".join(listed_lines) or "(none)") + "\n\n"
        "RECENTLY HANDLED EMAIL (time | sender | subject | outcome):\n" + ("\n".join(recent) or "(none)") + "\n\n"
        "NUMBERS:\n" + stats.digest_text() + "\n\n"
        f"OWNER'S MESSAGE:\n{question}"
    )
    try:
        raw = complete(_CHAT_SYSTEM, context, max_tokens=600).strip()
    except ConfigError as exc:
        return f"I could not answer that right now: {exc}\n\n{_HELP}"
    if not raw:
        return _HELP

    data = parse_json(raw)
    if not isinstance(data, dict):
        return raw

    intent = str(data.get("intent") or "answer").strip().lower()
    number = str(data.get("number") or "").strip().lstrip("#")
    query = str(data.get("query") or "").strip()

    if intent == "summarize":
        if query:
            return _search_mail(query, clients)
        return _summarize_unread(clients)

    if intent == "read":
        entry = _resolve_mail_entry(number, query, clients)
        if entry is None:
            return str(data.get("reply") or "Could not find that email. Send UNREAD or SEARCH <keyword> first.")
        return _read_entry(entry, clients)

    if intent == "draft_reply":
        entry = _resolve_mail_entry(number, query, clients)
        if entry is None:
            return "Which email should I reply to? Send UNREAD or SEARCH <keyword> first."
        instruction = str(data.get("instruction") or question).strip()
        return _reply_listed(str(entry.get("num") or "1"), entry, instruction, clients)

    if intent == "compose":
        to_addr = str(data.get("to") or "").strip()
        subject = str(data.get("subject") or "").strip() or "Hello"
        body = str(data.get("body") or "").strip()
        if not to_addr or not body:
            return str(data.get("reply") or "Please tell me the recipient email address and what you want to say.")
        set_state("wa_pending_action", {"kind": "send_new", "to": to_addr, "subject": subject, "body": body})
        return f"Ready to send:\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n\nReply YES to send, or NO to cancel."

    if intent == "archive":
        entry = _resolve_mail_entry(number, query, clients)
        if entry is None or not entry.get("mail_id"):
            return "Could not find which email to archive. Send UNREAD or SEARCH <keyword> first."
        who = entry.get("from_name") or entry.get("from_email") or "Unknown"
        label = f"{who} — {_short(entry.get('subject'))}"
        set_state(
            "wa_pending_action",
            {"kind": "archive", "mail_id": entry["mail_id"], "provider": entry.get("provider") or "gmail", "label": label},
        )
        return f"Archive/delete \"{label}\"? Reply YES to confirm, or NO to cancel."

    if intent == "mark_read":
        entry = _resolve_mail_entry(number, query, clients)
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

    return str(data.get("reply") or "").strip() or raw


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
    match = (
        re.search(r"#\s*(\d{1,6})\b", text)
        or re.match(r"^\s*(?:reply|re|回[复覆]?)\s+#?(\d{1,6})\b", text, flags=re.IGNORECASE)
        or re.match(r"^\s*(\d{1,6})\b", text)
        or re.fullmatch(r"\D{1,20}?\s(\d{1,6})\s*", text)
    )
    return match.group(1) if match else ""


def _strip_number(text: str) -> str:
    return re.sub(r"^\s*#?\s*\d{1,6}\s*[:：,.-]?\s*", "", text).strip() or text


def _instruction(text: str) -> str:
    cleaned = re.sub(r"^\s*(?:reply|re|回[复覆]?)\s+#?\d{1,6}\s*[:：,.-]?\s*", "", text.strip(), flags=re.IGNORECASE)
    if cleaned != text.strip():
        return cleaned.strip()
    cleaned = _strip_number(text)
    cleaned = re.sub(r"^(reply|re|回[复覆]?)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


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
    return entries


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
    needle = query.lower()
    seen_keys: set[str] = set()
    matches = []
    for client in found_clients:
        candidates = []
        for method_name, arg in (("list_unread", 50), ("list_recent", 50)):
            fn = getattr(client, method_name, None)
            if fn is None:
                continue
            try:
                candidates.extend(fn(arg))
            except ConfigError:
                continue
        for mail in candidates:
            if mail.key in seen_keys:
                continue
            seen_keys.add(mail.key)
            blob = " ".join([mail.from_name or "", mail.from_email or "", mail.subject or "", mail.snippet or "", mail.body or ""]).lower()
            if needle in blob:
                matches.append(mail)
    if not matches:
        return f"No recent emails matching \"{query}\"."
    matches.sort(key=lambda m: float(getattr(m, "received_ts", 0) or 0), reverse=True)
    entries = _save_mail_list(matches)
    return _format_mail_list(f"Search \"{query}\" ({len(entries)}):", entries)


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
