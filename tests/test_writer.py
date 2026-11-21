import io
import json
from unittest.mock import patch

from gmail_auto import approvals, assistant, contacts, invoices, knowledge, whatsapp, writer
from tests.test_compose import NewMailbox
from tests.test_contacts import _record
from tests.test_web import WebCase


def _upload(test, name, text, role):
    test.client.post(
        "/knowledge/upload",
        data={"files": (io.BytesIO(text), name), "role": role},
        content_type="multipart/form-data",
    )
    return next(doc for doc in knowledge.list_documents() if doc["title"] == name)


class WriterPageTests(WebCase):
    def test_talk_attach_make_a_quote_queue_and_send(self):
        intro = _upload(self, "intro.txt", b"Who we are: Kiwi Sample runs coaching.", "brochure")
        contacts.build([_record("a1", "lee@abcbuild.example", "Lee Wong")])
        lee = contacts.find("lee@abcbuild.example")
        contacts.save_edit(lee["id"], {"type": "client", "company": "ABC Builders", "salutation": "Dear Lee"})

        started = self.client.get(f"/write?doc={intro['id']}")
        session_id = started.headers["Location"].rsplit("/", 1)[1]
        _, page = self.page(f"/write/{session_id}")
        self.assertIn("📎 intro.txt", page)
        self.assertIn('<option value="lee@abcbuild.example">Lee Wong · ABC Builders</option>', page)

        answer = json.dumps(
            {
                "reply": "Written. I started a draft quote for the 3 sessions.",
                "to": "Lee",
                "subject": "Coaching for ABC Builders",
                "body": "Dear Lee,\n\nThank you for your interest. Our introduction is attached.\n\nRobin",
                "attach": [f"doc:{intro['id']}", "doc:notreal123"],
                "make": {"kind": "quote", "request": "3 coaching sessions"},
            }
        )
        prompts = []
        with patch("gmail_auto.llm.model_ready", return_value=True), patch(
            "gmail_auto.llm.complete", side_effect=lambda system, user, **_: prompts.append(user) or (answer if "writing assistant" in system else json.dumps({"items": []}))
        ):
            self.client.post(f"/write/{session_id}/say", data={"text": "Write to Lee about coaching, quote 3 sessions"})
        self.assertIn("Lee Wong <lee@abcbuild.example> · Client · ABC Builders", prompts[0])
        self.assertIn(f"doc:{intro['id']} | intro.txt (Company introduction)", prompts[0])

        session = writer.get(session_id)
        self.assertEqual((session["to"], session["subject"]), ("lee@abcbuild.example", "Coaching for ABC Builders"))
        self.assertEqual([item["name"] for item in session["attachments"]], ["intro.txt"])
        self.assertIn("Not attached", session["messages"][-1]["text"])
        quote = invoices.list_invoices("quote")[0]
        self.assertEqual((quote["status"], quote["client_email"]), ("draft", "lee@abcbuild.example"))
        self.assertEqual(quote["client_name"], "ABC Builders (attn. Lee Wong)")
        _, page = self.page(f"/write/{session_id}")
        self.assertIn(f'href="/invoices/{quote["id"]}"', page)
        self.assertIn("Dear Lee,", page)

        queued = self.client.post(f"/write/{session_id}/queue", data={"to": "lee@abcbuild.example", "subject": "Coaching for ABC Builders", "body": session["body"]})
        item = approvals.pending()[0]
        self.assertTrue(queued.headers["Location"].endswith(f"#a{item['id']}"))
        self.assertEqual([entry["name"] for entry in item["attachments"]], ["intro.txt"])

        self.client.post(f"/write/{session_id}/edit", data={"to": "lee@abcbuild.example", "subject": "Coaching", "body": "Dear Lee, see attached.", "detach": f"doc:{intro['id']}"})
        item = approvals.get(item["id"])
        self.assertEqual((item["subject"], item["draft"], item.get("attachments")), ("Coaching", "Dear Lee, see attached.", []))

        box = NewMailbox()
        with patch("gmail_auto.mailboxes.client_for", return_value=box):
            blocked = self.client.post(f"/write/{session_id}/send", data={"to": "lee@abcbuild.example", "subject": "Coaching", "body": "Dear Lee, see attached."}, follow_redirects=True)
            self.assertEqual(box.new, [])
            self.assertIn('mentions an attachment', blocked.get_data(as_text=True))
            self.client.post(f"/write/{session_id}/send", data={"to": "lee@abcbuild.example", "subject": "Coaching", "body": "Dear Lee, let us discuss coaching."})
        self.assertEqual(box.new, [("lee@abcbuild.example", "Coaching", "Dear Lee, let us discuss coaching.", [])])
        self.assertEqual(writer.get(session_id)["status"], "sent")
        late = self.client.post(f"/write/{session_id}/say", data={"text": "one more thing"}, follow_redirects=True)
        self.assertIn("already sent", late.get_data(as_text=True))

    def test_email_language_is_kept_apart_from_the_chat_language(self):
        started = self.client.get("/write?to=ann@b.example")
        session_id = started.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(writer.get(session_id)["language"], "en")
        _, page = self.page(f"/write/{session_id}")
        self.assertIn('<option value="en" selected>English</option>', page)

        prompts = []
        english = json.dumps({"reply": "好的，已写好英文邮件。", "subject": "Workshop on Friday", "body": "Hi Ann,\n\nSee you on Friday at 2pm.\n\nRobin"})
        with patch("gmail_auto.llm.model_ready", return_value=True), patch(
            "gmail_auto.llm.complete", side_effect=lambda system, user, **_: prompts.append(user) or english
        ):
            self.client.post(f"/write/{session_id}/say", data={"text": "告诉她周五下午两点见"})
        self.assertIn("EMAIL LANGUAGE: Write the email (subject and body) in English only, even when the owner talks to you in Chinese.", prompts[-1])
        self.assertEqual(writer.get(session_id)["messages"][-1]["text"], "好的，已写好英文邮件。")

        chinese = json.dumps({"reply": "Rewritten in Chinese.", "subject": "周五工作坊", "body": "Ann 你好，\n\n周五下午两点见。\n\nRobin"})
        with patch("gmail_auto.llm.model_ready", return_value=True), patch(
            "gmail_auto.llm.complete", side_effect=lambda system, user, **_: prompts.append(user) or chinese
        ):
            self.client.post(
                f"/write/{session_id}/language",
                data={"to": "ann@b.example", "subject": "Workshop on Friday", "body": "Hi Ann,\n\nSee you on Friday at 2pm.\n\nRobin", "language": "zh"},
            )
        session = writer.get(session_id)
        self.assertEqual((session["language"], session["subject"]), ("zh", "周五工作坊"))
        self.assertIn("Write the email (subject and body) in Chinese only", prompts[-1])
        self.assertIn("Rewrite the whole email in Chinese", prompts[-1])

        contacts.build([_record("c1", "ann@b.example", "Ann")])
        contacts.save_edit(contacts.find("ann@b.example")["id"], {"language": "bilingual"})
        self.assertIn("Chinese first", writer.language_rule({"language": "auto", "to": "ann@b.example"}))
        self.assertIn("in English", writer.language_rule({"language": "auto", "to": "nobody@x.example"}))

    def test_without_a_model_the_owner_types_the_email(self):
        started = self.client.get("/write?to=ann@b.example")
        session_id = started.headers["Location"].rsplit("/", 1)[1]
        self.client.post(f"/write/{session_id}/say", data={"text": "Write to Ann"})
        self.assertIn("No AI model", writer.get(session_id)["messages"][-1]["text"])
        empty = self.client.post(f"/write/{session_id}/queue", data={"to": "ann@b.example", "subject": "", "body": ""}, follow_redirects=True)
        self.assertIn("The email is empty", empty.get_data(as_text=True))
        self.client.post(f"/write/{session_id}/queue", data={"to": "ann@b.example", "subject": "Hi", "body": "See you Friday."})
        self.assertEqual(approvals.pending()[0]["draft"], "See you Friday.")


