"""Mail Autopilot: answers your Gmail and Outlook mail from your profile and knowledge base.

Run:            python main.py
One round only: python main.py --once
"""

import argparse
import os

from flask import Flask

from gmail_auto.console import configure_stdio, say
from gmail_auto.envfile import load_env
from gmail_auto.errors import ConfigError
from gmail_auto.paths import CODE_ROOT
from gmail_auto.runner import check_interval_seconds, start_scheduler
from gmail_auto.store import is_live
from gmail_auto.web import PORT, configure_app, run_server

app = Flask(
    __name__,
    template_folder=str(CODE_ROOT / "templates"),
    static_folder=str(CODE_ROOT / "static"),
)
configure_app(app)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    try:
        return _main(argv)
    except ConfigError as exc:
        say(str(exc))
        return 1
    except KeyboardInterrupt:
        say("Stopped.")
        return 0


def _main(argv: list[str] | None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Mail Autopilot (draft only by default; nothing is sent until you turn sending on)")
    parser.add_argument("--once", action="store_true", help="check mail once and exit, without opening the web page")
    parser.add_argument("--no-check", action="store_true", help="open the web page only, without checking mail on a schedule")
    args = parser.parse_args(argv)
    if args.once:
        _connect(optional=False)
        from gmail_auto.runner import run_once

        run_once()
        return 0
    if not args.no_check:
        _connect(optional=True)
        if not os.environ.get("VERCEL"):
            start_scheduler()
    mode = "sending is ON" if is_live() else "draft only: replies are saved here and nothing is sent"
    say("Mail Autopilot is running.")
    say(f"Web page: http://127.0.0.1:{PORT}")
    say(f"Mode: {mode}")
    if args.no_check:
        say("Mail is not checked on a schedule in this run. Use Check mail now on the page. Press Ctrl+C to stop.")
    else:
        say(f"Mail is checked every {check_interval_seconds() // 60} minutes. Press Ctrl+C to stop.")
    run_server()
    return 0


def _connect(optional: bool) -> None:
    from gmail_auto.gmail_client import GmailClient
    from gmail_auto.mailboxes import connected_clients

    later = "The web page will still open. Mail is checked once a mailbox is connected."
    try:
        client = GmailClient(allow_browser=True)
        say(f"Connected to Gmail: {client.get_user_email()}")
    except ConfigError as exc:
        others, _ = connected_clients()
        if others:
            say(f"Gmail is not connected ({exc}) Using the other connected mailbox.")
            return
        if not optional:
            raise
        say(str(exc))
        say(later)
    except Exception as exc:
        text = str(exc).strip().replace("\n", " ")
        if len(text) > 300:
            text = text[:300] + "…"
        message = f"Gmail was not connected: {text}" if text else "Gmail was not connected."
        if not optional:
            raise ConfigError(message) from exc
        say(message)
        say(later)


def _utf8_stdio() -> None:
    configure_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
