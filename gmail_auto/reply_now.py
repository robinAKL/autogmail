"""Draft a reply to any letter when the owner presses Reply.

The draft joins the approvals queue, so it is sent, rewritten or dropped the same way as the
drafts the scheduled check writes. Nothing here sends mail.
"""

from gmail_auto import approvals
from gmail_auto.errors import ConfigError, ReplyError
from gmail_auto.filters import detect_language
from gmail_auto.models import Draft, Mail
from gmail_auto.store import load_processed, mark_processed, update_processed

_PLAIN_ASK = "Write a short, polite reply to this letter that answers what they asked, using only facts from the profile and knowledge base."


def draft_now(mail: Mail, points: str, profile: dict, write=None, rewrite=None) -> dict:
    """Return the waiting approval for this letter, writing a fresh draft when there is none yet.

    `points` is what the owner wants said ("yes, Friday 2pm"). When the letter already has a waiting
    draft, the points rewrite it instead of starting again.
    """
    points = (points or "").strip()[:1000]
    record = load_processed().get(mail.key)
    record = record if isinstance(record, dict) else None
    current = _waiting_item(record)
    if current is not None:
        if points:
            return approvals.revise(current["id"], points, profile, rewrite=rewrite)
        return current

    language = (record or {}).get("language") or detect_language(f"{mail.subject}\n{mail.body}")
    draft = _write(mail, points, profile, language, write, rewrite)
    item = approvals.create(mail, draft, language, role="ask")
    changes = {
        "action": "waiting",
        "approval_id": item["id"],
        "reply": draft.reply,
        "needs_review": True,
        "reason": draft.reason,
    }
    if record is not None:
        update_processed(mail.key, changes)
    else:
        from gmail_auto.runner import _record

        mark_processed(_record(mail, "waiting", draft.reason, draft.reply, language, False, "", draft, approval_id=item["id"]))
    return item


def _waiting_item(record: dict | None) -> dict | None:
    if not record or not record.get("approval_id"):
        return None
    item = approvals.get(record["approval_id"])
    if item and item.get("status") == "pending" and item.get("role") != "notice":
        return item
    return None


def _write(mail: Mail, points: str, profile: dict, language: str, write, rewrite) -> Draft:
    if rewrite is None:
        from gmail_auto.generator import revise_reply as rewrite
    if not points:
        if write is None:
            from gmail_auto.generator import draft_reply as write
        try:
            draft = write(mail, profile, language)
        except ReplyError as exc:
            # The safety check held the draft back. The owner asked for one and reads it before it goes,
            # so write a plain one and say what to check.
            draft = Draft(action="ask", reason=f"Check this draft carefully: {exc}")
        if draft.action != "skip" and (draft.reply or "").strip():
            draft.action = "ask"
            draft.reason = draft.reason or "You asked for this reply."
            return draft
        held = draft.reason if draft.reason.startswith("Check") else ""
        draft = Draft(action="ask", reason=held or "You asked for this reply.", summary=draft.summary, sources=list(draft.sources or []))
    else:
        draft = Draft(action="ask", reason="Written from your points.")
    shell = {
        "from_name": mail.from_name,
        "from_email": mail.from_email,
        "subject": mail.subject,
        "body": mail.body or mail.snippet or "",
        "draft": "",
        "language": language,
    }
    text = rewrite(shell, points or _PLAIN_ASK, profile)
    if not (text or "").strip():
        raise ConfigError("The model returned an empty draft. Try again.")
    draft.reply = text.strip()
    return draft
