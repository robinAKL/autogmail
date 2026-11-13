import hashlib
import hmac
import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from gmail_auto import approvals, knowledge
from gmail_auto.models import Draft
from gmail_auto.paths import CODE_ROOT
from gmail_auto.profile_store import load_profile, save_profile
from gmail_auto.runner import run_once
from gmail_auto.settings import load_settings, save_settings
from gmail_auto.store import is_live, set_live
from gmail_auto.web import HOST, _forget_inbox, create_app
from tests.test_core import PLACEHOLDER, FakeGmail, sample

_ENV = (
    "AUTOMAIL_HOME",
    "OPENAI_API_KEY",
    "VERCEL",
    "CRON_SECRET",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_TOKEN_JSON",
    "KV_REST_API_URL",
    "KV_REST_API_TOKEN",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "WHATSAPP_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "TWILIO_ACCOUNT_SID",
)


class Inbox(FakeGmail):
    """A mailbox the pages can read, with one HTML letter in it."""

    def list_recent(self, limit=25, full=False):
        return self.mails

    def get_message(self, message_id):
        return next(mail for mail in self.mails if mail.id == message_id)


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.pop(name, None) for name in _ENV}
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        os.environ["OPENAI_API_KEY"] = "test-key-not-used"
        save_profile(
            {
                "name": "Test User",
                "role": "Tester",
                "introduction": "This introduction exists only for tests.",
                "tone": "Short and direct.",
                "cannot_promise": ["No dates"],
                "sample_phrases": ["I saw this."],
            }
        )
        _forget_inbox()
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value
        _forget_inbox()
        self.tmp.cleanup()

    def page(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        return response, response.get_data(as_text=True)


class PageTests(WebCase):
    def test_every_page_opens_in_english_and_only_on_this_computer(self):
        self.assertEqual(HOST, "127.0.0.1")
        for path, marker in (
            ("/", "Connect a mailbox to begin"),
            ("/approvals", "Nothing is waiting for you"),
            ("/dashboard", "No history yet"),
            ("/knowledge", "Reorganise knowledge"),
            ("/knowledge", "Each file under"),
            ("/knowledge", "Memory"),
            ("/knowledge?view=timeline", "Timeline"),
            ("/connections", "Connect Outlook" if False else "Outlook"),
            ("/profile", "Sending mode"),
            ("/login", "Email me a code to sign in"),
        ):
            response, text = self.page(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, text, path)
            self.assertIsNone(re.search(r"[一-鿿]", text), path)
            self.assertIn('lang="en"', text)
        remote = self.client.get("/", base_url="http://evil.example")
        self.assertEqual(remote.status_code, 403)
        forged = self.client.post("/run", headers={"Origin": "http://evil.example"})
        self.assertEqual(forged.status_code, 403)
        os.environ["VERCEL"] = "1"
        _, deployed = self.page("/", base_url="https://autogmail.vercel.app", follow_redirects=True)
        self.assertIn("Email me a code to sign in", deployed)

    def test_inbox_lists_mail_and_serves_the_letter_as_safe_formatted_html(self):
        html_mail = sample(
            id="h1",
            subject="Spring sale",
            body="Big sale",
            html='<div style="color:#c00"><h1>Big sale</h1><script>steal()</script><a href="https://shop.example.com" onclick="steal()">Shop</a></div>',
            snippet="Big sale",
            received_ts=1794387660.0,
        )
        plain_mail = sample(id="p1", subject="Lunch?", body="Lunch on <Friday>?", label_ids=["INBOX"], received_ts=1794380000.0)
        box = Inbox([html_mail, plain_mail])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, text = self.page("/")
            self.assertIn("Spring sale", text)
            self.assertIn("Lunch?", text)
            self.assertIn("Not handled yet", text)
            self.assertIn("Read, no reply", text)
            self.assertIn('src="/mail/body?key=h1"', text)
            self.assertIn("sandbox=", text)
            self.assertNotIn("<script>steal()", text)

            body = self.client.get("/mail/body?key=h1")
            page = body.get_data(as_text=True)
            self.assertIn('<h1>Big sale</h1>', page)
            self.assertIn('style="color:#c00"', page)
            self.assertNotIn("steal()", page)
            self.assertIn("default-src 'none'", body.headers["Content-Security-Policy"])
            self.assertIn("sandbox", body.headers["Content-Security-Policy"])

            _, opened = self.page("/?open=p1")
            self.assertIn('src="/mail/body?key=p1"', opened)
            plain = self.client.get("/mail/body?key=p1").get_data(as_text=True)
            self.assertIn("Lunch on &lt;Friday&gt;?", plain)

            _, needs = self.page("/?show=needs")
            self.assertIn("Spring sale", needs)
            self.assertNotIn("Lunch?", needs)

    def test_privacy_policy_is_public_on_the_hosted_site(self):
        os.environ["VERCEL"] = "1"
        os.environ["PRIVACY_CONTACT"] = "owner@example.com"
        try:
            response, text = self.page("/privacy", base_url="https://autogmail.vercel.app")
        finally:
            os.environ.pop("PRIVACY_CONTACT", None)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Privacy policy", text)
        self.assertIn("mailto:owner@example.com", text)
        self.assertNotIn("Sign out", text)

    def test_run_button_calls_one_round(self):
        with patch("gmail_auto.web.run_once") as run:
            response = self.client.post("/run")
        self.assertEqual(response.status_code, 302)
        run.assert_called_once()

    def test_sending_mode_needs_a_real_profile_and_a_confirmation(self):
        missing = self.client.post("/mode", data={"mode": "smart"}, follow_redirects=True)
        self.assertIn("Tick the confirmation box", missing.get_data(as_text=True))
        self.assertFalse(is_live())
        save_profile(PLACEHOLDER)
        refused = self.client.post("/mode", data={"mode": "smart", "confirm": "1"}, follow_redirects=True)
        self.assertIn("placeholder", refused.get_data(as_text=True))
        self.assertFalse(is_live())
        save_profile(dict(PLACEHOLDER, name="Test User", role="Tester", introduction="Real facts.", tone="Short."))
        on = self.client.post("/mode", data={"mode": "always", "confirm": "1"}, follow_redirects=True)
        self.assertIn("Ask every time", on.get_data(as_text=True))
        self.assertTrue(is_live())
        self.assertEqual(load_settings()["approval_mode"], "always")
        self.client.post("/mode", data={"mode": "draft"})
        self.assertFalse(is_live())
        self.assertEqual(self.client.post("/mode", data={"mode": "nonsense"}).status_code, 400)

    def test_profile_edit_escapes_text_and_stays_in_the_temp_home(self):
        original = (CODE_ROOT / "profile.json").read_text(encoding="utf-8")
        saved = self.client.post(
            "/profile",
            data={
                "name": "<b>New name</b>",
                "role": "New role",
                "introduction": "A new introduction for the test.",
                "tone": "Calm.",
                "cannot_promise": "No dates\nNo phone numbers",
                "sample_phrases": "I saw this.",
            },
            follow_redirects=True,
        )
        page = saved.get_data(as_text=True)
        self.assertIn("Profile saved", page)
        self.assertEqual(load_profile()["name"], "<b>New name</b>")
        self.assertEqual(load_profile()["cannot_promise"], ["No dates", "No phone numbers"])
        self.assertIn("&lt;b&gt;New name&lt;/b&gt;", page)
        self.assertNotIn("<b>New name</b>", page)
        self.assertEqual((CODE_ROOT / "profile.json").read_text(encoding="utf-8"), original)
        blank = self.client.post(
            "/profile",
            data={"name": "", "role": "r", "introduction": "i", "tone": "t", "cannot_promise": "x", "sample_phrases": "y"},
        )
        self.assertEqual(blank.status_code, 400)
        self.assertIn("Enter your name", blank.get_data(as_text=True))
        self.client.post("/settings", data={"reply_language": "bilingual", "daily_digest": "1", "digest_hour": "7"})
        settings = load_settings()
        self.assertEqual((settings["reply_language"], settings["daily_digest"], settings["notify_auto_replies"], settings["digest_hour"]), ("bilingual", True, False, 7))


class ApprovalPageTests(WebCase):
    def test_a_waiting_reply_can_be_edited_and_sent_or_dropped_from_the_page(self):
        set_live(True)
        fake = FakeGmail([sample(id="w1", subject="Meet Friday?"), sample(id="w2", subject="Quote please")])
        run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Friday at 2pm works.", reason="Commits to a time.", summary="Wants to meet."))
        _, text = self.page("/approvals")
        self.assertIn("Meet Friday?", text)
        self.assertIn("Commits to a time.", text)
        self.assertIn("Friday at 2pm works.", text)
        self.assertIn('<b class="badge">2</b>', text)
        first = next(item for item in approvals.pending() if item["mail_id"] == "w1")
        second = next(item for item in approvals.pending() if item["mail_id"] == "w2")
        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            sent = self.client.post(f"/approvals/{first['id']}/send", data={"draft": "Friday at 3pm works."}, follow_redirects=True)
        self.assertIn("Reply sent to Lee", sent.get_data(as_text=True))
        self.assertEqual(fake.sent, [("w1", "Friday at 3pm works.", "me@example.com")])
        dropped = self.client.post(f"/approvals/{second['id']}/reject", follow_redirects=True)
        page = dropped.get_data(as_text=True)
        self.assertIn("Nothing was sent", page)
        self.assertIn("Recent decisions", page)
        self.assertEqual(len(fake.sent), 1)
        again = self.client.post(f"/approvals/{first['id']}/send", follow_redirects=True)
        self.assertIn("no longer waiting", again.get_data(as_text=True))
        outside = self.client.post(f"/approvals/{first['id']}/reject", data={"next": "https://evil.example/"})
        self.assertEqual(outside.headers["Location"], "/approvals")

        _, board = self.page("/dashboard")
        self.assertIn("What happened to each day", board)
        self.assertIn("View as a table", board)


class KnowledgePageTests(WebCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("OPENAI_API_KEY", None)

    def test_files_notes_and_rules_build_the_knowledge_base(self):
        with patch("gmail_auto.knowledge.complete") as mocked:
            uploaded = self.client.post(
                "/knowledge/upload",
                data={"files": [(io.BytesIO(b"The October workshop costs 450 dollars."), "prices.txt"), (io.BytesIO(b"\x89PNG"), "photo.png")]},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            mocked.assert_not_called()
        text = uploaded.get_data(as_text=True)
        self.assertIn("Added 1 file", text)
        self.assertIn("not a supported file type", text)
        quick = self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"The October workshop costs 450 dollars."), "prices.txt")},
            content_type="multipart/form-data",
            headers={"Accept": "application/json"},
        )
        self.assertEqual(quick.status_code, 200)
        self.assertTrue(quick.get_json()["ok"])
        self.assertIn("prices.txt", text)
        self.client.post("/knowledge/note", data={"title": "Office hours", "text": "I answer mail between 9 and 5."})
        self.client.post("/knowledge/rules", data={"rules": "Never reply to recruiters.\nAlways ask before agreeing to a price."})
        self.assertEqual(len(knowledge.list_documents()), 2)
        self.assertEqual(len(knowledge.load_index()["rules"]), 2)
        _, found = self.page("/knowledge?q=workshop+cost")
        self.assertIn("450 dollars", found)
        doc = next(item for item in knowledge.list_documents() if item["title"] == "prices.txt")
        _, shown = self.page(f"/knowledge/doc/{doc['id']}")
        self.assertIn("The October workshop costs 450 dollars.", shown)
        self.client.post(f"/knowledge/doc/{doc['id']}/delete")
        self.assertEqual(len(knowledge.list_documents()), 1)
        self.assertEqual(self.client.get(f"/knowledge/doc/{doc['id']}").status_code, 404)
        saved = self.client.post("/knowledge/brief", data={"text": "## Who I am\n- **Tester** <script>x</script>"}, follow_redirects=True)
        page = saved.get_data(as_text=True)
        self.assertIn("<h4>Who I am</h4>", page)
        self.assertIn("<strong>Tester</strong> &lt;script&gt;", page)

    def test_sharepoint_and_outlook_explain_what_is_missing(self):
        _, text = self.page("/connections")
        self.assertIn("MS_CLIENT_ID", text)
        self.assertIn("/oauth/microsoft/callback", text)
        self.assertIn("/whatsapp/webhook", text)
        start = self.client.get("/oauth/microsoft/outlook", follow_redirects=True)
        self.assertIn("MS_CLIENT_ID", start.get_data(as_text=True))
        os.environ["MS_CLIENT_ID"] = "client-id-for-tests"
        os.environ["MS_CLIENT_SECRET"] = "client-secret-for-tests"
        hop = self.client.get("/oauth/microsoft/sharepoint", base_url="http://127.0.0.1:8765")
        self.assertEqual(hop.headers["Location"], "http://localhost:8765/oauth/microsoft/sharepoint")
        go = self.client.get("/oauth/microsoft/sharepoint", base_url="http://localhost:8765")
        self.assertEqual(go.status_code, 302)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Foauth%2Fmicrosoft%2Fcallback", go.headers["Location"])
        self.assertIn("login.microsoftonline.com", go.headers["Location"])
        self.assertIn("Sites.Read.All", go.headers["Location"])
        with self.client.session_transaction() as current:
            current["ms_state"] = {"state": "s0", "kind": "outlook"}
        refused = self.client.get(
            "/oauth/microsoft/callback?state=s0&error=access_denied&error_description="
            "AADSTS65004%3A+User+declined+to+consent+to+access+the+app.%0D%0ATrace+ID%3A+abc",
            follow_redirects=True,
        )
        refused_text = refused.get_data(as_text=True)
        self.assertIn("Outlook was not connected", refused_text)
        self.assertIn("Grant admin consent", refused_text)
        self.assertIn("AADSTS65004", refused_text)
        self.assertNotIn("Trace ID", refused_text)
        wrong = self.client.get("/oauth/microsoft/callback?state=forged&code=abc", follow_redirects=True)
        self.assertIn("could not be matched", wrong.get_data(as_text=True))
        with patch("gmail_auto.microsoft.finish_sign_in", return_value="me@contoso.com") as finish:
            with self.client.session_transaction() as current:
                current["ms_state"] = {"state": "s1", "kind": "outlook"}
            done = self.client.get("/oauth/microsoft/callback?state=s1&code=abc", follow_redirects=True)
        finish.assert_called_once()
        self.assertIn("Outlook is connected as me@contoso.com", done.get_data(as_text=True))
        linked = self.client.post("/knowledge/sharepoint", data={"url": "https://contoso.sharepoint.com/sites/Team"}, follow_redirects=True)
        self.assertIn("SharePoint is not connected yet", linked.get_data(as_text=True))


