import hashlib
import hmac
import io
import json
import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from gmail_auto import approvals, knowledge
from gmail_auto.errors import ConfigError
from gmail_auto.models import Draft
from gmail_auto.paths import CODE_ROOT
from gmail_auto.profile_store import load_profile, save_profile
from gmail_auto.runner import run_once
from gmail_auto.settings import load_settings, save_settings
from gmail_auto.store import is_live, mark_processed, set_live
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
    "AUTOMAIL_SAAS",
    "ALLOWED_EMAILS",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "RESEND_API_KEY",
    "MAIL_FROM",
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
            ("/profile", "Inbox cleanup"),
            ("/profile", "important mail"),
            ("/profile", "How far it sends"),
            ("/profile", "When to ping me"),
            ("/profile", "Who it sounds like"),
            ("/profile", "from:linkedin.com"),
            ("/calendar", "From email"),
            ("/calendar", "Mon"),
            ("/login", "Email me a code to sign in"),
        ):
            response, text = self.page(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(marker, text, path)
            # The language switch offers 中文; everything else is English until it is chosen.
            outside_switch = re.sub(r'(?s)<form[^>]*class="lang-switch".*?</form>', "", text)
            self.assertIsNone(re.search(r"[一-鿿]", outside_switch), path)
            self.assertIn('lang="en"', text)
            if path != "/login":
                self.assertIn('class="side"', text, path)
                self.assertIn(">Inbox</span>", text, path)
        remote = self.client.get("/", base_url="http://evil.example")
        self.assertEqual(remote.status_code, 403)
        forged = self.client.post("/run", headers={"Origin": "http://evil.example"})
        self.assertEqual(forged.status_code, 403)
        os.environ["VERCEL"] = "1"
        _, deployed = self.page("/", base_url="https://autogmail.vercel.app", follow_redirects=True)
        self.assertIn("Email me a code to sign in", deployed)

    def test_calendar_day_agenda_shows_count_and_compact_rows_for_many_events(self):
        events = [
            {
                "date": "2026-11-15",
                "when": "09:00–10:00",
                "hour": "09:00",
                "title": f"Fake meeting #{i}",
                "provider": "gmail",
                "provider_label": "Gmail",
                "link": f"https://calendar.google.com/event?eid={i}",
                "location": "Ponsonby Dental, 12 Example Rd" if i == 1 else "",
                "attendees": ["a@b.com"] if i == 1 else [],
            }
            for i in range(1, 29)
        ]
        board = {
            "year": 2026,
            "month": 11,
            "title": "November 2026",
            "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "weeks": [],
            "accounts": [],
            "errors": [],
            "today": "2026-11-15",
            "selected": "2026-11-15",
            "selected_label": "Sunday 15 November",
            "selected_events": events,
            "prev": {"year": 2026, "month": 10},
            "next": {"year": 2026, "month": 12},
            "this": {"year": 2026, "month": 11, "day": "2026-11-15"},
            "count": 28,
        }
        with patch("gmail_auto.calendars.month_board", return_value=board):
            response, text = self.page("/calendar?year=2026&month=11&day=2026-11-15")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<span class="chip tiny">28</span>', text)
        self.assertIn('class="cal-agenda"', text)
        self.assertIn('class="cal-dot gmail"', text)
        self.assertIn('>09:00<', text)
        self.assertIn('class="cal-item-title"', text)
        self.assertIn('<a href="https://calendar.google.com/event?eid=28"', text)
        self.assertIn("Fake meeting #28", text)
        self.assertIn("Ponsonby Dental, 12 Example Rd", text)
        self.assertIn("1 invited", text)

    def test_calendar_source_switch_keeps_the_day_and_shows_which_provider_failed(self):
        from datetime import datetime
        from gmail_auto.calendars import ZONE, _item

        gmail = _item("g1", "gmail", "Parent interview", datetime(2026, 11, 13, 15, 0, tzinfo=ZONE), datetime(2026, 11, 13, 16, 0, tzinfo=ZONE), "", [], "")
        outlook = _item("o1", "outlook", "Site visit", datetime(2026, 11, 13, 9, 0, tzinfo=ZONE), datetime(2026, 11, 13, 10, 0, tzinfo=ZONE), "", [], "")
        with patch("gmail_auto.calendars._gmail_events", return_value=([gmail], "me@example.com")), patch(
            "gmail_auto.calendars._outlook_events", return_value=([outlook], "robin@example.com")
        ):
            response, text = self.page("/calendar?year=2026&month=11&day=2026-11-13&provider=outlook")
        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-current="page"', text)
        self.assertIn("Site visit", text)
        self.assertNotIn("Parent interview", text)
        self.assertIn("day=2026-11-13&amp;provider=outlook", text)
        self.assertIn(">2<", text)
        self.assertIn(">1<", text)

        with patch("gmail_auto.calendars._gmail_events", return_value=([gmail], "me@example.com")), patch(
            "gmail_auto.calendars._outlook_events", side_effect=ConfigError("Calendar permission was not granted.")
        ):
            _, failed = self.page("/calendar?year=2026&month=11&day=2026-11-13")
        self.assertIn("Outlook: Calendar permission was not granted.", failed)
        self.assertIn('title="Could not read this calendar"', failed)
        self.assertIn(">—<", failed)

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
            _, text = self.page("/?show=all")
            self.assertIn("Spring sale", text)
            self.assertIn("Lunch?", text)
            self.assertIn("Not handled yet", text)
            self.assertIn("Read, no reply", text)
            self.assertIn('chip tiny gmail', text)
            self.assertNotIn("Older", text)
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

            _, opened = self.page("/?show=all&open=p1")
            self.assertIn('src="/mail/body?key=p1"', opened)
            plain = self.client.get("/mail/body?key=p1").get_data(as_text=True)
            self.assertIn("Lunch on &lt;Friday&gt;?", plain)

            _, needs = self.page("/?show=needs")
            self.assertIn("Spring sale", needs)
            self.assertNotIn('row-subject">Lunch?', needs)
            self.assertIn('class="tab on" href="/?show=todo"', needs)
            self.assertIn("Notices", needs)
            self.assertNotIn("In progress", needs)

    def test_inbox_story_timeline_only_when_the_same_matter_has_more_than_one_letter(self):
        first = sample(
            id="q1",
            thread_id="quote-thread",
            from_email="lee@client.example",
            from_name="Lee",
            subject="Quote for May",
            snippet="Please quote May",
            received_ts=1794300000.0,
            date="2026-11-01 10:00",
            label_ids=["INBOX"],
        )
        follow = sample(
            id="q2",
            thread_id="quote-thread",
            from_email="lee@client.example",
            from_name="Lee",
            subject="Re: Quote for May",
            snippet="Can you do better on price",
            received_ts=1794380000.0,
            date="2026-11-02 10:00",
            label_ids=["INBOX", "UNREAD"],
        )
        mark_processed(
            {
                "id": "q1",
                "provider": "gmail",
                "thread_id": "quote-thread",
                "from_name": "Lee",
                "from_email": "lee@client.example",
                "subject": "Quote for May",
                "date": "2026-11-01 10:00",
                "processed_at": "2026-11-01 10:00",
                "action": "sent",
                "reply": "Please find the quote attached.",
                "summary": "Asked for a May quote.",
                "sources": ["Price list"],
            }
        )
        box = Inbox([first, follow])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, one = self.page("/?show=all&open=q2")
            self.assertIn("Storylines", one)
            self.assertIn("Open storyline", one)
            self.assertIn("Waiting on you", one)
            self.assertIn('chip tiny story', one)
            _, drawer = self.page("/?show=all&open=q2&story=gmail:t:quote-thread")
            self.assertIn("matter-drawer", drawer)
            self.assertIn("Please find the quote attached.", drawer)
            self.assertIn("Price list", drawer)
            self.assertIn("Related mail", drawer)
            gone = self.client.post("/inbox/story/hide", data={"story": "gmail:t:quote-thread", "next": "/?show=all"}, follow_redirects=True)
            self.assertNotIn("Open storyline", gone.get_data(as_text=True))
            _, open_tab = self.page("/?show=open")
            self.assertIn("Quote for May", open_tab)
            self.assertIn("Can you do better", open_tab)
            lone = sample(id="solo", thread_id="solo-thread", subject="Lunch?", snippet="Friday?", received_ts=1794390000.0, label_ids=["INBOX"])
            box.mails = [lone]
            _forget_inbox()
            _, plain = self.page("/?show=all&open=solo")
            self.assertNotIn("Open storyline", plain)
            self.assertIn("Lunch?", plain)

    def test_inbox_opens_on_important_and_saved_letters_still_show_their_text(self):
        from gmail_auto.settings import save_settings

        records = [
            ("bill", "noreply@mercury.co.nz", "Your power bill is ready", "Amount due $182 by 30 November.", "system", False),
            ("jobs", "jobalerts-noreply@linkedin.com", "You may be a fit for a role", "Job alert.", "system", False),
            ("promo", "news@shop.example", "Big sale ends tonight", "50% off. Unsubscribe.", "list", False),
            ("code", "", "", "", "sensitive", True),
        ]
        for key, sender, subject, summary, code, sensitive in records:
            mark_processed(
                {
                    "id": key,
                    "provider": "gmail",
                    "from_email": sender,
                    "subject": subject,
                    "summary": summary,
                    "date": "2026-11-14 09:00",
                    "processed_at": "2026-11-14 09:00",
                    "action": "skipped",
                    "skip_code": code,
                    "sensitive": sensitive,
                }
            )
        mark_processed(
            {
                "id": "asked",
                "provider": "gmail",
                "from_name": "Lee",
                "from_email": "lee@client.example",
                "subject": "Workshop next week",
                "date": "2026-11-14 10:00",
                "processed_at": "2026-11-14 10:00",
                "action": "waiting",
            }
        )
        saved = sample(id="bill", subject="Your power bill is ready", body="Amount due $182 by 30 November.", from_email="noreply@mercury.co.nz")
        box = Inbox([saved])
        with patch("gmail_auto.web.connected_clients", return_value=([], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, first = self.page("/")
            self.assertIn('class="tab on" href="/?show=all"', first)
            self.assertIn("Your power bill is ready", first)
            self.assertIn("Big sale", first)

            _, text = self.page("/?show=todo")
            self.assertIn('class="tab on" href="/?show=todo"', text)
            self.assertIn("Workshop next week", text)
            self.assertNotIn("Your power bill is ready", text)
            self.assertNotIn("You may be a fit", text)
            self.assertNotIn("Big sale", text)
            self.assertIn("This list comes from saved records", text)
            self.assertNotIn("could not be reached", text)

            _, notices = self.page("/?show=notices")
            self.assertIn("Your power bill is ready", notices)
            self.assertNotIn("Workshop next week", notices)
            self.assertNotIn("You may be a fit", notices)
            self.assertNotIn("Big sale", notices)

            _, opened = self.page("/?show=notices&open=bill")
            self.assertIn('src="/mail/body?key=bill"', opened)
            self.assertIn('action="/mail/reply"', opened)
            self.assertIn("Amount due $182", self.client.get("/mail/body?key=bill").get_data(as_text=True))

            _, every = self.page("/?show=all")
            self.assertIn("You may be a fit", every)
            self.assertIn("Big sale", every)
            _, secret = self.page("/?show=all&open=code")
            self.assertNotIn('src="/mail/body?key=code"', secret)
            self.assertIn("passwords, codes or ID", secret)

        save_settings({"notify_kinds": ["fine"]})
        with patch("gmail_auto.web.connected_clients", return_value=([], [])):
            _, text = self.page("/?show=notices")
        self.assertNotIn("Your power bill is ready", text)
        with patch("gmail_auto.web.connected_clients", return_value=([], [])):
            _, old_link = self.page("/?show=important")
        self.assertIn("Workshop next week", old_link)

    def test_inbox_pages_older_history_and_keeps_mailbox_marks(self):
        live = sample(id="now", subject="Today's letter", received_ts=1794387660.0, date="2026-11-13 18:00")
        for index in range(25):
            mark_processed(
                {
                    "id": f"old{index}",
                    "provider": "outlook",
                    "from_name": "School",
                    "from_email": "school@example.com",
                    "subject": f"History {index}",
                    "date": f"2026-01-01 10:{index:02d}",
                    "processed_at": f"2026-01-01 10:{index:02d}",
                    "action": "skipped",
                    "skip_code": "no_reply_needed",
                }
            )
        box = Inbox([live])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, text = self.page("/?show=all")
            self.assertIn("Today&#39;s letter", text)
            self.assertIn("chip tiny gmail", text)
            self.assertIn("Older", text)
            self.assertIn("1 / 2", text)
            self.assertNotIn("History 0", text)
            _, older = self.page("/?show=all&page=2")
            self.assertIn("History 0", older)
            self.assertIn("chip tiny outlook", older)
            self.assertIn("Newer", older)
            self.assertNotIn("Today&#39;s letter", older)

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

    def test_calendar_offers_to_remove_items_added_from_email(self):
        from gmail_auto.storage import load_doc, save_doc

        _, text = self.page("/calendar")
        self.assertNotIn("Remove items added from email", text)
        save_doc("calendar_mail", {"m1": {"event_id": "ev1", "at": "2026-11-14 09:00"}, "m2": {"event_id": "ev2", "at": "2026-11-14 09:00", "auto": False}})
        _, text = self.page("/calendar")
        self.assertIn("Remove items added from email", text)
        self.assertIn("1 calendar items were added from email", text)
        removed = []
        with patch("gmail_auto.calendars._delete_event", lambda provider, event_id: removed.append(event_id)):
            response = self.client.post("/calendar/remove-added", follow_redirects=True)
        self.assertEqual(removed, ["ev1"])
        self.assertIn("Removed 1 calendar items", response.get_data(as_text=True))
        self.assertEqual(load_doc("calendar_mail", {})["m2"]["event_id"], "ev2")
        self.assertNotIn("Remove items added from email", self.page("/calendar")[1])

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
        self.assertIn("Show me every reply", on.get_data(as_text=True))
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
        self.assertEqual((settings["reply_language"], settings["daily_digest"], settings["notify_auto_replies"], settings["digest_hour"]), ("bilingual", True, True, 7))
        self.client.post(
            "/settings/cleanup-rules",
            data={
                "cleanup_match": "from:jobs.example.com\njob alert",
                "cleanup_keep": "from:etap.co.nz\nwestlake",
                "cleanup_skipped": "1",
            },
        )
        rules = load_settings()
        self.assertEqual(rules["cleanup_match"], ["from:jobs.example.com", "job alert"])
        self.assertEqual(rules["cleanup_keep"], ["from:etap.co.nz", "westlake"])
        self.assertTrue(rules["cleanup_skipped"])
        box = Inbox(
            [
                sample(id="j1", from_email="alerts@jobs.example.com", subject="New roles"),
                sample(id="s1", from_email="office@etap.co.nz", subject="School newsletter"),
                sample(id="p1", from_email="lee@example.com", subject="Lunch?"),
            ]
        )
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])):
            cleaned = self.client.post("/settings/cleanup", follow_redirects=True)
        self.assertIn("Deleted 1 letters to Trash", cleaned.get_data(as_text=True))
        self.assertEqual(box.archived, ["j1"])


