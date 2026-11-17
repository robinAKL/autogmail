import io
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from gmail_auto import approvals, files, invoices
from gmail_auto.errors import ConfigError
from tests.test_attachments import AttachingGmail
from tests.test_core import sample
from tests.test_web import Inbox, WebCase

_DETAILS = {
    "business_name": "Kiwi Sample Ltd",
    "address": "1 Queen Street\nAuckland",
    "gst_number": "123-456-789",
    "bank_account": "12-3456-7890123-00",
}


class InvoiceMathTests(WebCase):
    def test_totals_gst_and_numbering_are_worked_out_in_code(self):
        invoices.save_template(_DETAILS)
        draft = invoices.create(
            {"client_name": "Lee", "items": [{"description": "Workshop", "qty": 3, "unit_price": "450"}, {"description": "Books", "qty": "12", "unit_price": "$8.50"}]}
        )
        sums = invoices.totals(draft)
        self.assertEqual((sums["subtotal"], sums["gst"], sums["total"]), (Decimal("1452.00"), Decimal("217.80"), Decimal("1669.80")))

        issued = invoices.issue(draft["id"], today=date(2026, 11, 17))
        self.assertEqual(issued["number"], "INV-0001")
        self.assertEqual((issued["issue_date"], issued["due_date"]), ("2026-11-17", "2026-11-24"))
        self.assertEqual(invoices.load_template()["next_number"], 2)
        with self.assertRaises(ConfigError):
            invoices.update(draft["id"], {"client_name": "Changed"})
        with self.assertRaises(ConfigError):
            invoices.delete(draft["id"])
        # Later template changes do not alter an invoice already issued.
        invoices.save_template({"gst_rate": "10", "business_name": "New Name"})
        self.assertEqual(invoices.totals(invoices.get(draft["id"]))["gst"], Decimal("217.80"))

        invoices.save_template({"prices_include_gst": True, "gst_rate": "15"})
        inclusive = invoices.create({"client_name": "Ann", "items": [{"description": "Session", "qty": 1, "unit_price": 115}]})
        sums = invoices.totals(inclusive)
        self.assertEqual((sums["subtotal"], sums["gst"], sums["total"]), (Decimal("100.00"), Decimal("15.00"), Decimal("115.00")))

    def test_an_invoice_is_not_issued_until_it_is_complete(self):
        draft = invoices.create({"client_name": "", "items": [{"description": "Consulting", "qty": 2, "unit_price": None}]})
        with self.assertRaises(ConfigError) as caught:
            invoices.issue(draft["id"])
        message = str(caught.exception)
        for part in ("business details", "who the invoice is for", "every price"):
            self.assertIn(part, message)
        self.assertEqual(invoices.get(draft["id"])["status"], "draft")
        self.assertEqual(invoices.load_template()["next_number"], 1)

    def test_pdf_has_the_numbers_and_survives_odd_characters(self):
        invoices.save_template(_DETAILS)
        draft = invoices.create({"client_name": "Lee — Wong 李", "items": [{"description": "Workshop “Spring”", "qty": 1, "unit_price": 450}]})
        issued = invoices.issue(draft["id"])
        data = invoices.render_pdf(issued)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(invoices.file_name(issued), "INV-0001-Lee-Wong.pdf")

    def test_model_fills_lines_but_never_prices_it_was_not_given(self):
        mail = sample(id="m9", from_name="Lee", subject="Invoice for the October workshop", body="Please invoice the 3 sessions.")
        reply = json.dumps(
            {
                "client_name": "Lee Wong",
                "items": [{"description": "Workshop session", "qty": 3, "unit_price": None}],
                "missing": ["No price for a workshop session in the knowledge base."],
            }
        )
        fields = invoices.fill_from("invoice Lee", mail, complete=lambda *a, **k: reply)
        draft = invoices.create(fields, mail_key=mail.key)
        self.assertEqual(draft["client_name"], "Lee Wong")
        self.assertEqual(draft["reference"], "Invoice for the October workshop")
        self.assertIsNone(draft["items"][0]["unit_price"])
        self.assertIn("No price", draft["missing"][0])

    def test_template_is_read_from_an_old_invoice(self):
        reply = json.dumps({"business_name": "Kiwi Sample Ltd", "gst_number": "123-456-789", "prefix": "KS-", "last_number": "0041", "client": "ignored"})
        found = invoices.template_from_text("TAX INVOICE KS-0041 ...", complete=lambda *a, **k: reply)
        self.assertEqual(found["next_number"], 42)
        saved = invoices.save_template(found)
        self.assertEqual((saved["prefix"], saved["next_number"], saved["business_name"]), ("KS-", 42, "Kiwi Sample Ltd"))


