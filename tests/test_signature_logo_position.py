import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmail_auto.gmail_client import GmailClient
from gmail_auto.models import Mail
from gmail_auto.outlook_client import OutlookClient
from gmail_auto.settings import (
    load_settings,
    normalize_logo_position,
    save_settings,
    signature_logo_position_for_mailbox,
)
from gmail_auto.signatures import render_body_with_logo_html, split_body_and_signature
from gmail_auto.web import create_app

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ZkAAAAASUVORK5CYII="
)


class SignatureLogoPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = mock.patch.dict(os.environ, {"AUTOMAIL_HOME": str(self.root)}, clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_settings_positions_per_mailbox_and_default_bottom(self):
        self.assertEqual(normalize_logo_position("weird"), "bottom")
        self.assertEqual(signature_logo_position_for_mailbox(load_settings(), "gmail", "a@gmail.com"), "bottom")

        saved = save_settings(
            {
                "signature_logo_positions": {
                    "gmail:a@gmail.com": "LEFT",
                    "outlook:b@innopro.co.nz": "right",
                    "gmail:bad@gmail.com": "diagonal",
                }
            }
        )
        self.assertEqual(signature_logo_position_for_mailbox(saved, "gmail", "a@gmail.com"), "left")
        self.assertEqual(signature_logo_position_for_mailbox(saved, "outlook", "b@innopro.co.nz"), "right")
        self.assertEqual(signature_logo_position_for_mailbox(saved, "gmail", "bad@gmail.com"), "bottom")
        self.assertEqual(signature_logo_position_for_mailbox(saved, "outlook", "other@innopro.co.nz"), "bottom")

    def test_render_body_with_logo_html_arranges_only_around_signature(self):
        sig = "Robin E\nCo-Founder | INNOPRO"
        body = f"Hello,\n\nThanks for reaching out.\n\n{sig}"
        main_part, sig_part = split_body_and_signature(body, sig)
        self.assertEqual(main_part, "Hello,\n\nThanks for reaching out.")
        self.assertEqual(sig_part, sig)

        img = '<img src="cid:logo1@autogmail" alt="Company logo" width="120">'
        left_html = render_body_with_logo_html(body, sig, img, position="left")
        self.assertTrue(left_html.startswith("<div>Hello,<br><br>Thanks for reaching out.</div><table"))
        self.assertLess(left_html.index(img), left_html.index("Robin E<br>Co-Founder"))

        right_html = render_body_with_logo_html(body, sig, img, position="right")
        self.assertTrue(right_html.startswith("<div>Hello,<br><br>Thanks for reaching out.</div><table"))
        self.assertLess(right_html.index("Robin E<br>Co-Founder"), right_html.index(img))

        top_html = render_body_with_logo_html(body, sig, img, position="top")
        self.assertTrue(top_html.startswith("<div>Hello,<br><br>Thanks for reaching out.</div><div"))
        self.assertLess(top_html.index(img), top_html.index("Robin E<br>Co-Founder"))

        bottom_html = render_body_with_logo_html(body, sig, img, position="bottom")
        self.assertTrue(bottom_html.startswith("<div>Hello,<br><br>Thanks for reaching out.</div><div"))
        self.assertLess(bottom_html.index("Robin E<br>Co-Founder"), bottom_html.index(img))

    def test_gmail_send_uses_mailbox_logo_position_around_signature(self):
        sig = "Robin E\nINNOPRO"
        save_settings(
            {
                "mail_signatures": {"gmail": sig},
                "signature_logo_positions": {"gmail:robin@gmail.com": "left"},
            }
        )
        sent_raw = []

        class FakeSend:
            def __init__(self, body):
                self._body = body

            def execute(self):
                sent_raw.append(base64.urlsafe_b64decode(self._body["raw"]))
                return {"id": "msg-1"}

        class FakeMessages:
            def send(self, userId, body):
                return FakeSend(body)

        class FakeUsers:
            def messages(self):
                return FakeMessages()

            def getProfile(self, userId):
                return mock.Mock(execute=lambda: {"emailAddress": "robin@gmail.com"})

        client = GmailClient.__new__(GmailClient)
        client._email = "robin@gmail.com"
        client.service = mock.Mock(users=lambda: FakeUsers())

        with mock.patch("gmail_auto.signatures.load_logo", return_value=("logo.png", "image/png", _PNG_BYTES)):
            client.send_new("to@example.com", "Subject", f"Main text\n\n{sig}")

        self.assertEqual(len(sent_raw), 1)
        decoded = sent_raw[0].decode("utf-8", errors="replace")
        self.assertIn("<div>Main text</div><table", decoded)
        self.assertLess(decoded.index("Company logo"), decoded.index("Robin E<br>INNOPRO"))

    def test_outlook_send_uses_mailbox_logo_position_around_signature(self):
        sig = "Robin E\nINNOPRO"
        save_settings(
            {
                "mail_signatures": {"outlook": sig},
                "signature_logo_positions": {"outlook:robin@innopro.co.nz": "right"},
            }
        )
        captured = []

        def fake_graph(provider, method, path, body=None, headers=None, params=None):
            captured.append((method, path, body))
            if path.endswith("/createReply"):
                return {"id": "draft-1", "body": {"content": "<hr>Quoted"}}
            return {}

        client = OutlookClient.__new__(OutlookClient)
        client._email = "robin@innopro.co.nz"
        mail = Mail(
            id="m-1",
            thread_id="t-1",
            from_name="Sender",
            from_email="sender@example.com",
            reply_email="",
            subject="Hello",
            date="2026-11-20",
            body="Hi",
            headers={},
            label_ids=["INBOX"],
        )

        with mock.patch("gmail_auto.signatures.load_logo", return_value=("logo.png", "image/png", _PNG_BYTES)), mock.patch(
            "gmail_auto.outlook_client.microsoft.graph", side_effect=fake_graph
        ):
            client.send_new("to@example.com", "Test", f"Body line\n\n{sig}")
            client.send_reply(mail, f"Reply line\n\n{sig}", "robin@innopro.co.nz")

        send_mail_body = captured[0][2]["message"]["body"]["content"]
        self.assertTrue(send_mail_body.startswith("<div>Body line</div><table"))
        self.assertLess(send_mail_body.index("Robin E<br>INNOPRO"), send_mail_body.index("Company logo"))

        reply_patch_body = captured[2][2]["body"]["content"]
        self.assertTrue(reply_patch_body.startswith("<div>Reply line</div><table"))
        self.assertLess(reply_patch_body.index("Robin E<br>INNOPRO"), reply_patch_body.index("Company logo"))
        self.assertIn("<hr>Quoted", reply_patch_body)

    def test_profile_ui_saves_and_previews_position_per_provider(self):
        app = create_app()
        app.testing = True
        client = app.test_client()

        with mock.patch("gmail_auto.web._connected_mailbox_email", side_effect=lambda p: "a@gmail.com" if p == "gmail" else "b@innopro.co.nz"):
            resp = client.post(
                "/settings/signature/logo",
                data={"provider": "outlook", "action": "position", "logo_position": "left"},
                follow_redirects=True,
            )
            self.assertEqual(resp.status_code, 200)
            settings = load_settings()
            self.assertEqual(signature_logo_position_for_mailbox(settings, "outlook", "b@innopro.co.nz"), "left")
            self.assertEqual(signature_logo_position_for_mailbox(settings, "gmail", "a@gmail.com"), "bottom")

            html = client.get("/profile").get_data(as_text=True)
            self.assertIn('<option value="left" selected>Left of signature</option>', html)


if __name__ == "__main__":
    unittest.main()
