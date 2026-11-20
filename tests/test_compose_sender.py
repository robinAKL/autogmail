"""Tests for compose sender mailbox selection and default sending mailbox setting."""

import json
import unittest
from unittest.mock import Mock, patch

import pytest

from gmail_auto import approvals, outgoing, settings, web
from gmail_auto.errors import ConfigError
from gmail_auto.profile_store import save_profile
from tests.test_core import PLACEHOLDER


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
