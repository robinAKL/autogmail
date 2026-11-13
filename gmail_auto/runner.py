import os
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
from gmail_auto.profile_store import load_profile, profile_is_placeholder
from gmail_auto.settings import load_settings
from gmail_auto.store import (
    bump_failure,
    clear_failure,
    get_processed,
    get_state,
    is_live,
    live_from_env,
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
    with LOCK:
        return _execute(gmail, generate)


def toggle_and_run(confirmed: bool, gmail=None, generate=None) -> RunReport:
    with LOCK:
        switch_live(not is_live(), confirmed)
        return _execute(gmail, generate)


def switch_live(live: bool, confirmed: bool = False) -> None:
    """Turn real sending on or off. Turning it on needs a real profile, a key and an explicit yes."""
    if live_from_env() is not None:
        raise ConfigError("Sending is controlled by the LIVE_SEND environment variable. Change it and redeploy.")
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


def send_daily_digest(now: datetime | None = None) -> bool:
    """Once a day, after the chosen hour, tell the owner on WhatsApp how yesterday went."""
    from gmail_auto import whatsapp

    now = now or datetime.now()
    settings = load_settings()
    if not settings["daily_digest"] or not whatsapp.ready() or now.hour < settings["digest_hour"]:
        return False
    today = date.today().isoformat()
    if get_state("digest_day") == today:
        return False
    set_state("digest_day", today)
    try:
        whatsapp.send_text("Good morning. " + stats.digest_text())
    except ConfigError:
        return False
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
    for client in clients:
        try:
            already += _run_mailbox(client, generate, profile, settings, live, report)
        except ConfigError as exc:
            if len(clients) == 1:
                raise
            report.lines.append(named_error(getattr(client, "provider", "mailbox").title(), exc))
    if already and report.checked == 0:
        report.lines.append(f"{already} unread letters were already handled and were not answered again.")
    report.lines = report.lines[:40]
    try:
        send_daily_digest()
    except ConfigError:
        pass
    return report


def _run_mailbox(client, generate, profile: dict, settings: dict, live: bool, report: RunReport) -> int:
    user = client.get_user_email()
    already = 0
    for mail in client.list_unread(20):
        if not mail.account:
            mail.account = user
        if get_processed(mail.key):
            already += 1
            continue
        report.checked += 1
        stats.note_received(mail)
        decision = classify(mail, user)
        if isinstance(decision, Skip):
            _skip(client, mail, decision, report)
            continue
        if not isinstance(decision, Allow):
            continue
        try:
            from gmail_auto.calendars import maybe_create_from_mail

            added = maybe_create_from_mail(mail, user)
            if added:
                report.lines.append(f"Calendar: {added}")
        except Exception:
            pass
        try:
            draft = _as_draft(generate(mail, profile, decision.language))
        except ReplyError as exc:
            _fail(client, mail, report, str(exc))
            continue
        subject = short_subject(mail.subject)
        if draft.action == "skip":
            reason = draft.reason or "no reply needed"
            mark_processed(_record(mail, "skipped", reason, "", decision.language, False, skip_code="no_reply_needed", summary=draft.summary))
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
            elif settings["approval_mode"] == "always" or (settings["approval_mode"] == "smart" and draft.action == "ask"):
                item = approvals.create(mail, draft, decision.language)
                mark_processed(_record(mail, "waiting", draft.reason, draft.reply, decision.language, False, "", draft, approval_id=item["id"]))
                clear_failure(mail.key)
                _remember(client, mail.id, done=True)
                stats.note_outcome("asked", mail, sources=draft.sources)
                report.asked += 1
                report.lines.append(f"Waiting for your OK: {subject}" + _ask_owner(item))
            else:
                client.send_reply(mail, draft.reply, user)
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


def _skip(gmail, mail: Mail, decision: Skip, report: RunReport) -> None:
    mark_processed(_record(mail, "skipped", decision.reason, "", "", decision.sensitive, skip_code=decision.code))
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