class ApprovalPageTests(WebCase):
    def test_a_waiting_reply_can_be_edited_and_sent_or_dropped_from_the_page(self):
        set_live(True)
        fake = FakeGmail([sample(id="w1", subject="Meet Friday?"), sample(id="w2", subject="Quote please")])
        run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Friday at 2pm works.", reason="Commits to a time.", summary="Wants to meet."))
        _, text = self.page("/approvals")
        self.assertIn("Meet Friday?", text)
        self.assertIn("Commits to a time.", text)
        self.assertIn("Friday at 2pm works.", text)
        self.assertIn('title="Drafts waiting for your OK">2</b>', text)
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

    def test_approval_translates_an_explicit_first_language_edit(self):
        from gmail_auto.settings import save_settings

        save_settings({"first_language": "en", "translation_mode": "seamless", "translate_outgoing": True})
        mail = sample(id="french-reply", subject="Bonjour")
        fake = FakeGmail([mail])
        item = approvals.create(mail, Draft(action="ask", reply="Bonjour, merci.", reply_lang="fr"), "fr")
        _, page = self.page("/approvals")
        self.assertIn("Translate my edit from EN to FR", page)

        url = f"/approvals/{item['id']}/send"
        data = {"draft": "Thank you. The price is $50.", "translate_draft": "1"}
        with patch("gmail_auto.mailboxes.client_for", return_value=fake), patch(
            "gmail_auto.translator.model_ready", return_value=True
        ), patch("gmail_auto.translator.complete", side_effect=["Merci.", "Merci. Le prix est $50."]):
            rejected = self.client.post(url, data=data, follow_redirects=True)
            self.assertIn("translation changed", rejected.get_data(as_text=True))
            self.assertFalse(fake.sent)
            self.assertEqual(approvals.get(item["id"])["draft"], "Bonjour, merci.")
            self.client.post(url, data=data, follow_redirects=True)
        self.assertEqual(fake.sent[0][1], "Merci. Le prix est $50.")


        _, board = self.page("/dashboard")
        self.assertIn("What happened to each day", board)
        self.assertIn("View as a table", board)


