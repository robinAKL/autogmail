import html
import os
import re
import secrets
import time

from flask import Flask, Response, abort, flash, redirect, request, session, url_for
from markupsafe import Markup

from gmail_auto import approvals, knowledge, microsoft, sharepoint, stats, whatsapp
from gmail_auto.console import say
from gmail_auto.accounts import finish_verification, gmail_status, mark_gmail_authorized, start_verification
from gmail_auto.envfile import has_openai_key, openai_model
from gmail_auto.errors import ConfigError, NotConnected, named_error
from gmail_auto.extractors import SUPPORTED, extract_text
from gmail_auto.filters import Skip, classify
from gmail_auto.gmail_client import SCOPES, web_client_config
from gmail_auto.mailboxes import PROVIDERS, client_for, connected_clients
from gmail_auto.mailer import deliver_code
from gmail_auto.mailview import FRAME_POLICY, document
from gmail_auto.paths import CODE_ROOT, atomic_write, data_dir, root
from gmail_auto.profile_store import load_profile, profile_from_form, profile_is_placeholder, save_profile
from gmail_auto.runner import check_interval_seconds, format_report, run_once, switch_live
from gmail_auto.settings import APPROVALS, LANGUAGES, load_settings, save_settings
from gmail_auto.storage import persistent
from gmail_auto.token_store import storage_hint, storage_kind, storage_ready
from gmail_auto.store import is_live, list_processed, live_from_env, load_last_run, load_processed

HOST = "127.0.0.1"
PORT = 8765
_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
_OPEN_PATHS = {"/whatsapp/webhook"}
_INBOX_TTL = 45
_inbox_cache: dict = {"at": 0.0, "who": None, "data": None}
_FILTERS = {
    "all": "All",
    "needs": "Needs you",
    "replied": "Replied",
    "drafts": "Drafts",
    "skipped": "Skipped",
}
_MODES = {
    "draft": ("Draft only", "Replies are written and saved. Nothing is ever sent."),
    "always": ("Ask every time", "Every reply waits for your OK on WhatsApp or the Approvals page."),
    "smart": ("Ask when needed", "Routine replies go out by themselves. Anything with a commitment, money, a decision or a missing fact waits for your OK."),
    "never": ("Send automatically", "Every reply is sent without asking."),
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
        }

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
            return redirect(url_for("profile_page") + "#mode")
        flash(f"Sending mode is now: {_MODES[mode][0]}.")
        return redirect(url_for("profile_page") + "#mode")

    # ---- approvals -----------------------------------------------------------

    @app.get("/approvals")
    def approvals_page():
        blocked = _login_required()
        if blocked:
            return blocked
        return _render(
            "approvals.html",
            waiting=approvals.pending(),
            decided=approvals.recent(15),
            whatsapp_ready=whatsapp.ready(),
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
            if action == "send":
                text = request.form.get("draft")
                if text is not None:
                    approvals.set_draft(item_id, text)
                item = approvals.approve(item_id, "web")
                flash(f"Reply sent to {item.get('from_name') or item.get('from_email')}.")
            elif action == "save":
                approvals.set_draft(item_id, request.form.get("draft", ""))
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
            else:
                abort(404)
        except ConfigError as exc:
            flash(str(exc), "error")
        _forget_inbox()
        return back

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
        for item in request.files.getlist("files"):
            if not item or not item.filename:
                continue
            name = os.path.basename(item.filename.replace("\\", "/"))
            try:
                knowledge.add_document(name, extract_text(name, item.read()), "upload", origin=name)
                added += 1
            except ConfigError as exc:
                flash(str(exc), "error")
        if added:
            flash(f"Added {added} file{'s' if added != 1 else ''} to the knowledge base.")
        elif not request.files.getlist("files"):
            flash("Choose at least one file.", "error")
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
            entry = knowledge.learn_from_mailbox(client_for(provider), limit=30)
            flash(f"Read {entry['meta']['letters']} letters from {entry['meta']['account']} and saved what they show.")
        except ConfigError as exc:
            flash(str(exc), "error")
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
            abort(403)
        twilio = bool(request.headers.get("X-Twilio-Signature"))
        if twilio:
            if not whatsapp.twilio_signature_ok(_external_url(), request.form, request.headers.get("X-Twilio-Signature", "")):
                abort(403)
            messages = whatsapp.parse_twilio(request.form)
        else:
            if not whatsapp.meta_signature_ok(request.get_data(), request.headers.get("X-Hub-Signature-256", "")):
                abort(403)
            payload = request.get_json(silent=True)
            messages = whatsapp.parse_meta(payload if isinstance(payload, dict) else {})
        from gmail_auto import assistant

        for incoming in messages:
            if not whatsapp.is_owner(incoming.sender):
                continue
            try:
                if assistant.seen_before(incoming.message_id):
                    continue
                answer = assistant.handle(incoming)
                if answer:
                    whatsapp.send_text(answer)
            except ConfigError:
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
        return redirect(url_for("profile_page") + "#profile")

    @app.post("/settings")
    def settings_save():
        blocked = _login_required()
        if blocked:
            return blocked
        try:
            save_settings(
                {
                    "reply_language": request.form.get("reply_language", ""),
                    "notify_auto_replies": request.form.get("notify_auto_replies") == "1",
                    "daily_digest": request.form.get("daily_digest") == "1",
                    "digest_hour": request.form.get("digest_hour", "8"),
                }
            )
            flash("Reply settings saved.")
        except ConfigError as exc:
            flash(str(exc), "error")
        return redirect(url_for("profile_page") + "#replies")

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
        return "That upload is too large.", 413


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


# ---- inbox -------------------------------------------------------------------


def _home_context() -> dict:
    errors = []
    placeholder = True
    try:
        placeholder = profile_is_placeholder(load_profile())
    except ConfigError as exc:
        errors.append(str(exc))

    inbox = _inbox()
    errors.extend(inbox["errors"])
    rows = inbox["rows"]
    counts = {name: 0 for name in _FILTERS}
    for row in rows:
        counts["all"] += 1
        counts[row["group"]] = counts.get(row["group"], 0) + 1
    show = request.args.get("show", "all")
    if show not in _FILTERS:
        show = "all"
    visible = [row for row in rows if show == "all" or row["group"] == show]
    opened = None
    wanted = request.args.get("open", "")
    for row in visible:
        if row["key"] == wanted:
            opened = row
            break
    if opened is None and visible:
        opened = visible[0]

    waiting_item = None
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
        "accounts": inbox["accounts"],
        "connected": bool(inbox["accounts"]),
        "offline": inbox["offline"],
        "errors": errors,
        "rows": visible,
        "opened": opened,
        "waiting_item": waiting_item,
        "filters": _FILTERS,
        "counts": counts,
        "show": show,
        "last_run": last_run,
        "interval": _interval_text(),
        "auth_error": "" if inbox["accounts"] else session.get("auth_error", ""),
    }


