import os
import tempfile
import unittest
from unittest.mock import patch

from gmail_auto.gmail_client import _html_to_plain_signature
from gmail_auto.models import Draft
from gmail_auto.outgoing import start as outgoing_start
from gmail_auto.profile_store import save_profile
from gmail_auto.runner import run_once
from gmail_auto.settings import load_settings, save_settings
from gmail_auto.store import set_live
from gmail_auto.web import create_app
from tests.test_core import FakeGmail, sample


class FakeOutlook(FakeGmail):
    provider = "outlook"


class RulesAndSignaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("AUTOMAIL_HOME")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        save_profile(
            {
                "name": "Robin",
                "role": "Director",
                "introduction": "We build custom kitchens in Auckland.",
                "tone": "Warm and concise.",
                "cannot_promise": ["Do not promise delivery dates."],
                "sample_phrases": ["Happy to help."],
            }
        )

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("AUTOMAIL_HOME", None)
        else:
            os.environ["AUTOMAIL_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_auto_reply_block_and_allow_only_tighten_live_sending(self):
        save_settings(
            {
                "approval_mode": "never",
                "auto_reply_allow": ["quote", "@vip.co.nz"],
                "auto_reply_block": ["invoice", "@ird.govt.nz"],
            }
        )
        set_live(True)

        blocked_mail = sample(id="m-block", from_name="Billing", from_email="billing@ird.govt.nz", subject="Tax invoice", body="Please check.")
        unmatched_mail = sample(id="m-other", from_name="Alex", from_email="alex@example.com", subject="Quick question", body="Are you open?")
        allowed_mail = sample(id="m-allow", from_name="Sam", from_email="sam@client.co.nz", subject="Kitchen quote", body="Can I get a quote?")

        fake = FakeGmail([blocked_mail, unmatched_mail, allowed_mail])
        draft = Draft(action="reply", reply="Thanks, we can help with that.", reason="ok")
        with (
            patch("gmail_auto.runner.classify") as mock_classify,
            patch("gmail_auto.calendars.maybe_create_from_mail", return_value=None),
        ):
            from gmail_auto.filters import Allow

            mock_classify.return_value = Allow(language="en")
            report = run_once(gmail=fake, generate=lambda *_: Draft(action=draft.action, reply=draft.reply, reason=draft.reason))

        self.assertEqual(report.sent, 1)
        self.assertEqual(report.asked, 2)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0][0], "m-allow")

    def test_sending_mode_ceiling_cannot_be_loosened_by_allow_rules(self):
        save_settings({"approval_mode": "always", "auto_reply_allow": ["quote"]})
        set_live(True)
        allowed_mail = sample(id="m-1", from_name="Sam", from_email="sam@client.co.nz", subject="Quote", body="Need a quote.")
        fake = FakeGmail([allowed_mail])
        draft = Draft(action="reply", reply="Sure.", reason="ok")
        with (
            patch("gmail_auto.runner.classify") as mock_classify,
            patch("gmail_auto.calendars.maybe_create_from_mail", return_value=None),
        ):
            from gmail_auto.filters import Allow

            mock_classify.return_value = Allow(language="en")
            report = run_once(gmail=fake, generate=lambda *_: Draft(action=draft.action, reply=draft.reply, reason=draft.reason))

        self.assertEqual(report.sent, 0)
        self.assertEqual(report.asked, 1)
        self.assertEqual(fake.sent, [])

        # Write only mode also never sends even when allow rules match.
        set_live(False)
        save_settings({"approval_mode": "never", "auto_reply_allow": ["quote"]})
        fake2 = FakeGmail([sample(id="m-2", from_name="Sam", from_email="sam@client.co.nz", subject="Quote", body="Need a quote.")])
        with (
            patch("gmail_auto.runner.classify") as mock_classify,
            patch("gmail_auto.calendars.maybe_create_from_mail", return_value=None),
        ):
            from gmail_auto.filters import Allow

            mock_classify.return_value = Allow(language="en")
            report2 = run_once(gmail=fake2, generate=lambda *_: Draft(action=draft.action, reply=draft.reply, reason=draft.reason))

        self.assertEqual(report2.sent, 0)
        self.assertEqual(report2.drafted, 1)

    def test_per_mailbox_signatures_applied_to_reply_and_compose(self):
        save_settings(
            {
                "approval_mode": "never",
                "mail_signatures": {
                    "gmail": "Cheers,\nRobin (Gmail)",
                    "outlook": "Best regards,\nRobin (Outlook)",
                },
            }
        )
        set_live(True)
        outlook_mail = sample(id="o-1", from_name="Client", from_email="client@example.com", subject="Hello", body="Hi there")
        outlook_mail.provider = "outlook"
        fake_outlook = FakeOutlook([outlook_mail])
        draft = Draft(action="reply", reply="Hello back!", reason="ok")
        with (
            patch("gmail_auto.runner.classify") as mock_classify,
            patch("gmail_auto.calendars.maybe_create_from_mail", return_value=None),
        ):
            from gmail_auto.filters import Allow

            mock_classify.return_value = Allow(language="en")
            run_once(gmail=fake_outlook, generate=lambda *_: Draft(action=draft.action, reply=draft.reply, reason=draft.reason))

        self.assertIn("Best regards,\nRobin (Outlook)", fake_outlook.sent[0][1])
        self.assertNotIn("Robin (Gmail)", fake_outlook.sent[0][1])

        with (
            patch("gmail_auto.outgoing.validate_provider", return_value="gmail"),
            patch("gmail_auto.outgoing.write", return_value=("Subj", "Body text", [])),
        ):
            item = outgoing_start("to@example.com", "Body text", "Subj", load_settings(), [], provider="gmail")
        self.assertTrue(item["draft"].endswith("Cheers,\nRobin (Gmail)"))

    def test_gmail_signature_import_previews_without_overwriting_until_saved(self):
        self.assertEqual(_html_to_plain_signature("<div>Kind regards,<br>Robin</div><div>InnoPro</div>"), "Kind regards,\nRobin\nInnoPro")
        save_settings({"mail_signatures": {"gmail": "Old Gmail Sig", "outlook": "Outlook Sig"}})

        class FakeClient:
            def get_signature(self):
                return "Imported Gmail Sig"

        app = create_app()
        client = app.test_client()
        with patch("gmail_auto.web.client_for", return_value=FakeClient()):
            res = client.post("/settings/signature/import", follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("Imported Gmail Sig", body)
        self.assertIn("Loaded from Gmail. Click Save signatures below to keep it.", body)
        # Saved settings are untouched until the user submits Save signatures.
        self.assertEqual(load_settings()["mail_signatures"]["gmail"], "Old Gmail Sig")

        res2 = client.post(
            "/settings",
            data={"next": "#send", "signatures_present": "1", "signature_gmail": "Imported Gmail Sig", "signature_outlook": "Outlook Sig"},
            follow_redirects=True,
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(load_settings()["mail_signatures"], {"gmail": "Imported Gmail Sig", "outlook": "Outlook Sig"})
