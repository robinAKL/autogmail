import base64
import json
import os
import tempfile
import unittest
from email import message_from_bytes
from email.header import Header
from email.policy import default

from gmail_auto.errors import ConfigError, ProfileError, ReplyError
from gmail_auto.filters import Allow, Skip, classify, detect_language, is_system_address, same_mailbox
from gmail_auto.generator import clean_reply, ensure_safe, reply_language
from gmail_auto.gmail_client import build_send_body, find_client_file, load_credentials, parse_message
from gmail_auto.models import Draft, Mail
from gmail_auto.paths import root
from gmail_auto.profile_store import clean_profile, load_profile, profile_is_placeholder, save_profile
from gmail_auto.runner import run_once, toggle_and_run
from gmail_auto.settings import save_settings
from gmail_auto.store import get_processed, is_live, set_live

PLACEHOLDER = {
    "name": "示例用户",
    "role": "（请填写你的身份）",
    "introduction": "（请用几句事实介绍自己。）",
    "tone": "（请描述你平时写信的语气。）",
    "cannot_promise": ["No dates"],
    "sample_phrases": ["I saw this."],
}


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.get(name) for name in ("AUTOMAIL_HOME", "OPENAI_API_KEY", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "TWILIO_ACCOUNT_SID")}
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        os.environ["OPENAI_API_KEY"] = "test-key-not-used"
        for name in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "TWILIO_ACCOUNT_SID"):
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()


def sample(**kwargs) -> Mail:
    data = dict(
        id="m1",
        thread_id="t1",
        from_name="Lee",
        from_email="lee@example.com",
        reply_email="",
        subject="Hello",
        date="2026-09-30 10:00",
        body="Can you look at the notes?",
        headers={"message-id": "<m1@example.com>", "references": "<old@example.com>"},
        label_ids=["INBOX", "UNREAD"],
        snippet="Can you",
    )
    data.update(kwargs)
    return Mail(**data)


def write_test_profile(**overrides) -> dict:
    data = {
        "name": "Test User",
        "role": "Tester",
        "introduction": "This introduction exists only for tests.",
        "tone": "Short and direct.",
        "cannot_promise": ["No dates"],
        "sample_phrases": ["I saw this."],
    }
    data.update(overrides)
    return save_profile(data)


class FakeGmail:
    provider = "gmail"

    def __init__(self, mails, user="me@example.com"):
        self.mails = mails
        self.user = user
        self.sent = []
        self.read = []
        self.archived = []

    def get_user_email(self):
        return self.user

    def list_unread(self, limit=20):
        return self.mails[:limit]

    def send_reply(self, mail, body, user_email):
        self.sent.append((mail.id, body, user_email))
        return "sent"

    def mark_read(self, message_id):
        self.read.append(message_id)

    def archive(self, message_id):
        self.archived.append(message_id)

    def list_recent(self, limit=25, full=False):
        return self.mails[:limit]


def english_reply(*_args):
    return "I saw your note."


