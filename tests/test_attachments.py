import base64
import email
import email.policy
import io
import os
from datetime import datetime, timezone
from unittest.mock import patch

from gmail_auto import approvals, files, knowledge
from gmail_auto.errors import ConfigError
from gmail_auto.models import Draft
from gmail_auto.runner import run_once
from gmail_auto.store import set_live
from tests.test_core import FakeGmail, sample
from tests.test_web import WebCase


class AttachingGmail(FakeGmail):
    def send_reply(self, mail, body, user_email, attachments=None):
        self.sent.append((mail.id, body, [(name, mime, data) for name, mime, data in attachments or []]))
        return "sent"


class FileStoreTests(WebCase):
    def test_signature_matches_the_published_aws_example(self):
        headers = files.sign_v4(
            "GET",
            "examplebucket.s3.amazonaws.com",
            "/test.txt",
            {"Range": "bytes=0-9"},
            b"",
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "us-east-1",
            now=datetime(2013, 5, 24, tzinfo=timezone.utc),
        )
        self.assertTrue(headers["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
        self.assertNotIn("host", headers)

    def test_files_are_kept_on_this_computer_without_r2(self):
        record = files.save(b"hello", "../price list.pdf")
        self.assertEqual(record["name"], "price list.pdf")
        self.assertEqual(record["mime"], "application/pdf")
        self.assertEqual(files.read(record["id"]), b"hello")
        files.remove(record["id"])
        self.assertIsNone(files.get(record["id"]))
        with self.assertRaises(ConfigError):
            files.read(record["id"])
        with self.assertRaises(ConfigError):
            files.save(b"x" * (files.MAX_FILE_BYTES + 1), "big.bin")

    def test_hosted_site_needs_r2_and_uses_it(self):
        os.environ["VERCEL"] = "1"
        self.assertFalse(files.ready())
        with self.assertRaises(ConfigError) as caught:
            files.save(b"hello", "a.txt")
        self.assertIn("R2_BUCKET", str(caught.exception))

        names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        for name, value in zip(names, ("acct", "key", "secret", "bucket")):
            os.environ[name] = value
        self.addCleanup(lambda: [os.environ.pop(name, None) for name in names])
        calls = []

        class _Response:
            status_code = 200
            content = b"stored bytes"

        def fake_request(method, url, data=None, headers=None, timeout=None):
            calls.append((method, url, data, headers))
            return _Response()

        with patch("requests.request", side_effect=fake_request):
            record = files.save(b"hello", "a.txt", "text/plain")
            self.assertEqual(files.read(record["id"]), b"stored bytes")
            files.remove(record["id"])
        self.assertEqual([call[0] for call in calls], ["PUT", "GET", "DELETE"])
        self.assertEqual(calls[0][1], f"https://acct.r2.cloudflarestorage.com/bucket/autogmail/files/{record['id']}")
        self.assertEqual(calls[0][2], b"hello")
        self.assertIn("Credential=key/", calls[0][3]["authorization"])
        self.assertIn("/auto/s3/aws4_request", calls[0][3]["authorization"])
        self.assertEqual(calls[0][3]["content-type"], "text/plain")


class SendWithAttachmentTests(WebCase):
    def test_gmail_message_carries_the_files(self):
        from gmail_auto.gmail_client import build_send_body

        mail = sample(id="g1", subject="Invoice please")
        payload = build_send_body(mail, "Attached.", "me@example.com", [("invoice.pdf", "application/pdf", b"%PDF-1.4 test")])
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(payload["raw"]), policy=email.policy.default)
        parts = [part for part in parsed.walk() if part.get_filename()]
        self.assertEqual(parts[0].get_filename(), "invoice.pdf")
        self.assertEqual(parts[0].get_content_type(), "application/pdf")
        self.assertEqual(parts[0].get_payload(decode=True), b"%PDF-1.4 test")
        self.assertIn("Attached.", parsed.get_body(("plain",)).get_content())
        plain = email.message_from_bytes(base64.urlsafe_b64decode(build_send_body(mail, "Hi.", "me@example.com")["raw"]))
        self.assertFalse(plain.is_multipart())

    def test_outlook_adds_each_file_before_sending(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []

        def fake_graph(kind, method, path, params=None, body=None, headers=None, **_):
            calls.append((method, path, body))
            return {"id": "d1", "body": {"content": ""}} if path.endswith("createReply") else {}

        with patch("gmail_auto.microsoft.graph", side_effect=fake_graph):
            OutlookClient.__new__(OutlookClient).send_reply(sample(id="o1", provider="outlook"), "Here.", "me@example.com", [("a.pdf", "application/pdf", b"abc")])
        self.assertEqual([call[1] for call in calls], ["/me/messages/o1/createReply", "/me/messages/d1", "/me/messages/d1/attachments", "/me/messages/d1/send"])
        self.assertEqual(calls[2][2]["name"], "a.pdf")
        self.assertEqual(base64.b64decode(calls[2][2]["contentBytes"]), b"abc")
        self.assertNotIn("isInline", calls[2][2])

    def test_outlook_reply_embeds_a_signature_image_and_escapes_the_text(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []

        def fake_graph(kind, method, path, params=None, body=None, headers=None, **_):
            calls.append((method, path, body))
            return {"id": "d1", "body": {"content": "<p>quoted</p>"}} if path.endswith("createReply") else {}

        with patch("gmail_auto.microsoft.graph", side_effect=fake_graph):
            OutlookClient.__new__(OutlookClient).send_reply(
                sample(id="o1", provider="outlook"),
                "Hi <there>",
                "me@example.com",
                [("a.pdf", "application/pdf", b"abc")],
                html_suffix='<img src="cid:logo">',
                inline_images=[("cid:logo", "logo.png", "image/png", b"\x89PNG")],
            )
        html_body = calls[1][2]["body"]["content"]
        self.assertIn("Hi &lt;there&gt;", html_body)
        self.assertIn('<img src="cid:logo">', html_body)
        self.assertNotIn("isInline", calls[2][2])
        self.assertEqual(calls[3][2]["isInline"], True)
        self.assertEqual(calls[3][2]["contentId"], "logo")
        self.assertEqual(calls[3][2]["contentType"], "image/png")
        self.assertEqual(base64.b64decode(calls[3][2]["contentBytes"]), b"\x89PNG")

    def test_outlook_new_mail_embeds_a_signature_image(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []
        with patch("gmail_auto.microsoft.graph", side_effect=lambda *a, **k: calls.append((a, k)) or {}):
            OutlookClient.__new__(OutlookClient).send_new(
                "lee@a.example",
                "Hello",
                "Hi <there>",
                html_suffix='<img src="cid:logo">',
                inline_images=[("logo", "logo.png", "image/png", b"\x89PNG")],
            )
        message = calls[0][1]["body"]["message"]
        self.assertEqual(message["body"]["contentType"], "HTML")
        self.assertIn("Hi &lt;there&gt;", message["body"]["content"])
        self.assertIn('<img src="cid:logo">', message["body"]["content"])
        logo = message["attachments"][0]
        self.assertEqual(logo["isInline"], True)
        self.assertEqual(logo["contentId"], "logo")
        self.assertEqual(base64.b64decode(logo["contentBytes"]), b"\x89PNG")

    def test_outlook_refuses_a_signature_image_over_three_megabytes(self):
        from gmail_auto.outlook_client import OutlookClient

        graph = patch("gmail_auto.microsoft.graph")
        with graph as fake, self.assertRaises(ConfigError) as raised:
            OutlookClient.__new__(OutlookClient).send_new(
                "lee@a.example",
                "Hello",
                "Hi",
                inline_images=[("logo", "logo.png", "image/png", b"x" * 3_000_001)],
            )
        fake.assert_not_called()
        self.assertIn("too large to embed", str(raised.exception))

    def test_outlook_send_embeds_the_saved_logo_for_that_mailbox(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []
        client = OutlookClient.__new__(OutlookClient)
        client._email = "robin@innopro.co.nz"
        with patch("gmail_auto.microsoft.graph", side_effect=lambda *a, **k: calls.append(k) or {}), patch(
            "gmail_auto.signatures.load_logo", return_value=('evil".png', "image/png", b"\x89PNG")
        ) as loader:
            client.send_new("lee@a.example", "Hello", "Hi <there>")
        loader.assert_called_once_with("outlook", "robin@innopro.co.nz")
        message = calls[0]["body"]["message"]
        self.assertEqual(message["body"]["contentType"], "HTML")
        self.assertIn("Hi &lt;there&gt;", message["body"]["content"])
        logo = message["attachments"][0]
        self.assertEqual(logo["isInline"], True)
        self.assertTrue(logo["contentId"].startswith("logo.png."))
        self.assertTrue(logo["contentId"].endswith("@autogmail"))
        self.assertNotIn('"', logo["contentId"])
        self.assertIn(f'cid:{logo["contentId"]}', message["body"]["content"])
        self.assertEqual(base64.b64decode(logo["contentBytes"]), b"\x89PNG")

    def test_outlook_uses_a_draft_when_the_logo_and_files_exceed_one_request(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []

        def fake_graph(kind, method, path, params=None, body=None, headers=None, **_):
            calls.append((method, path))
            return {"id": "draft-1"}

        client = OutlookClient.__new__(OutlookClient)
        client._email = "robin@innopro.co.nz"
        big = b"x" * 2_000_000
        with patch("gmail_auto.microsoft.graph", side_effect=fake_graph), patch(
            "gmail_auto.signatures.load_logo", return_value=("logo.png", "image/png", big)
        ):
            client.send_new("lee@a.example", "Hello", "Hi", attachments=[("notes.pdf", "application/pdf", big)])
        self.assertEqual(calls[0], ("POST", "/me/messages"))
        self.assertIn(("POST", "/me/messages/draft-1/attachments"), calls)
        self.assertEqual(calls[-1], ("POST", "/me/messages/draft-1/send"))
        self.assertNotIn(("POST", "/me/sendMail"), calls)


class AttachOnPageTests(WebCase):
    def test_attach_from_knowledge_and_upload_then_send(self):
        uploaded = self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Spring course 300 dollars."), "prices.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("Added 1 file", uploaded.get_data(as_text=True))
        doc = knowledge.attachable_documents()[0]
        self.assertEqual(files.read(doc["meta"]["file_id"]), b"Spring course 300 dollars.")

        set_live(True)
        box = AttachingGmail([sample(id="a1", subject="Prices?")])
        run_once(gmail=box, generate=lambda *_: Draft(action="ask", reply="Prices attached.", reason="Check."))
        item = approvals.pending()[0]
        _, page = self.page("/approvals")
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertIn(f'<option value="{doc["id"]}">prices.txt</option>', page)

        self.client.post(
            f"/approvals/{item['id']}/attach",
            data={"doc": doc["id"], "draft": "Prices attached, see below.", "files": (io.BytesIO(b"PDFDATA"), "quote.pdf")},
            content_type="multipart/form-data",
        )
        item = approvals.get(item["id"])
        self.assertEqual([entry["name"] for entry in item["attachments"]], ["prices.txt", "quote.pdf"])
        self.assertEqual(item["draft"], "Prices attached, see below.")
        _, page = self.page("/approvals")
        self.assertIn("📎 quote.pdf", page)

        quote = item["attachments"][1]["id"]
        self.client.post(f"/approvals/{item['id']}/detach", data={"file": quote, "draft": item["draft"]})
        self.assertEqual([entry["name"] for entry in approvals.get(item["id"])["attachments"]], ["prices.txt"])
        self.assertIsNone(files.get(quote))

        with patch("gmail_auto.mailboxes.client_for", return_value=box):
            self.client.post(
                f"/approvals/{item['id']}/send",
                data={"draft": "Prices attached.", "files": (io.BytesIO(b"late"), "map.png")},
                content_type="multipart/form-data",
            )
        self.assertEqual(
            box.sent,
            [("a1", "Prices attached.", [("prices.txt", "text/plain", b"Spring course 300 dollars."), ("map.png", "image/png", b"late")])],
        )

        knowledge.delete_document(doc["id"])
        self.assertIsNone(files.get(doc["meta"]["file_id"]))

    def test_without_storage_the_page_says_how_to_set_it_up(self):
        set_live(True)
        box = AttachingGmail([sample(id="a2", subject="Hello")])
        run_once(gmail=box, generate=lambda *_: Draft(action="ask", reply="Hi there friend.", reason="Check."))
        os.environ["VERCEL"] = "1"
        with patch("gmail_auto.web._login_required", return_value=None), patch("gmail_auto.web._host_allowed", return_value=True):
            _, page = self.page("/approvals")
        self.assertIn("set up R2 file storage", page)
        self.assertNotIn('name="files"', page)
