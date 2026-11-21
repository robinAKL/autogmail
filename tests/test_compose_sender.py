"""Tests for compose sender mailbox selection and default sending mailbox setting."""

import json
import unittest
from unittest.mock import Mock, patch

import pytest

from gmail_auto import approvals, outgoing, settings, web, writer
from gmail_auto.errors import ConfigError
from gmail_auto.profile_store import save_profile
from tests.test_core import PLACEHOLDER, sample


class ComposeSenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = pytest.importorskip("tempfile").TemporaryDirectory()
        self._env = patch.dict(
            "os.environ",
            {
                "AUTOMAIL_HOME": self._tmp.name,
                "AUTOMAIL_SAAS": "0",
            },
        )
        self._env.start()
        save_profile(PLACEHOLDER)
        self.app = web.create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _fake_client(self, provider: str, email: str) -> Mock:
        c = Mock(provider=provider)
        c.get_user_email.return_value = email
        return c

    def test_default_sender_setting_validation_and_persistence(self):
        assert settings.load_settings()["default_sender"] == ""
        saved = settings.save_settings({"default_sender": "outlook"})
        assert saved["default_sender"] == "outlook"
        assert settings.load_settings()["default_sender"] == "outlook"

        with pytest.raises(ConfigError):
            settings.save_settings({"default_sender": "yahoo"})

    def test_default_provider_prefers_configured_connected_mailbox(self):
        gmail = self._fake_client("gmail", "a@gmail.com")
        outlook = self._fake_client("outlook", "b@outlook.com")

        settings.save_settings({"default_sender": "outlook"})
        with patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])):
            assert outgoing.default_provider() == "outlook"

        # Falls back to first connected when preferred is disconnected
        with patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail], [])):
            assert outgoing.default_provider() == "gmail"

    def test_compose_page_shows_dropdown_for_multiple_mailboxes_and_preselects_default(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")
        settings.save_settings({"default_sender": "outlook"})

        with patch("gmail_auto.web.connected_clients", return_value=([gmail, outlook], [])), patch(
            "gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])
        ):
            resp = self.client.get("/compose")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert '<select name="provider">' in html
        assert "Gmail (alice@gmail.com)" in html
        assert "Outlook (alice@outlook.com)" in html
        assert '<option value="outlook" selected>' in html

    def test_compose_page_uses_hidden_input_for_single_mailbox(self):
        outlook = self._fake_client("outlook", "solo@outlook.com")
        with patch("gmail_auto.web.connected_clients", return_value=([outlook], [])), patch(
            "gmail_auto.mailboxes.connected_clients", return_value=([outlook], [])
        ):
            resp = self.client.get("/compose")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert '<select name="provider">' not in html
        assert '<input type="hidden" name="provider" value="outlook">' in html
        assert "Outlook (solo@outlook.com)" in html

    def test_compose_post_uses_selected_provider_and_rejects_unconnected(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")
        written = json.dumps({"subject": "Hello", "body": "Hi there,\n\nBest,\nAlice"})

        with (
            patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])),
            patch("gmail_auto.llm.model_ready", return_value=True),
            patch("gmail_auto.llm.complete", return_value=written),
        ):
            resp = self.client.post(
                "/compose",
                data={"provider": "outlook", "to": "bob@example.com", "points": "Say hi"},
            )
        assert resp.status_code == 302
        items = approvals.pending()
        assert len(items) == 1
        assert items[0]["mode"] == "new"
        assert items[0]["provider"] == "outlook"

        # Approving sends through the chosen provider's client
        with patch("gmail_auto.mailboxes.client_for", return_value=outlook) as get_client:
            approvals.approve(items[0]["id"], "web")
        get_client.assert_called_once_with("outlook")
        outlook.send_notice.assert_called_once()

        # Unconnected provider is rejected
        with patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail], [])):
            bad = self.client.post(
                "/compose",
                data={"provider": "outlook", "to": "bob@example.com", "points": "Say hi"},
            )
        assert bad.status_code == 302
        assert "/compose" in bad.headers["Location"]

    def test_settings_save_updates_default_sender(self):
        resp = self.client.post(
            "/settings",
            data={"default_sender": "outlook", "next": "connections"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/connections")
        assert settings.load_settings()["default_sender"] == "outlook"

        resp2 = self.client.post(
            "/settings",
            data={"default_sender": "gmail", "next": "#send"},
        )
        assert resp2.status_code == 302
        assert resp2.headers["Location"].endswith("/profile#send")
        assert settings.load_settings()["default_sender"] == "gmail"

    def test_write_start_and_page_preselect_default_sender_for_multiple_mailboxes(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")
        settings.save_settings({"default_sender": "outlook"})

        with patch("gmail_auto.web.connected_clients", return_value=([gmail, outlook], [])), patch(
            "gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])
        ):
            start_resp = self.client.get("/write")
            assert start_resp.status_code == 302
            page_resp = self.client.get(start_resp.headers["Location"])
        html = page_resp.get_data(as_text=True)
        assert page_resp.status_code == 200
        assert '<select name="provider">' in html
        assert '<option value="outlook" selected>' in html

    def test_write_act_queue_and_sync_updates_provider_on_queued_draft(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")

        with patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])):
            session = writer.start(to="bob@example.com", subject="Hi", body="Hello Bob.", provider="gmail")
            q_resp = self.client.post(
                f"/write/{session['id']}/queue",
                data={"provider": "gmail", "to": "bob@example.com", "subject": "Hi", "body": "Hello Bob."},
            )
            assert q_resp.status_code == 302
            queued = approvals.pending()[0]
            assert queued["provider"] == "gmail"

            e_resp = self.client.post(
                f"/write/{session['id']}/edit",
                data={"provider": "outlook", "to": "bob@example.com", "subject": "Hi", "body": "Hello Bob."},
            )
            assert e_resp.status_code == 302
            assert writer.get(session["id"])["provider"] == "outlook"
            assert approvals.get(queued["id"])["provider"] == "outlook"

    def test_write_act_send_uses_selected_provider(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")

        with (
            patch("gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])),
            patch("gmail_auto.mailboxes.client_for", return_value=outlook) as get_client,
        ):
            session = writer.start(to="bob@example.com", subject="Hi", body="Hello Bob.")
            s_resp = self.client.post(
                f"/write/{session['id']}/send",
                data={"provider": "outlook", "to": "bob@example.com", "subject": "Hi", "body": "Hello Bob."},
            )
        assert s_resp.status_code == 302
        get_client.assert_called_once_with("outlook")
        outlook.send_notice.assert_called_once()

    def test_write_reply_session_locks_provider_to_receiving_mailbox(self):
        gmail = self._fake_client("gmail", "alice@gmail.com")
        outlook = self._fake_client("outlook", "alice@outlook.com")
        mail = sample()
        mail.provider = "gmail"
        mail.account = "alice@gmail.com"
        reply_item = approvals.create(mail, draft=type("D", (), {"summary": "Wants to meet", "reason": "Ask", "reply": "Sure.", "sources": []})(), role="ask", language="en")
        session = writer.from_approval(reply_item, source="web")

        with patch("gmail_auto.web.connected_clients", return_value=([gmail, outlook], [])), patch(
            "gmail_auto.mailboxes.connected_clients", return_value=([gmail, outlook], [])
        ):
            page = self.client.get(f"/write/{session['id']}")
            html = page.get_data(as_text=True)
            assert page.status_code == 200
            assert '<select name="provider">' not in html
            assert "locked to the receiving mailbox" in html

            with pytest.raises(ConfigError):
                writer.edit(session["id"], provider="outlook")
            assert approvals.get(reply_item["id"])["provider"] == "gmail"
