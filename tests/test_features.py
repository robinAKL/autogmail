import hashlib
import hmac
import io
import os
import zipfile
from datetime import date, timedelta
from unittest.mock import patch

from gmail_auto import approvals, assistant, knowledge, stats, whatsapp
from gmail_auto.errors import ConfigError
from gmail_auto.extractors import extract_text
from gmail_auto.mailview import clean_html, document
from gmail_auto.models import Draft
from gmail_auto.outlook_client import parse_message as parse_outlook
from gmail_auto.runner import run_once, send_daily_digest
from gmail_auto.settings import load_settings, save_settings
from gmail_auto.sharepoint import share_token
from gmail_auto.store import get_processed, is_live, set_live
from tests.test_core import FakeGmail, TempHome, sample, write_test_profile


def docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class ExtractorTests(TempHome):
    def test_reads_text_html_and_word_files(self):
        self.assertEqual(extract_text("notes.txt", "Workshop fee is 450 dollars.".encode("utf-8")), "Workshop fee is 450 dollars.")
        page = extract_text("page.html", b"<html><head><style>p{color:red}</style></head><body><p>Refunds within 14 days</p><script>x()</script></body></html>")
        self.assertEqual(page, "Refunds within 14 days")
        self.assertEqual(extract_text("policy.docx", docx_bytes("Refund policy", "Refunds within 14 days.")), "Refund policy\nRefunds within 14 days.")

    def test_unknown_and_empty_files_are_refused_with_a_reason(self):
        with self.assertRaises(ConfigError) as unknown:
            extract_text("photo.png", b"\x89PNG")
        self.assertIn("not a supported file type", str(unknown.exception))
        with self.assertRaises(ConfigError) as empty:
            extract_text("blank.txt", b"   ")
        self.assertIn("No text", str(empty.exception))
        with self.assertRaises(ConfigError):
            extract_text("broken.docx", b"not a zip")


class KnowledgeTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.pop("OPENAI_API_KEY", None)

    def test_documents_are_found_by_what_a_letter_is_about(self):
        knowledge.add_document("Workshop prices", "The October AI workshop costs 450 dollars per person. Group discount is 10 percent.", "note")
        knowledge.add_document("School calendar", "Term 4 ends on 16 December. Parent interviews are in week 6.", "upload", origin="calendar.txt")
        knowledge.save_rules("Always ask me before agreeing to a price.\n- Never reply to recruiters.")
        mail = sample(subject="Workshop price?", body="How much does the October workshop cost for a group?")
        context = knowledge.context_for(mail)
        self.assertEqual(context["sources"], ["Workshop prices"])
        self.assertIn("450 dollars", context["passages"][0]["text"])
        self.assertEqual(context["rules"], ["Always ask me before agreeing to a price.", "Never reply to recruiters."])
        prompt = knowledge.render_context(context)
        self.assertIn("MY RULES", prompt)
        self.assertIn("Workshop prices", prompt)
        self.assertEqual(knowledge.search("something about gardening tools"), [])
        chinese = knowledge.add_document("课程安排", "人工智能课程在每周三晚上七点开始。", "note")
        self.assertEqual(knowledge.search("课程什么时候开始")[0]["doc_id"], chinese["id"])

    def test_same_file_replaces_itself_and_removal_forgets_it(self):
        first = knowledge.add_document("prices.txt", "Old price 400 dollars.", "upload", origin="prices.txt")
        second = knowledge.add_document("prices.txt", "New price 450 dollars.", "upload", origin="prices.txt")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(knowledge.list_documents()), 1)
        self.assertIn("450", knowledge.search("price")[0]["text"])
        self.assertTrue(knowledge.delete_document(second["id"]))
        self.assertEqual(knowledge.search("price"), [])
        self.assertIsNone(knowledge.get_document(second["id"]))

    def test_understanding_is_rewritten_from_profile_rules_and_documents(self):
        profile = write_test_profile()
        knowledge.add_document("Workshop prices", "The October AI workshop costs 450 dollars.", "note")
        knowledge.save_rules(["Never reply to recruiters."])
        self.assertTrue(knowledge.load_index()["brief"]["stale"])
        seen = {}

        def fake_complete(system, user, **_kwargs):
            seen["user"] = user
            return "## Who I am\n- Test User, a tester."

        with patch("gmail_auto.knowledge.complete", fake_complete):
            knowledge.rebuild_brief(profile)
        self.assertIn("450 dollars", seen["user"])
        self.assertIn("Never reply to recruiters.", seen["user"])
        self.assertIn("Test User", seen["user"])
        brief = knowledge.load_index()["brief"]
        self.assertFalse(brief["stale"])
        self.assertIn("Who I am", brief["text"])
        knowledge.save_brief("## Who I am\n- Edited by hand.")
        self.assertIn("Edited by hand", knowledge.load_index()["brief"]["text"])

    def test_learning_from_email_leaves_out_secrets_and_promotions(self):
        class Box(FakeGmail):
            def list_sent(self, limit=30):
                return [sample(id="s1", subject="Re: Friday", body="Friday at 2pm suits me. Kind regards, Test", headers={"to": "lee@example.com"})]

            def list_recent(self, limit=30):
                return [
                    sample(id="r1", body="Can we move the workshop to Friday?"),
                    sample(id="r2", body="Your password is hunter2"),
                    sample(id="r3", body="Big sale today", label_ids=["INBOX", "CATEGORY_PROMOTIONS"]),
                ]

        seen = []

        def fake_complete(system, user, **_kwargs):
            seen.append(user)
            return "## People and organisations\n- Lee: arranges workshops."

        with patch("gmail_auto.knowledge.complete", fake_complete):
            entry = knowledge.learn_from_mailbox(Box([]))
        self.assertEqual(entry["meta"]["letters"], 2)
        self.assertNotIn("hunter2", seen[0])
        self.assertNotIn("Big sale", seen[0])
        self.assertIn("[SENT]", seen[0])
        self.assertEqual(knowledge.list_documents()[0]["source"], "email")


