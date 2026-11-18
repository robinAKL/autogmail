import html
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, Response, abort, flash, redirect, request, session, url_for
from markupsafe import Markup

from gmail_auto import approvals, files, knowledge, microsoft, sharepoint, stats, whatsapp
from gmail_auto.console import say
from gmail_auto.accounts import finish_verification, gmail_status, mark_gmail_authorized, start_verification
from gmail_auto.envfile import has_openai_key, using_gateway
from gmail_auto.errors import ConfigError, NotConnected, ReplyError, named_error
from gmail_auto.llm import active_model, complete
from gmail_auto.extractors import SUPPORTED, extract_text
from gmail_auto.filters import Skip, classify
from gmail_auto.gmail_client import SCOPES, web_client_config
from gmail_auto.mailboxes import PROVIDERS, client_for, connected_clients
from gmail_auto.mailer import deliver_code
from gmail_auto.mailview import FRAME_POLICY, document
from gmail_auto.paths import CODE_ROOT, atomic_write, data_dir, root
from gmail_auto.profile_store import load_profile, profile_from_form, profile_is_placeholder, save_profile
from gmail_auto.runner import check_interval_seconds, format_report, run_once, switch_live
from gmail_auto.settings import AI_MODELS, APPROVALS, LANGUAGES, load_settings, save_settings
from gmail_auto.importance import (
    DIGEST as PING_DIGEST,
    INTERRUPT as PING_INTERRUPT,
    QUIET as PING_QUIET,
    RULES as PING_RULES,
    known_from_knowledge,
    preset_of,
)
from gmail_auto.storage import persistent
from gmail_auto.token_store import storage_hint, storage_kind, storage_ready
from gmail_auto.store import get_state, is_live, list_processed, load_last_run, load_processed, now_text, set_state
from gmail_auto.story import attach_stories, hide_story, story_board, story_detail, story_view

HOST = "127.0.0.1"
PORT = 8765
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_OPEN_PATHS = {"/whatsapp/webhook"}
_INBOX_TTL = 60
_INBOX_STORE_TTL = 15 * 60
_INBOX_PAGE = 20
_inbox_cache: dict = {"at": 0.0, "who": None, "data": None}
_FILTERS = {
    "all": "All",
    "todo": "To do",
    "notices": "Notices",
    "replied": "Replied",
    "skipped": "Skipped",
}
_DEFAULT_FILTER = "all"
# Older links and bookmarks used these tab names.
_OLD_FILTERS = {"important": "todo", "needs": "todo", "drafts": "todo", "open": "all"}
_MODES = {
    "draft": ("Write only", "Drafts are saved. Nothing is sent from your mailbox."),
    "always": ("Show me every reply", "Every reply waits for your OK on WhatsApp or Approvals."),
    "smart": ("Send the easy ones", "If the knowledge base can answer without a new promise, it sends. Money, dates, missing facts wait for you."),
    "never": ("Send whenever it can", "Every reply goes out without asking. Use this only if you fully trust the knowledge base."),
}


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(CODE_ROOT / "templates"),
        static_folder=str(CODE_ROOT / "static"),
    )
    configure_app(app)
    return app


