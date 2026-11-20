from gmail_auto.gmail_client import build_reply_message
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
