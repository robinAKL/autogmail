import json
from types import SimpleNamespace
from unittest.mock import patch

from gmail_auto import approvals, assistant, contacts, invoices
from gmail_auto.models import Draft
from gmail_auto.runner import run_once
from gmail_auto.settings import save_settings
from gmail_auto.store import mark_processed, set_live
from tests.test_core import FakeGmail, sample
from tests.test_web import Inbox, WebCase


def _record(key, address, name, action="sent", code="", subject="Hello", summary=""):
    return {
        "id": key,
        "from_email": address,
        "from_name": name,
        "subject": subject,
        "summary": summary,
        "action": action,
        "skip_code": code,
        "processed_at": f"2026-11-{10 + len(key) % 9:02d} 10:00:00",
    }


class ContactStoreTests(WebCase):
    def test_built_from_people_not_robots_and_from_sent_mail(self):
        records = [
            _record("a1", "lee@abcbuild.co.nz", "Lee Wong"),
            _record("a2", "lee@abcbuild.co.nz", "Lee Wong", action="waiting"),
            _record("b1", "news@shop.example", "Shop", action="skipped", code="list"),
            _record("c1", "noreply@bank.example", "Bank", action="skipped", code="system"),
            _record("d1", "ann.k@gmail.com", "Ann K", action="skipped", code="no_reply_needed"),
        ]
        sent = [SimpleNamespace(date="2026-11-12 09:00", headers={"to": "Tom Supplier <tom@timber.example>, lee@abcbuild.co.nz"})]
        result = contacts.build(records + [_record("o1", "me@example.com", "Me")], sent, own={"me@example.com"})
        self.assertEqual(result["added"], 2)
        lee = contacts.find("LEE@abcbuild.co.nz")
        self.assertEqual((lee["name"], lee["company"], lee["letters"], lee["replied"]), ("Lee Wong", "", 2, 2))
        self.assertIsNone(contacts.find("ann.k@gmail.com"))
        self.assertIsNone(contacts.find("me@example.com"))
        self.assertEqual(contacts.find("tom@timber.example")["name"], "Tom Supplier")
        self.assertIsNone(contacts.find("news@shop.example"))
        self.assertIsNone(contacts.find("noreply@bank.example"))

    def test_robot_addresses_are_never_contacts_even_after_a_reply(self):
        for address in (
            "sender@mx252.csdn.net", "em@em1.cloudflare.com", "careers@email.careers.microsoft.com", "support@micro1.ai",
            "hello@send.evenrealities.com", "invoice+statements@vercel.com", "info@news.realestate.co.nz",
        ):
            self.assertTrue(contacts.looks_automated(address), address)
        for address in ("lee@abcbuild.co.nz", "joanna.wright@englishlanguage.org.nz", "tracywangtian@gmail.com", "y.lin3@barfoot.co.nz"):
            self.assertFalse(contacts.looks_automated(address), address)
        contacts.build([_record("r1", "sender@mx252.csdn.net", "CSDN"), _record("r2", "lee@abcbuild.co.nz", "Lee", action="expired")])
        self.assertEqual(contacts.all_contacts(), [])

    def test_replies_to_shared_or_automated_addresses_never_go_out_alone(self):
        for address in ("ats-notifications@atsmail.employmenthero.com", "communications@westlakegirls.school.nz", "uk.noreply@shop.example"):
            self.assertTrue(contacts.looks_automated(address), address)
        set_live(True)
        save_settings({"approval_mode": "never"})
        box = FakeGmail(
            [
                sample(id="s1", from_email="communications@westlakegirls.school.nz", from_name="Westlake Girls", subject="Important Term 4 Dates", body="Term 4 ends on 11 December."),
                sample(id="s2", from_email="lee@abcbuild.example", from_name="Lee", subject="Thanks", body="Got your note, thanks."),
                sample(id="s3", from_email="ann@b.example", from_name="Ann", subject="Can we meet?", body="Are you free on Friday?"),
            ]
        )
        drafts = {
            "s1": Draft(action="reply", reply="Thank you for the dates.", reason="Responding to important school dates."),
            "s2": Draft(action="reply", reply="You're welcome.", reason="This is a routine notification, but a short thank-you is polite."),
            "s3": Draft(action="reply", reply="Yes, Friday works.", reason="She asks about Friday."),
        }
        run_once(gmail=box, generate=lambda mail, *_: drafts[mail.id])
        self.assertEqual([sent[0] for sent in box.sent], ["s3"])
        waiting = {item["mail_id"]: item["reason"] for item in approvals.pending()}
        self.assertIn("shared or automated address", waiting["s1"])
        self.assertIn("Looks like a notice", waiting["s2"])

    def test_model_suggests_types_and_details_but_owner_edits_win(self):
        contacts.build([_record("a1", "lee@abcbuild.co.nz", "Lee Wong"), _record("e1", "mum@gmail.com", "Mum")])
        lee = contacts.find("lee@abcbuild.co.nz")
        mum = contacts.find("mum@gmail.com")
        contacts.save_edit(lee["id"], {"company": "ABC Builders Ltd"})
        reply = json.dumps(
            {
                "people": [
                    {"id": lee["id"], "type": "client", "reason": "Asked for a quote twice", "company": "Wrong Co", "title": "Director", "tone": "formal", "salutation": "Hi Lee"},
                    {"id": mum["id"], "type": "family", "reason": "Signs off as Mum", "tone": "casual"},
                    {"id": "nope", "type": "client"},
                ]
            }
        )
        self.assertEqual(contacts.sort_pending(complete=lambda *a, **k: reply), 2)
        lee = contacts.get(lee["id"])
        self.assertEqual((lee["type"], lee["type_status"], lee["type_reason"]), ("client", "suggested", "Asked for a quote twice"))
        self.assertEqual((lee["company"], lee["title"], lee["tone"], lee["salutation"]), ("ABC Builders Ltd", "Director", "formal", "Hi Lee"))
        self.assertEqual(contacts.always_ask("mum@gmail.com")["type"], "family")

        contacts.save_edit(lee["id"], {"name": "Lee W.", "type": "client"})
        self.assertEqual(contacts.get(lee["id"])["type_status"], "confirmed")
        contacts.build([_record("a9", "lee@abcbuild.co.nz", "Lee Wong-Smith")])
        self.assertEqual(contacts.get(lee["id"])["name"], "Lee W.")

        contacts.build([_record(f"z{index}", f"person{index}@example.org", f"Person {index}") for index in range(30)])
        calls = []

        def batches(_system, user, **_):
            calls.append(user.count("id="))
            return "not json"

        with self.assertRaises(Exception) as caught:
            contacts.sort_pending(complete=batches)
        self.assertIn("could not be read", str(caught.exception))
        self.assertEqual(calls, [12, 12])

        contacts.hide(mum["id"])
        contacts.build([_record("e2", "mum@gmail.com", "Mum")])
        self.assertIsNone(contacts.find("mum@gmail.com"))

    def test_signature_details_from_new_letters_fill_gaps_only(self):
        mail = sample(from_email="sam@supplies.example", from_name="Sam Lee")
        contacts.note_letter(mail, {"sender": {"company": "Supplies Ltd", "title": "Account manager", "phone": "09 123 4567"}})
        sam = contacts.find("sam@supplies.example")
        self.assertEqual((sam["company"], sam["title"], sam["phone"], sam["letters"]), ("Supplies Ltd", "Account manager", "09 123 4567", 1))
        contacts.save_edit(sam["id"], {"title": "Owner"})
        contacts.note_letter(mail, {"sender": {"title": "Sales"}})
        self.assertEqual(contacts.get(sam["id"])["title"], "Owner")
        self.assertIsNone(contacts.note_letter(sample(from_email="noreply@x.example"), {}))

    def test_contacts_shape_replies_invoices_and_whatsapp_names(self):
        from gmail_auto.generator import _user_prompt

        contacts.build([_record("a1", "lee@abcbuild.co.nz", "Lee Wong")])
        lee = contacts.find("lee@abcbuild.co.nz")
        contacts.save_edit(lee["id"], {"type": "client", "company": "ABC Builders Ltd", "salutation": "Dear Lee", "tone": "formal", "address": "5 Ponsonby Rd\nAuckland"})
        mail = sample(from_email="lee@abcbuild.co.nz", from_name="Lee Wong", subject="Quote", body="Can you quote?")
        prompt = _user_prompt(mail, {"name": "Robin", "role": "", "introduction": "", "tone": "", "cannot_promise": [], "sample_phrases": []}, "", "en")
        self.assertIn("Relationship to me: Client", prompt)
        self.assertIn("Greet them as: Dear Lee", prompt)

        fields = invoices.fill_from("invoice", mail, complete=lambda *a, **k: json.dumps({"client_name": "Lee", "items": []}))
        self.assertEqual(fields["client_name"], "ABC Builders Ltd (attn. Lee Wong)")
        self.assertEqual(fields["client_address"], "5 Ponsonby Rd\nAuckland")
        self.assertEqual(assistant._address_for("lee"), "lee@abcbuild.co.nz")
        self.assertEqual(assistant._address_for("ABC Builders Ltd"), "lee@abcbuild.co.nz")

    def test_replies_to_family_always_wait_whatever_the_mode(self):
        contacts.build([_record("e1", "mum@gmail.com", "Mum")])
        contacts.save_edit(contacts.find("mum@gmail.com")["id"], {"type": "family"})
        set_live(True)
        save_settings({"approval_mode": "never"})
        box = FakeGmail([sample(id="f1", from_email="mum@gmail.com", from_name="Mum", subject="Dinner Sunday?")])
        run_once(gmail=box, generate=lambda *_: Draft(action="reply", reply="Yes, see you Sunday.", reason="Easy yes."))
        self.assertEqual(box.sent, [])
        waiting = approvals.pending()
        self.assertEqual(len(waiting), 1)
        self.assertIn("marked as family", waiting[0]["reason"])


