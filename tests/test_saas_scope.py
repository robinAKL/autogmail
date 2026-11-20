import pytest
from unittest.mock import patch

from gmail_auto import files
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
