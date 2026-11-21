import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from gmail_auto import approvals, stats
from gmail_auto.console import say
from gmail_auto.envfile import require_openai_key
from gmail_auto.errors import ConfigError, NotConnected, ReplyError, named_error
from gmail_auto.filters import Allow, Skip, classify, short_subject
from gmail_auto.models import Draft, Mail
from gmail_auto.screen import screen_mail
from gmail_auto.profile_store import load_profile, profile_is_placeholder
from gmail_auto.settings import append_signature, evaluate_auto_reply_rules, load_settings, signature_for_mailbox
from gmail_auto.storage import workspace_operation
from gmail_auto.tenant import require_user_id, saas_enabled
from gmail_auto.token_store import kv_set_if_absent, storage_ready
from gmail_auto.store import (
    bump_failure,
    clear_failure,
    get_processed,
    get_state,
    is_live,
    mark_processed,
    now_text,
    save_last_run,
    set_live,
    set_state,
    write_draft,
)

LOCK = threading.Lock()


def check_interval_seconds() -> int:
    try:
        minutes = float(os.getenv("CHECK_INTERVAL_MINUTES", "") or 5)
    except ValueError:
        minutes = 5
    return int(max(1.0, minutes) * 60)


@dataclass
class RunReport:
    live: bool
    finished_at: str
    checked: int = 0
    sent: int = 0
    drafted: int = 0
    asked: int = 0
    skipped: int = 0
    failed: int = 0
    lines: list[str] = field(default_factory=list)


def run_once(gmail=None, generate=None) -> RunReport:
    if saas_enabled():
        with workspace_operation("mail-check"):
            return _execute(gmail, generate)
    with LOCK:
        return _execute(gmail, generate)


def toggle_and_run(confirmed: bool, gmail=None, generate=None) -> RunReport:
    with LOCK:
        switch_live(not is_live(), confirmed)
        return _execute(gmail, generate)


def switch_live(live: bool, confirmed: bool = False) -> None:
    """Turn real sending on or off. Turning it on needs a real profile, a key and an explicit yes."""
    if not live:
        set_live(False)
        return
    if is_live():
        return
    if not confirmed:
        raise ConfigError("Tick the confirmation box before turning on real sending.")
    if profile_is_placeholder(load_profile()):
        raise ConfigError("The profile is still the placeholder example. Fill in your own facts before turning on real sending.")
    require_openai_key()
    set_live(True)


def start_scheduler() -> None:
    def loop() -> None:
        time.sleep(5)
        while True:
            try:
                run_once()
            except ConfigError:
                pass
            except Exception as exc:
                text = str(exc).strip().replace("\n", " ")
                if len(text) > 300:
                    text = text[:300] + "…"
                say(f"This round did not finish: {text or 'unknown error'}")
            time.sleep(check_interval_seconds())

    threading.Thread(target=loop, name="mail-check", daemon=True).start()


def format_report(report: RunReport) -> str:
    mode = "Sending" if report.live else "Draft only"
    text = (
        f"[{report.finished_at}] {mode}: checked {report.checked}, sent {report.sent}, "
        f"waiting for your OK {report.asked}, drafted {report.drafted}, "
        f"skipped {report.skipped}, failed {report.failed}."
    )
    if report.checked == 0 and not report.lines:
        text += " No new unread mail."
    if report.lines:
        text += "\n" + "\n".join(f"- {line}" for line in report.lines)
    return text