def _inbox() -> dict:
    who = session.get("user", "")
    cached = _inbox_cache["data"]
    if cached is not None and _inbox_cache["who"] == who and time.time() - _inbox_cache["at"] < _INBOX_TTL:
        # Statuses change when a reply is approved, so they are always read fresh.
        _refresh_status(cached)
        return cached
    data = {"rows": [], "mails": {}, "accounts": [], "errors": [], "offline": False}
    try:
        clients, problems = connected_clients()
    except Exception as exc:
        clients, problems = [], [_safe_error(exc)]
    data["errors"].extend(problems)
    pairs = []
    for client in clients:
        label = PROVIDERS.get(getattr(client, "provider", "gmail"), "Mailbox")
        try:
            account = client.get_user_email()
            data["accounts"].append({"provider": client.provider, "label": label, "email": account})
            for mail in client.list_recent(25):
                if not mail.account:
                    mail.account = account
                pairs.append((mail, account))
        except ConfigError as exc:
            data["errors"].append(named_error(label, exc))
        except Exception as exc:
            data["errors"].append(named_error(label, _safe_error(exc)))
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
    _inbox_cache.update({"at": time.time(), "who": who, "data": data})
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


def _forget_inbox() -> None:
    _inbox_cache.update({"at": 0.0, "data": None})


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
    }


def _rows_from_store() -> list[dict]:
    try:
        records = list_processed(25)
    except ConfigError:
        return []
    rows = []
    for record in records:
        label, kind, group = _describe_record(record)
        rows.append(
            {
                "key": record.get("id", ""),
                "provider": record.get("provider") or "gmail",
                "provider_label": PROVIDERS.get(record.get("provider") or "gmail", "Mail"),
                "who": record.get("from_name") or record.get("from_email") or "Unknown sender",
                "email": record.get("from_email") or "",
                "subject": record.get("subject") or "(no subject)",
                "date": record.get("processed_at") or record.get("date") or "",
                "short_date": _short_date(record.get("processed_at") or record.get("date") or ""),
                "snippet": "",
                "unread": False,
                "status": label,
                "kind": kind,
                "group": group,
                "has_body": False,
                "reply": record.get("reply") or "",
                "summary": record.get("summary") or "",
                "reason": "",
                "sources": record.get("sources") or [],
                "outbox": record.get("outbox") or "",
                "approval_id": record.get("approval_id") or "",
                "needs_review": bool(record.get("needs_review")),
            }
        )
    return rows


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
        return "on the schedule in vercel.json"
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
        },
        "storage": {"persistent": persistent(), "kind": storage_kind() or "This computer (data folder)", "hint": "" if storage_ready() else storage_hint()},
        "ai": {"ready": has_openai_key(), "model": openai_model()},
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
    settings = load_settings()
    shared = {
        "settings": settings,
        "languages": LANGUAGES,
        "approval_labels": APPROVALS,
        "modes": _MODES,
        "mode": _current_mode(),
        "mode_locked": live_from_env() is not None,
        "whatsapp_ready": whatsapp.ready(),
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
