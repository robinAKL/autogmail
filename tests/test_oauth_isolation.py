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


def test_token_store_isolates_by_user_and_fails_closed(monkeypatch):
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
        monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
        with fake_tenant(True, "user_a"):
            with pytest.raises(ConfigError):
                token_store.save_token_raw('{"refresh_token":"ra"}')

        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")

        with fake_tenant(True, ""):
            with pytest.raises(ConfigError):
                token_store.load_token_raw()
            with pytest.raises(ConfigError):
                token_store.save_token_raw("{}")
            with pytest.raises(ConfigError):
                token_store.clear_token()

        with fake_tenant(True, "user_a"):
            assert token_store.save_token_raw('{"refresh_token":"ra"}') is True
            assert kv["autogmail:u:user_a:token"].startswith("enc:v1:")
            assert "refresh_token" not in kv["autogmail:u:user_a:token"]
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

        monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
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
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")
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

    # 3. Same user Gmail callback: rejects refresh_token reuse when provider identity differs,
    #    and reuses only when identity matches, without storing gmail_token in cookie.
    from gmail_auto.token_crypto import encrypt_token

    kv = {"autogmail:u:user_a:token": encrypt_token(json.dumps({"refresh_token": "kept-refresh"}))}
    fake_creds = MagicMock()
    fake_creds.refresh_token = None
    fake_creds.to_json.return_value = json.dumps({"token": "new-access", "client_id": "gid"})
    fake_flow = MagicMock()
    fake_flow.credentials = fake_creds
    fake_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?state=s1", "s1")
    fake_flow.code_verifier = "ver"

    fake_service = MagicMock()
    with (
        fake_tenant(True, "user_a"),
        patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=fake_flow),
        patch("gmail_auto.gmail_client._build_service", return_value=fake_service),
        patch("gmail_auto.web.gmail_status", return_value={"email": "bound@gmail.com"}),
        patch("gmail_auto.web.mark_gmail_authorized"),
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
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"

        # Different Gmail identity without refresh_token -> must reject
        fake_service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "other@gmail.com"}
        client.get("/oauth")
        resp = client.get("/oauth/callback?state=s1&code=ok")
        assert resp.status_code == 302
        assert "/connections" in resp.headers["Location"]

        # Same Gmail identity without refresh_token -> reuses kept-refresh, encrypted at rest, no cookie token
        fake_service.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "bound@gmail.com"}
        client.get("/oauth")
        resp = client.get("/oauth/callback?state=s1&code=ok")
        assert resp.status_code == 302
        assert "/connections" not in resp.headers["Location"]
        with client.session_transaction() as sess:
            assert "gmail_token" not in sess
        assert kv["autogmail:u:user_a:token"].startswith("enc:v1:")
        saved = json.loads(token_store.load_token_raw())
        assert saved["refresh_token"] == "kept-refresh"
        assert saved["token"] == "new-access"


def test_gmail_status_is_user_scoped_and_vercel_without_kv_fails_closed(tmp_path, monkeypatch):
    from gmail_auto import accounts, web

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")

    docs = {}
    with (
        patch("gmail_auto.storage.load_doc", side_effect=lambda name, default=None: docs.get((accounts._saas_scope()[1], name), default)),
        patch("gmail_auto.storage.save_doc", side_effect=lambda name, val: docs.__setitem__((accounts._saas_scope()[1], name), val)),
    ):
        with fake_tenant(True, "user_a"):
            accounts.mark_gmail_authorized("bound@gmail.com", at=1000.0)
            assert accounts.gmail_status()["email"] == "bound@gmail.com"
        with fake_tenant(True, "user_b"):
            assert accounts.gmail_status()["email"] == ""
        assert not (tmp_path / "data" / "accounts.json").exists()

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gsec")
    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    with fake_tenant(True, "user_a"), patch("gmail_auto.token_store.storage_ready", return_value=False):
        with client.session_transaction() as sess:
            sess["user"] = "a@example.com"
            sess["user_id"] = "user_a"
        resp = client.get("/oauth")
        assert resp.status_code == 302
        assert "/connections" in resp.headers["Location"]
        assert not (tmp_path / "data" / "users" / "user_a" / "token.json").exists()


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