def configure_app(app: Flask) -> None:
    hosted = bool(os.environ.get("VERCEL"))
    app.config["MAX_CONTENT_LENGTH"] = 4_400_000 if hosted else 30_000_000
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.secret_key = _secret()
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = hosted
    app.jinja_env.filters["md"] = render_markdown

    @app.before_request
    def local_only():
        if request.path in _OPEN_PATHS:
            return None
        if not _host_allowed(_request_host()):
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin", "")
            if origin and not _host_allowed(_origin_host(origin)):
                abort(403)
        return None

    @app.context_processor
    def shell():
        waiting = 0
        try:
            waiting = len(approvals.pending())
        except ConfigError:
            pass
        mode = _current_mode()
        return {
            "nav_waiting": waiting,
            "nav_mode": mode,
            "nav_mode_label": _MODES[mode][0],
            "user": session.get("user", ""),
            "hosted": bool(os.environ.get("VERCEL")),
            "ui_lang": _ui_language(),
        }

    @app.after_request
    def interface_language(response):
        """Pages are written in English; in 中文 mode each page is translated on its way out.
        The letter view (/mail/body) is someone's email and is never touched."""
        if (
            response.mimetype == "text/html"
            and response.status_code == 200
            and not response.direct_passthrough
            and not request.path.startswith(("/mail/body", "/static/"))
            and _ui_language() == "zh"
        ):
            from gmail_auto.i18n import translate_page

            response.set_data(translate_page(response.get_data(as_text=True), "zh"))
        return response

    @app.post("/language")
    def switch_language():
        blocked = _login_required()
        if blocked:
            return blocked
        wanted = "zh" if request.form.get("language") == "zh" else "en"
        try:
            save_settings({"ui_language": wanted})
        except ConfigError as exc:
            flash(str(exc), "error")
        back = request.form.get("next") or ""
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("home")
        return redirect(back)

    # ---- sign-in -------------------------------------------------------------

    @app.get("/login")
    def login_page():
        return _login_view()

    @app.post("/login/code")
    def login_code():
        mode = _mode(request.form.get("mode"))
        email = request.form.get("email", "").strip()
        if not _email_ok(email):
            return _login_view(mode=mode, email=email, error="Enter your email address."), 400
        try:
            code, otp = start_verification(email, mode, session.get("otp"))
            try:
                deliver_code(email, code)
            except ConfigError:
                session.pop("otp", None)
                raise
        except ConfigError as exc:
            return _login_view(mode=mode, email=email, error=str(exc)), 400
        session["otp"] = otp
        session["pending_email"] = email.strip().lower()
        session["pending_mode"] = mode
        return redirect(url_for("login_page", mode=mode, sent=1))

    @app.post("/login")
    def login_submit():
        mode = _mode(request.form.get("mode") or session.get("pending_mode"))
        email = (request.form.get("email") or session.get("pending_email") or "").strip()
        code = request.form.get("code", "").strip()
        if not _email_ok(email):
            return _login_view(mode=mode, email=email, error="Enter your email address."), 400
        if not code:
            return _login_view(mode=mode, email=email, sent=True, error="Enter the code from the email."), 400
        otp = dict(session.get("otp") or {})
        try:
            user = finish_verification(email, mode, code, otp)
        except ConfigError as exc:
            session["otp"] = otp
            return _login_view(mode=mode, email=email, sent=True, error=str(exc)), 400
        session["user"] = user
        session.pop("otp", None)
        session.pop("pending_email", None)
        session.pop("pending_mode", None)
        flash("You are signed in.")
        return redirect(url_for("home"))

    @app.get("/privacy")
    def privacy_page():
        """Public on purpose: Meta and Microsoft ask for a privacy policy address."""
        return _render(
            "privacy.html",
            app_name="Mail Autopilot",
            owner=os.getenv("PRIVACY_OWNER", "").strip(),
            contact=os.getenv("PRIVACY_CONTACT", "").strip(),
            updated="12 November 2026",
        )

    @app.get("/logout")
    def logout():
        session.pop("user", None)
        return redirect(url_for("login_page"))

    # ---- inbox ---------------------------------------------------------------

    @app.get("/")
    def home():
        blocked = _login_required()
        if blocked:
            return blocked
        return _render("index.html", **_home_context())

    @app.get("/inbox/refresh")
    def inbox_refresh():
        blocked = _login_required()
        if blocked:
            return blocked
        inbox = _inbox(live=True)
        if "application/json" in (request.headers.get("Accept") or ""):
            return {
                "ok": True,
                "count": len(inbox.get("rows") or []),
                "errors": inbox.get("errors") or [],
            }
        return redirect(url_for("home", **_inbox_query()))

    @app.post("/inbox/story/hide")
    def story_hide():
        blocked = _login_required()
        if blocked:
            return blocked
        hide_story(request.form.get("story", ""))
        nxt = request.form.get("next") or url_for("home")
        if not nxt.startswith("/"):
            nxt = url_for("home")
        return redirect(nxt)

    @app.get("/mail/body")
    def mail_body():
        blocked = _login_required()
        if blocked:
            return blocked
        mail = _find_mail(request.args.get("key", ""))
        if mail is None:
            page = document("", "This letter could not be loaded right now.")
        else:
            page = document(mail.html, mail.body)
        response = Response(page, mimetype="text/html")
        response.headers["Content-Security-Policy"] = FRAME_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "private, max-age=120"
        return response

    @app.post("/mail/reply")
    def mail_reply():
        """Write a draft reply to the open letter. It waits in To do until it is sent or dropped."""
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto.reply_now import draft_now

        key = request.form.get("key", "")
        mail = _find_mail(key)
        try:
            if mail is None:
                raise ConfigError("This letter could not be loaded right now. Try again in a moment.")
            draft_now(mail, request.form.get("points", ""), load_profile())
            flash("Draft ready. Check it, change anything you like, then send.")
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
            show = _OLD_FILTERS.get(request.form.get("show", ""), request.form.get("show", ""))
            _forget_inbox()
            return redirect(url_for("home", show=show if show in _FILTERS else None, open=key or None))
        _forget_inbox()
        return redirect(url_for("home", show="todo", open=key))

    @app.post("/mail/handled")
    def mail_handled():
        """Mark a letter as dealt with (a bill paid, a notice read), or undo it."""
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto.store import get_processed, set_handled

        key = request.form.get("key", "")
        handled = request.form.get("handled", "1") == "1"
        show = _OLD_FILTERS.get(request.form.get("show", ""), request.form.get("show", ""))
        try:
            record = get_processed(key) if key else None
            base = None
            if record is None:
                mail = _find_mail(key)
                if mail is None:
                    raise ConfigError("This letter could not be loaded right now. Try again in a moment.")
                from gmail_auto.filters import detect_language
                from gmail_auto.runner import _record

                base = _record(mail, "skipped", "Marked as handled by you", "", detect_language(f"{mail.subject}\n{mail.body}"), False, skip_code="notification")
            set_handled(key, handled, base)
            approval_id = (record or {}).get("approval_id")
            item = approvals.get(approval_id) if approval_id else None
            if handled and item and item.get("role") == "notice" and item.get("status") == "pending":
                approvals.reject(approval_id, "web")
            flash("Marked as handled. It is out of Notices and To do; find it under All." if handled else "Marked as not handled yet.")
        except ConfigError as exc:
            flash(str(exc), "error")
        _forget_inbox()
        return redirect(url_for("home", show=show if show in _FILTERS else None, open=key or None))

    @app.post("/run")
    def run_now():
        blocked = _login_required()
        if blocked:
            return blocked
        _run(lambda: run_once())
        _forget_inbox()
        return redirect(url_for("home"))

    @app.post("/mode")
    def set_mode():
        blocked = _login_required()
        if blocked:
            return blocked
        mode = request.form.get("mode", "")
        if mode not in _MODES:
            abort(400)
        try:
            if mode == "draft":
                switch_live(False)
            else:
                switch_live(True, request.form.get("confirm") == "1")
                save_settings({"approval_mode": mode})
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("profile_page") + "#send")
        flash(f"Sending mode is now: {_MODES[mode][0]}.")
        return redirect(url_for("profile_page") + "#send")

    # ---- approvals -----------------------------------------------------------

    @app.get("/approvals")
    def approvals_page():
        blocked = _login_required()
        if blocked:
            return blocked
        waiting = approvals.pending()
        return _render(
            "approvals.html",
            waiting=waiting,
            decided=approvals.recent(15),
            whatsapp_ready=whatsapp.ready(),
            **_attach_context(bool(waiting)),
        )

    @app.post("/approvals/<item_id>/<action>")
    def approvals_act(item_id, action):
        blocked = _login_required()
        if blocked:
            return blocked
        target = request.form.get("next") or ""
        if not target.startswith("/") or target.startswith("//") or "\\" in target:
            target = url_for("approvals_page")
        back = redirect(target)
        try:
            if action in ("send", "save", "attach", "detach") and request.form.get("to") is not None:
                from gmail_auto.outgoing import clean_recipients

                approvals.set_new_fields(item_id, clean_recipients(request.form.get("to", "")), request.form.get("subject", ""))
            if action in ("send", "attach", "detach") and request.form.get("draft") is not None:
                approvals.set_draft(item_id, request.form.get("draft"))
            if action == "send":
                _attach_to(item_id)
                item = approvals.approve(item_id, "web")
                flash(f"Reply sent to {item.get('from_name') or item.get('from_email')}.")
            elif action == "save":
                approvals.set_draft(item_id, request.form.get("draft", ""))
                _attach_to(item_id)
                flash("Draft saved. It is still waiting for your OK.")
            elif action == "rewrite":
                instruction = request.form.get("instruction", "").strip()
                if not instruction:
                    raise ConfigError("Say what to change first.")
                approvals.revise(item_id, instruction, load_profile())
                flash("The draft was rewritten. Check it, then send.")
            elif action == "reject":
                approvals.reject(item_id, "web")
                flash("Nothing was sent. The email stays in your inbox.")
            elif action == "attach":
                names = _attach_to(item_id)
                if names:
                    flash(f"Attached {', '.join(names)}. Check the reply, then send.")
                else:
                    flash("Choose a file to attach first.", "error")
            elif action == "detach":
                file_id = request.form.get("file", "")
                approvals.detach(item_id, file_id)
                record = files.get(file_id)
                if record and record.get("origin") == "reply":
                    files.remove(file_id)
                flash("Attachment removed.")
            else:
                abort(404)
        except ConfigError as exc:
            flash(str(exc), "error")
        _forget_inbox()
        return back

    # ---- documents -----------------------------------------------------------

    @app.get("/documents")
    def documents_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts, invoices

        tab = request.args.get("tab", "files")
        if tab not in ("files", "templates", "invoices", "quotes", "contracts"):
            tab = "files"
        docs = knowledge.list_documents()
        sendable = [doc for doc in docs if knowledge.role_of(doc) in knowledge.SEND_ROLES]
        bills = invoices.list_invoices("invoice")
        quotes = invoices.list_invoices("quote")
        return _render(
            "documents.html",
            tab=tab,
            sendable=sendable,
            roles=knowledge.ROLES,
            send_roles=knowledge.SEND_ROLES,
            role_of=knowledge.role_of,
            invoice_templates=knowledge.templates("invoice_template"),
            contract_templates=knowledge.templates("contract_template"),
            details=invoices.load_template(),
            ready=invoices.template_ready(),
            bills=bills,
            quotes=quotes,
            drafts=contracts.list_drafts(),
            sums={item["id"]: invoices.totals(item) for item in bills + quotes},
            money=invoices.money,
            files_ready=files.ready(),
            file_types=", ".join(sorted(item.lstrip(".") for item in SUPPORTED)),
            human_size=files.human_size,
        )

    @app.get("/documents/file/<doc_id>")
    def document_file(doc_id):
        blocked = _login_required()
        if blocked:
            return blocked
        doc = next((item for item in knowledge.attachable_documents() if item["id"] == doc_id), None)
        record = files.get(((doc or {}).get("meta") or {}).get("file_id") or "") if doc else None
        if record is None:
            abort(404)
        response = Response(files.read(record["id"]), mimetype=record.get("mime") or "application/octet-stream")
        safe = re.sub(r'[^A-Za-z0-9._ -]', "_", record.get("name") or "file")
        response.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    # ---- contacts ------------------------------------------------------------

    @app.get("/contacts")
    def contacts_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contacts

        everyone = contacts.all_contacts()
        kind = request.args.get("type", "")
        query = " ".join(request.args.get("q", "").split()).lower()
        shown = everyone
        if kind == "unsorted":
            shown = [item for item in shown if not item.get("type")]
        elif kind == "suggested":
            shown = [item for item in shown if item.get("type_status") == "suggested"]
        elif kind in contacts.TYPES:
            shown = [item for item in shown if item.get("type") == kind]
        if query:
            shown = [
                item
                for item in shown
                if query in " ".join([item.get("name") or "", item.get("company") or "", " ".join(item.get("emails") or [])]).lower()
            ]
        counts = {key: sum(1 for item in everyone if item.get("type") == key) for key in contacts.TYPES}
        counts["unsorted"] = sum(1 for item in everyone if not item.get("type"))
        counts["suggested"] = sum(1 for item in everyone if item.get("type_status") == "suggested")
        return _render(
            "contacts.html",
            shown=shown[:300],
            total=len(everyone),
            counts=counts,
            types=contacts.TYPES,
            kind=kind,
            query=request.args.get("q", ""),
        )

    @app.post("/contacts/scan")
    def contacts_scan():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contacts
        from gmail_auto.llm import model_ready

        sent = []
        own = {chip.get("email", "") for chip in _account_chips()}
        try:
            clients, _ = connected_clients()
        except Exception:
            clients = []
        for client in clients:
            try:
                own.add(client.get_user_email())
                sent.extend(client.list_sent(200))
            except Exception:
                continue
        try:
            result = contacts.build(list_processed(3000), sent, own)
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("contacts_page"))
        try:
            sorted_now = contacts.sort_pending()
        except (ConfigError, ReplyError) as exc:
            sorted_now = 0
            flash(str(exc), "error")
        left = sum(1 for item in contacts.all_contacts() if not item.get("type"))
        message = f"{result['added']} new and {result['updated']} updated from your mail; {result['total']} contacts in all."
        if sorted_now:
            message += f" Sorted {sorted_now}: check the suggested types."
        if left and model_ready():
            message += f" {left} not sorted yet: press Scan again to sort more."
        elif left:
            message += " Without an AI model, set each type yourself."
        flash(message)
        return redirect(url_for("contacts_page", type="suggested" if sorted_now else None))

    @app.get("/contacts/<contact_id>")
    def contact_page(contact_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contacts, invoices

        contact = contacts.get(contact_id)
        if contact is None:
            abort(404)
        emails = set(contact.get("emails") or [])
        billed = [item for item in invoices.list_invoices() if (item.get("client_email") or "").lower() in emails]
        return _render(
            "contact.html",
            contact=contact,
            types=contacts.TYPES,
            tones=contacts.TONES,
            languages=contacts.LANGUAGES,
            letters=contacts.history(contact),
            billed=billed,
            sums={item["id"]: invoices.totals(item) for item in billed},
            money=invoices.money,
            always_ask=contacts.ALWAYS_ASK,
        )

    @app.post("/contacts/<contact_id>/<action>")
    def contact_act(contact_id, action):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contacts

        try:
            if action == "save":
                values = {key: request.form.get(key, "") for key in contacts.EDITABLE if key in request.form}
                if "emails" in request.form:
                    values["emails"] = request.form.get("emails", "")
                contacts.save_edit(contact_id, values)
                flash("Contact saved. Your changes are kept even when contacts are scanned again.")
            elif action == "confirm":
                contacts.confirm_type(contact_id)
                flash("Type confirmed.")
                nxt = request.form.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):
                    return redirect(nxt)
            elif action == "hide":
                contacts.hide(contact_id)
                flash("Removed from contacts. It will not come back when mail is scanned.")
                return redirect(url_for("contacts_page"))
            else:
                abort(404)
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("contact_page", contact_id=contact_id))

    # ---- writing with the assistant ------------------------------------------

    @app.get("/write")
    def write_start():
        """A new writing session, optionally with someone to write to and files already attached."""
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import writer

        refs = [f"doc:{doc_id}" for doc_id in request.args.getlist("doc") if doc_id]
        if request.args.get("invoice"):
            refs.append(f"invoice:{request.args['invoice']}")
        if request.args.get("contract"):
            refs.append(f"contract:{request.args['contract']}")
        to = request.args.get("to", "")
        if not to and request.args.get("invoice"):
            from gmail_auto import invoices

            to = (invoices.get(request.args["invoice"]) or {}).get("client_email") or ""
        try:
            session = writer.start(to=to, subject=request.args.get("subject", ""), refs=refs)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("compose_page"))
        return redirect(url_for("write_page", session_id=session["id"]))

    @app.get("/write/<session_id>")
    def write_page(session_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import writer
        from gmail_auto.llm import model_ready

        session = writer.get(session_id)
        if session is None:
            flash("That draft is no longer here. Start a new email.", "error")
            return redirect(url_for("write_start"))
        from gmail_auto.writer import LANGUAGES

        return _render(
            "write.html",
            languages=LANGUAGES,
            session=session,
            people=_people_for_compose(),
            has_model=model_ready(),
            files_ready=files.ready(),
        )

    @app.post("/write/<session_id>/<action>")
    def write_act(session_id, action):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import writer

        back = redirect(url_for("write_page", session_id=session_id) + "#end")
        try:
            if action == "say":
                writer.say(session_id, request.form.get("text", ""), load_profile())
            elif action in ("edit", "language"):
                before = (writer.get(session_id) or {}).get("language")
                session = writer.edit(
                    session_id,
                    to=request.form.get("to"),
                    subject=request.form.get("subject"),
                    body=request.form.get("body"),
                    detach=request.form.get("detach", ""),
                    language=request.form.get("language"),
                )
                if session.get("language") != before and session["body"].strip():
                    writer.relanguage(session_id, load_profile())
            elif action == "queue":
                writer.edit(session_id, to=request.form.get("to"), subject=request.form.get("subject"), body=request.form.get("body"))
                item = writer.queue(session_id)
                flash(f"The email to {item['to']} is waiting in Approvals. Send it from there, or on WhatsApp.")
                return redirect(url_for("approvals_page") + f"#a{item['id']}")
            elif action == "send":
                writer.edit(session_id, to=request.form.get("to"), subject=request.form.get("subject"), body=request.form.get("body"))
                item = writer.send(session_id)
                flash(f"Sent to {item['to']}.")
                return redirect(url_for("home"))
            else:
                abort(404)
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
        return back

    # ---- new emails ----------------------------------------------------------

    @app.get("/compose")
    def compose_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts, invoices
        from gmail_auto.llm import model_ready

        issued = [item for item in invoices.list_invoices() if item.get("status") == "issued"]
        ready = [item for item in contracts.list_drafts() if not contracts.empty_fields(item)]
        chosen_invoice = invoices.get(request.args.get("invoice", ""))
        return _render(
            "compose.html",
            to=request.args.get("to") or (chosen_invoice or {}).get("client_email") or "",
            subject=request.args.get("subject", ""),
            points=request.args.get("points", ""),
            docs=knowledge.attachable_documents(),
            people=_people_for_compose(),
            issued=issued,
            ready_contracts=ready,
            chosen_invoice=request.args.get("invoice", ""),
            chosen_docs=request.args.getlist("doc"),
            roles=knowledge.ROLES,
            role_of=knowledge.role_of,
            chosen_contract=request.args.get("contract", ""),
            files_ready=files.ready(),
            has_model=model_ready(),
            money=invoices.money,
            sums={item["id"]: invoices.totals(item) for item in issued},
        )

    @app.post("/compose")
    def compose_send():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts, invoices, outgoing

        uploaded = []
        try:
            to = outgoing.clean_recipients(request.form.get("to", ""))
            records = [outgoing.knowledge_file(doc_id) for doc_id in request.form.getlist("doc") if doc_id]
            invoice = invoices.get(request.form.get("invoice", ""))
            if invoice is not None:
                records.append(outgoing.invoice_file(invoice))
            contract = contracts.get(request.form.get("contract", ""))
            if contract is not None:
                records.append(outgoing.contract_file(contract))
            for upload in request.files.getlist("files"):
                if upload and upload.filename:
                    record = files.save(upload.read(), upload.filename, upload.mimetype, origin="reply")
                    uploaded.append(record)
                    records.append(record)
            item = outgoing.start(to, request.form.get("points", ""), request.form.get("subject", ""), load_profile(), records)
        except (ConfigError, ReplyError) as exc:
            for record in uploaded:
                files.remove(record["id"])
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "compose_page",
                    to=request.form.get("to", ""),
                    subject=request.form.get("subject", ""),
                    points=request.form.get("points", "")[:1500] or None,
                    invoice=request.form.get("invoice") or None,
                    contract=request.form.get("contract") or None,
                )
            )
        flash(f"Email to {item['to']} is written. Check it, then send.")
        return redirect(url_for("approvals_page") + f"#a{item['id']}")

    # ---- invoices ------------------------------------------------------------

    @app.get("/invoices")
    def invoices_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        listed = invoices.list_invoices()
        return _render(
            "invoices.html",
            details=invoices.load_template(),
            ready=invoices.template_ready(),
            listed=listed,
            sums={item["id"]: invoices.totals(item) for item in listed},
            money=invoices.money,
            file_types=", ".join(sorted(item.lstrip(".") for item in SUPPORTED)),
        )

    @app.post("/invoices/template")
    def invoices_template():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        values = {key: request.form.get(key, "") for key in invoices.TEMPLATE_FIELDS}
        for key in ("gst_registered", "prices_include_gst"):
            values[key] = request.form.get(key) == "1"
        invoices.save_template(values)
        flash("Invoice details saved.")
        return redirect(url_for("invoices_page") + "#template")

    @app.post("/invoices/template/import")
    def invoices_template_import():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices
        from gmail_auto.llm import model_ready

        upload = request.files.get("file")
        try:
            if not upload or not upload.filename:
                raise ConfigError("Choose an old invoice first (PDF or Word).")
            if not model_ready():
                raise ConfigError("Reading an old invoice needs an AI model. Fill in the details by hand instead.")
            name = os.path.basename(upload.filename.replace("\\", "/"))
            found = invoices.template_from_text(extract_text(name, upload.read()))
            if not found:
                raise ConfigError("No business details were found in that file.")
            invoices.save_template(found)
            flash(f"Filled in {len(found)} details from {name}. Check them below and fix anything that is wrong.")
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("invoices_page") + "#template")

    @app.post("/invoices/new")
    def invoices_new():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        key = request.form.get("key", "")
        mail = _find_mail(key) if key else None
        if key and mail is None:
            flash("This letter could not be loaded right now. Try again in a moment.", "error")
            return redirect(url_for("home", open=key))
        kind = "quote" if request.form.get("kind") == "quote" else "invoice"
        wanted = request.form.get("request", "").strip()
        if mail is not None and not wanted and kind == "quote":
            wanted = "Make a quote for what this letter asks for."
        fields = invoices.fill_from(wanted, mail) if mail is not None or wanted else {}
        invoice = invoices.create(fields, mail_key=mail.key if mail is not None else "", kind=kind)
        return redirect(url_for("invoice_page", invoice_id=invoice["id"]))

    @app.get("/invoices/<invoice_id>")
    def invoice_page(invoice_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        invoice = invoices.get(invoice_id)
        if invoice is None:
            abort(404)
        template = invoice.get("template") or invoices.load_template()
        blank = max(0, 3 - len(invoice.get("items") or [])) + 1 if invoice["status"] == "draft" else 0
        return _render(
            "invoice.html",
            invoice=invoice,
            details=template,
            sums=invoices.totals(invoice, template),
            money=invoices.money,
            blank_rows=blank,
            problems=invoices.missing_for_issue(invoice, template) if invoice["status"] == "draft" else [],
            files_ready=files.ready(),
        )

    @app.post("/invoices/<invoice_id>/<action>")
    def invoice_act(invoice_id, action):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        back = redirect(url_for("invoice_page", invoice_id=invoice_id))
        try:
            if action in ("save", "issue", "issue_attach"):
                if (invoices.get(invoice_id) or {}).get("status") == "draft":
                    invoices.update(invoice_id, _invoice_form())
            word = invoices.KINDS[invoices.kind_of(invoices.get(invoice_id) or {})]
            if action == "save":
                flash(f"{word} saved.")
            elif action in ("issue", "issue_attach"):
                invoice = invoices.issue(invoice_id)
                if action == "issue_attach":
                    return _attach_invoice(invoice)
                flash(f"{word} {invoice['number']} issued. Download it, or attach it to a reply.")
            elif action == "attach":
                invoice = invoices.get(invoice_id)
                if invoice is None or invoice.get("status") != "issued":
                    raise ConfigError(f"Issue the {word.lower()} first.")
                return _attach_invoice(invoice)
            elif action == "to_invoice":
                made = invoices.quote_to_invoice(invoice_id)
                flash("Draft invoice made from the quote. Check it, then issue it.")
                return redirect(url_for("invoice_page", invoice_id=made["id"]))
            elif action == "delete":
                invoices.delete(invoice_id)
                flash(f"Draft {word.lower()} deleted.")
                return redirect(url_for("documents_page", tab="quotes" if word == "Quote" else "invoices"))
            else:
                abort(404)
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
        return back

    @app.get("/invoices/<invoice_id>/pdf")
    def invoice_pdf(invoice_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import invoices

        invoice = invoices.get(invoice_id)
        if invoice is None:
            abort(404)
        response = Response(invoices.render_pdf(invoice), mimetype="application/pdf")
        disposition = "attachment" if request.args.get("download") == "1" else "inline"
        response.headers["Content-Disposition"] = f'{disposition}; filename="{invoices.file_name(invoice)}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    # ---- dashboard -----------------------------------------------------------

    @app.get("/dashboard")
    def dashboard_page():
        blocked = _login_required()
        if blocked:
            return blocked
        span = request.args.get("span", "30")
        span = int(span) if span in {"7", "30", "90"} else 30
        try:
            stats.rebuild_from_records(list_processed(5000))
            data = stats.dashboard(span)
        except ConfigError as exc:
            flash(str(exc), "error")
            data = stats.dashboard(span)
        return _render(
            "dashboard.html",
            data=data,
            chart=_trend_chart(data["series"]),
            hours=_hours_chart(data["hours"]),
            waiting=approvals.pending(),
            kb=knowledge.overview(),
        )

    @app.get("/calendar")
    def calendar_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import calendars

        data = {"weeks": [], "accounts": [], "errors": [], "weekdays": [], "title": "Calendar"}
        try:
            data = calendars.month_board(
                request.args.get("year", type=int),
                request.args.get("month", type=int),
                request.args.get("day") or "",
            )
        except ConfigError as exc:
            data["errors"] = [str(exc)]
        errors = list(data.get("errors") or [])
        need_reconnect = any("connect" in (item or "").lower() or "permission" in (item or "").lower() or "grant" in (item or "").lower() for item in errors)
        return _render(
            "calendar.html",
            board=data,
            accounts=data.get("accounts") or [],
            errors=errors,
            need_reconnect=need_reconnect,
            auto_calendar=load_settings().get("auto_calendar", True),
            added_from_mail=calendars.added_from_mail(),
        )

    @app.post("/calendar/remove-added")
    def calendar_remove_added():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import calendars

        try:
            result = calendars.remove_added_from_mail()
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("calendar_page") + "#auto")
        if result["removed"]:
            more = f" {result['left']} still to go — press the button again." if result["left"] else ""
            flash(f"Removed {result['removed']} calendar items that were added from email.{more}")
        if result["failed"]:
            flash(f"{result['failed']} could not be removed: {result['error']}", "error")
        if not result["removed"] and not result["failed"]:
            flash("Nothing added from email is left to remove.")
        return redirect(url_for("calendar_page") + "#auto")

    @app.post("/calendar/settings")
    def calendar_settings_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            save_settings({"auto_calendar": request.form.get("auto_calendar") == "1"})
            flash("Calendar setting saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("calendar_page"))

    @app.post("/calendar/from-mail")
    def calendar_from_mail():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import calendars

        key = request.form.get("key") or request.args.get("key") or ""
        mail = _find_mail(key)
        next_url = request.form.get("next") or url_for("calendar_page")
        if mail is None:
            flash("That letter could not be opened.", "error")
            return redirect(next_url)
        try:
            event = calendars.create_from_mail(mail, mail.account or "")
            flash(f"Added to Calendar: {event.get('title') or mail.subject}.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(next_url)

    # ---- knowledge -----------------------------------------------------------

    @app.get("/knowledge")
    def knowledge_page():
        blocked = _login_required()
        if blocked:
            return blocked
        index = knowledge.load_index()
        query = request.args.get("q", "").strip()
        kind = request.args.get("kind", "")
        view = request.args.get("view", "memory")
        if kind not in knowledge.KINDS:
            kind = ""
        if view not in ("memory", "timeline"):
            view = "memory"
        mailboxes = []
        for provider, label in PROVIDERS.items():
            if _provider_connected(provider):
                mailboxes.append({"provider": provider, "label": label})
        all_items = index["model"]["items"]
        groups = []
        for name, meta in knowledge.KINDS.items():
            chunk = [item for item in all_items if item["kind"] == name]
            live = [item for item in chunk if knowledge.is_active(item)]
            past = [item for item in chunk if not knowledge.is_active(item)]
            groups.append({"kind": name, "cards": live, "past": past, "count": len(live), **meta})
        return _render(
            "knowledge.html",
            docs=knowledge.list_documents(),
            sources=knowledge.SOURCES,
            kinds=knowledge.KINDS,
            groups=groups,
            tiles=knowledge.memory_tiles(all_items),
            days=knowledge.timeline(all_items),
            graph=knowledge.graph_view(all_items),
            brief=index["brief"],
            rules_text="\n".join(index["rules"]),
            overview=knowledge.overview(),
            sharepoint=microsoft.status("sharepoint"),
            sharepoint_sources=sharepoint.list_sources(),
            mailboxes=mailboxes,
            query=query,
            kind=kind,
            view=view,
            hits=knowledge.search(query, 5) if query else [],
            has_key=has_openai_key(),
            editing=request.args.get("edit") == "brief",
            edit_item=request.args.get("item", ""),
            file_types=", ".join(sorted(item.lstrip(".") for item in SUPPORTED)),
            upload_limit="4 MB" if os.environ.get("VERCEL") else "30 MB",
            upload_max_bytes=3_800_000 if os.environ.get("VERCEL") else 28_000_000,
            roles=knowledge.ROLES,
            role_of=knowledge.role_of,
        )

    @app.get("/knowledge/doc/<doc_id>")
    def knowledge_doc(doc_id):
        blocked = _login_required()
        if blocked:
            return blocked
        doc = knowledge.get_document(doc_id)
        if doc is None:
            abort(404)
        return _render("document.html", doc=doc, source=knowledge.SOURCES.get(doc["source"], doc["source"]))

    @app.post("/knowledge/upload")
    def knowledge_upload():
        blocked = _login_required()
        if blocked:
            return blocked
        added = 0
        problems = []
        for item in request.files.getlist("files"):
            if not item or not item.filename:
                continue
            name = os.path.basename(item.filename.replace("\\", "/"))
            kept = None
            try:
                raw = item.read()
                text = extract_text(name, raw)
                kept = _keep_original(raw, name, item.mimetype)
                entry = knowledge.add_document(
                    name,
                    text,
                    "upload",
                    origin=name,
                    meta={"file_id": kept["id"], "size": kept["size"]} if kept else None,
                    summarize=False,
                )
                added += 1
                chosen = request.form.get("role", "")
                note = _apply_role(entry, chosen if chosen in knowledge.ROLES else knowledge.classify(name, text), text)
                if note:
                    flash(note)
            except ConfigError as exc:
                if kept:
                    files.remove(kept["id"])
                problems.append(str(exc))
                flash(str(exc), "error")
        if added:
            flash(f"Added {added} file{'s' if added != 1 else ''} to the knowledge base.")
        elif not request.files.getlist("files"):
            problems.append("Choose at least one file.")
            flash("Choose at least one file.", "error")
        if "application/json" in (request.headers.get("Accept") or ""):
            if added:
                return {"ok": True, "added": added, "errors": problems}
            return {"ok": False, "error": problems[0] if problems else "Choose at least one file."}, 400
        back = request.form.get("next", "")
        if back.startswith("/documents"):
            return redirect(back)
        return redirect(url_for("knowledge_page") + "#documents")

    @app.post("/knowledge/note")
    def knowledge_note():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            knowledge.add_document(request.form.get("title", ""), request.form.get("text", ""), "note")
            flash("Note added to the knowledge base.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page") + "#documents")

    @app.post("/knowledge/learn")
    def knowledge_learn():
        blocked = _login_required()
        if blocked:
            return blocked
        provider = request.form.get("provider", "gmail")
        if provider not in PROVIDERS:
            abort(400)
        try:
            entry = knowledge.learn_from_mailbox(client_for(provider), limit=12 if provider == "outlook" else 30)
            flash(f"Read {entry['meta']['letters']} letters from {entry['meta']['account']} and saved what they show.")
        except ConfigError as exc:
            flash(str(exc), "error")
        except Exception:
            flash("Could not summarise that mailbox right now. Outlook was too slow or returned nothing readable.", "error")
        return redirect(url_for("knowledge_page") + "#documents")

    @app.post("/knowledge/sharepoint")
    def knowledge_sharepoint():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            if request.form.get("remove"):
                sharepoint.remove_source(request.form.get("url", ""))
                flash("That SharePoint source and its documents were removed.")
            else:
                result = sharepoint.sync(request.form.get("url", ""))
                message = f"Read {result['files']} document{'s' if result['files'] != 1 else ''} from \"{result['title']}\"."
                if result["more"]:
                    message += f" {result['more']} more were left out (limit {sharepoint.MAX_FILES} per link)."
                flash(message)
                for line in result["skipped"][:3]:
                    flash(line, "error")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page") + "#sharepoint")

    @app.post("/knowledge/doc/<doc_id>/delete")
    def knowledge_delete(doc_id):
        blocked = _login_required()
        if blocked:
            return blocked
        if knowledge.delete_document(doc_id):
            flash("Document removed from the knowledge base.")
        return redirect(url_for("knowledge_page") + "#documents")

    @app.post("/knowledge/doc/<doc_id>/role")
    def knowledge_role(doc_id):
        blocked = _login_required()
        if blocked:
            return blocked
        doc = knowledge.get_document(doc_id)
        role = request.form.get("role", "")
        if doc is None or role not in knowledge.ROLES:
            abort(404)
        try:
            note = _apply_role(doc, role, doc.get("text") or "")
            flash(note or f"{doc['title']} is now used as {knowledge.ROLES[role].lower()}.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page") + "#documents")

    # ---- contracts -----------------------------------------------------------

    @app.get("/contracts")
    def contracts_page():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts

        return _render("contracts.html", templates=knowledge.templates("contract_template"), drafts=contracts.list_drafts())

    @app.post("/contracts/new")
    def contracts_new():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts

        found = knowledge.templates("contract_template")
        template = next((item for item in found if item["id"] == request.form.get("template", "")), found[0] if found else None)
        key = request.form.get("key", "")
        if template is None:
            flash("Upload a contract you use (Word, PowerPoint, Excel or PDF) to the knowledge base first.", "error")
            return redirect(url_for("knowledge_page") + "#documents")
        if not (template.get("meta") or {}).get("fields"):
            flash(f"No parts to fill in were found in {template['title']}. Mark them like {{{{Client name}}}} and upload it again.", "error")
            return redirect(url_for("contracts_page"))
        mail = _find_mail(key) if key else None
        if key and mail is None:
            flash("This letter could not be loaded right now. Try again in a moment.", "error")
            return redirect(url_for("home", open=key))
        wanted = request.form.get("request", "").strip()
        values, missing = contracts.fill_from(template, wanted, mail) if mail is not None or wanted else ({}, [])
        contract = contracts.create(template, values, missing, mail_key=mail.key if mail is not None else "")
        return redirect(url_for("contract_page", contract_id=contract["id"]))

    @app.get("/contracts/<contract_id>")
    def contract_page(contract_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts

        contract = contracts.get(contract_id)
        if contract is None:
            abort(404)
        return _render("contract.html", contract=contract, empty=contracts.empty_fields(contract), files_ready=files.ready())

    @app.post("/contracts/<contract_id>/<action>")
    def contract_act(contract_id, action):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts

        contract = contracts.get(contract_id)
        if contract is None:
            abort(404)
        try:
            if action in ("save", "attach"):
                contract = contracts.update(contract_id, {field["name"]: request.form.get(f"field_{index}", "") for index, field in enumerate(contract["fields"])})
            if action == "save":
                flash("Contract saved.")
            elif action == "attach":
                return _attach_contract(contract)
            elif action == "delete":
                contracts.delete(contract_id)
                flash("Contract draft deleted.")
                return redirect(url_for("contracts_page"))
            else:
                abort(404)
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("contract_page", contract_id=contract_id))

    @app.get("/contracts/<contract_id>/docx")
    def contract_docx(contract_id):
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto import contracts

        contract = contracts.get(contract_id)
        if contract is None:
            abort(404)
        try:
            data, mime, name = contracts.render(contract)
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("contract_page", contract_id=contract_id))
        response = Response(data, mimetype=mime)
        response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.post("/knowledge/rules")
    def knowledge_rules():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            saved = knowledge.save_rules(request.form.get("rules", ""))
            flash(f"Saved {len(saved)} rule{'s' if len(saved) != 1 else ''}.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page") + "#rules")

    @app.post("/knowledge/brief")
    def knowledge_brief():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            if request.form.get("rebuild"):
                knowledge.rebuild_brief(load_profile())
                flash("Knowledge was reorganised from your profile, rules and documents.")
            else:
                knowledge.save_brief(request.form.get("text", ""))
                flash("Your edits to the understanding were saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page") + "#map")

    @app.post("/knowledge/item")
    def knowledge_item_add():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            knowledge.add_item(request.form.get("kind", "rule"), request.form.get("text", ""))
            flash("Added to the knowledge map.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page", kind=request.form.get("kind", "")) + "#cards")

    @app.post("/knowledge/item/<item_id>")
    def knowledge_item_save(item_id):
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            if request.form.get("delete"):
                knowledge.delete_item(item_id)
                flash("Removed from the knowledge map.")
            elif request.form.get("end"):
                knowledge.end_item(item_id)
                flash("Marked as no longer current. It stays on the timeline.")
            else:
                knowledge.update_item(item_id, request.form.get("text", ""))
                flash("That item was updated.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("knowledge_page", kind=request.form.get("kind", ""), view=request.form.get("view", "memory")) + "#cards")

    # ---- connections ---------------------------------------------------------

    @app.get("/connections")
    def connections_page():
        blocked = _login_required()
        if blocked:
            return blocked
        return _render("connections.html", **_connections_context())

    @app.get("/oauth")
    def oauth_start():
        blocked = _login_required()
        if blocked:
            return blocked
        config = web_client_config()
        if config is None:
            if os.environ.get("VERCEL"):
                flash("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in Vercel first.", "error")
            else:
                flash(
                    "On this computer Gmail connects when the program starts: put credentials.json in the folder and "
                    "restart. To connect from this page instead, set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET.",
                    "error",
                )
            return redirect(url_for("connections_page"))
        if os.environ.get("VERCEL") and not os.getenv("FLASK_SECRET_KEY", "").strip():
            flash("Set FLASK_SECRET_KEY in Vercel before connecting Gmail.", "error")
            return redirect(url_for("connections_page"))
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(config, SCOPES)
        flow.redirect_uri = _oauth_redirect()
        hint = session.get("user") or ""
        options = {"access_type": "offline", "prompt": "consent"}
        if hint:
            options["login_hint"] = hint
        auth_url, state = flow.authorization_url(**options)
        session["oauth_state"] = state
        session["code_verifier"] = flow.code_verifier or ""
        session.pop("auth_error", None)
        return redirect(auth_url)

    @app.get("/oauth/callback")
    def oauth_callback():
        config = web_client_config()
        expected = session.get("oauth_state")
        if config is None or not expected or request.args.get("state") != expected:
            _auth_failed("The sign-in could not be matched to this browser. Choose Connect Gmail again.")
            return redirect(url_for("connections_page"))
        if request.args.get("error"):
            _auth_failed("Gmail was not connected. Choose Connect Gmail again.")
            return redirect(url_for("connections_page"))
        from google_auth_oauthlib.flow import Flow
        from gmail_auto.token_store import save_token_raw, storage_ready

        flow = Flow.from_client_config(config, SCOPES)
        flow.redirect_uri = _oauth_redirect()
        flow.code_verifier = session.get("code_verifier") or None
        try:
            flow.fetch_token(authorization_response=_external_url())
        except Exception:
            _auth_failed("Google did not accept the sign-in. Choose Connect Gmail again.")
            return redirect(url_for("connections_page"))
        creds = flow.credentials
        if not getattr(creds, "refresh_token", None):
            flash("Google did not return a long-lived grant. Choose Connect Gmail again.", "error")
            return redirect(url_for("connections_page"))
        raw = creds.to_json()
        session["gmail_token"] = raw
        session.pop("oauth_state", None)
        session.pop("code_verifier", None)
        session.pop("auth_error", None)
        if storage_ready():
            save_token_raw(raw)
        elif not os.environ.get("VERCEL"):
            try:
                atomic_write(root() / "token.json", raw)
            except OSError:
                pass
        gmail_email = session.get("user") or ""
        try:
            from gmail_auto.gmail_client import GmailClient

            gmail_email = GmailClient(allow_browser=False).get_user_email() or gmail_email
        except ConfigError:
            pass
        mark_gmail_authorized(gmail_email)
        _forget_inbox()
        flash("Gmail is connected.")
        return redirect(url_for("home"))

    @app.get("/oauth/microsoft/<kind>")
    def microsoft_start(kind):
        blocked = _login_required()
        if blocked:
            return blocked
        if kind not in microsoft.SCOPES:
            abort(404)
        if not microsoft.configured():
            flash("Set MS_CLIENT_ID and MS_CLIENT_SECRET first. The README explains how to create them.", "error")
            return redirect(url_for("connections_page"))
        if not os.environ.get("VERCEL") and _request_host() != "localhost":
            # Microsoft only returns to the exact address that was registered, and the sign-in
            # cookie belongs to one host name, so the whole local sign-in runs on localhost.
            return redirect(_local_origin() + request.full_path.rstrip("?"))
        state = secrets.token_urlsafe(24)
        session["ms_state"] = {"state": state, "kind": kind}
        return redirect(microsoft.authorize_url(kind, _microsoft_redirect(), state))

    @app.get("/oauth/microsoft/callback")
    def microsoft_callback():
        expected = session.pop("ms_state", None) or {}
        kind = expected.get("kind")
        label = microsoft.LABELS.get(kind, "Microsoft")
        if not kind or request.args.get("state") != expected.get("state"):
            flash("The sign-in could not be matched to this browser. Try connecting again.", "error")
            return redirect(url_for("connections_page"))
        if request.args.get("error") or not request.args.get("code"):
            error = request.args.get("error", "")
            description = request.args.get("error_description", "")
            say(f"{label} sign-in failed: {error} {description[:400]}")
            message = f"{label} was not connected. {microsoft.explain_error(error, description)}"
            if "AADSTS50194" in description or "AADSTS50020" in description:
                message += f" This site currently signs in through {microsoft.tenant_text()}."
            flash(message, "error")
            return redirect(url_for("connections_page"))
        try:
            account = microsoft.finish_sign_in(kind, request.args["code"], _microsoft_redirect())
        except ConfigError as exc:
            flash(str(exc), "error")
            return redirect(url_for("connections_page"))
        _forget_inbox()
        flash(f"{label} is connected{' as ' + account if account else ''}.")
        return redirect(url_for("connections_page"))

    @app.post("/connections/<name>/disconnect")
    def disconnect(name):
        blocked = _login_required()
        if blocked:
            return blocked
        if name in microsoft.SCOPES:
            microsoft.disconnect(name)
            flash(f"{microsoft.LABELS[name]} was disconnected.")
        elif name == "gmail":
            from gmail_auto.token_store import clear_token

            session.pop("gmail_token", None)
            try:
                clear_token()
                (root() / "token.json").unlink(missing_ok=True)
                mark_gmail_authorized("", at=0)
            except (ConfigError, OSError):
                pass
            flash("Gmail was disconnected.")
        else:
            abort(404)
        _forget_inbox()
        return redirect(url_for("connections_page"))

    @app.post("/connections/whatsapp")
    def whatsapp_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            if request.form.get("test"):
                whatsapp.send_text(
                    "Mail Autopilot is connected. When a reply needs your OK I will ask here. Send HELP to see what I can do."
                )
                flash("Test message sent. Check WhatsApp.")
            else:
                save_settings({"whatsapp_to": request.form.get("whatsapp_to", "")})
                flash("WhatsApp number saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("connections_page") + "#whatsapp")

    @app.route("/whatsapp/webhook", methods=["GET", "POST"])
    def whatsapp_webhook():
        if request.method == "GET":
            if request.args.get("hub.mode") == "subscribe" and whatsapp.verify_token_ok(request.args.get("hub.verify_token", "")):
                return Response(request.args.get("hub.challenge", ""), mimetype="text/plain")
            set_state("wa_webhook_last", {"status": "verify_failed", "detail": "hub.verify_token mismatch or WHATSAPP_VERIFY_TOKEN unset", "at": now_text()})
            abort(403)
        twilio = bool(request.headers.get("X-Twilio-Signature"))
        if twilio:
            if not whatsapp.twilio_signature_ok(_external_url(), request.form, request.headers.get("X-Twilio-Signature", "")):
                set_state("wa_webhook_last", {"status": "signature_failed", "detail": "Twilio signature mismatch", "at": now_text()})
                abort(403)
            messages = whatsapp.parse_twilio(request.form)
        else:
            if not whatsapp.meta_signature_ok(request.get_data(), request.headers.get("X-Hub-Signature-256", "")):
                set_state("wa_webhook_last", {"status": "signature_failed", "detail": "Meta X-Hub-Signature-256 mismatch or WHATSAPP_APP_SECRET unset", "at": now_text()})
                abort(403)
            payload = request.get_json(silent=True)
            messages = whatsapp.parse_meta(payload if isinstance(payload, dict) else {})
        from gmail_auto import assistant

        for incoming in messages:
            if not whatsapp.is_owner(incoming.sender):
                set_state("wa_webhook_last", {"status": "ignored_non_owner", "detail": "Sender did not match configured whatsapp_to / WHATSAPP_TO", "at": now_text()})
                continue
            try:
                if assistant.seen_before(incoming.message_id):
                    continue
                if whatsapp.has_voice(incoming):
                    try:
                        whatsapp.hear(incoming)
                    except ConfigError as exc:
                        whatsapp.send_text(f"Sorry, I couldn't make out that voice message. {exc} Please type it instead.")
                        set_state("wa_webhook_last", {"status": "voice_failed", "detail": str(exc), "at": now_text()})
                        continue
                    if not incoming.text:
                        whatsapp.send_text("I couldn't hear any words in that voice message. Please try again or type it.")
                        continue
                answer = assistant.handle(incoming)
                if answer and incoming.heard:
                    answer = f"🎤 I heard: \"{incoming.heard}\"\n\n{answer}"
                if answer:
                    whatsapp.send_text(answer)
                set_state("wa_webhook_last", {"status": "ok", "detail": "", "at": now_text()})
            except ConfigError as exc:
                set_state("wa_webhook_last", {"status": "send_failed", "detail": str(exc), "at": now_text()})
                continue
        _forget_inbox()
        if twilio:
            return Response("<Response></Response>", mimetype="text/xml")
        return {"ok": True}

    @app.get("/cron")
    def cron():
        if not _cron_allowed():
            abort(403)
        try:
            report = run_once()
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        text = format_report(report)
        return {"ok": True, "summary": text.split("\n", 1)[0]}

    # ---- settings ------------------------------------------------------------

    @app.get("/profile")
    def profile_page():
        blocked = _login_required()
        if blocked:
            return blocked
        return _render("profile.html", **_profile_context())

    @app.post("/profile")
    def profile_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            save_profile(profile_from_form(request.form))
        except ConfigError as exc:
            context = _profile_context(request.form)
            context["error"] = str(exc)
            return _render("profile.html", **context), 400
        flash("Profile saved.")
        return redirect(url_for("profile_page") + "#voice")

    @app.post("/settings")
    def settings_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            changes = {}
            if "reply_language" in request.form:
                changes["reply_language"] = request.form.get("reply_language", "")
            if "notify_preset" in request.form:
                changes["notify_auto_replies"] = request.form.get("notify_auto_replies") == "1"
                changes["notify_important"] = request.form.get("notify_important") == "1"
                changes["notify_preset"] = request.form.get("notify_preset") or "custom"
                changes["notify_kinds"] = request.form.getlist("notify_kinds")
                changes["notify_calendar"] = request.form.get("notify_calendar") == "1"
                changes["daily_digest"] = request.form.get("daily_digest") == "1"
                changes["digest_hour"] = request.form.get("digest_hour", "8")
            else:
                if "daily_digest" in request.form:
                    changes["daily_digest"] = request.form.get("daily_digest") == "1"
                if "digest_hour" in request.form:
                    changes["digest_hour"] = request.form.get("digest_hour", "8")
                if "notify_auto_replies" in request.form:
                    changes["notify_auto_replies"] = request.form.get("notify_auto_replies") == "1"
            if changes:
                save_settings(changes)
            flash("Settings saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        nxt = request.form.get("next") or "#interrupt"
        if not nxt.startswith("#"):
            nxt = "#interrupt"
        return redirect(url_for("profile_page") + nxt)

    @app.post("/settings/model")
    def model_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            save_settings(
                {
                    "ai_model": request.form.get("ai_model", ""),
                    "super_model": request.form.get("super_model", ""),
                }
            )
            which = request.form.get("test") or ""
            if which in {"1", "worker", "super"}:
                role = "super" if which == "super" else "worker"
                reply = complete("Reply with OK.", "Say OK.", max_tokens=16, temperature=0.2, role=role)
                if not reply.strip():
                    raise ReplyError("The model returned an empty reply. Try another model or retry.")
                flash(f"Model {active_model(role)} responded: {reply.strip()[:80]}")
            else:
                flash("Models saved.")
        except (ConfigError, ReplyError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("profile_page") + "#send")

    @app.post("/settings/cleanup-rules")
    def cleanup_rules_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            save_settings(
                {
                    "cleanup_match": request.form.get("cleanup_match", ""),
                    "cleanup_keep": request.form.get("cleanup_keep", ""),
                    "cleanup_skipped": request.form.get("cleanup_skipped") == "1",
                    "cleanup_pack": 3,
                }
            )
            flash("Cleanup rules saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("profile_page") + "#cleanup")

    @app.post("/settings/cleanup")
    def mailbox_cleanup():
        blocked = _login_required()
        if blocked:
            return blocked
        from gmail_auto.cleanup import run_all

        try:
            clients, problems = connected_clients()
        except Exception as exc:
            flash(_safe_error(exc), "error")
            return redirect(url_for("profile_page") + "#cleanup")
        if not clients:
            flash("Connect a mailbox first.", "error")
            return redirect(url_for("profile_page") + "#cleanup")
        if not any(getattr(client, "provider", "") == "gmail" for client in clients):
            flash("Gmail is not connected, so Gmail was not cleaned. Open Connections and connect Gmail again.", "error")
        result = run_all(clients)
        _forget_inbox()
        for message in problems + (result.get("errors") or []):
            flash(message, "error")
        flash(f"Deleted {result['archived']} letters to Trash. {result['scanned']} matched. School and keep-list mail were left.")
        return redirect(url_for("profile_page") + "#cleanup")

    @app.errorhandler(400)
    def bad_request(_exc):
        return "That request was not understood.", 400

    @app.errorhandler(403)
    def forbidden(_exc):
        return "This page can only be opened on this computer.", 403

    @app.errorhandler(404)
    def missing(_exc):
        return "There is no such page.", 404

    @app.errorhandler(405)
    def method_not_allowed(_exc):
        return "This action cannot be opened that way.", 405

    @app.errorhandler(413)
    def too_large(_exc):
        flash("That file is too large for this hosted site. Keep each file under 4 MB, or split it and upload the parts.", "error")
        return redirect(url_for("knowledge_page") + "#sources")


def run_server() -> None:
    app = create_app()
    try:
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    except OSError as exc:
        raise ConfigError("Port 8765 is already in use, so the web page did not open.") from exc


# ---- markdown-lite -----------------------------------------------------------


def render_markdown(text: str) -> Markup:
    """Headings, bullets and bold only: enough for the model's briefings, and safe to show."""
    blocks: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(stripped, quote=False))
        heading = re.match(r"^#{1,4}\s+(.*)$", escaped)
        bullet = re.match(r"^[-*•]\s+(.*)$", escaped)
        if heading:
            flush()
            blocks.append(f"<h4>{heading.group(1)}</h4>")
        elif bullet:
            bullets.append(bullet.group(1))
        else:
            flush()
            blocks.append(f"<p>{escaped}</p>")
    flush()
    return Markup("".join(blocks))



def _apply_role(entry: dict, role: str, text: str) -> str:
    """Record what kind of document an upload is, and set up the template it holds. Returns a note for the owner."""
    from gmail_auto import contracts, invoices

    title = entry.get("title") or "The file"
    if role not in knowledge.ROLES:
        role = "reference"
    changes = {"role": role}
    note = ""
    if role == "invoice_template":
        from gmail_auto.llm import model_ready

        found = {}
        if model_ready():
            try:
                found = invoices.template_from_text(text)
            except (ConfigError, ReplyError):
                found = {}
        current = invoices.load_template()
        fill = {key: value for key, value in found.items() if key != "next_number" and not str(current.get(key) or "").strip()}
        if int(found.get("next_number") or 0) > int(current.get("next_number") or 1):
            fill["next_number"] = found["next_number"]
        if fill:
            invoices.save_template(fill)
        note = (
            f"{title} looks like an invoice. It is kept as your invoice template, not as facts for replies"
            + (f", and {len(fill)} of your invoice details were filled in from it." if fill else ".")
        )
    elif role == "contract_template":
        fields = contracts.find_fields(text)
        changes["fields"] = fields
        layout = contracts.layout_kept(title)
        kept = bool((entry.get("meta") or {}).get("file_id"))
        note = f"{title} looks like a contract. It is kept as a contract template, not as facts for replies. "
        if layout and not kept:
            note += "The file itself was not kept (too large or no file storage), so upload it again to draft contracts."
        elif not fields:
            note += "No parts to fill in were found. Mark them like {{Client name}} in the file and upload it again."
        elif not layout:
            note += (
                f"{len(fields)} parts to fill in were found. Contracts from it come out as a plain PDF; "
                "upload the Word, PowerPoint or Excel version to keep your layout."
            )
        elif fields:
            note += f"{len(fields)} parts to fill in per client were found: " + ", ".join(field["name"] for field in fields[:6]) + "."
        else:
            note += "No parts to fill in were found. Mark them in the Word file like {{Client name}} and upload it again."
    elif role == "price_list":
        note = f"{title} looks like a price list. Replies, quotes and invoices will use its prices, and you can send it as it is."
    elif role in ("brochure", "product_info"):
        label = knowledge.ROLES[role].lower()
        kept = bool((entry.get("meta") or {}).get("file_id"))
        note = f"{title} is kept as your {label}. " + (
            "It is under Documents, ready to attach to any email, and replies can use what it says."
            if kept
            else "Replies can use what it says, but the file itself was not kept (too large or no file storage), so it cannot be attached."
        )
    knowledge.update_meta(entry["id"], changes)
    return note


def _people_for_compose() -> list[dict]:
    try:
        from gmail_auto.contacts import all_contacts

        return all_contacts()[:400]
    except ConfigError:
        return []


def _person_for(opened: dict | None) -> dict | None:
    if not opened or not opened.get("email"):
        return None
    try:
        from gmail_auto.contacts import find

        return find(opened["email"])
    except ConfigError:
        return None


def _ui_language() -> str:
    try:
        return load_settings().get("ui_language") or "en"
    except Exception:
        return "en"


def _contract_templates() -> list[dict]:
    try:
        return [item for item in knowledge.templates("contract_template") if (item.get("meta") or {}).get("fields")]
    except ConfigError:
        return []


def _attach_contract(contract: dict):
    """Put the drafted contract on the reply to its letter, writing that reply if there is none yet."""
    from gmail_auto.outgoing import contract_file
    from gmail_auto.reply_now import draft_now, waiting_for


    if not contract.get("mail_key"):
        raise ConfigError("This contract was not made from a letter. Use Send in a new email instead.")
    record = contract_file(contract)
    item = waiting_for(contract["mail_key"])
    if item is None:
        mail = _find_mail(contract["mail_key"])
        if mail is None:
            raise ConfigError("The letter for this contract could not be loaded right now. Try again in a moment.")
        item = draft_now(mail, "Say the contract is attached for them to read and sign, and to ask if anything needs changing. Keep it short.", load_profile())
    approvals.attach(item["id"], record)
    _forget_inbox()
    flash("The contract is attached to the reply. Check both, then send.")
    return redirect(url_for("home", show="todo", open=contract["mail_key"]))


def _invoice_form() -> dict:
    descriptions = request.form.getlist("description")
    quantities = request.form.getlist("qty")
    prices = request.form.getlist("unit_price")
    items = []
    for index, description in enumerate(descriptions):
        price = prices[index] if index < len(prices) else ""
        items.append(
            {
                "description": description,
                "qty": (quantities[index] if index < len(quantities) else "") or "1",
                "unit_price": price if price.strip() else None,
            }
        )
    fields = {key: request.form.get(key, "") for key in ("client_name", "client_email", "client_address", "reference", "notes", "issue_date", "due_date")}
    fields["items"] = items
    return fields


def _attach_invoice(invoice: dict):
    """Put the issued invoice's PDF on the reply to its letter, writing that reply if there is none yet."""
    from gmail_auto import invoices
    from gmail_auto.reply_now import draft_now, waiting_for

    from gmail_auto.outgoing import invoice_file

    if not invoice.get("mail_key"):
        raise ConfigError("This invoice was not made from a letter. Use Send in a new email instead.")
    record = invoice_file(invoice)
    item = waiting_for(invoice["mail_key"])
    if item is None:
        mail = _find_mail(invoice["mail_key"])
        if mail is None:
            raise ConfigError("The letter for this invoice could not be loaded right now. Try again in a moment.")
        sums = invoices.totals(invoice)
        points = (
            (
                f"Say that quote {invoice['number']} for {invoices.money(sums['total'], sums['currency'])} is attached, "
                f"valid until {invoice.get('due_date')}, and ask them to reply to accept. Keep it short and friendly."
            )
            if invoices.kind_of(invoice) == "quote"
            else (
                f"Say that invoice {invoice['number']} for {invoices.money(sums['total'], sums['currency'])} is attached, "
                f"due {invoice.get('due_date')}. Keep it short and friendly."
            )
        )
        item = draft_now(mail, points, load_profile())
    approvals.attach(item["id"], record)
    _forget_inbox()
    flash(f"{invoices.KINDS[invoices.kind_of(invoice)]} {invoice['number']} is attached to the reply. Check it, then send.")
    return redirect(url_for("home", show="todo", open=invoice["mail_key"]))


def _attach_context(needed: bool) -> dict:
    if not needed:
        return {"attach_docs": [], "files_ready": False}
    try:
        docs = knowledge.attachable_documents()
    except ConfigError:
        docs = []
    return {"attach_docs": docs, "files_ready": files.ready(), "file_limit": files.human_size(files.MAX_FILE_BYTES)}


def _attach_to(item_id: str) -> list[str]:
    """Attach uploaded files and/or a knowledge-base original to a waiting reply."""
    if approvals.get(item_id) is None:
        raise ConfigError("That reply was not found.")
    names = []
    doc_id = request.form.get("doc", "")
    if doc_id:
        doc = next((item for item in knowledge.attachable_documents() if item["id"] == doc_id), None)
        record = files.get((doc or {}).get("meta", {}).get("file_id", "")) if doc else None
        if record is None:
            raise ConfigError("That knowledge file is no longer kept. Upload it again.")
        approvals.attach(item_id, record)
        names.append(record["name"])
    for upload in request.files.getlist("files"):
        if not upload or not upload.filename:
            continue
        record = files.save(upload.read(), upload.filename, upload.mimetype, origin="reply")
        try:
            approvals.attach(item_id, record)
        except ConfigError:
            files.remove(record["id"])
            raise
        names.append(record["name"])
    return names


def _keep_original(raw: bytes, name: str, mime: str) -> dict | None:
    """Keep an uploaded knowledge file so replies can attach it. Skipped quietly when there is nowhere to keep it."""
    if not files.ready() or not raw or len(raw) > files.MAX_FILE_BYTES:
        return None
    try:
        return files.save(raw, name, mime, origin="knowledge")
    except ConfigError:
        return None

# ---- inbox -------------------------------------------------------------------


def _home_context() -> dict:
    errors = []
    placeholder = True
    try:
        placeholder = profile_is_placeholder(load_profile())
    except ConfigError as exc:
        errors.append(str(exc))

    hosted = bool(os.environ.get("VERCEL"))
    inbox = _inbox(live=not hosted or request.args.get("live") == "1")
    errors.extend(inbox["errors"])
    rows = attach_stories(_merge_history(inbox["rows"]))
    accounts = _account_chips() or inbox["accounts"]
    from gmail_auto.importance import inbox_kinds

    kinds = set(inbox_kinds(load_settings()))
    counts = {name: 0 for name in _FILTERS}
    for row in rows:
        row["bucket"] = _bucket(row, kinds)
        counts["all"] += 1
        if row["bucket"] in counts:
            counts[row["bucket"]] += 1
    show = _OLD_FILTERS.get(request.args.get("show", ""), request.args.get("show", _DEFAULT_FILTER))
    if show not in _FILTERS:
        show = _DEFAULT_FILTER
    visible = rows if show == "all" else [row for row in rows if row["bucket"] == show]
    wanted = request.args.get("open", "")
    page, pages = _inbox_page(visible, wanted)
    listed = visible[(page - 1) * _INBOX_PAGE : page * _INBOX_PAGE]
    opened = next((row for row in listed if row["key"] == wanted), None)
    if opened is None and listed and not hosted and not wanted:
        opened = listed[0]

    waiting_item = None
    if opened:
        try:
            _attach_review(opened)
        except (ConfigError, ReplyError):
            pass
    if opened and opened.get("approval_id") and opened["kind"] == "waiting":
        waiting_item = approvals.get(opened["approval_id"])

    last_run = load_last_run()
    if isinstance(last_run, dict):
        last_run.setdefault("lines", [])
        last_run.setdefault("summary", "")
        last_run.setdefault("error", "")
        if re.search(r"[一-鿿]", last_run["summary"]):
            last_run["summary"] = _legacy_summary(last_run)
            last_run["lines"] = []
    return {
        "live": is_live(),
        "placeholder": placeholder,
        "accounts": accounts,
        "connected": bool(accounts),
        "offline": inbox["offline"],
        "errors": errors,
        "rows": listed,
        "pager": {
            "page": page,
            "pages": pages,
            "prev": page - 1 if page > 1 else 0,
            "next": page + 1 if page < pages else 0,
        },
        "opened": opened,
        "story": story_view(opened, rows),
        "stories": story_board(rows),
        "matter": story_detail(request.args.get("story", ""), rows),
        "waiting_item": waiting_item,
        **_attach_context(waiting_item is not None),
        "contract_templates": _contract_templates() if opened else [],
        "person": _person_for(opened),
        "filters": _FILTERS,
        "counts": counts,
        "show": show,
        "last_run": last_run,
        "interval": _interval_text(),
        "auth_error": "" if accounts else session.get("auth_error", ""),
    }


def _inbox(live: bool = True) -> dict:
    who = session.get("user", "")
    cached = _inbox_cache["data"]
    if cached is not None and _inbox_cache["who"] == who and time.time() - _inbox_cache["at"] < _INBOX_TTL:
        # Statuses change when a reply is approved, so they are always read fresh.
        _refresh_status(cached)
        cached["needs_fetch"] = False
        return cached
    if not live:
        snapshot = _read_inbox_snapshot(who)
        if snapshot is not None:
            _refresh_status(snapshot)
            snapshot["needs_fetch"] = False
            _inbox_cache.update({"at": time.time(), "who": who, "data": snapshot})
            return snapshot
        rows = _rows_from_store()
        return {
            "rows": rows,
            "mails": {},
            "accounts": _account_chips(),
            "errors": [],
            "offline": bool(rows),
            "needs_fetch": False,
            "at": 0.0,
        }
    data = {"rows": [], "mails": {}, "accounts": [], "errors": [], "offline": False, "needs_fetch": False, "at": time.time()}
    try:
        clients, problems = connected_clients()
    except Exception as exc:
        clients, problems = [], [_safe_error(exc)]
    data["errors"].extend(problems)
    pairs = []
    fetched = _list_mailboxes(clients)
    for account, mails, error in fetched:
        if account.get("provider"):
            provider = account["provider"]
            label = PROVIDERS.get(provider, "Mailbox")
            data["accounts"].append({"provider": provider, "label": label, "email": account.get("email") or label})
        if error:
            data["errors"].append(error)
            continue
        for mail in mails:
            if not mail.account:
                mail.account = account["email"]
            pairs.append((mail, account["email"]))
    try:
        processed = load_processed()
    except ConfigError as exc:
        data["errors"].append(str(exc))
        processed = {}
    pairs.sort(key=lambda pair: pair[0].received_ts or 0, reverse=True)
    for mail, account in pairs:
        data["mails"][mail.key] = mail
        data["rows"].append(_row_from_mail(mail, processed.get(mail.key), account))
    if not pairs and not data["accounts"]:
        data["rows"] = _rows_from_store()
        data["offline"] = bool(data["rows"])
    data["at"] = time.time()
    data["needs_fetch"] = False
    _inbox_cache.update({"at": data["at"], "who": who, "data": data})
    _write_inbox_snapshot(who, data)
    return data


def _refresh_status(data: dict) -> None:
    try:
        processed = load_processed()
    except ConfigError:
        return
    accounts = {item["provider"]: item["email"] for item in data["accounts"]}
    rows = []
    for row in data["rows"]:
        mail = data["mails"].get(row["key"])
        if mail is None:
            rows.append(row)
            continue
        rows.append(_row_from_mail(mail, processed.get(mail.key), accounts.get(mail.provider, "")))
    data["rows"] = rows


def _list_mailboxes(clients) -> list[tuple]:
    """Read each mailbox on its own thread so Gmail and Outlook do not wait for each other."""
    if not clients:
        return []
    if len(clients) == 1:
        return [_list_one_mailbox(clients[0])]
    from flask import copy_current_request_context

    wrapped = [copy_current_request_context(lambda client=client: _list_one_mailbox(client)) for client in clients]
    out = []
    with ThreadPoolExecutor(max_workers=len(wrapped)) as pool:
        futures = [pool.submit(item) for item in wrapped]
        for future in as_completed(futures):
            out.append(future.result())
    return out


def _list_one_mailbox(client) -> tuple:
    label = PROVIDERS.get(getattr(client, "provider", "gmail"), "Mailbox")
    email = ""
    try:
        email = client.get_user_email()
        mails = client.list_recent(_INBOX_PAGE, full=False)
        return ({"provider": client.provider, "email": email}, mails, "")
    except ConfigError as exc:
        return (_known_account(client, email), [], named_error(label, exc))
    except Exception as extra:
        return (_known_account(client, email), [], named_error(label, _safe_error(extra)))


def _known_account(client, email: str) -> dict:
    if not email and getattr(client, "provider", "") == "outlook":
        email = (microsoft.status("outlook").get("account") or "").strip()
    if not email:
        return {}
    return {"provider": client.provider, "email": email}


def _forget_inbox() -> None:
    _inbox_cache.update({"at": 0.0, "data": None})
    try:
        from gmail_auto.storage import delete_doc

        delete_doc("inbox_cache")
    except Exception:
        pass


def _account_chips() -> list[dict]:
    chips = []
    outlook = microsoft.status("outlook")
    if outlook.get("connected"):
        chips.append({"provider": "outlook", "label": "Outlook", "email": outlook.get("account") or "Outlook"})
    gmail = gmail_status()
    if gmail.get("email"):
        chips.append({"provider": "gmail", "label": "Gmail", "email": gmail["email"]})
    return chips


def _read_inbox_snapshot(who: str) -> dict | None:
    from gmail_auto.storage import load_doc

    try:
        saved = load_doc("inbox_cache", None)
    except ConfigError:
        return None
    if not isinstance(saved, dict) or saved.get("who") != who:
        return None
    if time.time() - float(saved.get("at") or 0) > _INBOX_STORE_TTL:
        return None
    return {
        "rows": list(saved.get("rows") or []),
        "mails": {},
        "accounts": list(saved.get("accounts") or []),
        "errors": list(saved.get("errors") or []),
        "offline": bool(saved.get("offline")),
        "needs_fetch": False,
        "at": float(saved.get("at") or 0),
    }


def _write_inbox_snapshot(who: str, data: dict) -> None:
    from gmail_auto.storage import save_doc

    try:
        save_doc(
            "inbox_cache",
            {
                "who": who,
                "at": data.get("at") or time.time(),
                "rows": data.get("rows") or [],
                "accounts": data.get("accounts") or [],
                "errors": data.get("errors") or [],
                "offline": bool(data.get("offline")),
            },
        )
    except ConfigError:
        return


def _find_mail(key: str):
    if not key:
        return None
    cached = _inbox_cache["data"]
    listed = None
    if cached is not None and _inbox_cache["who"] == session.get("user", ""):
        listed = cached["mails"].get(key)
        if listed is not None and (listed.html or listed.body):
            return listed
    provider, _, message_id = key.partition(":")
    if not message_id:
        provider, message_id = "gmail", key
    if provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9_=+\-/.!]{1,400}", message_id):
        return listed
    try:
        mail = client_for(provider).get_message(message_id)
    except Exception:
        return listed
    if cached is not None and mail is not None:
        cached["mails"][mail.key] = mail
    return mail


def _inbox_query() -> dict:
    query = {}
    show = request.args.get("show", "")
    show = _OLD_FILTERS.get(show, show)
    if show in _FILTERS and show != _DEFAULT_FILTER:
        query["show"] = show
    opened = request.args.get("open", "")
    if opened:
        query["open"] = opened
    story = request.args.get("story", "")
    if story:
        query["story"] = story
    page = request.args.get("page", "")
    if page.isdigit() and int(page) > 1:
        query["page"] = int(page)
    return query


def _inbox_page(rows: list[dict], wanted: str) -> tuple[int, int]:
    total = len(rows)
    pages = max(1, (total + _INBOX_PAGE - 1) // _INBOX_PAGE) if total else 1
    page = 1
    raw = request.args.get("page", "")
    if raw.isdigit():
        page = int(raw)
    if wanted:
        for index, row in enumerate(rows):
            if row["key"] == wanted:
                page = index // _INBOX_PAGE + 1
                break
    return min(max(page, 1), pages), pages


def _merge_history(rows: list[dict]) -> list[dict]:
    """Mailbox page plus older handled mail, newest first."""
    seen = {row.get("key") for row in rows}
    extra = []
    try:
        records = list_processed(400)
    except ConfigError:
        records = []
    for record in records:
        key = record.get("id") or ""
        if not key or key in seen:
            continue
        extra.append(_row_from_record(record))
        seen.add(key)
    combined = list(rows) + extra
    combined.sort(key=lambda row: row.get("date") or "", reverse=True)
    return combined


def _row_from_mail(mail, record, user_email: str) -> dict:
    label, kind, group = _describe(mail, record, user_email)
    record = record if isinstance(record, dict) else {}
    snippet = " ".join((mail.snippet or mail.body or "").split())
    return {
        "key": mail.key,
        "provider": mail.provider,
        "provider_label": PROVIDERS.get(mail.provider, "Mail"),
        "who": mail.from_name or mail.from_email or "Unknown sender",
        "email": mail.from_email,
        "subject": mail.subject or "(no subject)",
        "date": mail.date,
        "short_date": _short_date(mail.date),
        "snippet": snippet[:160],
        "unread": "UNREAD" in (mail.label_ids or []),
        "labels": [item for item in (mail.label_ids or []) if str(item).startswith("CATEGORY_")],
        "thread_id": mail.thread_id or "",
        "received_ts": float(mail.received_ts or 0),
        "status": label,
        "kind": kind,
        "group": group,
        "has_body": True,
        "reply": record.get("reply") or "",
        "summary": record.get("summary") or "",
        "reason": record.get("reason") or "" if kind in ("waiting", "drafted") else "",
        "sources": record.get("sources") or [],
        "outbox": record.get("outbox") or "",
        "approval_id": record.get("approval_id") or "",
        "needs_review": bool(record.get("needs_review")),
        "advice": "",
        "handled": bool(record.get("handled")),
    }


def _attach_review(opened: dict) -> None:
    """Fill Super's suggestion and the clerk brief when a letter is opened."""
    from gmail_auto.llm import model_ready
    from gmail_auto.models import Mail
    from gmail_auto.screen import screen_mail
    from gmail_auto.worker import brief_mail, load_review, remember_verdict

    key = opened.get("key") or ""
    review = load_review(key)
    if review.get("summary") and not opened.get("summary"):
        opened["summary"] = review["summary"]
    if review.get("disposition"):
        opened["advice"] = _advice_line(review.get("disposition"), review.get("advice") or review.get("summary"))
        return
    if not model_ready():
        return
    mail = Mail(
        id=key.partition(":")[2] if ":" in key else key,
        thread_id="",
        from_name=opened.get("who") or "",
        from_email=opened.get("email") or "",
        reply_email="",
        subject=opened.get("subject") or "",
        date=opened.get("date") or "",
        body=opened.get("snippet") or opened.get("summary") or "",
        snippet=opened.get("snippet") or "",
        provider=opened.get("provider") or "gmail",
    )
    brief = brief_mail(mail)
    if brief.get("summary") and not opened.get("summary"):
        opened["summary"] = brief["summary"]
    verdict = screen_mail(mail, brief)
    if verdict is None:
        return
    remember_verdict(mail.key, verdict)
    opened["advice"] = _advice_line(verdict.disposition, verdict.reason)


def _advice_line(disposition: str, reason: str) -> str:
    labels = {
        "ignore": "Ignore",
        "reply": "Can reply",
        "ask": "Ask you first",
    }
    title = labels.get(disposition or "", disposition or "Review")
    reason = (reason or "").strip()
    return f"{title}: {reason}" if reason else title


def _rows_from_store() -> list[dict]:
    return _merge_history([])


def _bucket(row: dict, kinds: set[str]) -> str:
    """Which tab a letter belongs to, by what the owner has to do about it.

    To do: a draft waits for a look, or the letter has not been handled yet.
    Notices: no reply is needed, but a push rule (a bill, a fine, a school or government letter...) says
    it matters and that kind is switched on in Settings.
    """
    kind = row.get("kind")
    if kind in ("waiting", "drafted"):
        return "todo"
    if row.get("handled"):
        # Dealt with: out of To do and Notices, still under All.
        return "replied" if row.get("group") == "replied" else "all"
    if row.get("group") == "replied":
        return "replied"
    if _is_notice(row, kinds):
        return "notices"
    if kind == "pending":
        return "todo"
    if row.get("group") == "skipped":
        return "skipped"
    return "all"


def _is_notice(row: dict, kinds: set[str]) -> bool:
    if row.get("sensitive"):
        return False
    from gmail_auto.importance import assess
    from gmail_auto.models import Mail

    key = row.get("key") or ""
    provider = row.get("provider") or "gmail"
    mail = Mail(
        id=key.partition(":")[2] if ":" in key else key,
        thread_id="",
        from_name=row.get("who") or "",
        from_email=row.get("email") or "",
        reply_email="",
        subject=row.get("subject") or "",
        date=row.get("date") or "",
        body=row.get("snippet") or row.get("summary") or "",
        label_ids=list(row.get("labels") or []),
        provider=provider,
    )
    try:
        tag = assess(mail, lookups=False)
    except Exception:
        return False
    return bool(tag.get("important")) and tag.get("kind") in kinds


def _row_from_record(record: dict) -> dict:
    label, kind, group = _describe_record(record)
    when = record.get("processed_at") or record.get("date") or ""
    return {
        "key": record.get("id", ""),
        "provider": record.get("provider") or "gmail",
        "provider_label": PROVIDERS.get(record.get("provider") or "gmail", "Mail"),
        "who": record.get("from_name") or record.get("from_email") or "Unknown sender",
        "email": record.get("from_email") or "",
        "subject": record.get("subject") or "(no subject)",
        "date": when,
        "short_date": _short_date(when),
        "snippet": (record.get("summary") or "")[:160],
        "unread": False,
        "thread_id": record.get("thread_id") or "",
        "received_ts": _record_ts(when),
        "status": label,
        "kind": kind,
        "group": group,
        # The letter text is fetched from the mailbox when it is opened. Letters that mention passwords,
        # codes or ID are never shown.
        "has_body": bool(record.get("id")) and not record.get("sensitive"),
        "sensitive": bool(record.get("sensitive")),
        "reply": record.get("reply") or "",
        "summary": record.get("summary") or "",
        "advice": "",
        "reason": "",
        "sources": record.get("sources") or [],
        "outbox": record.get("outbox") or "",
        "approval_id": record.get("approval_id") or "",
        "needs_review": bool(record.get("needs_review")),
        "handled": bool(record.get("handled")),
    }


def _describe(mail, record, user_email: str) -> tuple[str, str, str]:
    if isinstance(record, dict):
        return _describe_record(record)
    if user_email:
        decision = classify(mail, user_email)
        if isinstance(decision, Skip):
            return f"Won't reply: {decision.reason}", "skipped", "skipped"
    if "UNREAD" in (mail.label_ids or []):
        return "Not handled yet", "pending", "needs"
    return "Read, no reply", "read", "all"


def _describe_record(record: dict) -> tuple[str, str, str]:
    action = record.get("action")
    if record.get("handled") and action not in ("sent", "waiting", "drafted"):
        return f"Handled{' ' + str(record.get('handled_at'))[:10] if record.get('handled_at') else ''}", "handled", "all"
    if action == "sent":
        return ("Replied after your OK" if record.get("approved") else "Replied automatically"), "sent", "replied"
    if action == "drafted":
        return ("Draft saved, needs a look" if record.get("needs_review") else "Draft saved"), "drafted", "drafts"
    if action == "waiting":
        return "Waiting for your OK", "waiting", "needs"
    if action == "rejected":
        return "Not sent: you declined", "skipped", "skipped"
    if action == "expired":
        return "Not sent: no answer in a week", "skipped", "skipped"
    code = record.get("skip_code") or stats._guess_code(record)
    reason = stats.SKIP_LABELS.get(code) if code != "other" else (record.get("reason") or "skipped")
    return f"Skipped: {reason}", "skipped", "skipped"


def _legacy_summary(last_run: dict) -> str:
    return (
        f"[{last_run.get('finished_at', '')}] checked {last_run.get('checked', 0)}, sent {last_run.get('sent', 0)}, "
        f"drafted {last_run.get('drafted', 0)}, skipped {last_run.get('skipped', 0)}, failed {last_run.get('failed', 0)}."
    )


def _record_ts(value: str) -> float:
    text = (value or "").strip().replace("T", " ")
    for width, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M"), (10, "%Y-%m-%d")):
        try:
            return time.mktime(time.strptime(text[:width], fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


def _short_date(value: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})", value or "")
    if not match:
        return value or ""
    year, month, day, clock = match.groups()
    today = time.strftime("%Y-%m-%d")
    if f"{year}-{month}-{day}" == today:
        return clock
    name = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[int(month) - 1]
    return f"{int(day)} {name}"


def _interval_text() -> str:
    if os.environ.get("VERCEL"):
        return "every 5 minutes"
    minutes = check_interval_seconds() // 60
    return f"every {minutes} minute{'s' if minutes != 1 else ''} while the program is running"


# ---- dashboard charts --------------------------------------------------------

_SERIES = (
    ("auto_sent", "Sent automatically"),
    ("approved_sent", "Sent after your OK"),
    ("asked", "Waiting / asked"),
    ("drafted", "Draft only"),
    ("skipped", "Skipped"),
    ("failed", "Failed"),
)


def _trend_chart(series: list[dict]) -> dict:
    """Geometry for the stacked daily bars, worked out here so the template only draws."""
    width, height, left, bottom, top = 920, 260, 36, 28, 10
    plot_w, plot_h = width - left - 8, height - bottom - top
    totals = [sum(day[key] for key, _ in _SERIES) for day in series]
    peak = max(totals + [1])
    step = _nice_step(peak)
    ceiling = step * max(1, -(-peak // step))
    slot = plot_w / max(1, len(series))
    bar = max(3.0, min(24.0, slot * 0.68))
    radius = min(4.0, bar / 2)
    every = max(1, round(len(series) / 10))
    bars = []
    for index, day in enumerate(series):
        x = left + index * slot + (slot - bar) / 2
        base = top + plot_h
        y = base
        parts = []
        for key, label in _SERIES:
            value = day[key]
            if not value:
                continue
            h = plot_h * value / ceiling
            y -= h
            # 2px of surface between stacked segments does the separating, not a stroke.
            parts.append({"key": key, "label": label, "value": value, "x": round(x, 1), "y": round(y + 2, 1), "h": round(max(h - 2, 1), 1), "w": round(bar, 1)})
        tip = min(y + 2, base - radius)
        bars.append(
            {
                "parts": parts,
                # Rounded at the data end, square on the baseline.
                "clip": (
                    f"M{x:.1f},{base:.1f} V{tip + radius:.1f} Q{x:.1f},{tip:.1f} {x + radius:.1f},{tip:.1f} "
                    f"H{x + bar - radius:.1f} Q{x + bar:.1f},{tip:.1f} {x + bar:.1f},{tip + radius:.1f} V{base:.1f} Z"
                ),
                "total": sum(part["value"] for part in parts),
                "label": day["label"] if index % every == 0 or index == len(series) - 1 else "",
                "cx": round(x + bar / 2, 1),
                "title": f"{day['weekday']} {day['label']}: " + (", ".join(f"{part['value']} {part['label'].lower()}" for part in parts) or "nothing handled"),
                "hit_x": round(left + index * slot, 1),
                "hit_w": round(slot, 1),
            }
        )
    ticks = [{"value": value, "y": round(top + plot_h - plot_h * value / ceiling, 1)} for value in range(0, ceiling + 1, step)]
    return {
        "width": width,
        "height": height,
        "left": left,
        "right": width - 8,
        "top": top,
        "base": top + plot_h,
        "bars": bars,
        "ticks": ticks,
        "legend": [{"key": key, "label": label} for key, label in _SERIES if any(day[key] for day in series)],
        "empty": not any(totals),
    }


def _hours_chart(hours: list[dict]) -> dict:
    peak = max([item["count"] for item in hours] + [1])
    busiest = max(hours, key=lambda item: item["count"])
    return {
        "bars": [{"hour": item["hour"], "count": item["count"], "pct": round(100 * item["count"] / peak)} for item in hours],
        "busiest": f"{busiest['hour']:02d}:00–{(busiest['hour'] + 1) % 24:02d}:00" if busiest["count"] else "",
        "empty": not any(item["count"] for item in hours),
    }


def _nice_step(peak: int) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if peak / step <= 5:
            return step
    return 2000


# ---- connections -------------------------------------------------------------


def _connections_context() -> dict:
    try:
        auth = gmail_status()
    except ConfigError:
        auth = {}
    gmail_account = ""
    gmail_error = ""
    try:
        gmail_account = client_for("gmail").get_user_email()
    except NotConnected:
        pass
    except ConfigError as exc:
        gmail_error = str(exc)
    except Exception as exc:
        gmail_error = _safe_error(exc)
    kind = whatsapp.provider()
    origin = _public_origin()
    local = not os.environ.get("VERCEL")
    return {
        "gmail": {
            "account": gmail_account or ("" if gmail_error else auth.get("email", "")),
            "connected": bool(gmail_account),
            "error": gmail_error or session.get("auth_error", ""),
            "when": auth.get("authorized_text", ""),
            "stale": auth.get("stale", False) and bool(os.environ.get("VERCEL")),
            "web_ready": web_client_config() is not None,
        },
        "outlook": microsoft.status("outlook"),
        "sharepoint": microsoft.status("sharepoint"),
        "ms_ready": microsoft.configured(),
        "ms_redirect": _microsoft_redirect(),
        "ms_tenant": microsoft.tenant_text(),
        "whatsapp": {
            "provider": kind,
            "provider_label": whatsapp.provider_label(),
            "to": whatsapp.recipient(),
            "ready": whatsapp.ready(),
            "webhook": origin + "/whatsapp/webhook",
            "needs_tunnel": local,
            "verify_set": bool(os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()),
            "secret_set": bool(os.getenv("WHATSAPP_APP_SECRET", "").strip()),
            "last_webhook": get_state("wa_webhook_last", {}),
        },
        "storage": {"persistent": persistent(), "kind": storage_kind() or "This computer (data folder)", "hint": "" if storage_ready() else storage_hint()},
        "ai": {
            "ready": has_openai_key(),
            "model": active_model("worker"),
            "super_model": active_model("super"),
            "gateway": using_gateway(),
        },
        "local": local,
    }


def _provider_connected(provider: str) -> bool:
    if provider == "outlook":
        return microsoft.status("outlook")["connected"]
    cached = _inbox_cache["data"]
    if cached is not None and any(item["provider"] == "gmail" for item in cached["accounts"]):
        return True
    try:
        client_for("gmail")
    except Exception:
        return False
    return True


def _current_mode() -> str:
    try:
        if not is_live():
            return "draft"
        return load_settings()["approval_mode"]
    except ConfigError:
        return "draft"


# ---- shared helpers ----------------------------------------------------------


def _run(action) -> None:
    try:
        report = action()
        flash(format_report(report).split("\n", 1)[0].split("] ", 1)[-1])
    except ConfigError as exc:
        flash(str(exc), "error")


def _auth_failed(message: str) -> None:
    session["auth_error"] = message
    flash(message, "error")


def _login_required():
    if os.environ.get("VERCEL") and not session.get("user"):
        return redirect(url_for("login_page"))
    return None


def _mode(value) -> str:
    return value if value in {"login", "register"} else "login"


def _login_view(mode=None, email=None, sent=None, error=""):
    chosen = _mode(mode or request.args.get("mode"))
    pending = session.get("pending_email", "")
    showing_code = sent if sent is not None else request.args.get("sent") == "1"
    if showing_code and pending and chosen != session.get("pending_mode"):
        showing_code = False
    return _render(
        "login.html",
        mode=chosen,
        email=email if email is not None else (pending if showing_code else ""),
        sent=bool(showing_code and (email or pending)),
        error=error,
    )


def _profile_context(form=None) -> dict:
    from gmail_auto.cleanup import LIMIT

    settings = load_settings()
    shared = {
        "settings": settings,
        "languages": LANGUAGES,
        "approval_labels": APPROVALS,
        "modes": _MODES,
        "mode": _current_mode(),
        "whatsapp_ready": whatsapp.ready(),
        "cleanup_limit": LIMIT,
        "ping_rules": PING_RULES,
        "ping_quiet": PING_QUIET,
        "ping_interrupt": PING_INTERRUPT,
        "ping_digest": PING_DIGEST,
        "notify_preset": preset_of(settings),
        "known_mail": known_from_knowledge(),
        "ai_models": AI_MODELS,
        "ai_active": active_model("worker"),
        "ai_super": active_model("super"),
        "ai_gateway": using_gateway(),
        "ai_ready": has_openai_key(),
    }
    if form is not None:
        return {**shared, "profile": _profile_view(form=form), "placeholder": False, "error": ""}
    try:
        profile = load_profile()
    except ConfigError as exc:
        return {**shared, "profile": _profile_view(), "placeholder": True, "error": str(exc)}
    return {**shared, "profile": _profile_view(profile), "placeholder": profile_is_placeholder(profile), "error": ""}


def _profile_view(profile=None, form=None) -> dict:
    if form is not None:
        return {
            "name": form.get("name", ""),
            "role": form.get("role", ""),
            "introduction": form.get("introduction", ""),
            "tone": form.get("tone", ""),
            "cannot_promise_text": form.get("cannot_promise", ""),
            "sample_phrases_text": form.get("sample_phrases", ""),
        }
    profile = profile or {}
    return {
        "name": profile.get("name", ""),
        "role": profile.get("role", ""),
        "introduction": profile.get("introduction", ""),
        "tone": profile.get("tone", ""),
        "cannot_promise_text": "\n".join(profile.get("cannot_promise") or []),
        "sample_phrases_text": "\n".join(profile.get("sample_phrases") or []),
    }


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    lowered = text.lower()
    if not text or len(text) > 300 or "sk-" in text or "api_key" in lowered or "token" in lowered:
        return "Something went wrong while reading the mailbox."
    return text


def _render(template: str, **context):
    from flask import render_template

    return render_template(template, **context)


def _email_ok(value: str) -> bool:
    if len(value) < 6 or len(value) > 254 or "@" not in value:
        return False
    name, _, domain = value.partition("@")
    return bool(name) and "." in domain and " " not in value


def _oauth_redirect() -> str:
    return _public_origin() + "/oauth/callback"


def _microsoft_redirect() -> str:
    if not os.environ.get("VERCEL"):
        return _local_origin() + "/oauth/microsoft/callback"
    return _public_origin() + "/oauth/microsoft/callback"


def _local_origin() -> str:
    port = (request.host or "").rpartition(":")[2]
    return f"http://localhost:{port if port.isdigit() else PORT}"


def _external_url() -> str:
    path = request.full_path if request.query_string else request.path
    return _public_origin() + path


def _public_origin() -> str:
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    return f"{proto}://{host}"


def _cron_allowed() -> bool:
    if not os.environ.get("VERCEL"):
        return _request_host() in _ALLOWED_HOSTS
    secret = os.getenv("CRON_SECRET", "").strip()
    header = request.headers.get("Authorization", "")
    return bool(secret) and header == f"Bearer {secret}"


def _request_host() -> str:
    forwarded = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    raw = forwarded or (request.host or "")
    return raw.split(":")[0].strip().lower()


def _origin_host(origin: str) -> str:
    return origin.split("://", 1)[-1].split("/")[0].split(":")[0].strip().lower()


def _host_allowed(host: str) -> bool:
    if os.environ.get("VERCEL"):
        return True
    return host in _ALLOWED_HOSTS


def _secret() -> str:
    configured = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if configured:
        return configured
    if os.environ.get("VERCEL"):
        return "autogmail-vercel"
    try:
        path = data_dir() / "web_secret.txt"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_hex(32)
        atomic_write(path, value + "\n")
        return value
    except OSError:
        return secrets.token_hex(32)