class InvoicePageTests(WebCase):
    def test_create_from_a_letter_edit_issue_and_attach_to_the_reply(self):
        invoices.save_template(_DETAILS)
        letter = sample(id="iv1", from_name="Lee", subject="Invoice please", body="Can you invoice the workshop?", label_ids=["INBOX"])
        box = Inbox([letter])
        filled = json.dumps({"client_name": "Lee Wong", "items": [{"description": "Workshop", "qty": 1, "unit_price": None}], "missing": ["Price"]})
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, page = self.page("/?show=all&open=iv1")
            self.assertIn('formaction="/invoices/new"', page)
            with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=filled):
                made = self.client.post("/invoices/new", data={"key": "iv1"})
            invoice_id = made.headers["Location"].rsplit("/", 1)[1]
            _, edit = self.page(f"/invoices/{invoice_id}")
            self.assertIn("Lee Wong", edit)
            self.assertIn("price?", edit)
            self.assertIn("Before it can be issued", edit)

            form = {
                "client_name": "Lee Wong",
                "client_email": "",
                "description": ["Workshop", "Printed notes", ""],
                "qty": ["1", "10", "1"],
                "unit_price": ["450", "5", ""],
            }
            self.client.post(f"/invoices/{invoice_id}/save", data=form)
            _, saved = self.page(f"/invoices/{invoice_id}")
            self.assertIn("575.00", saved)  # 450 + 50, plus 15% GST

            with patch("gmail_auto.generator.revise_reply", return_value="Hi Lee, invoice INV-0001 is attached."):
                done = self.client.post(f"/invoices/{invoice_id}/issue_attach", data=form)
            self.assertEqual(done.headers["Location"], "/?show=todo&open=iv1")

        invoice = invoices.get(invoice_id)
        self.assertEqual(invoice["number"], "INV-0001")
        item = approvals.pending()[0]
        self.assertEqual(item["draft"], "Hi Lee, invoice INV-0001 is attached.")
        self.assertEqual(item["attachments"][0]["name"], "INV-0001-Lee-Wong.pdf")
        self.assertTrue(files.read(invoice["file_id"]).startswith(b"%PDF"))

        sender = AttachingGmail([letter])
        with patch("gmail_auto.mailboxes.client_for", return_value=sender):
            self.client.post(f"/approvals/{item['id']}/send", data={"draft": item["draft"]})
        self.assertEqual(sender.sent[0][2][0][0], "INV-0001-Lee-Wong.pdf")

        pdf = self.client.get(f"/invoices/{invoice_id}/pdf?download=1")
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertIn("attachment;", pdf.headers["Content-Disposition"])
        _, listing = self.page("/invoices")
        self.assertIn("INV-0001", listing)
        self.assertIn("NZD 575.00", listing)

    def test_template_page_saves_details_and_blank_invoices_work(self):
        self.client.post(
            "/invoices/template",
            data={"business_name": "Kiwi Sample Ltd", "gst_number": "1", "gst_rate": "15", "next_number": "7", "prefix": "KS-", "payment_terms_days": "14", "currency": "nzd", "gst_registered": "1"},
        )
        template = invoices.load_template()
        self.assertEqual((template["next_number"], template["prefix"], template["currency"], template["payment_terms_days"]), (7, "KS-", "NZD", 14))
        self.assertTrue(template["gst_registered"])
        self.assertFalse(template["prices_include_gst"])
        made = self.client.post("/invoices/new")
        _, page = self.page(made.headers["Location"])
        self.assertIn("Draft invoice", page)
        self.assertNotIn("Issue and attach", page)
        failed = self.client.post(
            "/invoices/template/import",
            data={"file": (io.BytesIO(b"TAX INVOICE"), "old.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("needs an AI model", failed.get_data(as_text=True))