def test_microsoft_s256_pkce_flow(tmp_path, monkeypatch):
    import base64
    import hashlib
    import urllib.parse
    from gmail_auto import web

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOMAIL_SAAS", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-flask-secret")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")
    monkeypatch.setenv("MS_CLIENT_ID", "mid")
    monkeypatch.setenv("MS_CLIENT_SECRET", "msec")

    app = web.create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["user"] = "a@example.com"
        sess["user_id"] = "a" * 32

    resp = client.get("/oauth/microsoft/outlook")
    assert resp.status_code == 302
    parsed = urllib.parse.urlparse(resp.headers["Location"])
    params = urllib.parse.parse_qs(parsed.query)
    assert params["code_challenge_method"] == ["S256"]
    with client.session_transaction() as sess:
        verifier = sess["ms_state"]["code_verifier"]
        state = sess["ms_state"]["state"]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    )
    assert params["code_challenge"] == [expected_challenge]

    with patch("gmail_auto.microsoft.finish_sign_in", return_value="o@outlook.com") as mock_finish:
        cb = client.get(f"/oauth/microsoft/callback?state={state}&code=auth-code")
        assert cb.status_code == 302
        mock_finish.assert_called_once()
        assert mock_finish.call_args.kwargs["code_verifier"] == verifier


def test_gmail_refresh_does_not_overwrite_concurrent_disconnect(tmp_path, monkeypatch):
    from gmail_auto import gmail_client, token_store
    from gmail_auto.tenant import user_scope

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOMAIL_SAAS", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-flask-secret")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")

    uid = "a" * 32
    kv = {}
    initial_raw = gmail_client.stamp_token_gen(
        json.dumps({"token": "old", "refresh_token": "r1", "client_id": "cid", "client_secret": "sec"}),
        gen="gen1",
    )

    with (
        user_scope(uid),
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
        token_store.save_token_raw(initial_raw)
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "r1"
        fake_creds.to_json.return_value = json.dumps({"token": "refreshed", "refresh_token": "r1"})

        def disconnect_during_refresh(_req):
            token_store.clear_token()

        fake_creds.refresh.side_effect = disconnect_during_refresh
        with pytest.raises(NotConnected):
            gmail_client._refresh_saved(fake_creds, None, persist_remote=True, expected_gen="gen1")
        assert token_store.load_token_raw() is None


def test_microsoft_refresh_does_not_overwrite_concurrent_disconnect(tmp_path, monkeypatch):
    from gmail_auto import microsoft
    from gmail_auto.tenant import user_scope
    from gmail_auto.token_crypto import encrypt_token

    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOMAIL_SAAS", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-flask-secret")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-master-secret")
    monkeypatch.setenv("MS_CLIENT_ID", "mid")
    monkeypatch.setenv("MS_CLIENT_SECRET", "msec")

    uid = "b" * 32
    docs = {
        "ms_outlook": {
            "access_token": encrypt_token("old"),
            "refresh_token": encrypt_token("ms-r1"),
            "expires_at": 0,
            "conn_gen": "ms-gen-1",
        }
    }

    def fake_token_req(_form):
        docs.pop("ms_outlook", None)
        return {"access_token": "new-ms", "refresh_token": "ms-r2", "expires_in": 3600}

    with (
        user_scope(uid),
        patch("gmail_auto.microsoft.load_doc", side_effect=lambda name, default=None: docs.get(name, default)),
        patch("gmail_auto.microsoft.save_doc", side_effect=lambda name, val: docs.__setitem__(name, val)),
        patch("gmail_auto.microsoft._token_request", side_effect=fake_token_req),
    ):
        with pytest.raises(NotConnected):
            microsoft.access_token("outlook")
        assert "ms_outlook" not in docs