class HandledTests(WebCase):
    def test_a_notice_marked_handled_leaves_notices_and_can_come_back(self):
        mark_processed(
            {
                "id": "bill",
                "provider": "gmail",
                "from_email": "noreply@mercury.co.nz",
                "subject": "Your power bill is ready",
                "summary": "Amount due $182 by 30 November.",
                "date": "2026-11-14 09:00",
                "processed_at": "2026-11-14 09:00",
                "action": "skipped",
                "skip_code": "system",
            }
        )
        box = Inbox([sample(id="bill", subject="Your power bill is ready", body="Amount due $182 by 30 November.", from_email="noreply@mercury.co.nz")])
        with patch("gmail_auto.web.connected_clients", return_value=([], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, before = self.page("/?show=notices&open=bill")
            self.assertIn('value="1" title="Paid, read or done', before)
            done = self.client.post("/mail/handled", data={"key": "bill", "handled": "1", "show": "notices"}, follow_redirects=True)
            self.assertIn("Marked as handled", done.get_data(as_text=True))
            _, notices = self.page("/?show=notices")
            self.assertNotIn("Your power bill is ready", notices)
            _, every = self.page("/?show=all&open=bill")
            self.assertIn("Handled 20", every)
            self.assertIn("Not handled yet", every)
            self.client.post("/mail/handled", data={"key": "bill", "handled": "0"})
            _, again = self.page("/?show=notices")
            self.assertIn("Your power bill is ready", again)

    def test_closing_a_whatsapp_notice_counts_as_handled_not_declined(self):
        from gmail_auto.models import Draft

        mail = sample(id="n1", subject="Rates due", from_email="rates@council.example")
        item = approvals.create(mail, Draft(action="skip", summary="Rates due Friday"), "en", role="notice", kind="invoice")
        mark_processed({"id": "n1", "from_email": "rates@council.example", "subject": "Rates due", "action": "skipped", "approval_id": item["id"], "processed_at": "2026-11-14 09:00"})
        approvals.reject(item["id"], "whatsapp")
        from gmail_auto.store import get_processed

        record = get_processed("n1")
        self.assertEqual((record["action"], record["handled"]), ("skipped", True))
        self.assertEqual(approvals.get(item["id"])["status"], "handled")

    def test_a_letter_the_program_never_handled_can_be_marked_too(self):
        box = Inbox([sample(id="fresh", subject="School trip form", body="Please sign by Friday.", label_ids=["INBOX", "UNREAD"])])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            self.client.post("/mail/handled", data={"key": "fresh", "handled": "1"})
            _, todo = self.page("/?show=todo")
        self.assertNotIn("School trip form", todo)
        from gmail_auto.store import get_processed

        self.assertTrue(get_processed("fresh")["handled"])


class ReplyButtonTests(WebCase):
    def test_reply_on_any_letter_writes_a_draft_that_waits_then_sends(self):
        letter = sample(id="r1", subject="Course price?", body="How much is the spring course?", label_ids=["INBOX"])
        box = Inbox([letter])
        written = []

        def fake_write(mail, profile, language):
            written.append(mail.id)
            return Draft(action="reply", reply="The spring course is in the price list.", reason="", sources=["Price list"])

        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box), patch(
            "gmail_auto.generator.draft_reply", side_effect=fake_write
        ):
            _, before = self.page("/?show=all&open=r1")
            self.assertIn('action="/mail/reply"', before)
            self.assertNotIn("Reply waiting for your OK", before)
            made = self.client.post("/mail/reply", data={"key": "r1", "show": "all"})
            self.assertEqual(made.headers["Location"], "/?show=todo&open=r1")
            _, page = self.page(made.headers["Location"])
            self.assertIn("Reply waiting for your OK", page)
            self.assertIn("The spring course is in the price list.", page)
            self.assertIn('name="instruction"', page)
            self.assertNotIn('action="/mail/reply"', page)
            self.assertIn('title="Drafts waiting for your OK">1</b>', page)
            # Pressing Reply again keeps the one waiting draft instead of writing a second.
            self.client.post("/mail/reply", data={"key": "r1"})
        self.assertEqual(written, ["r1"])
        waiting = approvals.pending()
        self.assertEqual(len(waiting), 1)
        with patch("gmail_auto.mailboxes.client_for", return_value=box):
            self.client.post(f"/approvals/{waiting[0]['id']}/send", data={"draft": "Spring is $300.", "next": "/?show=replied&open=r1"})
        self.assertEqual(box.sent, [("r1", "Spring is $300.", box.get_user_email())])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, done = self.page("/?show=replied&open=r1")
        self.assertIn("Replied after your OK", done)
        self.assertIn("Reply again", done)

    def test_points_write_the_draft_and_failures_are_shown(self):
        letter = sample(id="r2", subject="Meet?", body="Can we meet next week?", label_ids=["INBOX"])
        box = Inbox([letter])
        asked = []

        def fake_rewrite(item, instruction, profile):
            asked.append((item["subject"], item["draft"], instruction))
            return "Yes, Friday 2pm works."

        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box), patch(
            "gmail_auto.generator.revise_reply", side_effect=fake_rewrite
        ):
            self.client.post("/mail/reply", data={"key": "r2", "points": "yes, Friday 2pm"})
        self.assertEqual(asked, [("Meet?", "", "yes, Friday 2pm")])
        self.assertEqual(approvals.pending()[0]["draft"], "Yes, Friday 2pm works.")

        box.mails.append(sample(id="r3", subject="Newsletter", body="Hi", label_ids=["INBOX"]))
        _forget_inbox()
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box), patch(
            "gmail_auto.generator.draft_reply", side_effect=lambda *_: Draft(action="skip")
        ), patch("gmail_auto.generator.revise_reply", side_effect=ConfigError("model is down")):
            failed = self.client.post("/mail/reply", data={"key": "r3", "show": "all"}, follow_redirects=True)
        self.assertIn("model is down", failed.get_data(as_text=True))
        self.assertEqual(len(approvals.pending()), 1)

        _forget_inbox()
        with patch("gmail_auto.web.client_for", side_effect=ConfigError("offline")), patch("gmail_auto.web.connected_clients", return_value=([], [])):
            gone = self.client.post("/mail/reply", data={"key": "nope"}, follow_redirects=True)
        self.assertIn("could not be loaded", gone.get_data(as_text=True))


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

    def deliver_payload(self, message):
        payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
        raw = json.dumps(payload).encode("utf-8")
        signature = "sha256=" + hmac.new(b"app-secret-for-tests", raw, hashlib.sha256).hexdigest()
        return self.client.post(
            "/whatsapp/webhook",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": signature},
            base_url="https://tunnel.example",
        )

    def test_voice_note_is_transcribed_and_handled_like_text(self):
        voice = {"from": "64211234567", "id": "wamid.voice1", "type": "audio", "audio": {"id": "media-1", "mime_type": "audio/ogg; codecs=opus"}}
        with patch("gmail_auto.whatsapp._download", return_value=b"OggS-fake") as download, patch(
            "gmail_auto.llm.transcribe", return_value="help"
        ) as heard:
            self.assertEqual(self.deliver_payload(voice).status_code, 200)
        download.assert_called_once()
        self.assertEqual(heard.call_args.args[2], "audio/ogg")
        body = self.out[-1]["text"]["body"]
        self.assertIn('I heard: "help"', body)

        self.out.clear()
        with patch("gmail_auto.whatsapp._download", side_effect=ConfigError("Could not download the voice message.")):
            self.deliver_payload({**voice, "id": "wamid.voice2"})
        self.assertIn("couldn't make out that voice message", self.out[-1]["text"]["body"])

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

    def test_connections_shows_last_whatsapp_webhook_status(self):
        from gmail_auto.store import set_state

        _, empty_text = self.page("/connections")
        self.assertIn("No WhatsApp webhook received yet.", empty_text)

        set_state(
            "wa_webhook_last",
            {
                "status": "signature_failed",
                "detail": "Meta X-Hub-Signature-256 mismatch or WHATSAPP_APP_SECRET unset",
                "at": "2026-11-15 14:20",
            },
        )
        _, filled_text = self.page("/connections")
        self.assertIn("signature_failed", filled_text)
        self.assertIn("Meta X-Hub-Signature-256 mismatch", filled_text)
        self.assertIn("2026-11-15 14:20", filled_text)
        self.assertNotIn("还没有", filled_text)


