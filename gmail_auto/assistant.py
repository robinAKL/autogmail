"""What the owner can say over WhatsApp, and what happens when they do."""

import re

from gmail_auto import approvals, stats, whatsapp
from gmail_auto.errors import ConfigError
from gmail_auto.llm import complete
from gmail_auto.profile_store import load_profile
from gmail_auto.store import get_state, is_live, list_processed, live_from_env, set_live, set_state

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
    "- Any other text while a reply is waiting — I rewrite the draft that way and ask again\n"
    "- LIST — replies waiting for you\n"
    "- STATUS — today's and yesterday's numbers\n"
    "- PAUSE — stop sending anything until you turn it back on in the web page\n"
    "- Anything else — ask me about your mail"
)
_CHAT_SYSTEM = """You are the owner's email assistant, chatting with the owner on WhatsApp.
Answer the owner's question in a few short lines of plain text, in the language the owner wrote in.
Use only the information given below. If it does not contain the answer, say so plainly.
Never invent emails, senders, numbers or dates."""


def handle(incoming: whatsapp.Incoming) -> str:
    """Act on one message from the owner and return what to answer."""
    text = incoming.text.strip()
    action, target = _button(incoming.button)
    waiting = approvals.pending()
    item = None
    if target:
        item = approvals.get(target)
    if item is None:
        item = approvals.find_by_whatsapp(incoming.context_id)
    number = _number(text)
    if item is None and number:
        item = approvals.get(number)
    word = _word(text)

    if action == "approve" or (not action and word in _YES):
        return _approve(_pick(item, waiting), waiting)
    if action == "reject" or (not action and word in _NO):
        return _reject(_pick(item, waiting), waiting)
    if word in {"help", "?", "帮助", "menu"}:
        return _HELP
    if word in {"list", "pending", "waiting", "待处理", "列表"}:
        return _list(waiting)
    if word in {"status", "stats", "today", "yesterday", "summary", "统计", "状态"}:
        return _status(waiting)
    if word in {"pause", "stop", "暂停"}:
        return _pause()
    if not text:
        return _HELP

    chosen = item if item is not None and item.get("status") == "pending" else (waiting[0] if len(waiting) == 1 else None)
    if chosen is not None:
        return _revise(chosen, _strip_number(text))
    if len(waiting) > 1:
        return "Several replies are waiting. Start your message with the number, for example: 12 make it shorter\n\n" + _list(waiting)
    return _answer(text)


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
    lines = [f"{len(waiting)} waiting for your OK:"]
    for item in waiting[:10]:
        lines.append(f"#{item['id']} {item.get('from_name') or item.get('from_email')} — {_short(item.get('subject'))}")
    lines.append("Answer with YES or NO and the number.")
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
    if live_from_env() is not None:
        return "Sending is fixed by the LIVE_SEND setting on the server, so I cannot pause it from here."
    set_live(False)
    return "Paused. I will only save drafts until you turn sending back on in the web page."


def _answer(question: str) -> str:
    from gmail_auto.knowledge import load_index

    recent = []
    for record in list_processed(15):
        if record.get("sensitive"):
            continue
        recent.append(
            f"- {record.get('processed_at', '')[:16]} | {record.get('from_name') or record.get('from_email') or 'unknown'} | "
            f"{_short(record.get('subject'))} | {record.get('action')}"
        )
    context = (
        "BRIEFING ABOUT THE OWNER:\n" + (load_index()["brief"]["text"] or "(none yet)") + "\n\n"
        "RECENTLY HANDLED EMAIL (time | sender | subject | outcome):\n" + ("\n".join(recent) or "(none)") + "\n\n"
        "NUMBERS:\n" + stats.digest_text() + "\n\n"
        f"OWNER'S QUESTION:\n{question}"
    )
    try:
        reply = complete(_CHAT_SYSTEM, context, max_tokens=350).strip()
    except ConfigError as exc:
        return f"I could not answer that right now: {exc}\n\n{_HELP}"
    return reply or _HELP


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
    match = re.search(r"#\s*(\d{1,6})\b", text) or re.match(r"^\s*(\d{1,6})\b", text) or re.fullmatch(r"\D{1,20}?\s(\d{1,6})\s*", text)
    return match.group(1) if match else ""


def _strip_number(text: str) -> str:
    return re.sub(r"^\s*#?\s*\d{1,6}\s*[:：,.-]?\s*", "", text).strip() or text


def _word(text: str) -> str:
    cleaned = re.sub(r"#?\s*\d{1,6}", " ", text.lower())
    cleaned = re.sub(r"[.!?。！？,，'’]", "", cleaned)
    return " ".join(cleaned.split())


def _short(text, limit: int = 60) -> str:
    text = " ".join(str(text or "(no subject)").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
