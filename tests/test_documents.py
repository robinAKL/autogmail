import io
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from gmail_auto import approvals, assistant, invoices, knowledge, whatsapp
from tests.test_compose import NewMailbox
from tests.test_core import sample
from tests.test_web import Inbox, WebCase

_DETAILS = {"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789", "bank_account": "12-3456-7890123-00"}


class QuoteTests(WebCase):
    def test_quotes_have_their_own_numbers_validity_and_become_invoices(self):
        invoices.save_template(_DETAILS)
        quote = invoices.create({"client_name": "Lee Wong", "client_email": "lee@abc.example", "items": [{"description": "Coaching", "qty": 3, "unit_price": 100}]}, kind="quote")
        bill = invoices.create({"client_name": "Ann", "items": [{"description": "Session", "qty": 1, "unit_price": 50}]})
        quote = invoices.issue(quote["id"], today=date(2026, 11, 18))
        bill = invoices.issue(bill["id"], today=date(2026, 11, 18))
        self.assertEqual((quote["number"], quote["due_date"]), ("Q-0001", "2026-12-18"))
        self.assertEqual((bill["number"], bill["due_date"]), ("INV-0001", "2026-11-25"))
        self.assertEqual([item["id"] for item in invoices.list_invoices("quote")], [quote["id"]])

        pdf = invoices.render_pdf(quote)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(invoices.file_name(quote), "Q-0001-Lee-Wong.pdf")

        made = invoices.quote_to_invoice(quote["id"])
        self.assertEqual((made["kind"], made["status"], made["reference"]), ("invoice", "draft", "Quote Q-0001"))
        self.assertEqual(invoices.totals(made)["total"], Decimal("345.00"))
        self.assertEqual(invoices.get(quote["id"])["invoice_id"], made["id"])

    def test_create_quote_from_a_letter_and_turn_it_into_an_invoice_on_the_page(self):
        invoices.save_template(_DETAILS)
        letter = sample(id="q1", from_name="Lee", subject="Price for coaching?", body="How much for 3 sessions?", label_ids=["INBOX"])
        box = Inbox([letter])
        filled = json.dumps({"client_name": "Lee Wong", "items": [{"description": "Coaching session", "qty": 3, "unit_price": 100}]})
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, reader = self.page("/?show=all&open=q1")
            self.assertIn('name="kind" value="quote"', reader)
            with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=filled):
                made = self.client.post("/invoices/new", data={"key": "q1", "kind": "quote"})
        quote_id = made.headers["Location"].rsplit("/", 1)[1]
        _, edit = self.page(f"/invoices/{quote_id}")
        self.assertIn("Draft quote", edit)
        self.assertIn("Valid until", edit)
        form = {"client_name": "Lee Wong", "description": ["Coaching session"], "qty": ["3"], "unit_price": ["100"]}
        self.client.post(f"/invoices/{quote_id}/issue", data=form)
        _, issued = self.page(f"/invoices/{quote_id}")
        self.assertIn("Quote Q-0001", issued)
        self.assertIn("Accepted: make the invoice", issued)
        turned = self.client.post(f"/invoices/{quote_id}/to_invoice")
        _, draft = self.page(turned.headers["Location"])
        self.assertIn("Draft invoice", draft)
        self.assertIn("Quote Q-0001", draft)
        _, quotes_tab = self.page("/documents?tab=quotes")
        self.assertIn("Q-0001", quotes_tab)
        self.assertIn("invoiced", quotes_tab)


class DocumentsTests(WebCase):
    def test_files_to_send_are_recognised_listed_downloaded_and_attached(self):
        self.assertEqual(knowledge.classify("about.pdf", "Company profile. About us: we have run workshops since 2015."), "brochure")
        self.assertEqual(knowledge.classify("deck.pptx", "Product overview: features and benefits of the coaching plan."), "product_info")

        page = self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Kiwi Sample: who we are and what we do."), "Company intro.txt"), "role": "brochure", "next": "/documents?tab=files"},
            content_type="multipart/form-data",
        )
        self.assertEqual(page.headers["Location"], "/documents?tab=files")
        self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"TAX INVOICE for Old Client. Amount due $900."), "old-invoice.txt"), "role": "invoice_template"},
            content_type="multipart/form-data",
        )
        brochure = knowledge.templates("brochure")[0]
        _, listing = self.page("/documents")
        self.assertIn("Company intro.txt", listing)
        self.assertIn("Company introduction", listing)
        self.assertNotIn("<strong>old-invoice.txt</strong>", listing)
        self.assertEqual([doc["title"] for doc in knowledge.attachable_documents()], ["Company intro.txt"])
        self.assertIn("who we are", knowledge.search("who we are what we do")[0]["text"])

        download = self.client.get(f"/documents/file/{brochure['id']}")
        self.assertEqual(download.data, b"Kiwi Sample: who we are and what we do.")
        self.assertIn("Company intro.txt", download.headers["Content-Disposition"])
        _, compose = self.page(f"/compose?doc={brochure['id']}")
        self.assertIn(f'value="{brochure["id"]}" checked', compose)

        _, templates = self.page("/documents?tab=templates")
        self.assertIn("Quote and invoice details", templates)
        for path in ("/documents?tab=invoices", "/documents?tab=contracts", "/documents?tab=nonsense"):
            response, _ = self.page(path)
            self.assertEqual(response.status_code, 200)

    def test_whatsapp_attaches_the_company_introduction_by_its_kind(self):
        self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"Who we are."), "Kiwi profile 2026.txt"), "role": "brochure"},
            content_type="multipart/form-data",
        )
        said = json.dumps({"intent": "compose", "to": "ann@b.example", "subject": "About us", "body": "Hi Ann, here is our company introduction.", "attach": [], "numbers": [], "query": "", "instruction": "", "reply": ""})
        box = NewMailbox()
        with patch("gmail_auto.assistant.complete", return_value=said):
            preview = assistant.handle(whatsapp.Incoming(sender="64210000001", text="给 ann@b.example 发邮件，附上公司介绍"), clients=[box])
        self.assertIn("Attached: Kiwi profile 2026.txt", preview)
        self.assertNotIn("Not attached", preview)
        self.assertEqual(approvals.pending()[0]["attachments"][0]["name"], "Kiwi profile 2026.txt")

    def test_explaining_a_price_does_not_ask_for_a_quote_file(self):
        said = json.dumps({"intent": "compose", "to": "ann@b.example", "subject": "Price", "body": "Hi Ann, our rate is in the price list.", "attach": [], "numbers": [], "query": "", "instruction": "", "reply": ""})
        with patch("gmail_auto.assistant.complete", return_value=said):
            preview = assistant.handle(whatsapp.Incoming(sender="64210000001", text="给 ann@b.example 发封邮件说明报价"), clients=[NewMailbox()])
        self.assertNotIn("Not attached", preview)
