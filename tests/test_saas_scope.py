import pytest
from unittest.mock import patch

from gmail_auto import files, login_sessions
from gmail_auto.errors import ConfigError
from gmail_auto.profile_store import profile_path
from gmail_auto.storage import load_doc, save_doc
from gmail_auto.tenant import current_user_id, user_scope
from gmail_auto.web import create_app


USER_A = "a" * 32
USER_B = "b" * 32


@pytest.fixture
def saas(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAIL_SAAS", "1")
    monkeypatch.setenv("FLASK_SECRET_KEY", "local-test-secret")
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("AUTOMAIL_PUBLIC_ORIGIN", raising=False)
    with patch("gmail_auto.storage.storage_ready", return_value=False):
        yield


def test_documents_files_and_profile_are_separate(saas):
    with user_scope(USER_A):
        save_doc("settings", {"private": True})
        attachment = files.save(b"private attachment", "private.txt")
        profile = profile_path()
        assert files.read(attachment["id"]) == b"private attachment"
        with user_scope(USER_B):
            assert load_doc("settings") is None
            assert files.get(attachment["id"]) is None
            assert profile_path() != profile
        assert load_doc("settings") == {"private": True}
    assert current_user_id() is None
    with pytest.raises(ConfigError, match="Sign in"):
        load_doc("settings")


def test_redis_and_object_keys_are_scoped(saas, monkeypatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "test-account")
    monkeypatch.setenv("R2_BUCKET", "test-bucket")
    with patch("gmail_auto.storage.storage_ready", return_value=True), patch("gmail_auto.storage.kv_set") as write:
        with user_scope(USER_A):
            save_doc("settings", {})
            path_a = files._where("1234")[1]
        with user_scope(USER_B):
            save_doc("settings", {})
            assert files._where("1234")[1] != path_a
    assert [call.args[0] for call in write.call_args_list] == [
        f"autogmail:u:{USER_A}:settings", f"autogmail:u:{USER_B}:settings"
    ]


def test_business_requests_use_only_verified_session_workspace(saas):
    app = create_app()

    @app.get("/workspace-test")
    def workspace_test():
        return {"workspace": current_user_id(), "data": load_doc("private")}

    with user_scope(USER_A):
        save_doc("private", "private A")

    client = app.test_client()
    assert client.get("/workspace-test").status_code == 302
    assert client.get("/cron").status_code == 404
    assert client.get("/login").status_code == 200
    with client.session_transaction() as session:
        session["user_id"] = USER_B
        session["login_session_id"] = login_sessions.create(USER_B)
        session["user"] = "b@example.com"
    assert client.get("/workspace-test").json == {"workspace": USER_B, "data": None}
    assert current_user_id() is None
    assert client.post("/run", headers={"Origin": "https://other.example"}).status_code == 403


def test_saas_requires_explicit_session_secret(saas, monkeypatch):
    monkeypatch.delenv("FLASK_SECRET_KEY")
    with pytest.raises(ConfigError, match="FLASK_SECRET_KEY"):
        create_app()



def test_uncertain_provider_delivery_cannot_be_retried(saas):
    from unittest.mock import Mock

    from gmail_auto import approvals

    client = Mock()
    client.send_notice.side_effect = TimeoutError("provider did not respond")
    with user_scope(USER_A):
        item = approvals.create_new("b@example.com", "Hello", "Hello")
        with pytest.raises(ConfigError, match="Delivery could not be confirmed"):
            approvals.approve(item["id"], "web", client)
        assert approvals.get(item["id"])["status"] == "send_unconfirmed"
        with pytest.raises(ConfigError, match="no longer waiting"):
            approvals.approve(item["id"], "web", client)
    client.send_notice.assert_called_once()


def test_approval_lock_uses_atomic_redis_claim(saas):
    from gmail_auto.storage import workspace_operation

    with patch("gmail_auto.storage.storage_ready", return_value=True), patch(
        "gmail_auto.storage.kv_set_if_absent", side_effect=[True, False]
    ) as claim, patch("gmail_auto.storage.kv_delete_if_value") as release:
        with user_scope(USER_A), workspace_operation("approvals"):
            with pytest.raises(ConfigError, match="already in progress"):
                with workspace_operation("approvals"):
                    pytest.fail("A second worker entered the critical section.")
    key = f"autogmail:u:{USER_A}:operation:approvals"
    assert claim.call_args_list[0].args[0] == claim.call_args_list[1].args[0] == key
    release.assert_called_once_with(key, claim.call_args_list[0].args[1])


def test_hosted_saas_requires_a_canonical_origin_and_persistent_storage(saas, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(ConfigError, match="AUTOMAIL_PUBLIC_ORIGIN"):
        create_app()
    monkeypatch.setenv("AUTOMAIL_PUBLIC_ORIGIN", "https://mail.example")
    with patch("gmail_auto.web.storage_ready", return_value=False):
        with pytest.raises(ConfigError, match="Redis"):
            create_app()
    with patch("gmail_auto.web.storage_ready", return_value=True):
        client = create_app().test_client()
        assert client.get("/login", base_url="https://other.example").status_code == 403
        assert client.get("/login", base_url="https://mail.example").status_code == 200


def test_reply_cannot_be_sent_from_a_replacement_mailbox(saas):
    from unittest.mock import Mock

    from gmail_auto import approvals
    from gmail_auto.models import Draft
    from tests.test_core import sample

    client = Mock()
    client.get_user_email.return_value = "replacement@example.com"
    mail = sample(id="original")
    mail.account = "original@example.com"
    with user_scope(USER_A):
        item = approvals.create(mail, Draft(reply="Original reply."), "en")
        with pytest.raises(ConfigError, match="Reconnect the mailbox"):
            approvals.approve(item["id"], "web", client)
        assert approvals.get(item["id"])["status"] == "pending"
    client.send_reply.assert_not_called()


def test_knowledge_cache_never_reuses_chunks_from_another_workspace(saas):
    from gmail_auto import knowledge

    index = {"stamp": "same", "docs": [{"id": "notes", "title": "Notes"}]}
    with patch.object(knowledge, "load_index", return_value=index):
        with user_scope(USER_A):
            save_doc(knowledge._body_name("notes"), {"text": "Workspace alpha owns this private note."})
            assert "alpha" in knowledge._chunks()[0]["text"]
        with user_scope(USER_B):
            save_doc(knowledge._body_name("notes"), {"text": "Workspace bravo owns this private note."})
            assert "bravo" in knowledge._chunks()[0]["text"]


def test_new_workspace_has_neutral_profile_without_inheriting_legacy_profile(saas, tmp_path):
    from gmail_auto.profile_store import load_profile

    (tmp_path / "profile.json").write_text('{"name":"Legacy user"}', encoding="utf-8")
    with user_scope(USER_B):
        assert load_profile()["name"] == "Mailbox owner"


def test_saas_opened_mail_is_stamped_with_the_connected_mailbox(saas):
    from gmail_auto.web import _find_mail
    from tests.test_core import sample

    app = create_app()
    mail = sample(id="message-id")
    with app.test_request_context(), user_scope(USER_A), patch("gmail_auto.web.client_for") as mailbox:
        mailbox.return_value.get_user_email.return_value = "connected@example.com"
        mailbox.return_value.get_message.return_value = mail
        assert _find_mail(mail.id).account == "connected@example.com"


def test_bound_gmail_login_opens_a_distinct_workspace_and_logout_revokes_old_cookie(saas, monkeypatch):
    from unittest.mock import Mock

    from gmail_auto.accounts import gmail_status
    from gmail_auto.gmail_client import local_token_path

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only-token-key")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "test-google-client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "test-google-secret")
    monkeypatch.setenv("AUTOMAIL_PUBLIC_ORIGIN", "https://mail.example")
    app = create_app()

    @app.get("/workspace-proof")
    def workspace_proof():
        return {
            "user_id": current_user_id(),
            "bound": gmail_status()["email"],
            "encrypted": local_token_path().read_text(encoding="utf-8").startswith("enc:v1:")
            if local_token_path().exists() else False,
        }

    host = "https://mail.example"
    browser = app.test_client()
    codes = {}
    flow = Mock()
    flow.authorization_url.return_value = ("https://accounts.google.com/test", "provider-state")
    flow.code_verifier = "provider-verifier"
    flow.credentials.refresh_token = "fake-refresh"
    flow.credentials.to_json.return_value = '{"refresh_token":"fake-refresh"}'
    service = Mock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "bound@gmail.com"
    }

    def login(email):
        started = browser.post("/login/code", base_url=host, data={"email": email})
        assert started.status_code == 302
        completed = browser.post("/login", base_url=host, data={"email": email, "code": codes[email]})
        assert completed.status_code == 302

    with patch("gmail_auto.web.deliver_code", side_effect=lambda email, code: codes.update({email: code})), patch(
        "google_auth_oauthlib.flow.Flow.from_client_config", return_value=flow
    ), patch("gmail_auto.gmail_client._build_service", return_value=service):
        login("sign-in@other.example")
        assert browser.get("/oauth", base_url=host).location == "https://accounts.google.com/test"
        result = browser.get("/oauth/callback?state=provider-state&code=one-use", base_url=host)
        assert result.status_code == 302
        first = browser.get("/workspace-proof", base_url=host).json
        assert first["bound"] == "bound@gmail.com" and first["encrypted"]
        saved_cookie = browser.get_cookie(app.config["SESSION_COOKIE_NAME"], domain="mail.example").value
        browser.get("/logout", base_url=host)

        replay = app.test_client()
        replay.set_cookie(app.config["SESSION_COOKIE_NAME"], saved_cookie, domain="mail.example", secure=True)
        assert replay.get("/workspace-proof", base_url=host).status_code == 302

        login("bound@gmail.com")
        second = browser.get("/workspace-proof", base_url=host).json
        assert second["user_id"] != first["user_id"]
        assert second["bound"] == "" and not second["encrypted"]


