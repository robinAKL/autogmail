import io
import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

from gmail_auto import approvals, files, knowledge, writer
from tests.test_core import sample
from tests.test_web import WebCase

_R2 = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKIDEXAMPLE", "R2_SECRET_ACCESS_KEY": "secret", "R2_BUCKET": "box"}


class PresignTests(WebCase):
    def test_matches_the_aws_example(self):
        # The pre-signed GET example from the Amazon S3 documentation.
        url = files.presign_url(
            "GET", "examplebucket.s3.amazonaws.com", "/test.txt", "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", 86400, now=datetime(2013, 5, 24, tzinfo=timezone.utc),
        )
        self.assertTrue(url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))

    def test_r2_addresses_carry_the_file_and_its_name(self):
        with patch.dict(os.environ, _R2):
            put = files.presign("PUT", "0123456789abcdef", expires=900)
            get = files.presign("GET", "0123456789abcdef", filename="报价 v2.pdf")
        self.assertTrue(put.startswith("https://acct.r2.cloudflarestorage.com/box/autogmail/files/0123456789abcdef?"))
        self.assertIn("X-Amz-Expires=900", put)
        self.assertIn("response-content-disposition=attachment%3B%20filename%2A%3DUTF-8%27%27%25E6%258A%25A5", get)


class DirectUploadTests(WebCase):
    def test_big_file_goes_straight_to_r2_then_attaches(self):
        with patch.dict(os.environ, _R2), patch("gmail_auto.files.allow_browser_uploads", return_value=True) as cors:
            ticket = self.client.post("/files/direct", json={"name": "deck.pptx", "size": 9_000_000, "type": ""}).get_json()
            too_big = self.client.post("/files/direct", json={"name": "film.mp4", "size": 40_000_000, "type": "video/mp4"})
        self.assertTrue(ticket["ok"])
        self.assertEqual(ticket["mime"], "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertIn(f"/box/autogmail/files/{ticket['id']}?", ticket["url"])
        cors.assert_called_once_with("http://localhost")
        self.assertEqual(too_big.status_code, 400)
        self.assertIn("Files can be up to 18.0 MB", too_big.get_json()["error"])

        session_id = self.client.get("/write?to=lee@client.example").headers["Location"].rsplit("/", 1)[1]
        stored = json.dumps({"id": ticket["id"], "name": ticket["name"], "mime": ticket["mime"]})
        with patch.dict(os.environ, _R2), patch("gmail_auto.files._r2_size", return_value=9_000_000):
            self.client.post(f"/write/{session_id}/upload", data={"to": "lee@client.example", "subject": "Deck", "body": "Hi", "stored": stored})
        attached = writer.get(session_id)["attachments"]
        self.assertEqual([(item["name"], item["size"]) for item in attached], [("deck.pptx", 9_000_000)])

        with patch.dict(os.environ, _R2), patch("gmail_auto.files._r2_size", return_value=0):
            missing = self.client.post(
                f"/write/{session_id}/upload",
                data={"stored": json.dumps({"id": "fedcba9876543210", "name": "x.pdf", "mime": ""})},
                follow_redirects=True,
            ).get_data(as_text=True)
        self.assertIn("The upload did not arrive", missing)

    def test_big_knowledge_file_is_read_from_r2_and_downloads_from_there(self):
        record = None
        with patch.dict(os.environ, _R2), patch("gmail_auto.files._r2_size", return_value=6_000_000), patch(
            "gmail_auto.files._get", return_value=b"Our full price list: coaching NZD 90 an hour."
        ):
            self.client.post(
                "/knowledge/upload",
                data={"stored": json.dumps({"id": "00112233445566aa", "name": "prices.txt", "mime": "text/plain"}), "role": "price_list"},
            )
            doc = knowledge.attachable_documents()[0]
            record = files.get(doc["meta"]["file_id"])
            self.assertEqual((doc["title"], record["size"]), ("prices.txt", 6_000_000))
            self.assertIn("coaching NZD 90", knowledge.get_document(doc["id"])["text"])
            download = self.client.get(f"/documents/file/{doc['id']}")
        self.assertEqual(download.status_code, 302)
        self.assertIn("acct.r2.cloudflarestorage.com/box/autogmail/files/00112233445566aa?", download.headers["Location"])

    def test_queue_takes_up_to_18_mb(self):
        item = approvals.create_new("lee@client.example", "Files", "Hi")
        approvals.attach(item["id"], {"id": "a" * 16, "name": "a.pdf", "mime": "application/pdf", "size": 10_000_000})
        with self.assertRaises(Exception):
            approvals.attach(item["id"], {"id": "b" * 16, "name": "b.pdf", "mime": "application/pdf", "size": 9_000_000})


class BigSendTests(WebCase):
    def test_gmail_uploads_a_big_message_as_a_file(self):
        from gmail_auto.gmail_client import GmailClient

        calls = []

        class _Run:
            def execute(self):
                return {"id": "sent1"}

        class _Messages:
            def send(self, userId, body, media_body=None):
                calls.append((body, media_body))
                return _Run()

        client = GmailClient.__new__(GmailClient)
        client.service = type("S", (), {"users": lambda self: type("U", (), {"messages": lambda self: _Messages()})()})()
        client.get_user_email = lambda: "me@example.com"
        client.send_new("lee@a.example", "Small", "Hi", attachments=[("a.txt", "text/plain", b"x" * 1000)])
        client.send_new("lee@a.example", "Big", "Hi", attachments=[("deck.pptx", "application/octet-stream", os.urandom(5_000_000))])
        mail = sample(id="m1", thread_id="t1")
        client.send_reply(mail, "Attached.", "me@example.com", attachments=[("deck.pptx", "application/octet-stream", os.urandom(5_000_000))])
        self.assertIn("raw", calls[0][0])
        self.assertIsNone(calls[0][1])
        self.assertEqual(calls[1][0], {})
        self.assertEqual(calls[1][1].mimetype(), "message/rfc822")
        self.assertGreater(calls[1][1].size(), 6_000_000)
        self.assertEqual(calls[2][0], {"threadId": "t1"})

    def test_outlook_sends_big_files_through_an_upload_session(self):
        from gmail_auto.outlook_client import OutlookClient

        graph_calls = []
        puts = []

        def graph(kind, method, path, **kwargs):
            graph_calls.append((method, path, kwargs.get("body")))
            if path == "/me/messages":
                return {"id": "d1"}
            if path.endswith("createUploadSession"):
                return {"uploadUrl": "https://outlook.example/upload/1"}
            return {}

        class _Ok:
            status_code = 200

        big = os.urandom(5_000_000)
        with patch("gmail_auto.microsoft.graph", side_effect=graph), patch(
            "requests.put", side_effect=lambda url, data, headers, timeout: puts.append(headers) or _Ok()
        ):
            OutlookClient.__new__(OutlookClient).send_new("lee@a.example", "Deck", "Hi", attachments=[("deck.pptx", "", big), ("a.txt", "text/plain", b"hello")])
        paths = [(method, path) for method, path, _ in graph_calls]
        self.assertEqual(paths[0], ("POST", "/me/messages"))
        self.assertIn(("POST", "/me/messages/d1/attachments/createUploadSession"), paths)
        self.assertIn(("POST", "/me/messages/d1/attachments"), paths)
        self.assertEqual(paths[-1], ("POST", "/me/messages/d1/send"))
        self.assertNotIn(("POST", "/me/sendMail"), paths)
        self.assertEqual(puts[0]["Content-Range"], f"bytes 0-3932159/{len(big)}")
        self.assertEqual(puts[-1]["Content-Range"], f"bytes 3932160-{len(big) - 1}/{len(big)}")
        self.assertNotIn("Authorization", puts[0])
