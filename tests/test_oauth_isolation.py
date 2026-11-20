import json
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from gmail_auto.errors import ConfigError, NotConnected


@contextmanager
def fake_tenant(saas: bool, user_id: str):
    mod = types.ModuleType("gmail_auto.tenant")
    mod.saas_enabled = lambda: saas
    mod.current_user_id = lambda: user_id
    prev = sys.modules.get("gmail_auto.tenant")
    sys.modules["gmail_auto.tenant"] = mod
    try:
        yield
    finally:
        if prev is None:
            sys.modules.pop("gmail_auto.tenant", None)
        else:
            sys.modules["gmail_auto.tenant"] = prev


def test_token_store_isolates_by_user_and_fails_closed():
    from gmail_auto import token_store

    kv = {}
    with (
        patch.object(token_store, "storage_ready", return_value=True),
        patch.object(
            token_store,
            "_command",
            side_effect=lambda args: (
                kv.get(args[1])
                if args[0] == "GET"
                else kv.__setitem__(args[1], args[2])
                if args[0] == "SET"
                else kv.pop(args[1], None)
            ),
        ),
    ):
        with fake_tenant(True, ""):
            with pytest.raises(ConfigError):
                token_store.load_token_raw()
            with pytest.raises(ConfigError):
                token_store.save_token_raw("{}")
            with pytest.raises(ConfigError):
                token_store.clear_token()

        with fake_tenant(True, "user_a"):
            assert token_store.save_token_raw('{"refresh_token":"ra"}') is True
            assert token_store.load_token_raw() == '{"refresh_token":"ra"}'

        with fake_tenant(True, "user_b"):
            assert token_store.load_token_raw() is None
            assert token_store.save_token_raw('{"refresh_token":"rb"}') is True

        with fake_tenant(True, "user_a"):
            assert token_store.load_token_raw() == '{"refresh_token":"ra"}'
            token_store.clear_token()
            assert token_store.load_token_raw() is None

        with fake_tenant(True, "user_b"):
            assert token_store.load_token_raw() == '{"refresh_token":"rb"}'

        with fake_tenant(False, ""):
            assert token_store.save_token_raw('{"refresh_token":"legacy"}') is True
            assert kv["autogmail:token"] == '{"refresh_token":"legacy"}'


def test_gmail_client_saas_ignores_env_and_isolates_local_token(tmp_path, monkeypatch):
    from gmail_auto import gmail_client

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv(
        "GMAIL_TOKEN_JSON",
        json.dumps({"refresh_token": "env-r", "client_id": "cid", "client_secret": "sec", "token_uri": "https://oauth2.googleapis.com/token"}),
    )

    with fake_tenant(True, ""):
        with pytest.raises(NotConnected):
            gmail_client.load_credentials(allow_browser=False)

    with fake_tenant(True, "user_a"):
        path_a = gmail_client.local_token_path()
        assert str(path_a).endswith(str(tmp_path / "data" / "users" / "user_a" / "token.json"))
        with pytest.raises(NotConnected):
            gmail_client.load_credentials(allow_browser=False)


def test_microsoft_fails_closed_without_user_in_saas():
    from gmail_auto import microsoft

    with fake_tenant(True, ""):
        assert microsoft.status("outlook") == {"connected": False, "account": "", "connected_at": ""}
        with pytest.raises(ConfigError):
            microsoft.disconnect("outlook")
        with pytest.raises(ConfigError):
            microsoft.access_token("outlook")
        with pytest.raises(ConfigError):
            microsoft.finish_sign_in("outlook", "code", "https://app.test/cb")


def test_oauth_callbacks_reject_user_mismatch_and_keep_tokens_server_side(tmp_path, monkeypatch):
    from gmail_auto import web
    from gmail_auto import token_store

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("MS_CLIENT_ID", "mid")
    monkeypatch.setenv("MS_CLIENT_SECRET", "msec")

    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # 1. User A starts Gmail OAuth, then session switches to User B before callback
    with fake_tenant(True, "user_a"):
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"
        resp = client.get("/oauth")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            state = sess["oauth_state"]
            assert sess["oauth_user_id"] == "user_a"

    with fake_tenant(True, "user_b"):
        with client.session_transaction() as sess:
            sess["user"] = "b@example.com"
            sess["user_id"] = "user_b"
        resp = client.get(f"/oauth/callback?state={state}&code=abc")
        assert resp.status_code == 302
        assert "/connections" in resp.headers["Location"]

    # 2. User A starts Microsoft OAuth, then session switches to User B before callback
    with fake_tenant(True, "user_a"):
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"
        resp = client.get("/oauth/microsoft/outlook")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            ms_state = sess["ms_state"]["state"]
            assert sess["ms_state"]["user_id"] == "user_a"

    with fake_tenant(True, "user_b"):
        with client.session_transaction() as sess:
            sess["user"] = "b@example.com"
            sess["user_id"] = "user_b"
        resp = client.get(f"/oauth/microsoft/callback?state={ms_state}&code=abc")
        assert resp.status_code == 302
        assert "/connections" in resp.headers["Location"]

    # 3. Same user Gmail callback does NOT put gmail_token in cookie and preserves refresh_token
    kv = {"autogmail:u:user_a:token": json.dumps({"refresh_token": "kept-refresh"})}
    fake_creds = MagicMock()
    fake_creds.refresh_token = None
    fake_creds.to_json.return_value = json.dumps({"token": "new-access", "client_id": "gid"})
    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds
    fake_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?state=s1", "s1")
    fake_flow.code_verifier = "ver"

    with (
        fake_tenant(True, "user_a"),
        patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=fake_flow),
        patch.object(token_store, "storage_ready", return_value=True),
        patch.object(
            token_store,
            "_command",
            side_effect=lambda args: (
                kv.get(args[1])
                if args[0] == "GET"
                else kv.__setitem__(args[1], args[2])
                if args[0] == "SET"
                else kv.pop(args[1], None)
            ),
        ),
        patch("gmail_auto.gmail_client.GmailClient") as mock_gc,
    ):
        mock_gc.return_value.get_user_email.return_value = "bound@gmail.com"
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"
        client.get("/oauth")
        resp = client.get("/oauth/callback?state=s1&code=ok")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert "gmail_token" not in sess
        saved = json.loads(kv["autogmail:u:user_a:token"])
        assert saved["refresh_token"] == "kept-refresh"
        assert saved["token"] == "new-access"


def test_connections_page_hides_dev_setup_hints_in_saas(tmp_path, monkeypatch):
    from gmail_auto import web

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    monkeypatch.delenv("MS_CLIENT_SECRET", raising=False)

    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with fake_tenant(True, "user_a"):
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"
        resp = client.get("/connections")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert "credentials.json" not in html
        assert "MS_CLIENT_ID" not in html
        assert "temporarily unavailable" in html
