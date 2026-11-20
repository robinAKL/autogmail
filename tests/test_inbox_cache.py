import os
import tempfile
import time
import unittest
from unittest.mock import patch

from gmail_auto.storage import save_doc
from gmail_auto.web import _INBOX_STORE_TTL, _forget_inbox, create_app
from tests.test_core import FakeGmail, sample


class InboxCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("AUTOMAIL_HOME")
        self.old_vercel = os.environ.get("VERCEL")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        _forget_inbox()

    def tearDown(self):
        _forget_inbox()
        if self.old_home is None:
            os.environ.pop("AUTOMAIL_HOME", None)
        else:
            os.environ["AUTOMAIL_HOME"] = self.old_home
        if self.old_vercel is None:
            os.environ.pop("VERCEL", None)
        else:
            os.environ["VERCEL"] = self.old_vercel
        self.tmp.cleanup()

    def test_stale_inbox_snapshot_kept_as_offline_in_hosted_mode(self):
        os.environ["VERCEL"] = "1"
        save_doc(
            "inbox_cache",
            {
                "who": "owner@example.com",
                "at": time.time() - (_INBOX_STORE_TTL + 120),
                "rows": [
                    {
                        "key": "gmail:m-stale",
                        "provider": "gmail",
                        "account": "owner@example.com",
                        "sender": "Client <client@example.com>",
                        "subject": "Stale snapshot subject",
                        "snippet": "Still visible after 15 minutes",
                        "date": "2026-11-20T05:00:00Z",
                        "kind": "open",
                        "label": "Open",
                        "group": "todo",
                        "story": "",
                    }
                ],
                "accounts": [{"provider": "gmail", "label": "Gmail", "email": "owner@example.com"}],
                "provider_counts": {"gmail": 1},
                "errors": [],
                "offline": False,
            },
        )
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "owner@example.com"
        with patch("gmail_auto.web.connected_clients", return_value=([], [])):
            res = client.get("/")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("Stale snapshot subject", body)
        self.assertIn("Press Refresh list to load the newest mail.", body)

    def test_inbox_refresh_bypasses_in_memory_cache(self):
        os.environ.pop("VERCEL", None)
        first = sample(id="m-1", subject="First fetch")
        second = sample(id="m-2", subject="Second fetch after refresh")
        fake = FakeGmail([first])
        app = create_app()
        client = app.test_client()

        with patch("gmail_auto.web.connected_clients", return_value=([fake], [])):
            res1 = client.get("/")
            self.assertEqual(res1.status_code, 200)
            self.assertIn("First fetch", res1.get_data(as_text=True))

            fake.mails = [second]
            res2 = client.get("/inbox/refresh", headers={"Accept": "application/json"})
            self.assertEqual(res2.status_code, 200)
            payload = res2.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["count"], 1)

            res3 = client.get("/")
            self.assertIn("Second fetch after refresh", res3.get_data(as_text=True))