class StatsTests(TempHome):
    def test_dashboard_counts_what_a_round_did(self):
        write_test_profile()
        set_live(True)
        mails = [
            sample(id="a1"),
            sample(id="a2", from_email="noreply@shop.example.com"),
            sample(id="a3", subject="Meet Friday?"),
        ]

        def generate(mail, *_):
            if mail.id == "a3":
                return Draft(action="ask", reply="Friday works.", reason="Commits to a time.")
            return "I saw your note."

        fake = FakeGmail(mails)
        run_once(gmail=fake, generate=generate)
        item = approvals.pending()[0]
        approvals.approve(item["id"], "web", client=fake)

        tomorrow = date.today() + timedelta(days=1)
        data = stats.dashboard(7, today=tomorrow)
        y = data["yesterday"]
        self.assertEqual((y["received"], y["auto_sent"], y["approved_sent"], y["skipped"], y["asked"]), (3, 1, 1, 1, 1))
        self.assertEqual(y["sent"], 2)
        self.assertEqual(y["minutes_saved"], 2 * stats.MINUTES_SAVED_PER_REPLY)
        self.assertEqual(y["approval_rate"], 100)
        self.assertEqual(data["skip_reasons"][0]["label"], "System or no-reply address")
        self.assertEqual(data["senders"][0]["key"], "lee@example.com")
        self.assertEqual(data["series"][-2]["auto_sent"], 1)
        self.assertIn("2 replies sent (1 automatic, 1 after your OK)", stats.digest_text(today=tomorrow))

    def test_history_is_filled_from_records_made_before_the_upgrade(self):
        records = [
            {"id": "x1", "action": "skipped", "reason": "系统地址，不回复", "processed_at": "2026-09-30 22:31:17", "from_email": "noreply@a.example.com"},
            {"id": "x2", "action": "skipped", "reason": "x", "sensitive": True, "processed_at": "2026-09-30 22:31:18"},
            {"id": "x3", "action": "drafted", "processed_at": "2026-10-01 08:00:00", "from_email": "lee@example.com"},
        ]
        stats.rebuild_from_records(records)
        data = stats.dashboard(7, today=date(2026, 10, 2))
        self.assertEqual(data["yesterday"]["drafted"], 1)
        self.assertEqual(data["all_time"]["skipped"], 2)
        self.assertEqual({item["key"] for item in data["skip_reasons"]}, {"system", "sensitive"})
        self.assertEqual(data["first_day"], "2026-09-30")


class WhatsAppTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["WHATSAPP_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234567890"
        save_settings({"whatsapp_to": "+64 21 123 4567", "daily_digest": False})
        write_test_profile()
        set_live(True)
        self.sent = []
        self.patch = patch("gmail_auto.whatsapp._meta_send", lambda to, message: self.sent.append((to, message)) or f"wamid.{len(self.sent)}")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.environ.pop("WHATSAPP_APP_SECRET", None)
        super().tearDown()

    def ask(self, mail_id="w1", subject="Can we meet Friday?"):
        fake = FakeGmail([sample(id=mail_id, subject=subject)])
        run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Friday at 2pm works.", reason="Commits to a time.", summary="Wants to meet."))
        return fake

    def test_a_reply_that_needs_an_ok_is_shown_on_whatsapp_and_yes_sends_it(self):
        fake = self.ask()
        self.assertEqual(load_settings()["whatsapp_to"], "64211234567")
        self.assertEqual(len(self.sent), 2)
        to, first = self.sent[0]
        self.assertEqual(to, "64211234567")
        self.assertIn("Can we meet Friday?", first["text"]["body"])
        self.assertIn("Friday at 2pm works.", first["text"]["body"])
        self.assertIn("Commits to a time.", first["text"]["body"])
        buttons = self.sent[1][1]["interactive"]["action"]["buttons"]
        self.assertEqual([button["reply"]["id"] for button in buttons], ["approve:1", "reject:1"])
        self.assertEqual(fake.sent, [])

        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="同意"))
        self.assertIn("Sent to Lee", answer)
        self.assertEqual(fake.sent, [("w1", "Friday at 2pm works.", "me@example.com")])
        self.assertEqual(get_processed("w1")["via"], "whatsapp")

    def test_plain_text_rewrites_the_draft_and_no_drops_it(self):
        fake = self.ask()
        with patch("gmail_auto.generator.revise_reply", lambda item, instruction, profile: f"Rewritten: {instruction}"):
            answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="say Monday instead"))
        self.assertIn("Rewritten: say Monday instead", answer)
        self.assertEqual(approvals.pending()[0]["draft"], "Rewritten: say Monday instead")
        self.assertTrue(approvals.pending()[0]["edited"])
        answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="No"))
        self.assertIn("nothing sent", answer)
        self.assertEqual(fake.sent, [])
        self.assertEqual(approvals.pending(), [])

    def test_with_several_waiting_the_number_or_the_button_picks_one(self):
        self.ask("w1", "First")
        fake = self.ask("w2", "Second")
        self.assertIn("Which one", assistant.handle(whatsapp.Incoming(sender="64211234567", text="yes")))
        self.assertIn("#1", assistant.handle(whatsapp.Incoming(sender="64211234567", text="list")))
        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            assistant.handle(whatsapp.Incoming(sender="64211234567", text="YES 2"))
            self.assertEqual([entry[0] for entry in fake.sent], ["w2"])
            assistant.handle(whatsapp.Incoming(sender="64211234567", button="reject:1", text="Don't send"))
        self.assertEqual(approvals.pending(), [])
        self.assertEqual(get_processed("w1")["action"], "rejected")

    def test_pause_stops_sending_and_duplicates_are_ignored(self):
        self.assertIn("Paused", assistant.handle(whatsapp.Incoming(sender="64211234567", text="pause")))
        self.assertFalse(is_live())
        self.assertFalse(assistant.seen_before("wamid.A"))
        self.assertTrue(assistant.seen_before("wamid.A"))

    def test_incoming_messages_are_parsed_and_only_the_owner_is_obeyed(self):
        payload = {
            "entry": [{"changes": [{"value": {"messages": [
                {"from": "64211234567", "id": "wamid.1", "type": "text", "text": {"body": " yes "}, "context": {"id": "wamid.0"}},
                {"from": "64211234567", "id": "wamid.2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "approve:7", "title": "Send"}}},
                {"from": "64211234567", "id": "wamid.3", "type": "image"},
            ]}}]}]
        }
        messages = whatsapp.parse_meta(payload)
        self.assertEqual([(m.text, m.button, m.context_id) for m in messages], [("yes", "", "wamid.0"), ("Send", "approve:7", "")])
        self.assertTrue(whatsapp.is_owner("+64 21 123 4567"))
        self.assertFalse(whatsapp.is_owner("64219999999"))
        twilio = whatsapp.parse_twilio({"From": "whatsapp:+64211234567", "Body": "ok", "MessageSid": "SM1"})
        self.assertEqual((twilio[0].sender, twilio[0].text), ("+64211234567", "ok"))

    def test_webhook_signature_must_match_the_app_secret(self):
        raw = b'{"entry": []}'
        self.assertFalse(whatsapp.meta_signature_ok(raw, "sha256=anything"))
        os.environ["WHATSAPP_APP_SECRET"] = "app-secret-for-tests"
        good = "sha256=" + hmac.new(b"app-secret-for-tests", raw, hashlib.sha256).hexdigest()
        self.assertTrue(whatsapp.meta_signature_ok(raw, good))
        self.assertFalse(whatsapp.meta_signature_ok(raw + b" ", good))

    def test_morning_summary_goes_out_once_a_day(self):
        from datetime import datetime

        save_settings({"daily_digest": True})
        self.sent.clear()
        early = datetime.now().replace(hour=6, minute=0)
        late = datetime.now().replace(hour=9, minute=0)
        self.assertFalse(send_daily_digest(early))
        self.assertTrue(send_daily_digest(late))
        self.assertFalse(send_daily_digest(late))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Yesterday", self.sent[0][1]["text"]["body"])


class OutlookAndViewTests(TempHome):
    def test_outlook_message_becomes_the_same_kind_of_letter(self):
        mail = parse_outlook(
            {
                "id": "AAMk=",
                "conversationId": "conv1",
                "subject": "Invoice question",
                "from": {"emailAddress": {"name": "Mina", "address": "mina@example.com"}},
                "replyTo": [],
                "receivedDateTime": "2026-11-11T09:01:00Z",
                "body": {"contentType": "html", "content": "<html><body><p>Hello <b>there</b></p></body></html>"},
                "bodyPreview": "Hello there",
                "isRead": False,
                "internetMessageId": "<abc@example.com>",
                "internetMessageHeaders": [{"name": "List-Id", "value": "<news.example.com>"}],
                "categories": ["autogmail-done"],
            }
        )
        self.assertEqual(mail.key, "outlook:AAMk=")
        self.assertEqual((mail.from_name, mail.from_email, mail.thread_id), ("Mina", "mina@example.com", "conv1"))
        self.assertEqual(mail.body, "Hello there")
        self.assertIn("<b>there</b>", mail.html)
        self.assertEqual(mail.headers["message-id"], "<abc@example.com>")
        self.assertEqual(mail.headers["list-id"], "<news.example.com>")
        self.assertEqual(mail.label_ids, ["INBOX", "UNREAD", "DONE"])
        self.assertEqual(mail.received_ts, 1794387660.0)

    def test_letter_html_is_shown_with_formatting_but_cannot_run_anything(self):
        dirty = (
            '<html><head><style>.a{color:red}</style><script>steal()</script></head>'
            '<body onload="steal()"><h1 class="a">Sale</h1><a href="javascript:steal()" onclick="steal()">Go</a>'
            '<img src="https://example.com/a.png" onerror="steal()"><iframe src="https://evil.example"></iframe>'
            '<form action="https://evil.example"><input name="q"></form></body></html>'
        )
        page = document(dirty)
        self.assertIn('<h1 class="a">Sale</h1>', page)
        self.assertIn(".a{color:red}", page)
        self.assertIn('<img src="https://example.com/a.png">', page)
        for banned in ("steal()", "<script", "<iframe", "<form", "<input", "javascript:", "onclick", "onerror"):
            self.assertNotIn(banned, page)
        plain = document("", "Line one\n<b>not bold</b> https://example.com/x")
        self.assertIn("&lt;b&gt;not bold&lt;/b&gt;", plain)
        self.assertIn('<a href="https://example.com/x"', plain)
        self.assertEqual(clean_html("<p onmouseover=x()>hi</p>"), "<p>hi</p>")

    def test_sharepoint_link_is_encoded_the_way_graph_expects(self):
        self.assertEqual(share_token("https://contoso.sharepoint.com/sites/Team/Shared Documents/Plan.docx"),
                         "u!aHR0cHM6Ly9jb250b3NvLnNoYXJlcG9pbnQuY29tL3NpdGVzL1RlYW0vU2hhcmVkIERvY3VtZW50cy9QbGFuLmRvY3g")


class MicrosoftErrorTests(TempHome):
    def test_microsoft_errors_are_explained(self):
        from gmail_auto.microsoft import explain_error

        self.assertIn("Value column, not its Secret ID", explain_error("invalid_client", "AADSTS7000215: Invalid client secret provided."))
        self.assertIn("MS_TENANT_ID", explain_error("invalid_request", "AADSTS50194: Application is not configured as a multi-tenant application."))
        self.assertIn("Grant admin consent", explain_error("access_denied", ""))
        message = explain_error("server_error", "AADSTS12345: Something odd.\r\nTrace ID: x")
        self.assertEqual(message, "Microsoft said: Something odd. (AADSTS12345)")

    def test_the_sign_in_directory_is_named(self):
        import os

        from gmail_auto import microsoft

        previous = os.environ.pop("MS_TENANT_ID", None)
        try:
            self.assertIn("MS_TENANT_ID is not set", microsoft.tenant_text())
            os.environ["MS_TENANT_ID"] = ' "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab" '
            self.assertEqual(microsoft.tenant(), "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab")
            self.assertIn("your organisation only", microsoft.tenant_text())
            self.assertIn("/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab/", microsoft.authorize_url("outlook", "https://x.example/cb", "s"))
        finally:
            os.environ.pop("MS_TENANT_ID", None)
            if previous is not None:
                os.environ["MS_TENANT_ID"] = previous


class StorageVariableTests(TempHome):
    NAMES = (
        "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
        "AUTOMAIL_KV_REST_API_URL", "AUTOMAIL_KV_REST_API_TOKEN", "AUTOMAIL_KV_REST_API_READ_ONLY_TOKEN",
        "AUTOMAIL_KV_URL", "AUTOMAIL_REDIS_URL", "REDIS_URL", "KV_URL",
    )

    def setUp(self):
        super().setUp()
        self.kept = {name: os.environ.pop(name, None) for name in self.NAMES}

    def tearDown(self):
        for name in self.NAMES:
            os.environ.pop(name, None)
            if self.kept[name] is not None:
                os.environ[name] = self.kept[name]
        super().tearDown()

    def test_prefixed_vercel_variables_are_found(self):
        from gmail_auto import token_store

        self.assertFalse(token_store.storage_ready())
        self.assertIn("No storage variables", token_store.storage_hint())
        os.environ["AUTOMAIL_KV_REST_API_READ_ONLY_TOKEN"] = "read-only"
        os.environ["AUTOMAIL_KV_REST_API_URL"] = "https://example-12345.upstash.io"
        self.assertFalse(token_store.storage_ready())
        self.assertIn("AUTOMAIL_KV_REST_API_URL", token_store.storage_hint())
        os.environ["AUTOMAIL_KV_REST_API_TOKEN"] = "write-token"
        self.assertEqual(token_store._redis(), ("https://example-12345.upstash.io", "write-token"))
        self.assertTrue(token_store.storage_ready())

    def test_a_plain_redis_address_is_accepted(self):
        from gmail_auto import token_store

        os.environ["AUTOMAIL_REDIS_URL"] = "rediss://default:secret@example.upstash.io:6379"
        self.assertTrue(token_store.storage_ready())
        self.assertEqual(token_store.storage_kind(), "Redis")
        os.environ["AUTOMAIL_REDIS_URL"] = "not-an-address"
        self.assertFalse(token_store.storage_ready())
