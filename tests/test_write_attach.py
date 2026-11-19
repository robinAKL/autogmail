import io
import json
from unittest.mock import patch

from gmail_auto import approvals, assistant, contracts, invoices, knowledge, whatsapp, writer
from gmail_auto.profile_store import load_profile
from tests.test_compose import NewMailbox
from tests.test_templates import _docx
from tests.test_web import WebCase


def _chat(*answers):
    """Patch the model so each writer turn gets the next answer."""
    replies = [json.dumps(answer) for answer in answers]
    return (
        patch("gmail_auto.llm.model_ready", return_value=True),
        patch("gmail_auto.llm.complete", side_effect=lambda *a, **k: replies.pop(0)),
    )


class WritePageAttachTests(WebCase):
    def _session(self):
        started = self.client.get("/write?to=lee@client.example")
        return started.headers["Location"].rsplit("/", 1)[1]

    def test_upload_pick_and_remove_on_the_page(self):
        self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Kiwi Sample Ltd runs coaching for small teams."), "company-intro.txt")},
            content_type="multipart/form-data",
        )
        doc = knowledge.attachable_documents()[0]
        session_id = self._session()
        _, page = self.page(f"/write/{session_id}")
        self.assertIn("Choose from your files", page)
        self.assertIn(f'value="doc:{doc["id"]}"', page)

        self.client.post(
            f"/write/{session_id}/upload",
            data={"to": "lee@client.example", "subject": "Hello", "body": "Hi Lee", "files": (io.BytesIO(b"%PDF-1.4 plan"), "plan.pdf")},
            content_type="multipart/form-data",
        )
        self.client.post(f"/write/{session_id}/pick", data={"to": "lee@client.example", "subject": "Hello", "body": "Hi Lee", "ref": f"doc:{doc['id']}"})
        session = writer.get(session_id)
        self.assertEqual(sorted(item["name"] for item in session["attachments"]), ["company-intro.txt", "plan.pdf"])
        self.assertEqual((session["subject"], session["body"]), ("Hello", "Hi Lee"))
        _, page = self.page(f"/write/{session_id}")
        self.assertNotIn(f'<option value="doc:{doc["id"]}"', page)

        upload_ref = next(item["ref"] for item in session["attachments"] if item["name"] == "plan.pdf")
        self.client.post(f"/write/{session_id}/edit", data={"to": "lee@client.example", "subject": "Hello", "body": "Hi Lee", "detach": upload_ref})
        self.assertEqual([item["name"] for item in writer.get(session_id)["attachments"]], ["company-intro.txt"])

    def test_documents_are_offered_by_what_they_are_about(self):
        self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Week one covers goal setting. Week two covers habits."), "Outline.txt")},
            content_type="multipart/form-data",
        )
        session = writer.start(to="lee@client.example")
        session["messages"].append({"who": "you", "text": "attach the course outline about goal setting", "at": "09:00"})
        prompt = writer._prompt(session, load_profile())
        self.assertIn("Outline.txt", prompt)
        self.assertIn("(relevant)", prompt)
        self.assertIn("about: Week one covers goal setting", prompt)

    def test_contract_asks_for_the_missing_part_then_attaches(self):
        self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(_docx(["SERVICES AGREEMENT", "This Agreement is made between Kiwi Sample Ltd and {{Client name}}.", "The parties agree the fee is {{Fee}}.", "Termination needs 30 days notice. Governing law: New Zealand."])), "services.docx")},
            content_type="multipart/form-data",
        )
        session_id = self._session()
        ready, model = _chat(
            {"reply": "I started the contract. What is the fee?", "make": {"kind": "contract", "request": "3 month coaching", "template": ""}},
        )
        with ready, model, patch("gmail_auto.contracts.fill_from", return_value=({"Client name": "Lee Wong", "Fee": ""}, [])):
            self.client.post(f"/write/{session_id}/say", data={"text": "附上服务合同"})
        session = writer.get(session_id)
        self.assertEqual(session["attachments"], [])
        self.assertIn("Contract still needs: Fee", session["messages"][-1]["text"])
        contract_id = session["made"][0]["id"]
        _, page = self.page(f"/write/{session_id}")
        self.assertIn("Still needed:", page)
        self.assertIn("Draft contract", page)

        ready, model = _chat({"reply": "Fee added.", "fill": {"ref": f"contract:{contract_id}", "values": {"Fee": "NZD 2,400 + GST", "Nonsense": "x"}}})
        with ready, model:
            self.client.post(f"/write/{session_id}/say", data={"text": "费用 2400 纽币加 GST"})
        session = writer.get(session_id)
        self.assertEqual(contracts.get(contract_id)["values"], {"Client name": "Lee Wong", "Fee": "NZD 2,400 + GST"})
        self.assertEqual([item["name"] for item in session["attachments"]], ["services-Lee-Wong.docx"])
        self.assertIn("Contract filled in and attached", session["messages"][-1]["text"])

    def test_quote_is_issued_only_after_the_owner_agrees(self):
        invoices.save_template({"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789"})
        session_id = self._session()
        fields = {"client_name": "Lee Wong", "items": [{"description": "Coaching", "qty": 3, "unit_price": None}]}
        ready, model = _chat({"reply": "What is the monthly price?", "make": {"kind": "quote", "request": "3 months coaching", "template": ""}})
        with ready, model, patch("gmail_auto.invoices.fill_from", return_value=fields):
            self.client.post(f"/write/{session_id}/say", data={"text": "做一份 3 个月辅导的报价"})
        session = writer.get(session_id)
        quote_id = session["made"][0]["id"]
        self.assertIn("fill in every price", session["messages"][-1]["text"])

        ready, model = _chat(
            {"reply": "That makes NZD 1,350. Shall I issue it?", "lines": {"ref": f"invoice:{quote_id}", "items": [{"description": "Coaching (month)", "qty": 3, "unit_price": 450}]}, "issue": f"invoice:{quote_id}"}
        )
        with ready, model:
            self.client.post(f"/write/{session_id}/say", data={"text": "每月 450"})
        session = writer.get(session_id)
        self.assertEqual(invoices.get(quote_id)["status"], "draft")
        self.assertEqual(session["attachments"], [])
        self.assertIn("Not issued yet", session["messages"][-1]["text"])
        _, page = self.page(f"/write/{session_id}")
        self.assertIn("Issue and attach", page)

        ready, model = _chat({"reply": "Issued.", "issue": f"invoice:{quote_id}"})
        with ready, model:
            self.client.post(f"/write/{session_id}/say", data={"text": "好的，开出"})
        quote = invoices.get(quote_id)
        self.assertEqual(quote["status"], "issued")
        self.assertEqual(len(writer.get(session_id)["attachments"]), 1)
        self.assertIn(quote["number"], writer.get(session_id)["messages"][-1]["text"])

    def test_issue_button_on_the_page(self):
        invoices.save_template({"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789"})
        session_id = self._session()
        fields = {"client_name": "Lee Wong", "items": [{"description": "Workshop", "qty": 1, "unit_price": 450}]}
        ready, model = _chat({"reply": "Here is the quote.", "make": {"kind": "quote", "request": "workshop", "template": ""}})
        with ready, model, patch("gmail_auto.invoices.fill_from", return_value=fields):
            self.client.post(f"/write/{session_id}/say", data={"text": "quote for the workshop"})
        quote_id = writer.get(session_id)["made"][0]["id"]
        self.assertIn("Say yes to issue and attach it", writer.get(session_id)["messages"][-1]["text"])
        self.client.post(f"/write/{session_id}/issue", data={"to": "lee@client.example", "subject": "Quote", "body": "Hi Lee", "invoice": quote_id})
        self.assertEqual(invoices.get(quote_id)["status"], "issued")
        self.assertEqual(len(writer.get(session_id)["attachments"]), 1)
        other = self.client.post(f"/write/{session_id}/issue", data={"invoice": "notmine123"}, follow_redirects=True)
        self.assertIn("not started in this email", other.get_data(as_text=True))