class FilterTests(unittest.TestCase):
    def test_language_follows_the_letter(self):
        self.assertEqual(detect_language("Please review the attached notes."), "en")
        self.assertEqual(detect_language("请帮我看一下明天的安排"), "zh")
        self.assertEqual(detect_language("Hello, the meeting is at 3. 请回复"), "en")
        self.assertEqual(classify(sample(), "me@example.com").language, "en")
        chinese = sample(subject="安排", body="请帮我看一下明天的安排")
        self.assertEqual(classify(chinese, "me@example.com").language, "zh")

    def test_skips_self_system_list_auto_and_categories(self):
        self.assertTrue(same_mailbox("me+news@gmail.com", "me@googlemail.com"))
        self.assertFalse(same_mailbox("me@gmail.com", "other@gmail.com"))
        self.assertEqual(classify(sample(from_email="me@example.com"), "me@example.com").code, "self")
        self.assertTrue(is_system_address("no-reply@news.example.com"))
        self.assertFalse(is_system_address("mina@example.com"))
        self.assertTrue(is_system_address("jobalerts-noreply@linkedin.com"))
        self.assertTrue(is_system_address("alerts.do-not-reply@bank.example.com"))
        self.assertFalse(is_system_address("norene.plyer@example.com"))
        self.assertEqual(classify(sample(from_email="noreply@news.example.com"), "me@example.com").code, "system")
        listed = sample(headers={"list-id": "<news.example.com>"})
        self.assertEqual(classify(listed, "me@example.com").code, "list")
        auto = sample(headers={"auto-submitted": "auto-replied"})
        self.assertEqual(classify(auto, "me@example.com").code, "auto")
        away = sample(subject="Automatic reply: away")
        self.assertEqual(classify(away, "me@example.com").code, "auto")
        promo = sample(label_ids=["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"])
        self.assertIn("promotion", classify(promo, "me@example.com").reason)
        self.assertEqual(classify(sample(body="  "), "me@example.com").code, "empty")

    def test_sensitive_mail_is_skipped_without_treating_ordinary_words_as_secrets(self):
        ordinary = classify(sample(body="I passed the exam yesterday. Can we talk tomorrow?"), "me@example.com")
        self.assertIsInstance(ordinary, Allow)
        secret = classify(sample(body="Please reset my password."), "me@example.com")
        self.assertIsInstance(secret, Skip)
        self.assertTrue(secret.sensitive)
        code = classify(sample(subject="验证码", body="123456"), "me@example.com")
        self.assertTrue(code.sensitive)