def send_daily_digest(now: datetime | None = None, clients: list | None = None) -> bool:
    """Once a day, at or after the chosen hour New Zealand time, send the morning report on WhatsApp."""
    from gmail_auto import digest, whatsapp

    settings = load_settings()
    if now is None:
        local = digest.now_local()
    else:
        local = now if now.tzinfo else now.replace(tzinfo=digest.ZONE)
    if not whatsapp.ready() or not digest.due(settings, get_state("digest_day") or "", local):
        return False
    if clients is None:
        try:
            from gmail_auto.mailboxes import connected_clients

            clients, _ = connected_clients()
        except (ConfigError, NotConnected):
            clients = []
    text, records = digest.build(
        clients,
        since=get_state("digest_sent_at") or "",
        language=settings.get("ui_language") or "en",
        jobs=bool(settings.get("digest_jobs")),
    )
    set_state("digest_day", local.date().isoformat())
    try:
        whatsapp.send_text(text)
    except ConfigError:
        return False
    set_state("digest_sent_at", now_text())
    if records:
        # "1", "2"... on WhatsApp now open the letters in this report.
        from gmail_auto.assistant import _save_mail_list

        _save_mail_list(digest.as_mail_list(records))
    return True


def _execute(gmail=None, generate=None) -> RunReport:
    try:
        report = _run_locked(gmail, generate)
    except ConfigError as exc:
        say(str(exc))
        save_last_run(
            {
                "finished_at": now_text(),
                "live": is_live(),
                "summary": "This round did not start.",
                "lines": [],
                "error": str(exc),
            }
        )
        raise
    save_last_run(_report_dict(report))
    say(format_report(report))
    return report


def _run_locked(gmail=None, generate=None) -> RunReport:
    profile = load_profile()
    live = is_live()
    if live and profile_is_placeholder(profile):
        set_live(False)
        raise ConfigError("The profile is still the placeholder example, so real sending was turned off. Fill in your own facts first.")
    settings = load_settings()
    if generate is None:
        require_openai_key()
        from gmail_auto.generator import draft_reply
        from gmail_auto.knowledge import refresh_brief_if_stale

        generate = draft_reply
        refresh_brief_if_stale(profile)

    problems: list[str] = []
    if gmail is not None:
        clients = [gmail]
    else:
        from gmail_auto.mailboxes import connected_clients

        clients, problems = connected_clients()
        if not clients:
            if problems:
                raise ConfigError(problems[0])
            raise NotConnected("No mailbox is connected yet. Open Connections and connect Gmail or Outlook.")

    report = RunReport(live=live, finished_at=now_text())
    report.lines.extend(problems)
    try:
        expired = approvals.expire_old()
        if expired:
            report.lines.append(f"{expired} waiting replies were dropped after a week without an answer.")
    except ConfigError:
        pass

    already = 0
    scanned_any = False
    for client in clients:
        try:
            already += _run_mailbox(client, generate, profile, settings, live, report)
            scanned_any = True
        except ConfigError as exc:
            if len(clients) == 1:
                raise
            report.lines.append(named_error(getattr(client, "provider", "mailbox").title(), exc))
    if scanned_any:
        try:
            set_state("scan_since_ts", max(_scan_since_ts(), int(time.time()) - 60))
        except ConfigError:
            pass
    if already and report.checked == 0:
        report.lines.append(f"{already} unread letters were already handled and were not answered again.")
    try:
        from gmail_auto.calendars import ping_due_whatsapp

        ping_due_whatsapp()
    except ConfigError:
        pass
    except Exception:
        pass
    report.lines = report.lines[:40]
    try:
        send_daily_digest(clients=clients)
    except ConfigError:
        pass
    return report


def _scan_since_ts() -> int:
    """High-water mark for real-time scanning; first run defaults to the last 24 hours."""
    try:
        saved = int(get_state("scan_since_ts", 0) or 0)
    except (ConfigError, TypeError, ValueError):
        saved = 0
    if saved > 0:
        return saved
    return int(time.time()) - 86400


def _list_unread(client, since_ts: int) -> list[Mail]:
    try:
        mails = client.list_unread(20, since_ts=since_ts)
    except TypeError:
        mails = client.list_unread(20)
    return [mail for mail in mails if not mail.received_ts or mail.received_ts >= since_ts]


