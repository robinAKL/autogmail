import json
import os
import tempfile
import unittest
from unittest.mock import patch

from gmail_auto.accounts import finish_verification, start_verification
from gmail_auto.errors import ConfigError
from gmail_auto.mailer import deliver_code
from gmail_auto.paths import data_dir


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.pop(name, None) for name in ("AUTOMAIL_HOME", "ALLOWED_EMAILS", "VERCEL")}
        os.environ["AUTOMAIL_HOME"] = self.tmp.name

    def tearDown(self):
        for name in ("AUTOMAIL_HOME", "ALLOWED_EMAILS", "VERCEL"):
            os.environ.pop(name, None)
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_code_is_hashed_once_and_the_same_login_keeps_its_user_id(self):
        clock = {"now": 1_700_000_000.0}

        def now():
            return clock["now"]

        with patch("gmail_auto.accounts.time.time", side_effect=now):
            first = start_verification("Ada@Example.com", "203.0.113.8")
            with self.assertRaises(ConfigError):
                start_verification("ada@example.com", "203.0.113.8")
            clock["now"] += 61
            stored = json.loads((data_dir(create=False) / "accounts.json").read_text(encoding="utf-8"))
            self.assertNotIn(first, json.dumps(stored))
            self.assertNotIn("code", stored["otp"]["ada@example.com"])
            for _ in range(4):
                with self.assertRaises(ConfigError) as wrong:
                    finish_verification("ada@example.com", "000000", "203.0.113.8")
                self.assertIn("not right", str(wrong.exception))
            account = finish_verification("ada@example.com", first, "203.0.113.8")
            self.assertEqual(account["email"], "ada@example.com")
            self.assertRegex(account["user_id"], r"^[0-9a-f]{32}$")
            with self.assertRaises(ConfigError):
                finish_verification("ada@example.com", first, "203.0.113.8")
            clock["now"] += 61
            again_code = start_verification("ada@example.com", "203.0.113.9")
            again = finish_verification("ada@example.com", again_code, "203.0.113.9")
            other = start_verification("ada@outlook.com", "203.0.113.9")
            other_account = finish_verification("ada@outlook.com", other, "203.0.113.9")
        self.assertEqual(again["user_id"], account["user_id"])
        self.assertNotEqual(other_account["user_id"], account["user_id"])

    def test_expired_and_overused_codes_are_thrown_away(self):
        clock = {"now": 1_700_000_000.0}
        with patch("gmail_auto.accounts.time.time", side_effect=lambda: clock["now"]):
            code = start_verification("lee@qq.com", "198.51.100.4")
            clock["now"] += 601
            with self.assertRaises(ConfigError) as expired:
                finish_verification("lee@qq.com", code, "198.51.100.4")
            self.assertIn("expired", str(expired.exception))
            clock["now"] += 61
            fresh = start_verification("lee@qq.com", "198.51.100.4")
            for _ in range(4):
                with self.assertRaises(ConfigError) as wrong:
                    finish_verification("lee@qq.com", "111111", "198.51.100.4")
                self.assertIn("not right", str(wrong.exception))
            with self.assertRaises(ConfigError) as locked:
                finish_verification("lee@qq.com", "111111", "198.51.100.4")
            self.assertIn("no longer valid", str(locked.exception))
            with self.assertRaises(ConfigError) as gone:
                finish_verification("lee@qq.com", fresh, "198.51.100.4")
            self.assertIn("Ask for a code first", str(gone.exception))

    def test_email_and_ip_send_limits(self):
        clock = {"now": 1_700_000_000.0}
        with patch("gmail_auto.accounts.time.time", side_effect=lambda: clock["now"]):
            for _ in range(5):
                start_verification("busy@example.com", "192.0.2.10")
                clock["now"] += 61
            with self.assertRaises(ConfigError) as email_limited:
                start_verification("busy@example.com", "192.0.2.11")
            self.assertIn("this email", str(email_limited.exception))
            for index in range(20):
                start_verification(f"u{index}@example.com", "192.0.2.20")
            with self.assertRaises(ConfigError) as network_limited:
                start_verification("u-last@example.com", "192.0.2.20")
            self.assertIn("this network", str(network_limited.exception))

    def test_invite_list_still_blocks_other_addresses(self):
        os.environ["ALLOWED_EMAILS"] = "owner@example.com"
        with self.assertRaises(ConfigError):
            start_verification("stranger@qq.com", "127.0.0.1")


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.names = ("SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST", "RESEND_API_KEY", "MAIL_FROM")
        self.saved = {name: os.environ.pop(name, None) for name in self.names}

    def tearDown(self):
        for name in self.names:
            os.environ.pop(name, None)
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value

    def test_missing_platform_mailer_does_not_fall_back_to_gmail(self):
        with self.assertRaises(ConfigError) as caught:
            deliver_code("lee@qq.com", "123456")
        text = str(caught.exception)
        self.assertIn("RESEND_API_KEY", text)
        self.assertNotIn("gmail", text.lower())

    def test_resend_is_used_when_smtp_is_not_configured(self):
        os.environ["RESEND_API_KEY"] = "re_test"
        os.environ["MAIL_FROM"] = "codes@example.com"
        opened = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def capture(request, timeout):
            opened["url"] = request.full_url
            opened["auth"] = request.get_header("Authorization")
            opened["body"] = json.loads(request.data.decode("utf-8"))
            opened["timeout"] = timeout
            return Response()

        with patch("urllib.request.urlopen", side_effect=capture):
            deliver_code("lee@qq.com", "654321")
        self.assertEqual(opened["url"], "https://api.resend.com/emails")
        self.assertEqual(opened["auth"], "Bearer re_test")
        self.assertEqual(opened["body"]["from"], "codes@example.com")
        self.assertEqual(opened["body"]["to"], ["lee@qq.com"])
        self.assertIn("654321", opened["body"]["text"])
