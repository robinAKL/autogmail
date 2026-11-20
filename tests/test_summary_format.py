import json
from unittest.mock import patch

from gmail_auto.generator import draft_reply
from gmail_auto.models import Mail
from gmail_auto.screen import screen_mail
from gmail_auto.storage import save_doc
from gmail_auto.worker import brief_mail, clean_summary


def _mail() -> Mail:
    return Mail(
        id="m1",
        thread_id="t1",
        from_name="Kogan Support",
        from_email="support@kogan.com",
        reply_email="support@kogan.com",
        subject="Help request: Change Address",
        date="2026-11-19 08:25",
        body="Please reply to this email to respond about ticket 22112278.",
        snippet="Please reply to this email...",
    )


def test_clean_summary_flattens_lists_dicts_and_stringified_lists():
    assert clean_summary(["Line 1", "Line 2"]) == "Line 1; Line 2"
    assert clean_summary("['Subject: Ticket', 'Please reply to respond']") == "Subject: Ticket; Please reply to respond"
    assert clean_summary('["First point", "Second point"]') == "First point; Second point"
    assert clean_summary({"summary": "Customer asks to update address"}) == "Customer asks to update address"
    assert clean_summary("  Normal   sentence.  ") == "Normal sentence."


def test_brief_mail_handles_list_summary_and_cleans_legacy_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    mail = _mail()

    payload = json.dumps({
        "summary": ["Update on ticket 22112278", "Please reply to respond"],
        "facts": ["asks=reply"],
        "quotes": [],
        "spam_cues": [],
    })
    with patch("gmail_auto.worker.model_ready", return_value=True), patch("gmail_auto.worker.complete", return_value=payload):
        brief = brief_mail(mail, use_cache=False)
    assert brief["summary"] == "Update on ticket 22112278; Please reply to respond"
    assert not brief["summary"].startswith("[")

    # Legacy cached stringified list in mail_briefs must also be cleaned on read
    save_doc("mail_briefs", {mail.key: {"summary": "['Old cached line 1', 'Old cached line 2']"}})
    cached = brief_mail(mail, use_cache=True)
    assert cached["summary"] == "Old cached line 1; Old cached line 2"


def test_screen_and_generator_flatten_list_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    mail = _mail()

    screen_payload = json.dumps({
        "disposition": "ask",
        "reason": ["Needs owner reply"],
        "summary": ["Support updated ticket 22112278", "Asks owner to reply"],
        "code": "other",
    })
    with patch("gmail_auto.screen.model_ready", return_value=True), patch("gmail_auto.screen.complete", return_value=screen_payload):
        verdict = screen_mail(mail)
    assert verdict is not None
    assert verdict.summary == "Support updated ticket 22112278; Asks owner to reply"
    assert verdict.reason == "Needs owner reply"

    draft_payload = json.dumps({
        "action": "ask",
        "reason": ["Needs confirmation"],
        "summary": ["Customer care updated ticket", "Waiting for reply"],
        "reply": "Hi, I will confirm your address update shortly.",
    })
    with (
        patch("gmail_auto.generator.complete", return_value=draft_payload),
        patch("gmail_auto.generator.profile_as_prompt", return_value="Owner: Robin"),
        patch("gmail_auto.knowledge.context_for", return_value={"text": "", "sources": []}),
        patch("gmail_auto.knowledge.render_context", return_value=""),
        patch("gmail_auto.story.history_for_mail", return_value=[]),
        patch("gmail_auto.story.format_history", return_value=""),
    ):
        draft = draft_reply(mail, {"name": "Robin", "never_share": []}, "en")
    assert draft.summary == "Customer care updated ticket; Waiting for reply"
    assert draft.reason == "Needs confirmation"