def _run_mailbox(client, generate, profile: dict, settings: dict, live: bool, report: RunReport) -> int:
    user = client.get_user_email()
    since_ts = _scan_since_ts()
    already = 0
    for mail in _list_unread(client, since_ts):
        if not mail.account:
            mail.account = user
        if get_processed(mail.key):
            already += 1
            continue
        report.checked += 1
        stats.note_received(mail)
        decision = classify(mail, user)
        if isinstance(decision, Skip):
            notice_id = _maybe_notice(mail, decision, settings, report)
            _skip(client, mail, decision, report, approval_id=notice_id)
            continue
        if not isinstance(decision, Allow):
            continue
        if settings.get("translation_mode", "off") != "off":
            try:
                from gmail_auto.translator import translate_inbound_mail

                mail = translate_inbound_mail(
                    mail,
                    user_lang=settings.get("first_language", "zh"),
                    preserve_langs=settings.get("preserve_langs", []),
                )
            except Exception:
                pass
        try:
            from gmail_auto.calendars import maybe_create_from_mail

            added = maybe_create_from_mail(mail, user)
            if added:
                report.lines.append(f"Calendar: {added}")
        except Exception:
            pass
        from gmail_auto.worker import brief_mail, remember_verdict

        brief = brief_mail(mail)
        verdict = screen_mail(mail, brief)
        if verdict is not None:
            remember_verdict(mail.key, verdict)
        if verdict is not None and verdict.disposition == "ignore":
            # "No reply needed" is not "not important": a school letter or an IRD notice still reaches WhatsApp.
            notice_id = ""
            if (verdict.code or "") not in ("junk", "phishing", "ad"):
                notice_id = _maybe_notice(mail, None, settings, report, summary=brief.get("summary") or "")
            _skip(client, mail, Skip(verdict.reason or "junk", code=verdict.code or "junk"), report, approval_id=notice_id)
            continue
        try:
            from gmail_auto.contacts import note_letter

            note_letter(mail, brief)
        except ConfigError:
            pass
        try:
            draft = _as_draft(generate(mail, profile, decision.language))
        except ReplyError as exc:
            _fail(client, mail, report, str(exc))
            continue
        if verdict is not None and verdict.disposition == "ask" and draft.action == "reply":
            draft.action = "ask"
            draft.reason = draft.reason or verdict.reason
        draft = _restrain_auto_reply(mail, draft)
        # Replies the safety checks held (automated senders, notices) wait whatever the sending mode.
        must_wait = bool(getattr(draft, "hold", False))
        if draft.action in {"reply", "ask"}:
            from gmail_auto.contacts import TYPES, always_ask

            try:
                close = always_ask(mail.reply_email or mail.from_email)
            except ConfigError:
                close = None
            if close is not None:
                # Friends and family: never sent on its own, even in "send whenever it can" mode.
                must_wait = True
                draft.action = "ask"
                draft.reason = f"{close.get('name') or 'This person'} is marked as {TYPES[close['type']].lower()}; replies to them always wait for you."
        if draft.action in {"reply", "ask"} and not draft.summary and brief.get("summary"):
            draft.summary = brief["summary"]
        if draft.action in {"reply", "ask"}:
            sig = signature_for_mailbox(settings, provider=getattr(mail, "provider", ""), email=getattr(mail, "account", ""))
            if sig:
                draft.reply = append_signature(draft.reply, sig)
            if live and settings.get("approval_mode") in {"smart", "never"} and not must_wait:
                allowed, rule_reason = evaluate_auto_reply_rules(mail, settings)
                if not allowed:
                    must_wait = True
                    draft.action = "ask"
                    draft.reason = rule_reason
        subject = short_subject(mail.subject)
        if draft.action == "skip":
            reason = draft.reason or "no reply needed"
            notice_id = _maybe_notice(mail, None, settings, report, summary=draft.summary)
            mark_processed(_record(mail, "skipped", reason, "", decision.language, False, skip_code="no_reply_needed", summary=draft.summary, approval_id=notice_id))
            _remember(client, mail.id, done=True)
            stats.note_outcome("skipped", mail, skip_code="no_reply_needed")
            report.skipped += 1
            report.lines.append(f"No reply needed: {subject}")
            continue
        try:
            if not live:
                outbox = write_draft(mail, draft.reply)
                mark_processed(_record(mail, "drafted", draft.reason, draft.reply, decision.language, False, outbox, draft))
                clear_failure(mail.key)
                stats.note_outcome("drafted", mail, sources=draft.sources)
                report.drafted += 1
                report.lines.append(f"Draft saved: {subject}")
            elif must_wait or settings["approval_mode"] == "always" or (settings["approval_mode"] == "smart" and draft.action == "ask"):
                tag = _tag(mail)
                item = approvals.create(mail, draft, decision.language, kind=tag.get("kind") if tag.get("important") else "")
                mark_processed(_record(mail, "waiting", draft.reason, draft.reply, decision.language, False, "", draft, approval_id=item["id"]))
                clear_failure(mail.key)
                _remember(client, mail.id, done=True)
                stats.note_outcome("asked", mail, sources=draft.sources)
                report.asked += 1
                report.lines.append(f"Waiting for your OK: {subject}" + _ask_owner(item))
            else:
                if not _deliver_reply(client, mail, draft, decision.language, user, report):
                    continue
                mark_processed(_record(mail, "sent", draft.reason, draft.reply, decision.language, False, "", draft))
                clear_failure(mail.key)
                stats.note_outcome("auto_sent", mail, sources=draft.sources)
                report.sent += 1
                report.lines.append(f"Replied: {subject}")
                _remember(client, mail.id, done=True)
                try:
                    client.mark_read(mail.id)
                except ConfigError:
                    report.lines.append(f"Replied to {subject}, but it could not be marked as read.")
                if settings["notify_auto_replies"]:
                    _tell_owner(f"Replied automatically to {mail.from_name or mail.from_email} — \"{subject}\":\n\n{draft.reply}")
        except ConfigError as exc:
            _fail(client, mail, report, str(exc))
    return already