class WhatsAppWritingTests(WebCase):
    def test_changes_by_message_until_yes(self):
        prices = _upload(self, "prices.txt", b"Our rates: coaching is $90 per hour.", "price_list")
        compose = json.dumps({"intent": "compose", "to": "ann@b.example", "subject": "Coaching", "body": "Hi Ann, happy to help.", "attach": [], "numbers": [], "query": "", "instruction": "", "reply": ""})
        box = NewMailbox()
        with patch("gmail_auto.assistant.complete", return_value=compose):
            assistant.handle(whatsapp.Incoming(sender="64210000001", text="email ann@b.example about coaching"), clients=[box])

        change = json.dumps({"reply": "Added the price list and made it more formal.", "body": "Dear Ann,\n\nOur rates are attached.\n\nRobin", "attach": [f"doc:{prices['id']}"]})
        with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=change):
            reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="also attach the price list, more formal"), clients=[box])
        self.assertIn("Added the price list", reply)
        self.assertIn("Attached: prices.txt", reply)
        item = approvals.pending()[0]
        self.assertEqual((item["draft"], [entry["name"] for entry in item["attachments"]]), ("Dear Ann,\n\nOur rates are attached.\n\nRobin", ["prices.txt"]))

        with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=json.dumps({"off_topic": True})), patch(
            "gmail_auto.assistant.complete", return_value=json.dumps({"intent": "status", "numbers": [], "query": "", "instruction": "", "reply": ""})
        ):
            other = assistant.handle(whatsapp.Incoming(sender="64210000001", text="how many emails today?"), clients=[box])
        self.assertIn("Today so far", other)

        done = assistant.handle(whatsapp.Incoming(sender="64210000001", text="YES"), clients=[box])
        self.assertIn("with 1 attachment", done)
        self.assertEqual(box.new, [("ann@b.example", "Coaching", "Dear Ann,\n\nOur rates are attached.\n\nRobin", ["prices.txt"])])
