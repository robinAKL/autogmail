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

    def test_refresh_returns_the_first_page_and_later_pages_come_from_the_server(self):
        os.environ["VERCEL"] = "1"
        fake = _PagedMailbox()
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "owner@example.com"
        with patch("gmail_auto.web.connected_clients", return_value=([fake], [])):
            refreshed = client.get("/inbox/refresh", headers={"Accept": "application/json"})
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(refreshed.get_json()["count"], 20)
            self.assertEqual(fake.calls, [("", 20, False)])

            first = client.get("/")
            self.assertEqual(first.status_code, 200)
            first_body = first.get_data(as_text=True)
            self.assertIn("Letter 000", first_body)
            self.assertNotIn("Letter 020", first_body)
            self.assertIn("Older", first_body)
            self.assertEqual(fake.calls, [("", 20, False)])

            second = client.get("/?page=2")
            second_body = second.get_data(as_text=True)
            self.assertIn("Letter 020", second_body)
            self.assertNotIn("Letter 040", second_body)
            self.assertEqual(fake.calls[1][0], "20")

            client.get("/?page=6")
            self.assertEqual(len(fake.calls), 5)
            self.assertNotIn("100", [token for token, _limit, _full in fake.calls])

    def test_a_failed_later_page_keeps_the_letters_already_loaded(self):
        os.environ["VERCEL"] = "1"
        fake = _PagedMailbox(fail_after="")
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "owner@example.com"
        with patch("gmail_auto.web.connected_clients", return_value=([fake], [])):
            client.get("/inbox/refresh", headers={"Accept": "application/json"})
            later = client.get("/?page=2")
            body = later.get_data(as_text=True)
            self.assertIn("Letter 000", body)
            self.assertNotIn("Letter 020", body)
            self.assertIn("Gmail timed out", body)

    def test_more_reads_the_next_server_page_until_the_cap(self):
        os.environ["VERCEL"] = "1"
        fake = _PagedMailbox()
        app = create_app()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = "owner@example.com"
        with patch("gmail_auto.web.connected_clients", return_value=([fake], [])):
            client.get("/inbox/refresh", headers={"Accept": "application/json"})
            first = client.get("/").get_data(as_text=True)
            self.assertIn('data-inbox-more="1"', first)
            self.assertIn("/inbox/more", first)
            self.assertNotIn("Letter 020", first)
            body = {"hasMore": True}
            while body.get("hasMore"):
                body = client.get("/inbox/more", headers={"Accept": "application/json"}).get_json()
                self.assertTrue(body["ok"])
            self.assertEqual(body["count"], 100)
            self.assertEqual(body["added"], 20)
            self.assertEqual(len(fake.calls), 5)
            self.assertEqual(fake.calls[-1][0], "80")
            filled = client.get("/").get_data(as_text=True)
            self.assertIn("All<span>100</span>", filled)
            self.assertNotIn('data-inbox-more="1"', filled)

    def test_outgoing_translation_checkbox_sits_beside_its_label(self):
        os.environ.pop("VERCEL", None)
        app = create_app()
        page = app.test_client().get("/profile").get_data(as_text=True)
        self.assertIn('class="span-2 check"', page)
        self.assertIn('name="translate_outgoing"', page)
        self.assertNotIn("inline-check", page)


class _PagedMailbox:
    provider = "gmail"

    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def get_user_email(self):
        return "owner@example.com"

    def list_recent_page(self, limit=20, token="", full=False):
        self.calls.append((token, limit, full))
        if self.fail_after is not None and token != self.fail_after and token:
            from gmail_auto.errors import ConfigError

            raise ConfigError("Gmail timed out")
        start = int(token or "0")
        if start >= 100:
            return [], ""
        mails = []
        for i in range(start, start + limit):
            hour, minute = divmod(12 * 60 - i, 60)
            mails.append(sample(id=f"m{i}", subject=f"Letter {i:03d}", snippet="note", date=f"2026-11-20 {hour:02d}:{minute:02d}"))
        nxt = "" if start + limit >= 100 else str(start + limit)
        return mails, nxt