def _deliver_reply(client, mail: Mail, draft: Draft, language: str, user: str, report: RunReport) -> bool:
    if not saas_enabled():
        client.send_reply(mail, draft.reply, user)
        return True

    if storage_ready():
        digest = hashlib.sha256(f"{user.casefold()}\n{mail.key}".encode("utf-8")).hexdigest()
        key = f"autogmail:u:{require_user_id()}:dispatch:{digest}"
        if not kv_set_if_absent(key, "1", 30 * 24 * 60 * 60):
            return False

    # Keep an attempt record before dispatch so a worker crash cannot make it retry blindly.
    reason = "Delivery could not be confirmed. Check Sent mail before sending another reply."
    mark_processed(_record(mail, "send_unconfirmed", reason, draft.reply, language, False, "", draft))
    try:
        client.send_reply(mail, draft.reply, user)
    except Exception:
        report.failed += 1
        report.lines.append(f"Delivery unconfirmed: {short_subject(mail.subject)}. Check Sent mail.")
        return False
    return True


def _ask_owner(item: dict) -> str:
    """Put the waiting reply in front of the owner on WhatsApp. The web page shows it either way."""
    from gmail_auto import whatsapp

    if not whatsapp.ready():
        return " (see the Approvals page)"
    try:
        approvals.remember_whatsapp(item["id"], whatsapp.send_approval(item))
    except ConfigError as exc:
        return f" (WhatsApp message failed: {exc})"
    return " (asked on WhatsApp)"


def _tell_owner(text: str) -> None:
    from gmail_auto import whatsapp

    if not whatsapp.ready():
        return
    try:
        whatsapp.send_text(text)
    except ConfigError:
        return


def _as_draft(result) -> Draft:
    if isinstance(result, Draft):
        return result
    return Draft(action="reply", reply=str(result or ""))


_NOTICE_REASON = re.compile(
    r"(?i)\b(notification|notice|automated|newsletter|routine (?:update|summary|reminder)|no reply (?:is )?needed|"
    r"status update|for your information|fyi|mass (?:email|mailing)|marketing)\b"
)