class WhatsAppWebhookTests(WebCase):
    def setUp(self):
        super().setUp()
        os.environ["WHATSAPP_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234567890"
        os.environ["WHATSAPP_APP_SECRET"] = "app-secret-for-tests"
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "verify-token-for-tests"
        save_settings({"whatsapp_to": "+64 21 123 4567", "daily_digest": False})
        self.out = []
        self.patch = patch("gmail_auto.whatsapp._meta_send", lambda to, message: self.out.append(message) or "wamid.out")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def deliver(self, sender, text, message_id, sign=True):
        payload = {"entry": [{"changes": [{"value": {"messages": [{"from": sender, "id": message_id, "type": "text", "text": {"body": text}}]}}]}]}
        raw = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"app-secret-for-tests", raw, hashlib.sha256).hexdigest()
        return self.client.post(
            "/whatsapp/webhook",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": signature if sign else "sha256=bad"},
            base_url="https://tunnel.example",
        )

    def test_owner_says_yes_on_whatsapp_and_the_reply_goes_out(self):
        check = self.client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=verify-token-for-tests&hub.challenge=42", base_url="https://tunnel.example")
        self.assertEqual(check.get_data(as_text=True), "42")
        self.assertEqual(self.client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=42").status_code, 403)

        set_live(True)
        fake = FakeGmail([sample(id="w1", subject="Meet Friday?")])
        run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Friday at 2pm works.", reason="Commits to a time."))
        self.out.clear()

        self.assertEqual(self.deliver("64211234567", "yes", "wamid.in1", sign=False).status_code, 403)
        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            self.assertEqual(self.deliver("64219999999", "yes", "wamid.in2").status_code, 200)
            self.assertEqual(fake.sent, [])
            self.assertEqual(self.out, [])
            self.assertEqual(self.deliver("64211234567", "yes", "wamid.in3").status_code, 200)
            self.assertEqual(self.deliver("64211234567", "yes", "wamid.in3").status_code, 200)
        self.assertEqual(fake.sent, [("w1", "Friday at 2pm works.", "me@example.com")])
        self.assertEqual(len(self.out), 1)
        self.assertIn("Sent to Lee", self.out[0]["text"]["body"])

    def test_number_is_saved_and_a_test_message_can_be_sent(self):
        bad = self.client.post("/connections/whatsapp", data={"whatsapp_to": "123"}, follow_redirects=True)
        self.assertIn("country code", bad.get_data(as_text=True))
        self.client.post("/connections/whatsapp", data={"whatsapp_to": "+64 21 765 4321"})
        self.assertEqual(load_settings()["whatsapp_to"], "64217654321")
        sent = self.client.post("/connections/whatsapp", data={"test": "1"}, follow_redirects=True)
        self.assertIn("Test message sent", sent.get_data(as_text=True))
        self.assertIn("Mail Autopilot is connected", self.out[-1]["text"]["body"])


class SignInTests(WebCase):
    def test_oauth_without_client_id_explains_the_missing_setting(self):
        page = self.client.get("/oauth", follow_redirects=True)
        self.assertIn("GMAIL_CLIENT_ID", page.get_data(as_text=True))

    def test_register_verifies_the_code_then_login_uses_it(self):
        sent = {}
        _, text = self.page("/login")
        self.assertNotIn("assword", text)
        rejected = self.client.post("/login/code", data={"email": "not an email", "mode": "register"})
        self.assertEqual(rejected.status_code, 400)
        with patch("gmail_auto.web.deliver_code", lambda _to, code: sent.update(code=code)):
            started = self.client.post("/login/code", data={"email": "lee@example.com", "mode": "register"}, follow_redirects=True)
        self.assertIn("A code was sent to lee@example.com", started.get_data(as_text=True))
        wrong = self.client.post("/login", data={"email": "lee@example.com", "mode": "register", "code": "000000"})
        self.assertEqual(wrong.status_code, 400)
        done = self.client.post("/login", data={"email": "lee@example.com", "mode": "register", "code": sent["code"]}, follow_redirects=True)
        self.assertIn("You are signed in", done.get_data(as_text=True))
        again = self.client.post("/login/code", data={"email": "lee@example.com", "mode": "register"})
        self.assertIn("already registered", again.get_data(as_text=True))
        missing = self.client.post("/login/code", data={"email": "new@example.com", "mode": "login"})
        self.assertIn("not registered yet", missing.get_data(as_text=True))

    def test_verification_code_does_not_need_vercel_kv(self):
        os.environ["VERCEL"] = "1"
        sent = {}
        with patch("gmail_auto.web.deliver_code", lambda _to, code: sent.update(code=code)):
            started = self.client.post("/login/code", data={"email": "lee@example.com", "mode": "register"}, follow_redirects=True)
        self.assertNotIn("Vercel KV", started.get_data(as_text=True))
        done = self.client.post("/login", data={"email": "lee@example.com", "mode": "register", "code": sent["code"]}, follow_redirects=True)
        text = done.get_data(as_text=True)
        self.assertIn("You are signed in", text)
        _, connections = self.page("/connections")
        self.assertIn("has no storage connected", connections)

    def test_oauth_keeps_the_grant_in_the_session_without_kv(self):
        os.environ["GMAIL_CLIENT_ID"] = "test.apps.googleusercontent.com"
        os.environ["GMAIL_CLIENT_SECRET"] = "test-secret"

        class _Creds:
            refresh_token = "refresh-test"

            def to_json(self):
                return '{"refresh_token":"refresh-test","client_id":"test","client_secret":"test-secret","token":"access"}'

        class _Flow:
            credentials = _Creds()

            def fetch_token(self, authorization_response):
                return None

        with self.client.session_transaction() as current:
            current["oauth_state"] = "state-test"
            current["user"] = "lee@example.com"
        box = Inbox([], user="lee@example.com")
        with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=_Flow()), patch(
            "gmail_auto.gmail_client.GmailClient", return_value=box
        ), patch("gmail_auto.web.connected_clients", return_value=([box], [])):
            page = self.client.get("/oauth/callback?state=state-test&code=abc", follow_redirects=True)
        text = page.get_data(as_text=True)
        self.assertNotIn("Vercel KV", text)
        self.assertIn("Gmail is connected", text)
        self.assertIn("lee@example.com", text)
        with self.client.session_transaction() as current:
            self.assertIn("refresh-test", current.get("gmail_token", ""))

    def test_cron_on_vercel_requires_the_secret(self):
        os.environ["VERCEL"] = "1"
        os.environ["CRON_SECRET"] = "cron-test-secret"
        blocked = self.client.get("/cron")
        allowed = self.client.get("/cron", headers={"Authorization": "Bearer cron-test-secret"})
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(allowed.get_json()["ok"])


class OnceModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.pop(name, None) for name in ("AUTOMAIL_HOME", "GMAIL_TOKEN_JSON")}
        os.environ["AUTOMAIL_HOME"] = self.tmp.name

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_once_without_credentials_does_not_open_a_browser(self):
        import main

        before = (CODE_ROOT / "profile.json").read_text(encoding="utf-8")
        code = main.main(["--once"])
        self.assertEqual(code, 1)
        self.assertEqual((CODE_ROOT / "profile.json").read_text(encoding="utf-8"), before)
