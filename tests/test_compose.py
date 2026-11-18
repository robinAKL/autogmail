import base64
import email
import email.policy
import io
import json
from unittest.mock import patch

from gmail_auto import approvals, assistant, files, invoices, knowledge, outgoing, whatsapp
from gmail_auto.errors import ConfigError
from gmail_auto.store import mark_processed
from tests.test_core import FakeGmail
from tests.test_web import WebCase


class NewMailbox(FakeGmail):
    def __init__(self):
        super().__init__([])
        self.new = []

    def send_notice(self, to, subject, body):
        self.new.append((to, subject, body, []))

    def send_new(self, to, subject, body, attachments=None):
        self.new.append((to, subject, body, [name for name, _mime, _data in attachments or []]))


def _issued_invoice(email_address="lee@client.example"):
    invoices.save_template({"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789"})
    draft = invoices.create({"client_name": "Lee Wong", "client_email": email_address, "items": [{"description": "Workshop", "qty": 1, "unit_price": 450}]})
    return invoices.issue(draft["id"])


class ComposeTests(WebCase):
    def test_recipients_are_checked(self):
        self.assertEqual(outgoing.clean_recipients("lee@a.example; ann@b.example lee@a.example"), "lee@a.example, ann@b.example")
        with self.assertRaises(ConfigError):
            outgoing.clean_recipients("lee at example")
        with self.assertRaises(ConfigError):
            outgoing.clean_recipients("")

    def test_gmail_new_email_carries_attachments(self):
        from gmail_auto.gmail_client import GmailClient

        sent = []

        class _Send:
            def execute(self):
                return {}

        class _Messages:
            def send(self, userId, body):
                sent.append(body)
                return _Send()

        client = GmailClient.__new__(GmailClient)
        client.service = type("S", (), {"users": lambda self: type("U", (), {"messages": lambda self: _Messages()})()})()
        client.get_user_email = lambda: "me@example.com"
        client.send_new("lee@a.example, ann@b.example", "Quote", "Attached.", [("quote.pdf", "application/pdf", b"%PDF")])
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]), policy=email.policy.default)
        self.assertEqual(parsed["To"], "lee@a.example, ann@b.example")
        self.assertEqual([part.get_filename() for part in parsed.iter_attachments()], ["quote.pdf"])

    def test_outlook_new_email_has_every_recipient_and_file(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []
        with patch("gmail_auto.microsoft.graph", side_effect=lambda *a, **k: calls.append((a, k)) or {}):
            OutlookClient.__new__(OutlookClient).send_new("lee@a.example, ann@b.example", "Quote", "Hi", [("a.pdf", "application/pdf", b"x")])
        message = calls[0][1]["body"]["message"]
        self.assertEqual([item["emailAddress"]["address"] for item in message["toRecipients"]], ["lee@a.example", "ann@b.example"])
        self.assertEqual(message["attachments"][0]["name"], "a.pdf")

    def test_write_page_drafts_attaches_and_sends(self):
        invoice = _issued_invoice()
        uploaded = self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Our rates: coaching is $90 per hour."), "price-list.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 302)
        doc = knowledge.attachable_documents()[0]

        _, page = self.page(f"/compose?invoice={invoice['id']}")
        self.assertIn('value="lee@client.example"', page)
        self.assertIn(f'value="{invoice["id"]}" selected', page)
        self.assertIn("price-list.txt", page)

        written = json.dumps({"subject": "Your invoice", "body": "Hi Lee,\n\nYour invoice and our price list are attached.\n\nRobin"})
        with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=written) as model:
            made = self.client.post(
                "/compose",
                data={"to": "lee@client.example", "points": "send the invoice and prices", "invoice": invoice["id"], "doc": doc["id"]},
                content_type="multipart/form-data",
            )
        self.assertIn("ATTACHED FILES: price-list.txt, INV-0001-Lee-Wong.pdf", model.call_args[0][1])
        item = approvals.pending()[0]
        self.assertTrue(made.headers["Location"].endswith(f"/approvals#a{item['id']}"))
        self.assertEqual((item["mode"], item["to"], item["subject"]), ("new", "lee@client.example", "Your invoice"))
        self.assertEqual(sorted(entry["name"] for entry in item["attachments"]), ["INV-0001-Lee-Wong.pdf", "price-list.txt"])

        _, queue = self.page("/approvals")
        self.assertIn("New email", queue)
        self.assertIn('name="to" value="lee@client.example"', queue)
        self.assertNotIn("Show the original letter", queue)

        box = NewMailbox()
        with patch("gmail_auto.mailboxes.client_for", return_value=box):
            self.client.post(
                f"/approvals/{item['id']}/send",
                data={"to": "lee@client.example, accounts@client.example", "subject": "Invoice INV-0001", "draft": "Hi Lee, attached."},
            )
        self.assertEqual(box.new, [("lee@client.example, accounts@client.example", "Invoice INV-0001", "Hi Lee, attached.", ["price-list.txt", "INV-0001-Lee-Wong.pdf"])])
        self.assertEqual(approvals.get(item["id"])["status"], "sent")

    def test_without_a_model_the_points_are_the_email(self):
        made = self.client.post("/compose", data={"to": "ann@b.example", "subject": "Hello", "points": "See you Friday."}, content_type="multipart/form-data")
        self.assertEqual(made.status_code, 302)
        item = approvals.pending()[0]
        self.assertEqual((item["subject"], item["draft"]), ("Hello", "See you Friday."))
        bad = self.client.post("/compose", data={"to": "nobody", "points": "Hi there"}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Not an email address: nobody", bad.get_data(as_text=True))
        self.assertIn("Hi there", bad.get_data(as_text=True))


class WhatsAppComposeTests(WebCase):
    def test_compose_by_name_with_an_invoice_then_yes_sends_it(self):
        _issued_invoice()
        mark_processed({"id": "old1", "from_name": "Lee Wong", "from_email": "lee@client.example", "subject": "Workshop", "processed_at": "2026-11-10 10:00:00", "action": "sent"})
        said = json.dumps({"intent": "compose", "to": "Lee", "subject": "Invoice", "body": "Hi Lee, the invoice is attached.", "attach": ["invoice"], "numbers": [], "query": "", "instruction": "", "reply": ""})
        box = NewMailbox()
        with patch("gmail_auto.assistant.complete", return_value=said):
            preview = assistant.handle(whatsapp.Incoming(sender="64210000001", text="给 Lee 写邮件，附上发票"), clients=[box])
        self.assertIn("To: lee@client.example", preview)
        self.assertIn("Attached: INV-0001-Lee-Wong.pdf", preview)
        self.assertEqual(box.new, [])
        item = approvals.pending()[0]
        self.assertEqual(item["mode"], "new")

        done = assistant.handle(whatsapp.Incoming(sender="64210000001", text="YES"), clients=[box])
        self.assertIn("Sent email to lee@client.example with 1 attachment", done)
        self.assertEqual(box.new, [("lee@client.example", "Invoice", "Hi Lee, the invoice is attached.", ["INV-0001-Lee-Wong.pdf"])])

    def test_missing_attachment_is_named_and_no_cancels(self):
        said = json.dumps({"intent": "compose", "to": "ann@b.example", "subject": "Contract", "body": "Hi Ann, here is the contract.", "attach": ["contract"], "numbers": [], "query": "", "instruction": "", "reply": ""})
        box = NewMailbox()
        with patch("gmail_auto.assistant.complete", return_value=said):
            preview = assistant.handle(whatsapp.Incoming(sender="64210000001", text="email ann@b.example the contract"), clients=[box])
        self.assertIn("Not attached:", preview)
        self.assertIn("no finished contract draft", preview)
        cancelled = assistant.handle(whatsapp.Incoming(sender="64210000001", text="NO"), clients=[box])
        self.assertIn("Cancelled", cancelled)
        self.assertEqual(approvals.pending(), [])
        self.assertEqual(box.new, [])