def _restrain_auto_reply(mail: Mail, draft: Draft) -> Draft:
    """Do not send a drafted reply back to an automated notice, even if the model was confident."""
    if draft.action != "reply":
        return draft
    from gmail_auto.filters import is_status_notice, is_system_address
    from gmail_auto.importance import assess, is_notification

    automated = (
        is_status_notice(mail)
        or is_system_address(mail.from_email or "")
        or is_system_address(mail.reply_email or "")
        or is_notification(mail)
    )
    if not automated:
        from gmail_auto.contacts import looks_automated

        # A shared or mailing address (communications@, hello@ on a mailing subdomain...) may still be a person
        # asking something, so it is not dropped; it just never goes out without the owner seeing it.
        if looks_automated(mail.reply_email or mail.from_email or ""):
            draft.action = "ask"
            draft.hold = True
            draft.reason = "Sent from a shared or automated address, so this reply waits for you." + (f" {draft.reason}" if draft.reason else "")
            return draft
        # The model said it is a notice but still chose to reply: trust the reason, not the action.
        if _NOTICE_REASON.search(draft.reason or ""):
            draft.action = "ask"
            draft.hold = True
            draft.reason = f"{draft.reason} (Looks like a notice, so this waits for you.)"
            return draft
        return draft
    if assess(mail).get("important"):
        draft.action = "ask"
        draft.hold = True
        draft.reason = draft.reason or "Automated sender — check before this goes out."
        return draft
    draft.action = "skip"
    draft.reason = draft.reason or "Automated notice, no reply needed."
    draft.reply = ""
    return draft


def _tag(mail: Mail) -> dict:
    from gmail_auto.importance import assess

    return assess(mail)


def _summarize_notice(mail: Mail, tag: dict) -> str:
    """Produce a concise real-time summary grounded in the knowledge base when an LLM key is configured."""
    from gmail_auto.envfile import has_openai_key

    if not has_openai_key():
        return tag["why"]
    try:
        from gmail_auto.knowledge import context_for, render_context
        from gmail_auto.llm import complete

        kb = render_context(context_for(mail))
        sender = f"{mail.from_name} <{mail.from_email}>" if mail.from_name else mail.from_email
        prompt = (
            f"From: {sender}\n"
            f"Subject: {mail.subject}\n"
            f"Category: {tag.get('label', 'important')}\n"
            f"Body:\n{mail.body[:1200]}"
        )
        if kb:
            prompt = f"Knowledge base:\n{kb[:1500]}\n\n{prompt}"
        summary = complete(
            "Summarize this incoming email in 1-2 short sentences for a WhatsApp alert, noting any action or deadline.",
            prompt,
            max_tokens=120,
            temperature=0.2,
            role="worker",
        ).strip()
        return summary or tag["why"]
    except ConfigError:
        return tag["why"]


def _maybe_notice(mail: Mail, decision, settings: dict, report: RunReport, summary: str = "") -> str:
    from gmail_auto.importance import PING_SKIPS

    if not settings.get("notify_important"):
        return ""
    if decision is not None and (getattr(decision, "sensitive", False) or getattr(decision, "code", "") not in PING_SKIPS):
        return ""
    tag = _tag(mail)
    contact = _ping_contact(mail)
    if contact is not None:
        from gmail_auto.contacts import TYPES

        # A client, student, partner, friend or family member: always worth a ping.
        tag = {**tag, "important": True, "kind": tag.get("kind") if tag.get("important") else "person",
               "why": tag.get("why") or f"From your {TYPES[contact['type']].lower()} {contact.get('name') or ''}".strip(),
               "label": tag.get("label") or TYPES[contact["type"]].lower()}
    if not tag.get("important"):
        return ""
    from gmail_auto.importance import ping_kinds

    if contact is None and tag.get("kind") not in ping_kinds(settings):
        return ""
    from gmail_auto import whatsapp

    summary_text = (summary or "").strip() or _summarize_notice(mail, tag)
    item = approvals.create(mail, Draft(action="skip", summary=summary_text, reason=tag["why"]), "en", role="notice", kind=tag["kind"])
    if whatsapp.ready():
        try:
            approvals.remember_whatsapp(item["id"], [whatsapp.send_notice(item)])
        except ConfigError:
            pass
    report.lines.append(f"WhatsApp: important {tag['label']} — {short_subject(mail.subject)}")
    return item["id"]