def test_refreshing_two_mailboxes_keeps_request_and_worker_scopes_separate(saas):
    from unittest.mock import Mock

    app = create_app()
    app.testing = True
    browser = app.test_client()
    with browser.session_transaction() as session:
        session["user"] = "owner@example.com"
        session["user_id"] = USER_A
        session["login_session_id"] = login_sessions.create(USER_A)

    workers = []

    def list_recent(*_args, **_kwargs):
        workers.append(current_user_id())
        return []

    gmail = Mock(provider="gmail")
    gmail.get_user_email.return_value = "gmail@example.com"
    gmail.list_recent.side_effect = list_recent
    outlook = Mock(provider="outlook")
    outlook.get_user_email.return_value = "outlook@example.com"
    outlook.list_recent.side_effect = list_recent

    with patch("gmail_auto.web.connected_clients", return_value=([gmail, outlook], [])):
        response = browser.get("/inbox/refresh")

    assert response.status_code == 302
    assert workers == [USER_A, USER_A]
    assert current_user_id() is None


def test_saas_sending_modes_require_confirmation_before_automatic_sending(saas):
    from gmail_auto.settings import load_settings
    from gmail_auto.store import is_live

    browser = create_app().test_client()
    with browser.session_transaction() as session:
        session["user"] = "owner@example.com"
        session["user_id"] = USER_A
        session["login_session_id"] = login_sessions.create(USER_A)

    page = browser.get("/profile").get_data(as_text=True)
    for mode in ("draft", "always", "smart", "never"):
        assert f'name="mode" value="{mode}"' in page

    with patch("gmail_auto.runner.require_openai_key"):
        browser.post("/mode", data={"mode": "always", "confirm": "1"})
        browser.post("/mode", data={"mode": "smart"})
        with user_scope(USER_A):
            assert load_settings()["approval_mode"] == "always"
        for mode in ("smart", "never"):
            browser.post("/mode", data={"mode": mode, "confirm": "1"})
            with user_scope(USER_A):
                assert is_live()
                assert load_settings()["approval_mode"] == mode
        browser.post("/mode", data={"mode": "draft"})
        with user_scope(USER_A):
            assert not is_live()


