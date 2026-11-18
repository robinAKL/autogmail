import io
import json
import zipfile
from unittest.mock import patch

from gmail_auto import approvals, contracts, files, invoices, knowledge
from tests.test_core import sample
from tests.test_web import Inbox, WebCase

_CONTRACT_TEXT = (
    "SERVICES AGREEMENT. This Agreement is made between Kiwi Sample Ltd and {{Client name}}. "
    "The parties agree that the fee is {{Fee}}. Termination needs 30 days notice. Governing law: New Zealand."
)


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        f"{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


def _document_xml(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")


class RecogniseTests(WebCase):
    def test_uploads_are_sorted_without_a_model(self):
        invoice = "TAX INVOICE\nInvoice No: INV-0041\nBill to: Lee Wong\nAmount due: $450.00"
        self.assertEqual(knowledge.classify("old.pdf", invoice), "invoice_template")
        self.assertEqual(knowledge.classify("services.docx", _CONTRACT_TEXT), "contract_template")
        self.assertEqual(knowledge.classify("rates.txt", "Our rates: coaching is $90 per hour."), "price_list")
        self.assertEqual(knowledge.classify("notes.txt", "We are closed on public holidays."), "reference")
        answer = json.dumps({"role": "contract_template"})
        self.assertEqual(knowledge.classify("x.docx", "anything", complete=lambda *a, **k: answer), "contract_template")

    def test_templates_never_feed_replies(self):
        old = knowledge.add_document("old-invoice.pdf", "TAX INVOICE for Lee Wong. Workshop 450 dollars. Amount due.", "upload", summarize=False)
        prices = knowledge.add_document("prices.txt", "The workshop costs 300 dollars per person.", "upload", summarize=False)
        self.assertEqual({hit["doc_id"] for hit in knowledge.search("workshop dollars")}, {old["id"], prices["id"]})
        knowledge.update_meta(old["id"], {"role": "invoice_template"})
        self.assertEqual([hit["doc_id"] for hit in knowledge.search("workshop dollars")], [prices["id"]])
        from gmail_auto.profile_store import load_profile

        self.assertNotIn("Lee Wong", knowledge._source_material(load_profile()))
        self.assertEqual([item["id"] for item in knowledge.templates("invoice_template")], [old["id"]])

    def test_contract_fields_from_placeholders_or_from_a_filled_contract(self):
        found = contracts.find_fields(_CONTRACT_TEXT)
        self.assertEqual(found, [{"name": "Client name", "example": "{{Client name}}"}, {"name": "Fee", "example": "{{Fee}}"}])
        filled = "This Agreement is between Kiwi Sample Ltd and John  Smith. The fee is $5,000 + GST."
        reply = json.dumps(
            {"fields": [{"name": "Client", "example": "John Smith"}, {"name": "Fee", "example": "$5,000 + GST"}, {"name": "Made up", "example": "Not in it"}]}
        )
        self.assertEqual(
            contracts.find_fields(filled, complete=lambda *a, **k: reply),
            [{"name": "Client", "example": "John Smith"}, {"name": "Fee", "example": "$5,000 + GST"}],
        )

    def test_word_file_keeps_formatting_around_replaced_text(self):
        xml = (
            '<w:document xmlns:w="w"><w:body>'
            "<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Client: </w:t></w:r><w:r><w:t>John</w:t></w:r><w:r><w:t> Smith</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Contact John Smith &amp; team.</w:t></w:r></w:p></w:body></w:document>"
        )
        out = contracts._swap_xml(xml, [("John Smith", "Lee Wong")])
        self.assertIn('<w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Client: </w:t>', out)
        self.assertIn(">Lee Wong</w:t>", out)
        self.assertIn("Contact Lee Wong &amp; team.", out)
        self.assertNotIn("John", out)
        grown = contracts._swap_xml(xml, [("John Smith", "John Smith Jr")])
        self.assertEqual(grown.count("John Smith Jr"), 2)


def _zip(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buf.getvalue()


class OtherFormatTests(WebCase):
    contract = {
        "template_title": "Proposal.pptx",
        "fields": [{"name": "Client name", "example": "{{Client name}}"}, {"name": "Fee", "example": "{{Fee}}"}],
        "values": {"Client name": "Lee Wong 李", "Fee": "NZD 2,400"},
    }

    def test_powerpoint_keeps_its_layout_and_never_gets_xml_space(self):
        slide = (
            '<p:sld xmlns:a="a" xmlns:p="p"><p:cSld><p:spTree><p:sp><p:txBody>'
            "<a:p><a:r><a:rPr b=\"1\"/><a:t>Prepared for </a:t></a:r><a:r><a:t>{{Client</a:t></a:r><a:r><a:t> name}}</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        out = contracts.render_office(_zip({"ppt/slides/slide1.xml": slide, "ppt/presentation.xml": "<p/>"}), self.contract, "pptx")
        xml = zipfile.ZipFile(io.BytesIO(out)).read("ppt/slides/slide1.xml").decode("utf-8")
        self.assertIn('<a:rPr b="1"/><a:t>Prepared for </a:t>', xml)
        self.assertIn("<a:t>Lee Wong 李</a:t>", xml)
        self.assertNotIn("xml:space", xml)
        self.assertNotIn("{{", xml)

    def test_excel_fills_shared_and_inline_strings(self):
        shared = '<sst xmlns="x"><si><t>Fee: {{Fee}}</t></si><si><r><t>Client: </t></r><r><t>{{Client name}}</t></r></si></sst>'
        sheet = '<worksheet xmlns="x"><sheetData><row><c t="inlineStr"><is><t>{{Fee}}</t></is></c></row></sheetData></worksheet>'
        out = contracts.render_office(_zip({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet}), self.contract, "xlsx")
        archive = zipfile.ZipFile(io.BytesIO(out))
        self.assertIn("Fee: NZD 2,400", archive.read("xl/sharedStrings.xml").decode("utf-8"))
        self.assertIn("Lee Wong 李", archive.read("xl/sharedStrings.xml").decode("utf-8"))
        self.assertIn(">NZD 2,400</t>", archive.read("xl/worksheets/sheet1.xml").decode("utf-8"))

    def test_formats_names_and_a_pdf_template_becomes_a_plain_pdf(self):
        self.assertEqual([contracts.format_of(name) for name in ("a.docx", "b.PPTX", "c.xlsx", "d.pdf", "e.txt")], ["docx", "pptx", "xlsx", "pdf", "pdf"])
        self.assertTrue(contracts.layout_kept("a.xlsx"))
        self.assertFalse(contracts.layout_kept("a.pdf"))
        self.assertEqual(contracts.file_name({**self.contract, "template_title": "Agreement.pdf"}), "Agreement-Lee-Wong.pdf")
        pdf = contracts.render_pdf("SERVICES AGREEMENT\n\nBetween Kiwi Sample and {{Client name}}.\n\n费用：{{Fee}}", self.contract)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_pdfs_with_chinese_text_are_made_with_or_without_the_chinese_font(self):
        from gmail_auto import pdffonts

        invoices.save_template({"business_name": "奇异果知识有限公司", "gst_number": "123"})
        draft = invoices.create({"client_name": "李先生", "items": [{"description": "三个月辅导课程", "qty": 1, "unit_price": 2400}]})
        self.assertTrue(invoices.render_pdf(invoices.issue(draft["id"])).startswith(b"%PDF"))
        self.assertEqual(pdffonts.clean("李 — Lee", False), "? - Lee")
        self.assertEqual(pdffonts.clean("李 — Lee", True), "李 — Lee")


class TemplatePageTests(WebCase):
    def test_uploaded_invoice_is_kept_as_a_template_not_facts(self):
        page = self.client.post(
            "/knowledge/upload",
            data={"files": (io.BytesIO(b"TAX INVOICE\nInvoice No: INV-0041\nBill to: Lee Wong\nAmount due: $450.00"), "old-invoice.txt")},
            content_type="multipart/form-data",
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("looks like an invoice", page)
        self.assertEqual(knowledge.search("Lee Wong invoice"), [])
        _, listing = self.page("/knowledge")
        self.assertIn("role-invoice_template", listing)
        self.assertIn("not used as facts", listing)

        doc = knowledge.templates("invoice_template")[0]
        self.client.post(f"/knowledge/doc/{doc['id']}/role", data={"role": "reference"})
        self.assertEqual(knowledge.role_of(knowledge.get_document(doc["id"])), "reference")
        self.assertTrue(knowledge.search("Lee Wong invoice"))

    def test_contract_from_a_letter_fills_the_word_file_and_attaches_it(self):
        uploaded = self.client.post(
            "/knowledge/upload",
            data={
                "files": (
                    io.BytesIO(_docx(["SERVICES AGREEMENT", "This Agreement is made between Kiwi Sample Ltd and {{Client name}}.", "The parties agree the fee is {{Fee}}.", "Termination needs 30 days notice. Governing law: New Zealand."])),
                    "services.docx",
                )
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("looks like a contract", uploaded)
        self.assertIn("Client name, Fee", uploaded)
        template = knowledge.templates("contract_template")[0]

        letter = sample(id="c1", from_name="Lee", subject="Contract please", body="Please send the contract for the 3 month coaching.", label_ids=["INBOX"])
        box = Inbox([letter])
        filled = json.dumps({"values": {"Client name": "Lee Wong", "Fee": ""}, "missing": ["No fee for 3 month coaching in the knowledge base."]})
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, reader = self.page("/?show=all&open=c1")
            self.assertIn('formaction="/contracts/new"', reader)
            with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=filled):
                made = self.client.post("/contracts/new", data={"key": "c1", "template": template["id"]})
            contract_id = made.headers["Location"].rsplit("/", 1)[1]
            _, edit = self.page(f"/contracts/{contract_id}")
            self.assertIn("Lee Wong", edit)
            self.assertIn("No fee for 3 month coaching", edit)
            self.assertIn("Still empty: Fee", edit)

            blocked = self.client.post(f"/contracts/{contract_id}/attach", data={"field_0": "Lee Wong", "field_1": ""}, follow_redirects=True)
            self.assertIn("Fill in every part of the contract first: Fee", blocked.get_data(as_text=True))

            with patch("gmail_auto.generator.revise_reply", return_value="Hi Lee, the contract is attached."):
                done = self.client.post(f"/contracts/{contract_id}/attach", data={"field_0": "Lee Wong", "field_1": "NZD 2,400 + GST"})
            self.assertEqual(done.headers["Location"], "/?show=todo&open=c1")

        item = approvals.pending()[0]
        attached = item["attachments"][0]
        self.assertEqual(attached["name"], "services-Lee-Wong.docx")
        xml = _document_xml(files.read(attached["id"]))
        self.assertIn("made between Kiwi Sample Ltd and Lee Wong.", xml)
        self.assertIn("the fee is NZD 2,400 + GST.", xml)
        self.assertNotIn("{{", xml)

        download = self.client.get(f"/contracts/{contract_id}/docx")
        self.assertIn("services-Lee-Wong.docx", download.headers["Content-Disposition"])
        self.assertIn("Lee Wong", _document_xml(download.data))
        _, listing = self.page("/contracts")
        self.assertIn("services.docx", listing)
        self.assertIn("Client name: Lee Wong", listing)

    def test_invoice_upload_fills_empty_invoice_details_only(self):
        invoices.save_template({"business_name": "Already Set Ltd"})
        reply = json.dumps({"business_name": "Other Name", "gst_number": "123-456-789", "prefix": "KS-", "last_number": "41"})
        with patch("gmail_auto.llm.model_ready", return_value=True), patch("gmail_auto.llm.complete", return_value=reply), patch(
            "gmail_auto.knowledge.classify", return_value="invoice_template"
        ):
            self.client.post(
                "/knowledge/upload",
                data={"files": (io.BytesIO(b"TAX INVOICE KS-0041"), "ks.txt")},
                content_type="multipart/form-data",
            )
        saved = invoices.load_template()
        self.assertEqual(saved["business_name"], "Already Set Ltd")
        self.assertEqual((saved["gst_number"], saved["next_number"]), ("123-456-789", 42))
