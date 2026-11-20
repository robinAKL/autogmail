import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

from gmail_auto.gmail_client import GmailClient, build_reply_message
from tests.test_core import sample


def test_reply_keeps_plain_text_and_embeds_logo_in_html_alternative():
    mail = sample(id="original")
    logo = ("company.png", "image/png", b"inline-logo-test")
    attachments = [("terms.txt", "text/plain", b"terms")]
    message = build_reply_message(mail, "Thanks <Robin>\nSignature", "owner@example.com", attachments, logo=logo)

    assert message.get_body(preferencelist=("plain",)).get_content() == "Thanks <Robin>\nSignature\n"
    rich = message.get_body(preferencelist=("html",)).get_content()
    assert "Thanks &lt;Robin&gt;<br>Signature" in rich
    image = next(part for part in message.walk() if part.get_content_type() == "image/png")
    assert f'cid:{image["Content-ID"][1:-1]}' in rich
    assert image.get_content_disposition() == "inline"
    assert image.get_content() == logo[2]
    assert [part.get_filename() for part in message.iter_attachments()] == ["terms.txt"]


def test_reply_without_a_logo_keeps_plain_text_message():
    message = build_reply_message(sample(), "Plain reply", "owner@example.com")
    assert message.get_content_type() == "text/plain"


def test_gmail_send_and_reply_use_connected_mailbox_logo(monkeypatch):
    account = "owner@example.com"
    logo = ("company.png", "image/png", b"inline-logo-test")
    loader = Mock(return_value=logo)
    monkeypatch.setattr("gmail_auto.signatures.load_logo", loader)
    client = GmailClient.__new__(GmailClient)
    client._email = account
    client.service = Mock()
    send = client.service.users().messages().send
    send.return_value.execute.return_value = {"id": "sent"}
    mail = sample(thread_id="thread")

    client.send_new("recipient@example.com", "New", "New message")
    assert client.send_reply(mail, "Reply", account) == "sent"

    assert [call.args for call in loader.call_args_list] == [("gmail", account)] * 2
    new, reply = [call.kwargs["body"] for call in send.call_args_list]
    assert "threadId" not in new
    assert reply["threadId"] == "thread"
    for payload in (new, reply):
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(payload["raw"]))
        image = next(part for part in message.walk() if part.get_content_type() == "image/png")
        assert image.get_content() == logo[2]
        assert f'cid:{image["Content-ID"][1:-1]}' in message.get_body(("html",)).get_content()