class WhatsAppFileTests(WebCase):
    def _compose(self, box):
        said = json.dumps({"intent": "compose", "to": "lee@client.example", "subject": "Plan", "body": "Hi Lee, the plan is attached.", "attach": [], "numbers": [], "query": "", "instruction": "", "reply": ""})
        with patch("gmail_auto.assistant.complete", return_value=said):
            assistant.handle(whatsapp.Incoming(sender="64210000001", text="write to lee@client.example about the plan"), clients=[box])

    def test_meta_and_twilio_files_are_read(self):
        payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "64210000001", "id": "m1", "type": "document", "document": {"id": "media9", "mime_type": "application/pdf", "filename": "plan.pdf", "caption": "add this"}}]}}]}]}
        found = whatsapp.parse_meta(payload)[0]
        self.assertEqual((found.file_id, found.file_name, found.file_type, found.text), ("media9", "plan.pdf", "application/pdf", "add this"))
        self.assertTrue(whatsapp.has_file(found))
        self.assertFalse(whatsapp.has_voice(found))
        form = {"From": "whatsapp:+64210000001", "Body": "", "NumMedia": "1", "MediaContentType0": "image/jpeg", "MediaUrl0": "https://api.twilio.example/m/1", "MessageSid": "SM1"}
        twilio = whatsapp.parse_twilio(form)[0]
        self.assertEqual((twilio.file_url, twilio.file_type, twilio.audio_url), ("https://api.twilio.example/m/1", "image/jpeg", ""))

    def test_a_sent_file_joins_the_email_being_written(self):
        box = NewMailbox()
        early = assistant.attach_sent_file(whatsapp.Incoming(sender="64210000001", file_id="media9", file_name="plan.pdf", file_type="application/pdf"))
        self.assertIn("first tell me who to write to", early)

        self._compose(box)
        with patch("gmail_auto.whatsapp._download", return_value=b"%PDF-1.4 plan"):
            answer = assistant.attach_sent_file(whatsapp.Incoming(sender="64210000001", file_id="media9", file_name="plan.pdf", file_type="application/pdf"))
        self.assertIn("📎 Attached plan.pdf", answer)
        self.assertIn("Reply YES to send", answer)
        item = approvals.pending()[0]
        self.assertEqual([entry["name"] for entry in item["attachments"]], ["plan.pdf"])
        assistant.handle(whatsapp.Incoming(sender="64210000001", text="YES"), clients=[box])
        self.assertEqual(box.new, [("lee@client.example", "Plan", "Hi Lee, the plan is attached.", ["plan.pdf"])])

    def test_yes_to_a_waiting_quote_issues_it_instead_of_sending(self):
        invoices.save_template({"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789"})
        box = NewMailbox()
        self._compose(box)
        pending = assistant.get_state("wa_pending_action")
        fields = {"client_name": "Lee Wong", "items": [{"description": "Workshop", "qty": 1, "unit_price": 450}]}
        ready, model = _chat({"reply": "Quote ready, issue it?", "make": {"kind": "quote", "request": "workshop", "template": ""}})
        with ready, model, patch("gmail_auto.invoices.fill_from", return_value=fields):
            writer.say(pending["session_id"], "add a quote for the workshop", load_profile())
        quote_id = writer.get(pending["session_id"])["made"][0]["id"]

        ready, model = _chat({"reply": "Issued and attached.", "issue": f"invoice:{quote_id}"})
        with ready, model:
            answer = assistant.handle(whatsapp.Incoming(sender="64210000001", text="yes"), clients=[box])
        self.assertEqual(box.new, [])
        self.assertEqual(invoices.get(quote_id)["status"], "issued")
        self.assertIn("Reply YES to send", answer)
        assistant.handle(whatsapp.Incoming(sender="64210000001", text="YES"), clients=[box])
        self.assertEqual(len(box.new), 1)
        self.assertEqual(len(box.new[0][3]), 1)