def _ping_contact(mail: Mail) -> dict | None:
    try:
        from gmail_auto.contacts import find
        from gmail_auto.importance import PING_CONTACTS

        person = find(mail.reply_email or mail.from_email)
    except ConfigError:
        return None
    return person if person and person.get("type") in PING_CONTACTS else None


def _skip(gmail, mail: Mail, decision: Skip, report: RunReport, approval_id: str = "") -> None:
    mark_processed(_record(mail, "skipped", decision.reason, "", "", decision.sensitive, skip_code=decision.code, approval_id=approval_id))
    _remember(gmail, mail.id, done=True)
    stats.note_outcome("skipped", mail, skip_code=decision.code)
    report.skipped += 1
    if decision.sensitive:
        report.lines.append("Skipped a letter that mentions passwords, codes, ID or bank details.")
    else:
        report.lines.append(f"Skipped: {short_subject(mail.subject)} ({decision.reason})")


def _fail(gmail, mail: Mail, report: RunReport, message: str) -> None:
    already = False
    has_retry = getattr(gmail, "has_retry", None)
    if has_retry is not None:
        try:
            already = bool(has_retry(mail))
        except Exception:
            already = False
    try:
        count = bump_failure(mail.key)
    except ConfigError:
        count = 1
    if already:
        count = max(count, 2)
    subject = short_subject(mail.subject)
    if count >= 2:
        mark_processed(_record(mail, "skipped", f"{message} Stopped trying.", "", "", False, skip_code="error"))
        _remember(gmail, mail.id, done=True)
        stats.note_outcome("skipped", mail, skip_code="error")
        report.skipped += 1
        report.lines.append(f"Stopped trying: {subject} ({message})")
        return
    _remember(gmail, mail.id, done=False)
    stats.note_outcome("failed", mail)
    report.failed += 1
    report.lines.append(f"Not written this time: {subject} ({message}) It will be tried once more next round.")


def _remember(gmail, message_id: str, done: bool) -> None:
    note = getattr(gmail, "note_done" if done else "note_retry", None)
    if note is None:
        return
    try:
        note(message_id)
    except Exception:
        return


def _record(
    mail: Mail,
    action: str,
    reason: str,
    reply: str,
    language: str,
    sensitive: bool,
    outbox: str = "",
    draft: Draft | None = None,
    skip_code: str = "",
    summary: str = "",
    approval_id: str = "",
) -> dict:
    return {
        "id": mail.key,
        "mail_id": mail.id,
        "provider": getattr(mail, "provider", "gmail") or "gmail",
        "account": getattr(mail, "account", ""),
        "thread_id": "" if sensitive else mail.thread_id,
        "from_name": "" if sensitive else mail.from_name,
        "from_email": "" if sensitive else mail.from_email,
        "subject": "" if sensitive else mail.subject,
        "date": mail.date,
        "action": action,
        "reason": reason,
        "skip_code": skip_code,
        "reply": "" if sensitive else reply,
        "summary": "" if sensitive else (draft.summary if draft else summary),
        "sources": [] if sensitive or draft is None else list(draft.sources or []),
        "needs_review": bool(draft and draft.action == "ask"),
        "approval_id": approval_id,
        "language": language,
        "processed_at": now_text(),
        "sensitive": sensitive,
        "outbox": "" if sensitive else outbox,
    }


def _report_dict(report: RunReport) -> dict:
    return {
        "finished_at": report.finished_at,
        "live": report.live,
        "summary": format_report(report).split("\n", 1)[0],
        "lines": report.lines,
        "error": "",
        "checked": report.checked,
        "sent": report.sent,
        "drafted": report.drafted,
        "asked": report.asked,
        "skipped": report.skipped,
        "failed": report.failed,
    }