class SignInTests(WebCase):
    def test_oauth_without_client_id_explains_the_missing_setting(self):
        page = self.client.get("/oauth", follow_redirects=True)
        self.assertIn("GMAIL_CLIENT_ID", page.get_data(as_text=True))

    def test_any_email_signs_in_once_and_replaces_the_old_session(self):
        sent = {}
        _, text = self.page("/login")
        self.assertNotIn("assword", text)
        self.assertNotIn(">Register<", text)
        rejected = self.client.post("/login/code", data={"email": "not an email"})
        self.assertEqual(rejected.status_code, 400)
        with self.client.session_transaction() as current:
            current["gmail_token"] = "old-grant"
            current["user"] = "other@example.com"
            current["user_id"] = "old-user"
        with patch("gmail_auto.web.deliver_code", lambda _to, code: sent.update(code=code)):
            started = self.client.post("/login/code", data={"email": "Lee@QQ.com"}, follow_redirects=True)
        self.assertIn("A code was sent to lee@qq.com", started.get_data(as_text=True))
        with self.client.session_transaction() as current:
            blob = json.dumps(dict(current))
            self.assertNotIn("otp", current)
            self.assertNotIn(sent["code"], blob)
        wrong = self.client.post("/login", data={"email": "lee@qq.com", "code": "000000"})
        self.assertEqual(wrong.status_code, 400)
        done = self.client.post("/login", data={"email": "lee@qq.com", "code": sent["code"]}, follow_redirects=True)
        self.assertIn("You are signed in", done.get_data(as_text=True))
        with self.client.session_transaction() as current:
            self.assertEqual(current.get("user"), "lee@qq.com")
            self.assertRegex(current.get("user_id", ""), r"^[0-9a-f]{32}$")
            self.assertNotIn("gmail_token", current)
            self.assertNotEqual(current.get("user_id"), "old-user")
            first_id = current["user_id"]
        reused = self.client.post("/login", data={"email": "lee@qq.com", "code": sent["code"]})
        self.assertEqual(reused.status_code, 400)
        self.assertIn("Ask for a code first", reused.get_data(as_text=True))
        later = time.time() + 120
        with patch("gmail_auto.accounts.time.time", return_value=later):
            with patch("gmail_auto.web.deliver_code", lambda _to, code: sent.update(code=code)):
                self.client.post("/login/code", data={"email": "lee@qq.com"})
        again = self.client.post("/login", data={"email": "lee@qq.com", "code": sent["code"]}, follow_redirects=True)
        self.assertIn("You are signed in", again.get_data(as_text=True))
        with self.client.session_transaction() as current:
            self.assertEqual(current.get("user_id"), first_id)
        other = {}
        with patch("gmail_auto.accounts.time.time", return_value=later + 120):
            with patch("gmail_auto.web.deliver_code", lambda _to, code: other.update(code=code)):
                self.client.post("/login/code", data={"email": "lee@outlook.com"})
        self.client.post("/login", data={"email": "lee@outlook.com", "code": other["code"]}, follow_redirects=True)
        with self.client.session_transaction() as current:
            self.assertEqual(current.get("user"), "lee@outlook.com")
            self.assertNotEqual(current.get("user_id"), first_id)
        self.client.get("/logout")
        os.environ["AUTOMAIL_SAAS"] = "1"
        blocked = self.client.get("/")
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/login", blocked.headers["Location"])
        with self.client.session_transaction() as current:
            current["user"] = "lee@qq.com"
        still = self.client.get("/approvals")
        self.assertEqual(still.status_code, 302)
        self.assertIn("/login", still.headers["Location"])

    def test_verification_code_does_not_need_vercel_kv(self):
        os.environ["VERCEL"] = "1"
        os.environ["ALLOWED_EMAILS"] = "lee@example.com"
        sent = {}
        with patch("gmail_auto.web.deliver_code", lambda _to, code: sent.update(code=code)):
            started = self.client.post("/login/code", data={"email": "lee@example.com"}, follow_redirects=True)
        self.assertNotIn("Vercel KV", started.get_data(as_text=True))
        done = self.client.post("/login", data={"email": "lee@example.com", "code": sent["code"]}, follow_redirects=True)
        text = done.get_data(as_text=True)
        self.assertIn("You are signed in", text)
        with self.client.session_transaction() as current:
            self.assertTrue(current.get("user_id"))
            self.assertNotIn(sent["code"], json.dumps(dict(current)))
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