def test_auto_reply_timeout_records_unconfirmed_delivery_and_skips_retry(saas):
    from unittest.mock import Mock

    from gmail_auto.models import Draft
    from gmail_auto.profile_store import load_profile
    from gmail_auto.runner import _run_mailbox, RunReport
    from gmail_auto.settings import load_settings
    from gmail_auto.store import get_processed
    from tests.test_core import sample

    mail = sample(id="auto-delivery")
    gmail = Mock(provider="gmail")
    gmail.get_user_email.return_value = "owner@example.com"
    gmail.list_unread.return_value = [mail]
    gmail.send_reply.side_effect = TimeoutError("no provider response")
    generate = Mock(return_value=Draft(action="reply", reply="Thanks for writing."))
    report = RunReport(live=True, finished_at="")
    with user_scope(USER_A), patch("gmail_auto.runner.storage_ready", return_value=False):
        settings = load_settings()
        settings.update(approval_mode="never", translation_mode="off")
        profile = load_profile()
        _run_mailbox(gmail, generate, profile, settings, True, report)
        assert get_processed(mail.key)["action"] == "send_unconfirmed"
        assert report.failed == 1
        _run_mailbox(gmail, generate, profile, settings, True, report)

    gmail.send_reply.assert_called_once()


def test_inbox_reports_counts_and_filters_connected_mailboxes(saas):
    from unittest.mock import Mock
    from tests.test_core import sample

    browser = create_app().test_client()
    with browser.session_transaction() as session:
        session["user"] = "owner@example.com"
        session["user_id"] = USER_A
        session["login_session_id"] = login_sessions.create(USER_A)

    gmail = Mock(provider="gmail")
    gmail.get_user_email.return_value = "gmail@example.com"
    gmail.list_recent.return_value = [sample(id="gmail-one", subject="Only in Gmail")]
    outlook = Mock(provider="outlook")
    outlook.get_user_email.return_value = "outlook@example.com"
    outlook_mail = sample(id="outlook-one", subject="Only in Outlook")
    outlook_mail.provider = "outlook"
    outlook.list_recent.return_value = [outlook_mail]

    with patch("gmail_auto.web.connected_clients", return_value=([gmail, outlook], [])), patch(
        "gmail_auto.web._account_chips", return_value=[]
    ):
        refresh = browser.get("/inbox/refresh?mailbox=outlook")
        assert "mailbox=outlook" in refresh.location
        counts = browser.get("/inbox/refresh", headers={"Accept": "application/json"}).json
        assert counts["provider_counts"] == {"gmail": 1, "outlook": 1}
        page = browser.get("/?show=all&mailbox=outlook").get_data(as_text=True)
        assert "Only in Outlook" in page
        assert "Only in Gmail" not in page
        assert "Outlook · outlook@example.com · 1 loaded" in page