class SafetyTests(unittest.TestCase):
    profile = {
        "name": "Test User",
        "role": "Tester",
        "introduction": "This introduction exists only for tests.",
        "tone": "Short and direct.",
        "cannot_promise": ["No dates"],
        "sample_phrases": ["I saw this."],
    }

    def test_reply_language_follows_the_setting(self):
        self.assertEqual(reply_language("en", "zh"), "en")
        self.assertEqual(reply_language("match", "zh"), "zh")
        self.assertEqual(reply_language("match", "en"), "en")
        self.assertEqual(reply_language("bilingual", "en"), "bilingual")

    def test_blocks_invented_number_link_email_and_wrong_language(self):
        ensure_safe("I will reply on Friday.", self.profile, "en")
        ensure_safe("我周五再回复你。\n\nI will reply on Friday.", self.profile, "bilingual")
        ensure_safe("Reference 20260930, I saw the note.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("Please call me on 13800138000 today.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("Please call 138-0013-8000 when you can.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("See https://example.com/secret for the details.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("Write to me at other@example.com today please.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("I saw your note.", self.profile, "zh")
        with self.assertRaises(ReplyError):
            ensure_safe("我看到了。", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("I saw your note.", self.profile, "bilingual")
        with self.assertRaises(ReplyError):
            ensure_safe("As an AI I saw your note.", self.profile, "en")
        with self.assertRaises(ReplyError):
            ensure_safe("The key is sk-abcdefghijklmnopqrstuvwxyz right here.", self.profile, "en")

    def test_allows_number_and_link_that_are_already_in_the_material(self):
        profile = dict(self.profile, introduction="My office line is 13800138000.")
        ensure_safe("My number is 13800138000, as you asked.", profile, "en")
        mail = sample(body="Please see https://example.com/a")
        ensure_safe("I saw https://example.com/a in your note.", self.profile, "en", mail)
        ensure_safe(
            "The workshop costs 1200000 rupiah, see https://kb.example.com/prices today.",
            self.profile,
            "en",
            None,
            "FROM MY DOCUMENT: price 1200000 rupiah https://kb.example.com/prices",
        )

    def test_clean_reply_strips_wrapper(self):
        self.assertEqual(clean_reply("```\nI saw it.\n```"), "I saw it.")
        self.assertEqual(clean_reply("Subject: Hello\nI saw it."), "I saw it.")


class GmailFormatTests(unittest.TestCase):
    def test_reply_stays_on_the_thread_and_uses_reply_to(self):
        mail = sample(subject="你好", reply_email="other@example.com")
        payload = build_send_body(mail, "I saw it.", "me@example.com")
        parsed = message_from_bytes(base64.urlsafe_b64decode(payload["raw"]), policy=default)
        self.assertEqual(payload["threadId"], "t1")
        self.assertEqual(parsed["Subject"], "Re: 你好")
        self.assertEqual(parsed["In-Reply-To"], "<m1@example.com>")
        self.assertIn("<old@example.com>", parsed["References"])
        self.assertIn("<m1@example.com>", parsed["References"])
        self.assertEqual(parsed["To"], "other@example.com")
        self.assertEqual(parsed["From"], "me@example.com")
        self.assertEqual(parsed.get_content().strip(), "I saw it.")

        again = sample(subject="Re: Hello")
        parsed_again = message_from_bytes(
            base64.urlsafe_b64decode(build_send_body(again, "I saw your note.", "me@example.com")["raw"]),
            policy=default,
        )
        self.assertEqual(parsed_again["Subject"], "Re: Hello")
        self.assertIn("lee@example.com", parsed_again["To"])

    def test_parse_keeps_text_for_drafting_and_html_for_display(self):
        raw = {
            "id": "h1",
            "threadId": "t",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1710000000000",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Lee <lee@example.com>"},
                    {"name": "Subject", "value": Header("你好", "utf-8").encode()},
                    {"name": "Message-ID", "value": "<m1@example.com>"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "headers": [{"name": "Content-Disposition", "value": "attachment; filename=a.txt"}],
                        "body": {"data": _b64("hidden attachment")},
                    },
                    {"mimeType": "text/plain", "body": {"data": _b64("visible note")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<script>alert(1)</script><p><b>Hello</b> &amp; welcome</p>")}},
                ],
            },
        }
        mail = parse_message(raw)
        self.assertEqual(mail.subject, "你好")
        self.assertEqual(mail.body, "visible note")
        self.assertIn("<b>Hello</b>", mail.html)
        self.assertEqual(mail.received_ts, 1710000000.0)
        self.assertEqual(mail.key, "h1")
        html_only = parse_message(
            {
                "id": "h2",
                "threadId": "t",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [{"name": "From", "value": "Lee <lee@example.com>"}, {"name": "Subject", "value": "Hi"}],
                    "body": {"data": _b64("<script>alert(1)</script><p>Hello &amp; welcome</p>")},
                },
            }
        )
        self.assertIn("Hello & welcome", html_only.body)
        self.assertNotIn("alert", html_only.body)
        self.assertNotIn("<p>", html_only.body)


class RunnerTests(TempHome):
    def test_dry_run_writes_locally_and_is_not_sent_later(self):
        write_test_profile()
        fake = FakeGmail([sample()])
        report = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(report.drafted, 1)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.read, [])
        record = get_processed("m1")
        self.assertEqual(record["action"], "drafted")
        text = (root() / record["outbox"]).read_text(encoding="utf-8")
        self.assertIn("I saw your note.", text)
        self.assertIn("Nothing was sent", text)
        set_live(True)
        again = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(fake.sent, [])
        self.assertEqual(again.sent, 0)
        self.assertIn("already handled", again.lines[0])

    def test_live_send_replies_once_and_marks_read(self):
        write_test_profile()
        set_live(True)
        fake = FakeGmail([sample(id="m2")])
        seen = {}

        def generate(mail, _profile, language):
            seen["language"] = language
            return "I saw your note."

        report = run_once(gmail=fake, generate=generate)
        self.assertEqual(report.sent, 1)
        self.assertEqual(seen["language"], "en")
        self.assertEqual(fake.read, ["m2"])
        self.assertEqual(fake.sent[0][0], "m2")
        run_once(gmail=fake, generate=generate)
        self.assertEqual(len(fake.sent), 1)

    def test_reply_that_needs_confirmation_waits_and_is_sent_only_after_approval(self):
        from gmail_auto import approvals

        write_test_profile()
        set_live(True)
        fake = FakeGmail([sample(id="m3", subject="Can we meet Friday?")])

        def generate(*_args):
            return Draft(action="ask", reply="Friday at 2pm works for me.", reason="It commits to a meeting time.", summary="Wants to meet on Friday.")

        report = run_once(gmail=fake, generate=generate)
        self.assertEqual(report.asked, 1)
        self.assertEqual(fake.sent, [])
        self.assertEqual(get_processed("m3")["action"], "waiting")
        waiting = approvals.pending()
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["reason"], "It commits to a meeting time.")

        approvals.set_draft(waiting[0]["id"], "Friday at 3pm works for me.")
        approvals.approve(waiting[0]["id"], "whatsapp", client=fake)
        self.assertEqual(fake.sent, [("m3", "Friday at 3pm works for me.", "me@example.com")])
        self.assertEqual(fake.read, ["m3"])
        record = get_processed("m3")
        self.assertEqual(record["action"], "sent")
        self.assertTrue(record["approved"])
        self.assertEqual(approvals.pending(), [])
        with self.assertRaises(ConfigError):
            approvals.approve(waiting[0]["id"], "web", client=fake)
        self.assertEqual(len(fake.sent), 1)

    def test_ask_every_time_holds_even_routine_replies_and_reject_sends_nothing(self):
        from gmail_auto import approvals

        write_test_profile()
        set_live(True)
        save_settings({"approval_mode": "always"})
        fake = FakeGmail([sample(id="m4")])
        report = run_once(gmail=fake, generate=english_reply)
        self.assertEqual((report.asked, report.sent), (1, 0))
        item = approvals.pending()[0]
        approvals.reject(item["id"], "web")
        self.assertEqual(fake.sent, [])
        self.assertEqual(get_processed("m4")["action"], "rejected")

    def test_send_automatically_never_asks(self):
        write_test_profile()
        set_live(True)
        save_settings({"approval_mode": "never"})
        fake = FakeGmail([sample(id="m5")])
        report = run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Yes, Friday works."))
        self.assertEqual((report.asked, report.sent), (0, 1))

    def test_model_can_decide_no_reply_is_needed(self):
        write_test_profile()
        set_live(True)
        fake = FakeGmail([sample(id="m6")])
        report = run_once(gmail=fake, generate=lambda *_: Draft(action="skip", reason="Just a thank-you."))
        self.assertEqual(report.skipped, 1)
        self.assertEqual(fake.sent, [])
        self.assertEqual(get_processed("m6")["skip_code"], "no_reply_needed")

    def test_sensitive_mail_never_reaches_the_model(self):
        write_test_profile()
        set_live(True)
        called = []

        def generate(*args):
            called.append(args)
            return "I saw your note."

        mail = sample(body="Here is my password: hunter2")
        fake = FakeGmail([mail])
        report = run_once(gmail=fake, generate=generate)
        self.assertEqual(called, [])
        self.assertEqual(fake.sent, [])
        self.assertEqual(report.skipped, 1)
        record = get_processed(mail.id)
        self.assertEqual(record["subject"], "")
        self.assertTrue(record["sensitive"])
        self.assertNotIn("hunter2", json.dumps(record, ensure_ascii=False))

    def test_failed_generation_retries_once_then_stops(self):
        write_test_profile()

        def generate(*_args):
            raise ReplyError("Could not reach OpenAI.")

        fake = FakeGmail([sample()])
        first = run_once(gmail=fake, generate=generate)
        self.assertEqual(first.failed, 1)
        self.assertIsNone(get_processed("m1"))
        second = run_once(gmail=fake, generate=generate)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(get_processed("m1")["action"], "skipped")
        self.assertEqual(fake.sent, [])

    def test_send_failure_does_not_mark_read(self):
        write_test_profile()
        set_live(True)

        class Boom(FakeGmail):
            def send_reply(self, mail, body, user_email):
                raise ConfigError("Could not reach Gmail.")

        fake = Boom([sample()])
        report = run_once(gmail=fake, generate=english_reply)
        self.assertEqual(report.failed, 1)
        self.assertEqual(fake.read, [])
        self.assertIsNone(get_processed("m1"))

    def test_outlook_and_gmail_letters_keep_separate_records(self):
        write_test_profile()
        outlook = FakeGmail([sample(id="m1", provider="outlook")])
        outlook.provider = "outlook"
        run_once(gmail=FakeGmail([sample(id="m1")]), generate=english_reply)
        report = run_once(gmail=outlook, generate=english_reply)
        self.assertEqual(report.drafted, 1)
        self.assertEqual(get_processed("outlook:m1")["provider"], "outlook")
        self.assertEqual(get_processed("m1")["provider"], "gmail")

    def test_placeholder_cannot_turn_on_live_sending(self):
        save_profile(PLACEHOLDER)
        self.assertTrue(profile_is_placeholder(load_profile()))
        set_live(True)
        fake = FakeGmail([sample()])
        with self.assertRaises(ConfigError) as caught:
            run_once(gmail=fake, generate=english_reply)
        self.assertIn("placeholder", str(caught.exception))
        self.assertFalse(is_live())
        self.assertEqual(fake.sent, [])
        with self.assertRaises(ConfigError):
            toggle_and_run(True)
        self.assertFalse(is_live())

    def test_toggle_requires_confirmation_and_then_sends(self):
        write_test_profile()
        with self.assertRaises(ConfigError) as caught:
            toggle_and_run(False)
        self.assertIn("confirmation", str(caught.exception))
        self.assertFalse(is_live())
        fake = FakeGmail([sample(id="m9")])
        report = toggle_and_run(True, gmail=fake, generate=english_reply)
        self.assertTrue(report.live)
        self.assertEqual(fake.sent[0][0], "m9")
        self.assertEqual(fake.read, ["m9"])
        fake_off = FakeGmail([sample(id="m10")])
        dry = toggle_and_run(False, gmail=fake_off, generate=english_reply)
        self.assertFalse(is_live())
        self.assertEqual(fake_off.sent, [])
        self.assertEqual(dry.drafted, 1)

    def test_live_send_environment_variable_is_ignored(self):
        from gmail_auto.runner import switch_live

        previous = os.environ.get("LIVE_SEND")
        os.environ["LIVE_SEND"] = "true"
        try:
            self.assertFalse(is_live())
            write_test_profile()
            switch_live(True, True)
            self.assertTrue(is_live())
            os.environ["LIVE_SEND"] = "false"
            self.assertTrue(is_live())
            switch_live(False)
            self.assertFalse(is_live())
        finally:
            if previous is None:
                os.environ.pop("LIVE_SEND", None)
            else:
                os.environ["LIVE_SEND"] = previous

    def test_client_secret_must_be_a_desktop_app(self):
        with self.assertRaises(ConfigError) as missing:
            find_client_file()
        self.assertIn("credentials.json", str(missing.exception))
        path = root() / "credentials.json"
        path.write_text(json.dumps({"web": {"client_id": "placeholder"}}), encoding="utf-8")
        with self.assertRaises(ConfigError) as wrong:
            find_client_file()
        self.assertIn("Desktop app", str(wrong.exception))
        path.write_text(json.dumps({"installed": {"client_id": "placeholder"}}), encoding="utf-8")
        self.assertEqual(find_client_file(), path)

    def test_gmail_token_can_come_from_the_environment(self):
        previous = os.environ.get("GMAIL_TOKEN_JSON")
        os.environ["GMAIL_TOKEN_JSON"] = json.dumps(
            {
                "token": "test-access-token",
                "refresh_token": "test-refresh-token",
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "test-secret",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/calendar.events",
                ],
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
        try:
            creds = load_credentials(allow_browser=False)
            os.environ["GMAIL_TOKEN_JSON"] = "not-json"
            with self.assertRaises(ConfigError) as bad:
                load_credentials(allow_browser=False)
        finally:
            if previous is None:
                os.environ.pop("GMAIL_TOKEN_JSON", None)
            else:
                os.environ["GMAIL_TOKEN_JSON"] = previous
        self.assertEqual(creds.refresh_token, "test-refresh-token")
        self.assertIn("JSON", str(bad.exception))

    def test_profile_rejects_secrets_and_blank_identity(self):
        with self.assertRaises(ProfileError):
            clean_profile(dict(SafetyTests.profile, introduction="key sk-abcdefghijklmnopqrstuvwxyz"))
        with self.assertRaises(ProfileError):
            clean_profile(dict(SafetyTests.profile, name="  "))


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