class ModelSettingsTests(WebCase):
    def setUp(self):
        super().setUp()
        self.env_saved = {
            k: os.environ.pop(k, None)
            for k in ("AI_GATEWAY_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
        }

    def tearDown(self):
        for k, v in self.env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_gateway_key_uses_vercel_base_url_and_selected_model(self):
        from gmail_auto.llm import complete

        os.environ["AI_GATEWAY_API_KEY"] = "gw-test-key"
        save_settings({"ai_model": "google/gemini-2.5-flash"})

        captured = {}

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["create"] = kwargs
                msg = type("Msg", (), {"content": "OK"})()
                choice = type("Choice", (), {"message": msg})()
                return type("Resp", (), {"choices": [choice]})()

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                captured["init"] = kwargs
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        with patch("openai.OpenAI", _FakeOpenAI):
            out = complete("sys", "usr", max_tokens=16, temperature=0.2)

        self.assertEqual(out, "OK")
        self.assertEqual(captured["init"]["api_key"], "gw-test-key")
        self.assertEqual(captured["init"]["base_url"], "https://ai-gateway.vercel.sh/v1")
        self.assertEqual(captured["create"]["model"], "google/gemini-2.5-flash")

        _, conn_text = self.page("/connections")
        self.assertIn("google/gemini-2.5-flash", conn_text)
        self.assertIn("via Vercel AI Gateway", conn_text)

    def test_direct_openai_strips_openai_prefix_and_rejects_other_vendors(self):
        from gmail_auto.errors import ReplyError
        from gmail_auto.llm import complete

        os.environ["OPENAI_API_KEY"] = "sk-test-direct"
        save_settings({"ai_model": "openai/gpt-4o-mini"})

        captured = {}

        class _FakeCompletions:
            def create(self, **kwargs):
                captured["create"] = kwargs
                msg = type("Msg", (), {"content": "OK"})()
                choice = type("Choice", (), {"message": msg})()
                return type("Resp", (), {"choices": [choice]})()

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                captured["init"] = kwargs
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        with patch("openai.OpenAI", _FakeOpenAI):
            complete("sys", "usr")

        self.assertNotIn("base_url", captured["init"])
        self.assertEqual(captured["create"]["model"], "gpt-4o-mini")

        save_settings({"ai_model": "anthropic/claude-haiku-5.5"})
        with self.assertRaises(ReplyError) as ctx:
            complete("sys", "usr")
        self.assertIn("AI_GATEWAY_API_KEY", str(ctx.exception))

    def test_invalid_ai_model_is_dropped_and_test_model_button_works(self):
        saved = save_settings({"ai_model": "not-a-real/model"})
        self.assertEqual(saved["ai_model"], "")

        os.environ["AI_GATEWAY_API_KEY"] = "gw-test-key"
        with patch("gmail_auto.web.complete", return_value="OK") as mocked:
            resp = self.client.post(
                "/settings/model",
                data={"ai_model": "anthropic/claude-haiku-5.5", "test": "1"},
                follow_redirects=True,
            )
        text = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        mocked.assert_called_once()
        self.assertIn("Model anthropic/claude-haiku-5.5 responded: OK", text)
        self.assertEqual(load_settings()["ai_model"], "anthropic/claude-haiku-5.5")

    def test_gateway_default_model_reasoning_params_and_explicit_base_url(self):
        from gmail_auto.llm import complete

        captured = []

        class _FakeCompletions:
            def create(self, **kwargs):
                captured[-1]["create"] = kwargs
                msg = type("Msg", (), {"content": "OK"})()
                choice = type("Choice", (), {"message": msg})()
                return type("Resp", (), {"choices": [choice]})()

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                captured.append({"init": kwargs})
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        os.environ["AI_GATEWAY_API_KEY"] = "gw-test-key"
        save_settings({"ai_model": ""})
        with patch("openai.OpenAI", _FakeOpenAI):
            complete("sys", "usr", max_tokens=16, temperature=0.2)
        self.assertEqual(captured[-1]["init"]["base_url"], "https://ai-gateway.vercel.sh/v1")
        self.assertEqual(captured[-1]["create"]["model"], "openai/gpt-4o-mini")
        self.assertEqual(captured[-1]["create"]["max_tokens"], 16)
        self.assertEqual(captured[-1]["create"]["temperature"], 0.2)

        save_settings({"ai_model": "openai/gpt-5-mini"})
        with patch("openai.OpenAI", _FakeOpenAI):
            complete("sys", "usr", max_tokens=16, temperature=0.2)
        self.assertEqual(captured[-1]["create"]["model"], "openai/gpt-5-mini")
        self.assertEqual(captured[-1]["create"]["reasoning_effort"], "low")
        self.assertEqual(captured[-1]["create"]["max_completion_tokens"], 2000)
        self.assertNotIn("max_tokens", captured[-1]["create"])
        self.assertNotIn("temperature", captured[-1]["create"])

        os.environ["OPENAI_API_KEY"] = "sk-custom"
        os.environ["OPENAI_BASE_URL"] = "https://custom.example/v1"
        save_settings({"ai_model": ""})
        with patch("openai.OpenAI", _FakeOpenAI):
            complete("sys", "usr", max_tokens=16, temperature=0.2)
        self.assertEqual(captured[-1]["init"]["api_key"], "sk-custom")
        self.assertEqual(captured[-1]["init"]["base_url"], "https://custom.example/v1")
        self.assertEqual(captured[-1]["create"]["model"], "gpt-4o-mini")

    def test_empty_reply_raises_and_test_model_reports_failure(self):
        from gmail_auto.errors import ReplyError
        from gmail_auto.llm import complete

        class _EmptyCompletions:
            def create(self, **kwargs):
                msg = type("Msg", (), {"content": "   "})()
                choice = type("Choice", (), {"message": msg})()
                return type("Resp", (), {"choices": [choice]})()

        class _EmptyOpenAI:
            def __init__(self, **kwargs):
                self.chat = type("Chat", (), {"completions": _EmptyCompletions()})()

        os.environ["AI_GATEWAY_API_KEY"] = "gw-test-key"
        save_settings({"ai_model": "openai/gpt-5-mini"})
        with patch("openai.OpenAI", _EmptyOpenAI):
            with self.assertRaises(ReplyError) as ctx:
                complete("sys", "usr")
        self.assertIn("empty reply", str(ctx.exception))

        with patch("openai.OpenAI", _EmptyOpenAI):
            resp = self.client.post(
                "/settings/model",
                data={"ai_model": "openai/gpt-5-mini", "test": "1"},
                follow_redirects=True,
            )
        text = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("empty reply", text)
        self.assertNotIn("responded:", text)

    def test_settings_page_uses_compact_tabs_and_preserves_section_anchors(self):
        _, text = self.page("/profile")
        for label in ("How far it sends", "When to ping me", "Who it sounds like", "Cleanup"):
            self.assertIn(label, text)
        for panel in ('data-panel="send"', 'data-panel="interrupt"', 'data-panel="voice"', 'data-panel="cleanup"'):
            self.assertIn(panel, text)
        for anchor in ('id="mode"', 'id="model"', 'id="replies"', 'id="cleanup"', 'id="profile"'):
            self.assertIn(anchor, text)
        self.assertIn('class="model-row"', text)
        self.assertIn('class="ping-grid"', text)
        self.assertIn('class="ping-info"', text)