def _signed_in(browser, user_id):
    with browser.session_transaction() as session:
        session["user"] = f"{user_id[:6]}@example.com"
        session["user_id"] = user_id
        session["login_session_id"] = login_sessions.create(user_id)


def _sent_client(provider, email, subject, recipient, stamp):
    from unittest.mock import Mock

    from gmail_auto.models import Mail

    mail = Mail(
        id=f"{provider}-{stamp}",
        thread_id=f"thread-{stamp}",
        from_name="Owner",
        from_email=email,
        reply_email="",
        subject=subject,
        date="2026-11-20 10:00",
        body="",
        headers={"to": recipient},
        snippet=subject,
        provider=provider,
        received_ts=stamp,
    )
    client = Mock(provider=provider)
    client.get_user_email.return_value = email

    def list_sent(*_args, **_kwargs):
        assert current_user_id()
        return [mail]

    client.list_sent.side_effect = list_sent
    return client


def test_sent_menu_lists_only_the_signed_in_users_mailboxes(saas):
    app = create_app()
    app.testing = True
    browser = app.test_client()
    anonymous = browser.get("/sent")
    assert anonymous.status_code == 302
    assert "/login" in (anonymous.location or "")

    gmail_a = _sent_client("gmail", "a.gmail@example.com", "Alpha sent note", "lee@client.example", 200)
    outlook_a = _sent_client("outlook", "a.outlook@example.com", "Alpha outlook note", "sam@client.example", 100)
    gmail_b = _sent_client("gmail", "b.gmail@example.com", "Beta sent note", "other@client.example", 300)

    def clients():
        if current_user_id() == USER_A:
            return [gmail_a, outlook_a], []
        if current_user_id() == USER_B:
            return [gmail_b], []
        return [], []

    with patch("gmail_auto.web.connected_clients", side_effect=clients):
        _signed_in(browser, USER_A)
        first = browser.get("/sent")
        _signed_in(browser, USER_B)
        second = browser.get("/sent")

    assert first.status_code == 200
    page_a = first.get_data(as_text=True)
    assert "Alpha sent note" in page_a
    assert "Alpha outlook note" in page_a
    assert 'class="chip tiny source-mailbox" translate="no">a.gmail@example.com' in page_a
    assert 'class="chip tiny source-mailbox" translate="no">a.outlook@example.com' in page_a
    assert 'href="/sent"' in page_a
    assert "Beta sent note" not in page_a

    assert second.status_code == 200
    page_b = second.get_data(as_text=True)
    assert "Beta sent note" in page_b
    assert 'class="chip tiny source-mailbox" translate="no">b.gmail@example.com' in page_b
    assert "Alpha sent note" not in page_b
    assert "a.gmail@example.com" not in page_b
    assert "a.outlook@example.com" not in page_b