class ContactPageTests(WebCase):
    def test_scan_list_edit_confirm_and_reader_chip(self):
        mark_processed(_record("p1", "lee@abcbuild.co.nz", "Lee Wong", summary="Asked for a quote"))
        mark_processed(_record("p2", "news@shop.example", "Shop", action="skipped", code="list"))
        with patch("gmail_auto.web.connected_clients", return_value=([], [])):
            scanned = self.client.post("/contacts/scan", follow_redirects=True).get_data(as_text=True)
        self.assertIn("1 new and 0 updated", scanned)
        self.assertIn("Lee Wong", scanned)
        self.assertNotIn("news@shop.example", scanned)

        lee = contacts.find("lee@abcbuild.co.nz")
        _, page = self.page(f"/contacts/{lee['id']}")
        self.assertIn("Asked for a quote", page)
        self.client.post(f"/contacts/{lee['id']}/save", data={"name": "Lee Wong", "type": "student", "company": "", "notes": "Prefers WhatsApp"})
        saved = contacts.get(lee["id"])
        self.assertEqual((saved["type"], saved["type_status"], saved["notes"]), ("student", "confirmed", "Prefers WhatsApp"))
        _, students = self.page("/contacts?type=student")
        self.assertIn("Lee Wong", students)
        _, search = self.page("/contacts?q=nobody")
        self.assertNotIn("Lee Wong</strong>", search)

        box = Inbox([sample(id="p1", from_email="lee@abcbuild.co.nz", from_name="Lee Wong", subject="Hello", label_ids=["INBOX"])])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            _, reader = self.page("/?show=all&open=p1")
        self.assertIn(f'href="/contacts/{lee["id"]}"', reader)
        self.assertIn("Student", reader)
        _, compose = self.page("/compose")
        self.assertIn('<option value="lee@abcbuild.co.nz">Lee Wong</option>', compose)

        self.client.post(f"/contacts/{lee['id']}/hide")
        self.assertIsNone(contacts.find("lee@abcbuild.co.nz"))
