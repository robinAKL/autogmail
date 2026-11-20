from types import SimpleNamespace
from unittest.mock import Mock, patch

from gmail_auto import knowledge_mail, microsoft
from gmail_auto.gmail_client import GmailClient
from tests.test_core import sample


def test_gmail_history_uses_provider_tokens_and_includes_archived_messages():
    client = GmailClient.__new__(GmailClient)
    client.service = Mock()
    messages = client.service.users().messages()
    messages.list.return_value.execute.return_value = {"messages": [{"id": "archived"}], "nextPageToken": "older"}
    with patch.object(client, "_mails_for_ids", return_value=[sample(id="archived")]) as hydrate:
        letters, next_cursor = knowledge_mail.page(client, "inbox", "previous", 8)
        assert [letter.id for letter in letters] == ["archived"]
        assert next_cursor == "older"
        query = messages.list.call_args.kwargs
        assert "in:inbox" not in query["q"]
        assert all(term in query["q"] for term in ("-in:sent", "-in:drafts", "-in:spam", "-in:trash"))
        assert query["pageToken"] == "previous"
        hydrate.assert_called_once_with(["archived"], True)

        knowledge_mail.page(client, "sent", "", 8)
        assert messages.list.call_args.kwargs["q"].startswith("in:sent ")


def test_outlook_history_follows_folder_and_message_cursors():
    next_folders = microsoft.GRAPH + "/me/mailFolders?$skip=50"
    next_letters = microsoft.GRAPH + "/me/mailFolders/inbox/messages?$skip=8"
    prefix = "/me/mailFolders"
    requested = []

    def letters(message_id):
        return {"value": [{
            "id": message_id, "subject": "Test email",
            "uniqueBody": {"contentType": "text", "content": "The latest message text"},
            "receivedDateTime": "2026-11-20T00:15:00Z",
        }]}

    def graph(provider, method, path, **kwargs):
        assert provider == "outlook"
        requested.append(path)
        if method == "POST":
            assert path == "/$batch"
            return {"responses": [
                {"id": request["id"], "status": 200, "body": {"id": request["id"]}}
                for request in kwargs["body"]["requests"]
            ]}
        if path == prefix:
            return {"value": [
                {"id": "deleteditems", "childFolderCount": 2},
                {"id": "junkemail", "childFolderCount": 1},
                {"id": "sentitems", "childFolderCount": 0},
                {"id": "drafts", "childFolderCount": 0},
                {"id": "inbox", "childFolderCount": 0},
                {"id": "search", "@odata.type": "#microsoft.graph.mailSearchFolder"},
            ], "@odata.nextLink": next_folders}
        if path == next_folders:
            assert kwargs["params"] is None
            return {"value": [{"id": "custom", "childFolderCount": 1}]}
        if path == prefix + "/custom/childFolders":
            return {"value": [{"id": "nested", "childFolderCount": 0}]}
        if path == prefix + "/inbox/messages":
            assert kwargs["params"]["$top"] == 8
            assert kwargs["params"]["$filter"] == "isDraft eq false"
            return {**letters("inbox-1"), "@odata.nextLink": next_letters}
        if path == next_letters:
            assert kwargs["params"] is None
            return letters("inbox-2")
        if path == prefix + "/custom/messages":
            return letters("archive-1")
        if path == prefix + "/nested/messages":
            return letters("archive-2")
        if path == prefix + "/sentitems/messages":
            return letters("sent-1")
        raise AssertionError(path)

    with patch("gmail_auto.knowledge_mail.microsoft.graph", side_effect=graph):
        client = SimpleNamespace(provider="outlook")
        cursor, visited = "", []
        for _ in range(10):
            result, cursor = knowledge_mail.page(client, "inbox", cursor, 8)
            visited.extend(result)
            if not cursor:
                break
        assert not cursor
        assert [letter.id for letter in visited] == ["inbox-1", "inbox-2", "archive-1", "archive-2"]
        assert all(letter.body == "The latest message text" for letter in visited)
        assert prefix + "/sentitems/messages" not in requested
        sent, cursor = knowledge_mail.page(client, "sent", "", 8)
        assert [letter.id for letter in sent] == ["sent-1"]
        assert "SENT" in sent[0].label_ids
        assert not cursor


def test_history_filters_cleanup_patterns_and_notices_but_keeps_sent_replies():
    settings = {"cleanup_match": ["from:noise.example.com"], "cleanup_keep": [], "cleanup_skipped": False}
    owner = "owner@example.com"
    assert knowledge_mail.exclude(sample(from_email="offer@noise.example.com", body="An offer for you"), owner, settings)
    assert knowledge_mail.exclude(sample(from_email="noreply@service.example.com", body="This is an automatic receipt"), owner, settings)
    assert knowledge_mail.exclude(sample(label_ids=["CATEGORY_PROMOTIONS"]), owner, settings)
    assert not knowledge_mail.exclude(sample(from_email=owner, label_ids=["SENT"], body="Thanks for your question"), owner, settings)
