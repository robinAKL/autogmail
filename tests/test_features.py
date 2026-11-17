import hashlib
import hmac
import io
import os
import zipfile
from datetime import date, timedelta
from unittest.mock import patch

from gmail_auto import approvals, assistant, cleanup, knowledge, stats, whatsapp
from gmail_auto.errors import ConfigError, NotConnected
from gmail_auto.extractors import extract_text
from gmail_auto.mailview import clean_html, document
from gmail_auto.models import Draft
from gmail_auto.outlook_client import parse_message as parse_outlook
from gmail_auto.runner import run_once, send_daily_digest
from gmail_auto.settings import load_settings, save_settings
from gmail_auto.sharepoint import share_token
from gmail_auto.store import get_processed, is_live, set_live
from tests.test_core import FakeGmail, TempHome, sample, write_test_profile


def docx_bytes(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


class ExtractorTests(TempHome):
    def test_reads_text_html_and_word_files(self):
        self.assertEqual(extract_text("notes.txt", "Workshop fee is 450 dollars.".encode("utf-8")), "Workshop fee is 450 dollars.")
        page = extract_text("page.html", b"<html><head><style>p{color:red}</style></head><body><p>Refunds within 14 days</p><script>x()</script></body></html>")
        self.assertEqual(page, "Refunds within 14 days")
        self.assertEqual(extract_text("policy.docx", docx_bytes("Refund policy", "Refunds within 14 days.")), "Refund policy\nRefunds within 14 days.")

    def test_unknown_and_empty_files_are_refused_with_a_reason(self):
        with self.assertRaises(ConfigError) as unknown:
            extract_text("photo.png", b"\x89PNG")
        self.assertIn("not a supported file type", str(unknown.exception))
        with self.assertRaises(ConfigError) as empty:
            extract_text("blank.txt", b"   ")
        self.assertIn("No text", str(empty.exception))
        with self.assertRaises(ConfigError):
            extract_text("broken.docx", b"not a zip")


class KnowledgeTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ.pop("OPENAI_API_KEY", None)

    def test_documents_are_found_by_what_a_letter_is_about(self):
        knowledge.add_document("Workshop prices", "The October AI workshop costs 450 dollars per person. Group discount is 10 percent.", "note")
        knowledge.add_document("School calendar", "Term 4 ends on 16 December. Parent interviews are in week 6.", "upload", origin="calendar.txt")
        knowledge.save_rules("Always ask me before agreeing to a price.\n- Never reply to recruiters.")
        mail = sample(subject="Workshop price?", body="How much does the October workshop cost for a group?")
        context = knowledge.context_for(mail)
        self.assertEqual(context["sources"], ["Workshop prices"])
        self.assertIn("450 dollars", context["passages"][0]["text"])
        self.assertEqual(context["rules"], ["Always ask me before agreeing to a price.", "Never reply to recruiters."])
        prompt = knowledge.render_context(context)
        self.assertIn("MY RULES", prompt)
        self.assertIn("Workshop prices", prompt)
        self.assertEqual(knowledge.search("something about gardening tools"), [])
        chinese = knowledge.add_document("课程安排", "人工智能课程在每周三晚上七点开始。", "note")
        self.assertEqual(knowledge.search("课程什么时候开始")[0]["doc_id"], chinese["id"])

    def test_same_file_replaces_itself_and_removal_forgets_it(self):
        first = knowledge.add_document("prices.txt", "Old price 400 dollars.", "upload", origin="prices.txt")
        second = knowledge.add_document("prices.txt", "New price 450 dollars.", "upload", origin="prices.txt")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(knowledge.list_documents()), 1)
        self.assertIn("450", knowledge.search("price")[0]["text"])
        self.assertTrue(knowledge.delete_document(second["id"]))
        self.assertEqual(knowledge.search("price"), [])
        self.assertIsNone(knowledge.get_document(second["id"]))

    def test_understanding_is_rewritten_from_profile_rules_and_documents(self):
        profile = write_test_profile()
        knowledge.add_document("Workshop prices", "The October AI workshop costs 450 dollars.", "note")
        knowledge.save_rules(["Never reply to recruiters."])
        self.assertTrue(knowledge.load_index()["brief"]["stale"])
        seen = {}

        def fake_complete(system, user, **_kwargs):
            seen["user"] = user
            return "## Who I am\n- Test User, a tester."

        with patch("gmail_auto.knowledge.complete", fake_complete):
            knowledge.rebuild_brief(profile)
        self.assertIn("450 dollars", seen["user"])
        self.assertIn("Never reply to recruiters.", seen["user"])
        self.assertIn("Test User", seen["user"])
        brief = knowledge.load_index()["brief"]
        self.assertFalse(brief["stale"])
        self.assertIn("Who I am", brief["text"])
        knowledge.save_brief("## Who I am\n- Edited by hand.")
        self.assertIn("Edited by hand", knowledge.load_index()["brief"]["text"])

    def test_facts_stay_locked_and_rules_can_be_changed(self):
        knowledge.save_brief("## Key facts\n- The workshop costs 450 dollars.\n## How to handle my mail\n- Never reply to recruiters.")
        items = {item["text"]: item for item in knowledge.list_items()}
        fact = items["The workshop costs 450 dollars."]
        rule = items["Never reply to recruiters."]
        self.assertTrue(fact["locked"])
        with self.assertRaises(ConfigError):
            knowledge.update_item(fact["id"], "The workshop is free.")
        with self.assertRaises(ConfigError):
            knowledge.delete_item(fact["id"])
        knowledge.update_item(rule["id"], "Ask me before replying to recruiters.")
        self.assertEqual(knowledge.get_item(rule["id"])["text"], "Ask me before replying to recruiters.")
        ended = knowledge.end_item(fact["id"])
        self.assertEqual(ended["status"], "ended")
        self.assertFalse(knowledge.is_active(knowledge.get_item(fact["id"])))
        self.assertTrue(knowledge.delete_item(rule["id"]))
        self.assertIsNone(knowledge.get_item(rule["id"]))
        graph = knowledge.graph_view()
        self.assertTrue(any(node["id"] == "you" for node in graph["nodes"]))
        self.assertFalse(any(node["id"] == fact["id"] for node in graph["nodes"]))
        self.assertTrue(all("from" in edge and "to" in edge for edge in graph["edges"]))

    def test_reorganise_keeps_old_facts_on_the_timeline(self):
        profile = write_test_profile()
        knowledge.save_brief("## Key facts\n- The workshop costs 450 dollars.")
        old = knowledge.list_items("fact")[0]

        def fake_complete(_system, _user, **_kwargs):
            return '{"items":[{"kind":"fact","text":"The workshop costs 500 dollars.","as_of":"2026-11-13"}]}'

        with patch("gmail_auto.knowledge.complete", fake_complete):
            knowledge.rebuild_model(profile)
        live = knowledge.list_items("fact", active_only=True)
        past = [item for item in knowledge.list_items("fact") if not knowledge.is_active(item)]
        self.assertEqual([item["text"] for item in live], ["The workshop costs 500 dollars."])
        self.assertEqual(past[0]["id"], old["id"])
        self.assertEqual(past[0]["status"], "superseded")
        self.assertEqual(live[0]["replaces"], old["id"])
        self.assertIn("500 dollars", knowledge.load_index()["brief"]["text"])
        self.assertNotIn("450 dollars", knowledge.load_index()["brief"]["text"])
        self.assertTrue(knowledge.timeline())
        self.assertEqual(knowledge.memory_tiles()[0]["count"], 1)

    def test_learning_from_email_leaves_out_secrets_and_promotions(self):
        class Box(FakeGmail):
            def list_sent(self, limit=30, full=False):
                return [sample(id="s1", subject="Re: Friday", body="Friday at 2pm suits me. Kind regards, Test", headers={"to": "lee@example.com"})]

            def list_recent(self, limit=30, full=False):
                return [
                    sample(id="r1", body="Can we move the workshop to Friday?"),
                    sample(id="r2", body="Your password is hunter2"),
                    sample(id="r3", body="Big sale today", label_ids=["INBOX", "CATEGORY_PROMOTIONS"]),
                ]

        seen = []

        def fake_complete(system, user, **_kwargs):
            seen.append(user)
            return "## People and organisations\n- Lee: arranges workshops."

        with patch("gmail_auto.knowledge.complete", fake_complete):
            entry = knowledge.learn_from_mailbox(Box([]))
        self.assertEqual(entry["meta"]["letters"], 2)
        self.assertNotIn("hunter2", seen[0])
        self.assertNotIn("Big sale", seen[0])
        self.assertIn("[SENT]", seen[0])
        self.assertEqual(knowledge.list_documents()[0]["source"], "email")

    def test_outlook_history_uses_previews_when_bodies_are_missing(self):
        class OutlookBox:
            provider = "outlook"

            def get_user_email(self):
                return "robin@kiwiknowledge.co.nz"

            def list_sent(self, limit=12, full=False):
                raise ConfigError("Outlook timed out after 8s. The mailbox list was too slow; Gmail can still be used.")

            def list_recent(self, limit=12, full=False):
                self.assertFalse(full)
                return [
                    sample(
                        id="o1",
                        provider="outlook",
                        subject="School values",
                        body="",
                        snippet="Dear Parents, please remember hats and healthy eating.",
                    )
                ]

            def assertFalse(self, value):
                if value:
                    raise AssertionError("Outlook history should not request full HTML bodies")

        seen = []

        def fake_complete(system, user, **_kwargs):
            seen.append(user)
            return "## People and organisations\n- Wairau Intermediate School: sends parent mail."

        with patch("gmail_auto.knowledge.complete", fake_complete):
            entry = knowledge.learn_from_mailbox(OutlookBox())
        self.assertEqual(entry["meta"]["letters"], 1)
        self.assertEqual(entry["meta"]["provider"], "outlook")
        self.assertIn("healthy eating", seen[0])
        self.assertEqual(entry["title"], "What your email shows (robin@kiwiknowledge.co.nz)")


class CalendarTests(TempHome):
    def test_meeting_mail_becomes_a_calendar_draft_and_job_alerts_do_not(self):
        from gmail_auto.calendars import suggest_event

        meeting = sample(
            subject="Meeting 14 November 2026 15:00",
            body="Please join the meeting on 14 November 2026 15:00. Invite jane@client.example.",
            from_email="lee@example.com",
        )
        draft = suggest_event(meeting, "me@example.com")
        self.assertIsNotNone(draft)
        self.assertEqual(draft["start"].day, 14)
        self.assertEqual(draft["start"].hour, 15)
        self.assertIn("jane@client.example", draft["attendees"])
        alert = sample(
            from_email="jobalerts-noreply@linkedin.com",
            subject="You may be a fit for a meeting role",
            body="Job alert for meetings.",
        )
        self.assertIsNone(suggest_event(alert, "me@example.com"))

    def test_automatic_events_need_a_real_future_date_from_a_person(self):
        from datetime import datetime, timedelta
        from gmail_auto.calendars import ZONE, suggest_event

        soon = (datetime.now(ZONE) + timedelta(days=10)).strftime("%d %B %Y")
        real = sample(subject=f"Catch-up {soon} 15:00", body=f"Can we meet on {soon} 15:00?", from_email="lee@example.com")
        no_date = sample(subject="Meeting notes", body="Thanks for the meeting today.", from_email="lee@example.com")
        past = sample(subject="Meeting 14 November 2020 15:00", body="Meeting 14 November 2020 15:00.", from_email="lee@example.com")
        newsletter = sample(
            subject=f"Webinar invite {soon} 15:00",
            body="Join our Zoom webinar. Unsubscribe.",
            from_email="news@saas.example",
        )
        far_date = sample(
            subject="Project notes",
            body="Invoice dated " + soon + ". " + ("Lots of unrelated text. " * 20) + "Join us on Teams.",
            from_email="lee@example.com",
        )
        self.assertIsNotNone(suggest_event(real, "me@example.com", strict=True))
        self.assertIsNone(suggest_event(no_date, "me@example.com", strict=True))
        self.assertIsNone(suggest_event(past, "me@example.com", strict=True))
        self.assertIsNone(suggest_event(newsletter, "me@example.com", strict=True))
        self.assertIsNone(suggest_event(far_date, "me@example.com", strict=True))
        # By hand the owner still gets a draft to move, even without a date.
        self.assertIsNotNone(suggest_event(no_date, "me@example.com"))

    def test_month_board_is_a_monday_week_grid(self):
        from datetime import datetime
        from gmail_auto.calendars import ZONE, _item, month_board

        start = datetime(2026, 11, 13, 15, 0, tzinfo=ZONE)
        event = _item("e1", "gmail", "Parent interview", start, start.replace(hour=16), "Westlake", [], "")

        with patch("gmail_auto.calendars._outlook_events", side_effect=NotConnected()), patch(
            "gmail_auto.calendars._gmail_events", return_value=([event], "me@example.com")
        ):
            board = month_board(2026, 11, "2026-11-13")
        self.assertEqual(board["title"], "November 2026")
        self.assertEqual(board["weekdays"][0], "Mon")
        self.assertGreaterEqual(len(board["weeks"]), 5)
        for week in board["weeks"]:
            self.assertEqual(len(week), 7)
        self.assertEqual(board["weeks"][0][0]["date"], "2026-10-26")
        found = next(cell for week in board["weeks"] for cell in week if cell["date"] == "2026-11-13")
        self.assertTrue(found["in_month"])
        self.assertEqual(found["events"][0]["title"], "Parent interview")
        self.assertEqual(board["selected_label"], "Friday 13 November")
        self.assertEqual(len(board["selected_events"]), 1)

    def test_items_added_from_mail_can_be_removed_and_hand_made_ones_stay(self):
        from gmail_auto import calendars
        from gmail_auto.storage import load_doc, save_doc

        save_doc(
            "calendar_mail",
            {
                "old1": {"event_id": "ev-old1", "at": "2026-11-14 09:00"},
                "outlook:old2": {"event_id": "ev-old2", "at": "2026-11-14 10:00"},
                "auto3": {"event_id": "ev-auto3", "at": "2026-11-15 08:00", "auto": True, "provider": "gmail"},
                "hand4": {"event_id": "ev-hand4", "at": "2026-11-15 08:30", "auto": False, "provider": "gmail"},
                "broken5": {"event_id": "ev-broken5", "at": "2026-11-15 09:00", "auto": True, "provider": "gmail"},
            },
        )
        self.assertEqual(calendars.added_from_mail(), 4)
        deleted = []

        def fake_delete(provider, event_id):
            if event_id == "ev-broken5":
                raise ConfigError("Gmail calendar did not remove the item.")
            deleted.append((provider, event_id))

        with patch("gmail_auto.calendars._delete_event", fake_delete):
            result = calendars.remove_added_from_mail()
        self.assertEqual(sorted(deleted), [("gmail", "ev-auto3"), ("gmail", "ev-old1"), ("outlook", "ev-old2")])
        self.assertEqual((result["removed"], result["failed"], result["left"]), (3, 1, 1))
        saved = load_doc("calendar_mail", {})
        self.assertEqual(saved["hand4"]["event_id"], "ev-hand4")
        self.assertTrue(saved["old1"]["removed"])
        # A removed letter stays on record so it is not added again; the failed one can be retried.
        self.assertTrue(calendars.already_created("old1"))
        self.assertEqual(calendars.added_from_mail(), 1)

    def test_outlook_times_are_read_in_their_own_time_zone(self):
        from gmail_auto.calendars import _view

        # Graph sends UTC by default: Tuesday 06:45 in Auckland (NZDT, +13) arrives as Monday 17:45 UTC.
        weekly = {
            "id": "o1",
            "subject": "Elite Ponsonby Breakfast",
            "start": {"dateTime": "2026-11-16T17:45:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-16T19:15:00.0000000", "timeZone": "UTC"},
        }
        event = _view(weekly, "outlook")
        self.assertEqual(event["date"], "2026-11-17")
        self.assertEqual(event["hour"], "06:45")
        self.assertTrue(event["day"].startswith("Tuesday"))
        local = _view(
            {
                "id": "o2",
                "subject": "Local",
                "start": {"dateTime": "2026-11-17T06:45:00.0000000", "timeZone": "New Zealand Standard Time"},
                "end": {"dateTime": "2026-11-17T08:15:00.0000000", "timeZone": "New Zealand Standard Time"},
            },
            "outlook",
        )
        self.assertEqual((local["date"], local["hour"]), ("2026-11-17", "06:45"))
        missing = _view({"id": "o3", "subject": "No zone", "start": {"dateTime": "2026-11-16T17:45:00"}, "end": {"dateTime": "2026-11-16T18:45:00"}}, "outlook")
        self.assertEqual((missing["date"], missing["hour"]), ("2026-11-17", "06:45"))

    def test_removal_works_in_batches(self):
        from gmail_auto import calendars
        from gmail_auto.storage import save_doc

        save_doc("calendar_mail", {f"m{n}": {"event_id": f"ev{n}", "at": "2026-11-14 09:00"} for n in range(5)})
        with patch("gmail_auto.calendars._delete_event", lambda provider, event_id: None):
            first = calendars.remove_added_from_mail(limit=3)
            second = calendars.remove_added_from_mail(limit=3)
        self.assertEqual((first["removed"], first["left"]), (3, 2))
        self.assertEqual((second["removed"], second["left"]), (2, 0))

    def test_whatsapp_pings_events_about_to_start_without_changing_calendar_reminders(self):
        from datetime import datetime, timedelta
        from gmail_auto.calendars import ZONE, ping_due_whatsapp

        start = datetime.now(ZONE) + timedelta(minutes=12)
        event = {
            "id": "cal1",
            "title": "Parent interview",
            "start": start,
            "when": "15:00–16:00",
            "location": "Westlake",
        }
        sent = []
        save_settings({"whatsapp_to": "64211234567", "notify_calendar": True, "daily_digest": False})
        os.environ["WHATSAPP_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "123"
        with patch("gmail_auto.calendars.list_upcoming", return_value={"events": [event]}), patch(
            "gmail_auto.whatsapp.send_text", lambda body: sent.append(body) or "wamid.1"
        ):
            self.assertEqual(ping_due_whatsapp(), 1)
            self.assertEqual(ping_due_whatsapp(), 0)
        self.assertIn("Parent interview", sent[0])
        self.assertIn("Calendar in", sent[0])


class ImportanceTests(TempHome):
    def test_invoices_enquiries_and_school_are_important_job_alerts_are_not(self):
        from gmail_auto.importance import assess

        invoice = sample(from_email="noreply@xero.com", subject="Invoice 1042 is due")
        enquiry = sample(from_email="lee@client.example", subject="Quote for the workshop")
        school = sample(from_email="office@wairau.school.nz", subject="Westlake newsletter")
        junk = sample(from_email="jobalerts-noreply@linkedin.com", subject="You may be a fit")
        self.assertEqual(assess(invoice)["kind"], "invoice")
        self.assertEqual(assess(enquiry)["kind"], "enquiry")
        self.assertEqual(assess(school)["kind"], "school")
        self.assertFalse(assess(junk)["important"])

    def test_job_digests_are_not_health_or_home_but_real_clinic_mail_is(self):
        from gmail_auto.importance import assess

        digest = sample(
            from_email="team@remoteusa.co",
            from_name="Remoteusa.co",
            subject="💼 23 new jobs this week on Remoteusa.co",
            body="Clinic Coordinator at Sunrise Medical Centre. Hospital Referral Specialist. Leasing agent for a property manager.",
        )
        bulk_clinic_word = sample(
            from_email="digest@community.example",
            subject="Your weekly roundup",
            body="Stories this week: a new clinic opens downtown.",
            headers={"list-unsubscribe": "<mailto:x@community.example>"},
        )
        results = sample(from_email="noreply@managemyhealth.co.nz", subject="Your test results are ready")
        doctor = sample(from_email="reception@smithdoctors.co.nz", subject="Appointment", body="Your referral to the specialist has been sent.")
        self.assertFalse(assess(digest)["important"])
        self.assertFalse(assess(bulk_clinic_word)["important"])
        self.assertEqual(assess(results)["kind"], "health")
        self.assertEqual(assess(doctor)["kind"], "health")

    def test_notifications_receipts_and_marketing_do_not_ping(self):
        from gmail_auto.importance import assess

        quiet = [
            sample(from_email="no-reply@apple.com", subject="Your receipt from Apple", body="Receipt for your payment. Thank you for your payment."),
            sample(from_email="news@shop.example", subject="Big sale ends tonight", body="50% off everything. Unsubscribe here."),
            sample(from_email="notifications@github.com", subject="Reminder: meeting digest", body="Your weekly update. Invite your team."),
            sample(from_email="alerts@bank.example", subject="New sign-in to your account", body="We noticed a new login."),
            sample(
                from_email="lee@shop.example",
                subject="Your order has shipped",
                body="Tracking number 123.",
                headers={"list-unsubscribe": "<mailto:x@shop.example>"},
            ),
            sample(from_email="hello@saas.example", subject="Join our webinar on 20 December 2026", body="Zoom webinar invite."),
        ]
        for mail in quiet:
            self.assertFalse(assess(mail)["important"], mail.subject)

    def test_people_bills_fines_institutions_and_appointments_do_ping(self):
        from datetime import datetime, timedelta
        from gmail_auto.calendars import ZONE
        from gmail_auto.importance import assess

        soon = (datetime.now(ZONE) + timedelta(days=12)).strftime("%d %B %Y")
        bill = sample(from_email="noreply@mercury.co.nz", subject="Your power bill is ready", body="Amount due $182 by 30 November.")
        fine = sample(from_email="noreply@at.govt.nz", subject="Infringement notice", body="Parking fine of $40.")
        ird = sample(from_email="donotreply@ird.govt.nz", subject="Your tax account", body="You have a new letter in myIR.")
        appointment = sample(
            from_email="jane@dentist.example",
            subject=f"Appointment {soon} 10:30",
            body=f"See you for your appointment on {soon} 10:30.",
        )
        enquiry = sample(from_email="lee@client.example", subject="Quote for the workshop", body="How much for two days?")
        self.assertEqual(assess(bill)["kind"], "invoice")
        self.assertEqual(assess(fine)["kind"], "fine")
        self.assertEqual(assess(ird)["kind"], "institution")
        self.assertEqual(assess(appointment)["kind"], "meeting")
        self.assertEqual(assess(enquiry)["kind"], "enquiry")

    def test_more_kinds_beyond_the_examples(self):
        from gmail_auto.importance import KINDS, assess

        sign = sample(from_email="dse@docusign.net", subject="Please sign: Lease agreement", body="Review and sign the document.")
        declined = sample(from_email="noreply@spark.co.nz", subject="Payment declined", body="Your card was declined. Update your card.")
        health = sample(from_email="noreply@citymed.example", subject="Your test results are ready", body="Log in to see your lab results.")
        rent = sample(from_email="pm@homes.example", subject="Rent increase from January", body="Your landlord has reviewed the rent.")
        promo = sample(from_email="noreply@shop.example", subject="Final notice: 50% off ends tonight", body="Shop now. Unsubscribe.")
        self.assertEqual(assess(sign)["kind"], "action")
        self.assertEqual(assess(declined)["kind"], "action")
        self.assertEqual(assess(health)["kind"], "health")
        self.assertEqual(assess(rent)["kind"], "housing")
        self.assertFalse(assess(promo)["important"])
        self.assertEqual(set(KINDS), {"fine", "invoice", "action", "school", "institution", "health", "housing", "enquiry", "meeting", "person", "company"})

    def test_settings_keep_only_known_kinds_and_busy_preset_pings_urgent_only(self):
        from gmail_auto.importance import INTERRUPT, inbox_kinds, ping_kinds

        self.assertEqual(load_settings()["notify_preset"], "busy")
        self.assertEqual(load_settings()["notify_kinds"], list(INTERRUPT))
        self.assertEqual(ping_kinds(load_settings()), list(INTERRUPT))
        self.assertIn("enquiry", inbox_kinds(load_settings()))
        save_settings({"notify_kinds": ["invoice", "nonsense", "fine"]})
        self.assertEqual(load_settings()["notify_preset"], "custom")
        self.assertEqual(load_settings()["notify_kinds"], ["fine", "invoice"])
        save_settings({"notify_kinds": []})
        self.assertEqual(load_settings()["notify_kinds"], [])
        save_settings({"notify_preset": "urgent", "notify_kinds": ["enquiry"]})
        self.assertEqual(load_settings()["notify_kinds"], list(INTERRUPT))
        self.assertNotIn("enquiry", ping_kinds(load_settings()))


class CleanupTests(TempHome):
    def test_cleanup_follows_match_and_keep_rules(self):
        save_settings(
            {
                "cleanup_match": ["from:linkedin.com", "job alert"],
                "cleanup_keep": ["from:etap.co.nz", "westlake"],
                "cleanup_skipped": False,
            }
        )
        settings = load_settings()
        job = sample(id="j1", from_email="jobs-noreply@linkedin.com", subject="You may be a fit")
        school = sample(id="s1", from_email="office@wairau.school.nz", subject="Westlake and Wairau update")
        etap = sample(id="e1", from_email="Wairau_Intermediate_School@etap.co.nz", subject="Job alert newsletter")
        person = sample(id="p1", from_email="lee@example.com", subject="Lunch Friday?")
        self.assertTrue(cleanup.should_clean(job, "me@example.com", settings))
        self.assertFalse(cleanup.should_clean(school, "me@example.com", settings))
        self.assertFalse(cleanup.should_clean(etap, "me@example.com", settings))
        self.assertFalse(cleanup.should_clean(person, "me@example.com", settings))
        box = FakeGmail([job, school, etap, person])
        result = cleanup.run(box, settings)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(box.archived, ["j1"])
        many = FakeGmail([sample(id=f"x{i}", from_email="jobs-noreply@linkedin.com", subject="You may be a fit") for i in range(60)])
        deep = cleanup.run(many, settings, limit=60)
        self.assertEqual(deep["scanned"], 60)
        self.assertEqual(deep["archived"], 60)

    def test_default_rules_clean_ads_and_job_alerts_but_keep_codes_and_school(self):
        settings = load_settings()
        ads = [
            sample(id="a1", from_email="hello@doordash.com", from_name="DoorDash", subject="Welcome to DashPass"),
            sample(id="a2", from_email="listings@mail.trademe.co.nz", from_name="Trade Me", subject="Latest listings in mark-sh"),
            sample(id="a3", from_email="jobalerts-noreply@linkedin.com", from_name="LinkedIn Job Alerts", subject="Solution Architect role"),
            sample(id="a4", from_email="team@heygen.com", from_name="David from HeyGen", subject="We saved you a spot in the HeyGen Community"),
            sample(id="a5", from_email="news@moomoo.com", from_name="Moomoo Securities A.", subject="Tech shares fall: how concentrated is your portfolio?"),
            sample(id="a6", from_email="shots@caffeine.ai", from_name="Caffeine", subject="Caffeine Daily Shot: Airie's $100k seed"),
            sample(id="a7", from_email="digest@thetip.ai", from_name="TheTip.ai | AI News.", subject="Your Notion AI chats may be exposed"),
            sample(id="a8", from_email="mail@shopify.com", from_name="Shopify", subject="Your first sale is waiting"),
            sample(id="a9", from_email="alerts@opps.example.com", subject="Opportunities for You: ICANN89 Fellowship 2027"),
            sample(id="a10", from_email="jobs@linkedin.com", from_name="LinkedIn", subject="Crossing Hurdles is hiring for a Remote role"),
            sample(id="a11", from_email="npm@npmjs.com", from_name="npm", subject="Your weekly npm update"),
            sample(id="a12", from_email="jobs@zeil.com", from_name="Zeil", subject="New roles this week"),
            sample(id="a13", from_email="team@openai.com", from_name="OpenAI", subject="What's new in ChatGPT"),
            sample(id="a14", from_email="hello@osl.com", from_name="OSL", subject="Market update"),
            sample(id="a15", from_email="news@email.apple.com", from_name="Apple", subject="New on the App Store"),
            sample(id="a16", from_email="noreply@freelancer.com", from_name="Freelancer", subject="Freelance projects you might like"),
        ]
        for mail in ads:
            self.assertTrue(cleanup.should_clean(mail, "me@example.com", settings), mail.subject)
        self.assertFalse(
            cleanup.should_clean(
                sample(id="k1", from_email="security@facebookmail.com", from_name="Facebook", subject="629743 is your code to confirm this email"),
                "me@example.com",
                settings,
            )
        )
        self.assertFalse(
            cleanup.should_clean(
                sample(id="k2", from_email="no-reply@accounts.google.com", subject="Security alert — a new sign-in on Windows"),
                "me@example.com",
                settings,
            )
        )
        self.assertFalse(
            cleanup.should_clean(
                sample(id="k3", from_email="office@wairau.school.nz", subject="Westlake newsletter"),
                "me@example.com",
                settings,
            )
        )
        self.assertFalse(
            cleanup.should_clean(
                sample(id="k4", from_email="appleid@id.apple.com", from_name="Apple", subject="Your Apple ID verification code is 123456"),
                "me@example.com",
                settings,
            )
        )

    def test_gmail_search_targets_inbox_ads_and_excludes_school(self):
        settings = load_settings()
        queries = " ".join(cleanup.gmail_queries(settings))
        self.assertIn("in:inbox", queries)
        self.assertIn("from:linkedin.com", queries)
        self.assertIn("from:doordash.com", queries)
        self.assertIn("from:npmjs.com", queries)
        self.assertIn("from:zeil.com", queries)
        self.assertIn("from:openai.com", queries)
        self.assertIn("from:apple.com", queries)
        self.assertIn("-from:etap.co.nz", queries)
        self.assertIn('-"westlake"', queries)

        class SearchBox(FakeGmail):
            def list_ids(self, query, limit=500):
                self.queries.append(query)
                return ["g1", "g2"]

        box = SearchBox([])
        box.queries = []
        result = cleanup.run(box, settings)
        self.assertEqual(result["archived"], 2)
        self.assertEqual(box.archived, ["g1", "g2"])
        self.assertTrue(any("from:linkedin.com" in item for item in box.queries))


class StatsTests(TempHome):
    def test_dashboard_counts_what_a_round_did(self):
        write_test_profile()
        set_live(True)
        mails = [
            sample(id="a1"),
            sample(id="a2", from_email="noreply@shop.example.com"),
            sample(id="a3", subject="Meet Friday?"),
        ]

        def generate(mail, *_):
            if mail.id == "a3":
                return Draft(action="ask", reply="Friday works.", reason="Commits to a time.")
            return "I saw your note."

        fake = FakeGmail(mails)
        run_once(gmail=fake, generate=generate)
        item = approvals.pending()[0]
        approvals.approve(item["id"], "web", client=fake)

        tomorrow = date.today() + timedelta(days=1)
        data = stats.dashboard(7, today=tomorrow)
        y = data["yesterday"]
        self.assertEqual((y["received"], y["auto_sent"], y["approved_sent"], y["skipped"], y["asked"]), (3, 1, 1, 1, 1))
        self.assertEqual(y["sent"], 2)
        self.assertEqual(y["minutes_saved"], 2 * stats.MINUTES_SAVED_PER_REPLY)
        self.assertEqual(y["approval_rate"], 100)
        self.assertEqual(data["skip_reasons"][0]["label"], "System or no-reply address")
        self.assertEqual(data["senders"][0]["key"], "lee@example.com")
        self.assertEqual(data["series"][-2]["auto_sent"], 1)
        self.assertIn("2 replies sent (1 automatic, 1 after your OK)", stats.digest_text(today=tomorrow))

    def test_history_is_filled_from_records_made_before_the_upgrade(self):
        records = [
            {"id": "x1", "action": "skipped", "reason": "系统地址，不回复", "processed_at": "2026-09-30 22:31:17", "from_email": "noreply@a.example.com"},
            {"id": "x2", "action": "skipped", "reason": "x", "sensitive": True, "processed_at": "2026-09-30 22:31:18"},
            {"id": "x3", "action": "drafted", "processed_at": "2026-10-01 08:00:00", "from_email": "lee@example.com"},
        ]
        stats.rebuild_from_records(records)
        data = stats.dashboard(7, today=date(2026, 10, 2))
        self.assertEqual(data["yesterday"]["drafted"], 1)
        self.assertEqual(data["all_time"]["skipped"], 2)
        self.assertEqual({item["key"] for item in data["skip_reasons"]}, {"system", "sensitive"})
        self.assertEqual(data["first_day"], "2026-09-30")


class WhatsAppTests(TempHome):
    def setUp(self):
        super().setUp()
        os.environ["WHATSAPP_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1234567890"
        save_settings({"whatsapp_to": "+64 21 123 4567", "daily_digest": False})
        write_test_profile()
        set_live(True)
        self.sent = []
        self.patch = patch("gmail_auto.whatsapp._meta_send", lambda to, message: self.sent.append((to, message)) or f"wamid.{len(self.sent)}")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.environ.pop("WHATSAPP_APP_SECRET", None)
        super().tearDown()

    def test_a_trunk_zero_after_the_country_code_is_dropped(self):
        from gmail_auto.settings import normalize_phone
        from gmail_auto.storage import load_doc, save_doc

        save_settings({"whatsapp_to": "+64 021 123 4567"})
        self.assertEqual(load_settings()["whatsapp_to"], "64211234567")
        self.assertTrue(whatsapp.is_owner("whatsapp:+64211234567"))

        # A number saved before this rule still matches the sender WhatsApp reports.
        saved = load_doc("settings", {})
        save_doc("settings", {**saved, "whatsapp_to": "640211234567"})
        self.assertEqual(whatsapp.recipient(), "64211234567")
        self.assertTrue(whatsapp.is_owner("+64 21 123 4567"))

        self.assertEqual(normalize_phone("+44 07700 900123"), "447700900123")
        self.assertEqual(normalize_phone("+39 06 1234 5678"), "390612345678")  # Italy keeps its 0
        self.assertEqual(normalize_phone("+1 650 555 0100"), "16505550100")
        self.assertEqual(normalize_phone("+64 21 123 4567"), "64211234567")

    def ask(self, mail_id="w1", subject="Can we meet Friday?"):
        fake = FakeGmail([sample(id=mail_id, subject=subject)])
        run_once(gmail=fake, generate=lambda *_: Draft(action="ask", reply="Friday at 2pm works.", reason="Commits to a time.", summary="Wants to meet."))
        return fake

    def test_a_reply_that_needs_an_ok_is_shown_on_whatsapp_and_yes_sends_it(self):
        fake = self.ask()
        self.assertEqual(load_settings()["whatsapp_to"], "64211234567")
        self.assertEqual(len(self.sent), 2)
        to, first = self.sent[0]
        self.assertEqual(to, "64211234567")
        self.assertIn("Can we meet Friday?", first["text"]["body"])
        self.assertIn("Friday at 2pm works.", first["text"]["body"])
        self.assertIn("Commits to a time.", first["text"]["body"])
        buttons = self.sent[1][1]["interactive"]["action"]["buttons"]
        self.assertEqual([button["reply"]["id"] for button in buttons], ["approve:1", "reject:1"])
        self.assertEqual(fake.sent, [])

        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="同意"))
        self.assertIn("Sent to Lee", answer)
        self.assertEqual(fake.sent, [("w1", "Friday at 2pm works.", "me@example.com")])
        self.assertEqual(get_processed("w1")["via"], "whatsapp")

    def test_plain_text_rewrites_the_draft_and_no_drops_it(self):
        fake = self.ask()
        with patch("gmail_auto.generator.revise_reply", lambda item, instruction, profile: f"Rewritten: {instruction}"):
            answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="say Monday instead"))
        self.assertIn("Rewritten: say Monday instead", answer)
        self.assertEqual(approvals.pending()[0]["draft"], "Rewritten: say Monday instead")
        self.assertTrue(approvals.pending()[0]["edited"])
        answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text="No"))
        self.assertIn("nothing sent", answer)
        self.assertEqual(fake.sent, [])
        self.assertEqual(approvals.pending(), [])

    def test_with_several_waiting_the_number_or_the_button_picks_one(self):
        self.ask("w1", "First")
        fake = self.ask("w2", "Second")
        self.assertIn("Which one", assistant.handle(whatsapp.Incoming(sender="64211234567", text="yes")))
        self.assertIn("#1", assistant.handle(whatsapp.Incoming(sender="64211234567", text="list")))
        with patch("gmail_auto.mailboxes.client_for", return_value=fake):
            assistant.handle(whatsapp.Incoming(sender="64211234567", text="YES 2"))
            self.assertEqual([entry[0] for entry in fake.sent], ["w2"])
            assistant.handle(whatsapp.Incoming(sender="64211234567", button="reject:1", text="Don't send"))
        self.assertEqual(approvals.pending(), [])
        self.assertEqual(get_processed("w1")["action"], "rejected")

    def test_drop_several_waiting_ids_in_one_message(self):
        self.ask("w1", "First")
        self.ask("w2", "Second")
        self.ask("w3", "Third")
        ids = [item["id"] for item in approvals.open_items()]
        self.assertEqual(len(ids), 3)
        answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text=f"Drop {ids[0]} {ids[1]} {ids[2]}"))
        self.assertIn("nothing sent", answer.lower())
        self.assertEqual(approvals.pending(), [])
        self.assertEqual({get_processed("w1")["action"], get_processed("w2")["action"], get_processed("w3")["action"]}, {"rejected"})

    def test_a_kind_switched_off_on_settings_is_not_pinged(self):
        save_settings({"notify_kinds": ["fine", "school"]})
        fake = FakeGmail(
            [sample(id="inv2", from_email="noreply@xero.com", subject="Invoice 1043 is due", body="Please pay invoice 1043.")]
        )
        run_once(gmail=fake, generate=lambda *_: Draft(action="skip", reason="Notification."))
        self.assertEqual(approvals.open_items(), [])
        self.assertEqual(self.sent, [])

    def test_important_invoice_is_pinged_and_a_numbered_whatsapp_reply_drafts_it(self):
        fake = FakeGmail(
            [sample(id="inv1", from_email="noreply@xero.com", subject="Invoice 1042 is due", body="Please pay invoice 1042.")]
        )
        run_once(gmail=fake, generate=lambda *_: Draft(action="skip", reason="Notification."))
        item = approvals.open_items()[0]
        self.assertEqual(item["role"], "notice")
        self.assertEqual(item["kind"], "invoice")
        self.assertIn("Invoice 1042", self.sent[0][1]["text"]["body"])
        with patch("gmail_auto.generator.revise_reply", lambda item, instruction, profile: f"We will pay on Friday. ({instruction})"):
            answer = assistant.handle(whatsapp.Incoming(sender="64211234567", text=f"{item['id']} we'll pay Friday"))
        self.assertIn("We will pay on Friday", answer)
        self.assertIn("YES to send", answer)
        self.assertEqual(approvals.pending()[0]["draft"], "We will pay on Friday. (we'll pay Friday)")

    def test_pause_stops_sending_and_duplicates_are_ignored(self):
        self.assertIn("Paused", assistant.handle(whatsapp.Incoming(sender="64211234567", text="pause")))
        self.assertFalse(is_live())
        self.assertFalse(assistant.seen_before("wamid.A"))
        self.assertTrue(assistant.seen_before("wamid.A"))

    def test_incoming_messages_are_parsed_and_only_the_owner_is_obeyed(self):
        payload = {
            "entry": [{"changes": [{"value": {"messages": [
                {"from": "64211234567", "id": "wamid.1", "type": "text", "text": {"body": " yes "}, "context": {"id": "wamid.0"}},
                {"from": "64211234567", "id": "wamid.2", "type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "approve:7", "title": "Send"}}},
                {"from": "64211234567", "id": "wamid.3", "type": "image"},
            ]}}]}]
        }
        messages = whatsapp.parse_meta(payload)
        self.assertEqual([(m.text, m.button, m.context_id) for m in messages], [("yes", "", "wamid.0"), ("Send", "approve:7", "")])
        self.assertTrue(whatsapp.is_owner("+64 21 123 4567"))
        self.assertFalse(whatsapp.is_owner("64219999999"))
        voice = whatsapp.parse_twilio(
            {"From": "whatsapp:+64211234567", "Body": "", "MessageSid": "SM2", "NumMedia": "1", "MediaContentType0": "audio/ogg", "MediaUrl0": "https://api.twilio.com/media/1"}
        )
        self.assertEqual(voice[0].audio_url, "https://api.twilio.com/media/1")
        self.assertTrue(whatsapp.has_voice(voice[0]))
        twilio = whatsapp.parse_twilio({"From": "whatsapp:+64211234567", "Body": "ok", "MessageSid": "SM1"})
        self.assertEqual((twilio[0].sender, twilio[0].text), ("+64211234567", "ok"))

    def test_webhook_signature_must_match_the_app_secret(self):
        raw = b'{"entry": []}'
        self.assertFalse(whatsapp.meta_signature_ok(raw, "sha256=anything"))
        os.environ["WHATSAPP_APP_SECRET"] = "app-secret-for-tests"
        good = "sha256=" + hmac.new(b"app-secret-for-tests", raw, hashlib.sha256).hexdigest()
        self.assertTrue(whatsapp.meta_signature_ok(raw, good))
        self.assertFalse(whatsapp.meta_signature_ok(raw + b" ", good))

    def test_morning_summary_goes_out_once_a_day(self):
        from datetime import datetime

        save_settings({"daily_digest": True})
        self.sent.clear()
        early = datetime.now().replace(hour=6, minute=0)
        late = datetime.now().replace(hour=9, minute=0)
        self.assertFalse(send_daily_digest(early))
        self.assertTrue(send_daily_digest(late))
        self.assertFalse(send_daily_digest(late))
        self.assertEqual(len(self.sent), 1)
        body = self.sent[0][1]["text"]["body"]
        self.assertIn("Good morning.", body)
        self.assertNotIn("new emails checked", body)

    def test_morning_summary_caps_at_100_unread_and_lists_important_only(self):
        from datetime import datetime
        from gmail_auto.models import Mail

        save_settings({"daily_digest": True})
        self.sent.clear()
        letters = []
        for idx in range(130):
            # 120 newest are automated notifications; 10 oldest (beyond top 100) are real bills
            # so only the single important letter placed inside the newest 100 should appear.
            is_old_bill = idx < 10
            letters.append(
                Mail(
                    id=f"m{idx}",
                    thread_id=f"t{idx}",
                    from_name="Billing" if is_old_bill else "Newsletter",
                    from_email="billing@vendor.co.nz" if is_old_bill else "noreply@news.example.com",
                    reply_email="",
                    subject="Invoice due" if is_old_bill else f"Daily digest {idx}",
                    date="2026-11-14",
                    body="Please pay invoice #99" if is_old_bill else "Unsubscribe anytime",
                    snippet="Please pay invoice #99" if is_old_bill else "Unsubscribe anytime",
                    received_ts=1000.0 + idx,
                )
            )
        letters.append(
            Mail(
                id="imp1",
                thread_id="timp1",
                from_name="Sarah Chen",
                from_email="sarah@client.co.nz",
                reply_email="",
                subject="Quote for kitchen renovation",
                date="2026-11-15",
                body="Could you send a quote for next Tuesday?",
                snippet="Could you send a quote for next Tuesday?",
                received_ts=5000.0,
            )
        )

        class _Client:
            def list_unread(self, limit=20):
                ordered = sorted(letters, key=lambda m: m.received_ts, reverse=True)
                return ordered[:limit]

        late = datetime.now().replace(hour=9, minute=0)
        self.assertTrue(send_daily_digest(late, clients=[_Client()]))
        body = self.sent[0][1]["text"]["body"]
        self.assertIn("Sarah Chen:", body)
        self.assertIn("Quote for kitchen renovation", body)
        self.assertNotIn("Daily digest", body)
        self.assertNotIn("Invoice due", body)
        self.assertNotIn("new emails checked", body)

    def test_whatsapp_search_finds_handled_letters_that_left_the_recent_inbox(self):
        from gmail_auto import store

        store.mark_processed(
            {
                "id": "g7",
                "mail_id": "g7",
                "provider": "gmail",
                "thread_id": "tg7",
                "from_name": "Georgie Hutchison",
                "from_email": "g.hutchison@example.co.nz",
                "subject": "Your enquiry for 1/7 Hastings Road, Mairangi Bay",
                "date": "2026-11-14",
                "action": "skipped",
                "summary": "No viewing times are available for the property.",
                "processed_at": "2026-11-14 06:01:00",
                "sensitive": False,
            }
        )

        class _Client:
            provider = "gmail"

            def list_unread(self, limit=20):
                return []

            def list_recent(self, limit=25, full=False):
                return []

        for text in ('搜索 "1/7', "find Georgie Hutchison"):
            reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text=text), clients=[_Client()])
            self.assertIn("1. Georgie Hutchison", reply)
            self.assertIn("1/7 Hastings Road", reply)

    def test_voice_goes_through_gateway_chat_when_only_a_gateway_key_is_set(self):
        from types import SimpleNamespace

        from gmail_auto import llm

        calls = []

        class _Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='"find 1/7"'))])

        class _OpenAI:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.chat = SimpleNamespace(completions=_Completions())

        saved = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "AI_GATEWAY_API_KEY", "OPENAI_BASE_URL")}
        try:
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ["OPENAI_BASE_URL"] = ""
            os.environ["AI_GATEWAY_API_KEY"] = "gw-test-key"
            with patch("gmail_auto.envfile.load_env"), patch("openai.OpenAI", _OpenAI):
                heard = llm.transcribe(b"OggS", "voice.ogg", "audio/ogg")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(heard, "find 1/7")
        self.assertEqual(calls[0]["base_url"], llm.VERCEL_GATEWAY_URL)
        part = calls[1]["messages"][0]["content"][1]
        self.assertEqual(part["type"], "file")
        self.assertTrue(part["file"]["file_data"].startswith("data:audio/ogg;base64,"))

    def test_number_after_a_search_opens_that_letter_not_an_older_approval(self):
        from gmail_auto import store

        notice = approvals.create(
            sample(id="old1", from_email="rates@council.govt.nz", subject="Rates notice", body="Rates due."),
            Draft(action="skip", reply="", reason="FYI", summary="A notice."),
            "en",
            role="notice",
            kind="invoice",
        )
        self.assertEqual(notice["id"], "1")
        store.mark_processed(
            {
                "id": "g7",
                "mail_id": "g7",
                "provider": "gmail",
                "from_name": "Georgie Hutchison",
                "from_email": "g.hutchison@example.co.nz",
                "subject": "More viewing times available - Register to view 1/7 Hastings Road",
                "date": "2026-11-15",
                "summary": "New viewing times are open.",
                "processed_at": "2026-11-15 09:00:00",
                "sensitive": False,
            }
        )

        class _Client:
            provider = "gmail"

            def list_unread(self, limit=20):
                return []

            def list_recent(self, limit=25, full=False):
                return []

        client = _Client()
        assistant.handle(whatsapp.Incoming(sender="64210000001", text="find Georgie Hutchison"), clients=[client])
        reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="Tell me #1 details"), clients=[client])
        self.assertNotIn("no draft yet", reply)
        self.assertIn("Subject: More viewing times available", reply)

        listed = assistant.handle(whatsapp.Incoming(sender="64210000001", text="list"), clients=[client])
        self.assertIn("Rates notice", listed)
        again = assistant.handle(whatsapp.Incoming(sender="64210000001", text="Tell me #1 details"), clients=[client])
        self.assertNotIn("More viewing times", again)

        for text in ("More details about #1", "1"):
            shown = assistant.handle(whatsapp.Incoming(sender="64210000001", text=text), clients=[client])
            self.assertNotIn("no draft yet", shown)
            self.assertIn("Subject: Rates notice", shown)
            self.assertIn("Rates due.", shown)
            self.assertIn("To reply, send: 1", shown)

    def test_whatsapp_can_summarize_unread_search_reply_and_archive_by_number(self):
        from gmail_auto.models import Mail

        unread_mail = Mail(
            id="u1",
            thread_id="tu1",
            from_name="Alice Wong",
            from_email="alice@client.co.nz",
            reply_email="",
            subject="Invoice #42 question",
            date="2026-11-15",
            body="Can you send the updated invoice?",
            snippet="Can you send the updated invoice?",
            received_ts=2000.0,
        )
        recent_mail = Mail(
            id="r1",
            thread_id="tr1",
            from_name="Auckland Council",
            from_email="notices@aucklandcouncil.govt.nz",
            reply_email="",
            subject="Rates reminder",
            date="2026-11-14",
            body="Rates installment due next week.",
            snippet="Rates installment due next week.",
            received_ts=1500.0,
        )
        archived_ids = []

        class _Client:
            provider = "gmail"

            def list_unread(self, limit=20):
                return [unread_mail][:limit]

            def list_recent(self, limit=25, full=False):
                return [unread_mail, recent_mail][:limit]

            def get_message(self, message_id):
                return unread_mail if message_id == "u1" else recent_mail

            def archive(self, message_id):
                archived_ids.append(message_id)

        client = _Client()
        unread_reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="总结未读"), clients=[client])
        self.assertIn("1. Alice Wong", unread_reply)
        self.assertIn("Invoice #42 question", unread_reply)

        search_reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="搜索 Rates"), clients=[client])
        self.assertIn("1. Auckland Council", search_reply)
        self.assertIn("Rates reminder", search_reply)

        with patch("gmail_auto.approvals.revise", side_effect=ConfigError("The OpenAI key was rejected.")):
            failed_reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="回复 1 收到"), clients=[client])
        self.assertIn("could not draft a reply", failed_reply)
        self.assertEqual(approvals.open_items(), [])

        with patch("gmail_auto.approvals.revise", side_effect=lambda item_id, _inst, _prof: {"id": item_id, "draft": "We will handle this tomorrow."}):
            draft_reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="回复 1 收到，明天处理"), clients=[client])
        self.assertIn("Drafted reply #", draft_reply)
        self.assertIn("We will handle this tomorrow.", draft_reply)

        not_delete = assistant.handle(whatsapp.Incoming(sender="64210000001", text="删除 1"), clients=[client])
        self.assertNotIn("Archived #1", not_delete)
        self.assertEqual(archived_ids, [])

        archive_reply = assistant.handle(whatsapp.Incoming(sender="64210000001", text="归档 1"), clients=[client])
        self.assertIn("Archived #1", archive_reply)
        self.assertEqual(archived_ids, ["r1"])


class OutlookAndViewTests(TempHome):
    def test_outlook_message_becomes_the_same_kind_of_letter(self):
        mail = parse_outlook(
            {
                "id": "AAMk=",
                "conversationId": "conv1",
                "subject": "Invoice question",
                "from": {"emailAddress": {"name": "Mina", "address": "mina@example.com"}},
                "replyTo": [],
                "receivedDateTime": "2026-11-11T09:01:00Z",
                "body": {"contentType": "html", "content": "<html><body><p>Hello <b>there</b></p></body></html>"},
                "bodyPreview": "Hello there",
                "isRead": False,
                "internetMessageId": "<abc@example.com>",
                "internetMessageHeaders": [{"name": "List-Id", "value": "<news.example.com>"}],
                "categories": ["autogmail-done"],
            }
        )
        self.assertEqual(mail.key, "outlook:AAMk=")
        self.assertEqual((mail.from_name, mail.from_email, mail.thread_id), ("Mina", "mina@example.com", "conv1"))
        self.assertEqual(mail.body, "Hello there")
        self.assertIn("<b>there</b>", mail.html)
        self.assertEqual(mail.headers["message-id"], "<abc@example.com>")
        self.assertEqual(mail.headers["list-id"], "<news.example.com>")
        self.assertEqual(mail.label_ids, ["INBOX", "UNREAD", "DONE"])
        self.assertEqual(mail.received_ts, 1794387660.0)

    def test_outlook_lists_headers_only_then_loads_bodies_in_a_batch(self):
        from gmail_auto.outlook_client import OutlookClient

        calls = []

        def fake_graph(kind, method, path, params=None, body=None, headers=None, raw=False, timeout=40, attempts=None):
            calls.append({"method": method, "path": path, "params": params or {}, "body": body, "timeout": timeout, "attempts": attempts})
            if path == "/me":
                return {"mail": "robin@example.com"}
            if method == "GET" and path.endswith("/messages"):
                return {
                    "value": [
                        {
                            "id": "AAMk",
                            "conversationId": "conv1",
                            "subject": "Hello",
                            "from": {"emailAddress": {"name": "Mina", "address": "mina@example.com"}},
                            "receivedDateTime": "2026-11-11T09:01:00Z",
                            "bodyPreview": "Hello there",
                            "isRead": False,
                            "categories": [],
                        }
                    ]
                }
            if method == "POST" and path.endswith("$batch"):
                return {
                    "responses": [
                        {
                            "id": "0",
                            "status": 200,
                            "body": {
                                "id": "AAMk",
                                "conversationId": "conv1",
                                "subject": "Hello",
                                "from": {"emailAddress": {"name": "Mina", "address": "mina@example.com"}},
                                "receivedDateTime": "2026-11-11T09:01:00Z",
                                "body": {"contentType": "html", "content": "<p>Hello there</p>"},
                                "bodyPreview": "Hello there",
                                "isRead": False,
                                "internetMessageHeaders": [{"name": "List-Id", "value": "<news.example.com>"}],
                                "categories": [],
                            },
                        }
                    ]
                }
            return {}

        with patch("gmail_auto.outlook_client.microsoft.access_token", return_value="tok"):
            with patch("gmail_auto.outlook_client.microsoft.graph", side_effect=fake_graph):
                client = OutlookClient()
                recent = client.list_recent(10)
                unread = client.list_unread(10)

        listed = [item for item in calls if item["method"] == "GET" and item["path"].endswith("/messages")]
        self.assertTrue(listed)
        self.assertNotIn("body", listed[0]["params"]["$select"].split(","))
        self.assertNotIn("$filter", listed[0]["params"])
        self.assertEqual(listed[0]["timeout"], 8)
        unread_list = next(item for item in listed if "isRead" in item["params"].get("$filter", ""))
        self.assertIn("receivedDateTime ge", unread_list["params"]["$filter"])
        self.assertEqual(recent[0].snippet, "Hello there")
        self.assertEqual(recent[0].html, "")
        self.assertIn("<p>Hello there</p>", unread[0].html)
        self.assertEqual(unread[0].headers["list-id"], "<news.example.com>")
        self.assertTrue(any(item["path"].endswith("$batch") for item in calls))

    def test_letter_html_is_shown_with_formatting_but_cannot_run_anything(self):
        dirty = (
            '<html><head><style>.a{color:red}</style><script>steal()</script></head>'
            '<body onload="steal()"><h1 class="a">Sale</h1><a href="javascript:steal()" onclick="steal()">Go</a>'
            '<img src="https://example.com/a.png" onerror="steal()"><iframe src="https://evil.example"></iframe>'
            '<form action="https://evil.example"><input name="q"></form></body></html>'
        )
        page = document(dirty)
        self.assertIn('<h1 class="a">Sale</h1>', page)
        self.assertIn(".a{color:red}", page)
        self.assertIn('<img src="https://example.com/a.png">', page)
        for banned in ("steal()", "<script", "<iframe", "<form", "<input", "javascript:", "onclick", "onerror"):
            self.assertNotIn(banned, page)
        plain = document("", "Line one\n<b>not bold</b> https://example.com/x")
        self.assertIn("&lt;b&gt;not bold&lt;/b&gt;", plain)
        self.assertIn('<a href="https://example.com/x"', plain)
        self.assertEqual(clean_html("<p onmouseover=x()>hi</p>"), "<p>hi</p>")

    def test_sharepoint_link_is_encoded_the_way_graph_expects(self):
        self.assertEqual(share_token("https://contoso.sharepoint.com/sites/Team/Shared Documents/Plan.docx"),
                         "u!aHR0cHM6Ly9jb250b3NvLnNoYXJlcG9pbnQuY29tL3NpdGVzL1RlYW0vU2hhcmVkIERvY3VtZW50cy9QbGFuLmRvY3g")


class MicrosoftErrorTests(TempHome):
    def test_microsoft_errors_are_explained(self):
        from gmail_auto.microsoft import explain_error

        self.assertIn("Value column, not its Secret ID", explain_error("invalid_client", "AADSTS7000215: Invalid client secret provided."))
        self.assertIn("MS_TENANT_ID", explain_error("invalid_request", "AADSTS50194: Application is not configured as a multi-tenant application."))
        self.assertIn("Grant admin consent", explain_error("access_denied", ""))
        message = explain_error("server_error", "AADSTS12345: Something odd.\r\nTrace ID: x")
        self.assertEqual(message, "Microsoft said: Something odd. (AADSTS12345)")

    def test_graph_gateway_errors_are_timeouts(self):
        from gmail_auto import microsoft

        class Reply:
            status_code = 503

        error = microsoft._translate("outlook", Reply())
        self.assertIn("timed out", str(error))
        self.assertEqual(error.status, 503)

    def test_graph_retries_a_503_then_reads_the_mailbox(self):
        from gmail_auto import microsoft

        class Reply:
            def __init__(self, status, payload=None):
                self.status_code = status
                self.content = b'{"value":[]}' if payload is None else payload

            def json(self):
                return {"value": []}

        n = {"calls": 0}

        def fake_request(*args, **kwargs):
            n["calls"] += 1
            if n["calls"] < 3:
                return Reply(503)
            return Reply(200)

        with patch("gmail_auto.microsoft.access_token", return_value="tok"):
            with patch("requests.request", fake_request):
                with patch("gmail_auto.microsoft.time.sleep"):
                    data = microsoft.graph("outlook", "GET", "/me/messages")
        self.assertEqual(n["calls"], 3)
        self.assertEqual(data, {"value": []})

    def test_the_sign_in_directory_is_named(self):
        import os

        from gmail_auto import microsoft

        previous = os.environ.pop("MS_TENANT_ID", None)
        try:
            self.assertIn("MS_TENANT_ID is not set", microsoft.tenant_text())
            os.environ["MS_TENANT_ID"] = ' "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab" '
            self.assertEqual(microsoft.tenant(), "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab")
            self.assertIn("your organisation only", microsoft.tenant_text())
            self.assertIn("/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab/", microsoft.authorize_url("outlook", "https://x.example/cb", "s"))
        finally:
            os.environ.pop("MS_TENANT_ID", None)
            if previous is not None:
                os.environ["MS_TENANT_ID"] = previous


class StorageVariableTests(TempHome):
    NAMES = (
        "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
        "AUTOMAIL_KV_REST_API_URL", "AUTOMAIL_KV_REST_API_TOKEN", "AUTOMAIL_KV_REST_API_READ_ONLY_TOKEN",
        "AUTOMAIL_KV_URL", "AUTOMAIL_REDIS_URL", "REDIS_URL", "KV_URL",
    )

    def setUp(self):
        super().setUp()
        self.kept = {name: os.environ.pop(name, None) for name in self.NAMES}

    def tearDown(self):
        for name in self.NAMES:
            os.environ.pop(name, None)
            if self.kept[name] is not None:
                os.environ[name] = self.kept[name]
        super().tearDown()

    def test_prefixed_vercel_variables_are_found(self):
        from gmail_auto import token_store

        self.assertFalse(token_store.storage_ready())
        self.assertIn("No storage variables", token_store.storage_hint())
        os.environ["AUTOMAIL_KV_REST_API_READ_ONLY_TOKEN"] = "read-only"
        os.environ["AUTOMAIL_KV_REST_API_URL"] = "https://example-12345.upstash.io"
        self.assertFalse(token_store.storage_ready())
        self.assertIn("AUTOMAIL_KV_REST_API_URL", token_store.storage_hint())
        os.environ["AUTOMAIL_KV_REST_API_TOKEN"] = "write-token"
        self.assertEqual(token_store._redis(), ("https://example-12345.upstash.io", "write-token"))
        self.assertTrue(token_store.storage_ready())

    def test_a_plain_redis_address_is_accepted(self):
        from gmail_auto import token_store

        os.environ["AUTOMAIL_REDIS_URL"] = "rediss://default:secret@example.upstash.io:6379"
        self.assertTrue(token_store.storage_ready())
        self.assertEqual(token_store.storage_kind(), "Redis")
        os.environ["AUTOMAIL_REDIS_URL"] = "not-an-address"
        self.assertFalse(token_store.storage_ready())


class RealtimeScanTests(TempHome):
    def setUp(self):
        super().setUp()
        from gmail_auto.profile_store import save_profile

        save_profile(
            {
                "name": "Robin",
                "role": "Owner",
                "introduction": "Consultant",
                "tone": "Brief",
                "cannot_promise": ["Discounts"],
                "sample_phrases": ["Happy to help."],
            }
        )

    def test_scan_since_ts_skips_historical_mail_and_advances_watermark(self):
        import time
        from gmail_auto.runner import run_once
        from gmail_auto.store import get_state, set_state

        now = int(time.time())
        set_state("scan_since_ts", now - 300)
        old_mail = sample(id="old-1", subject="Old backlog", received_ts=now - 3600)
        new_mail = sample(id="new-1", subject="Fresh email", received_ts=now - 30)

        seen_since = []

        class _FakeClient(FakeGmail):
            def list_unread(self, limit=20, since_ts=None):
                seen_since.append(since_ts)
                return [old_mail, new_mail]

        fake = _FakeClient([old_mail, new_mail])
        report = run_once(gmail=fake, generate=lambda *_: Draft(action="skip", reason="ok"))
        self.assertEqual(seen_since, [now - 300])
        self.assertEqual(report.checked, 1)
        self.assertGreaterEqual(int(get_state("scan_since_ts", 0)), now - 60)

    def test_realtime_important_notice_uses_llm_summary_and_knowledge(self):
        import time
        from gmail_auto import whatsapp
        from gmail_auto.knowledge import add_document
        from gmail_auto.runner import run_once
        from gmail_auto.settings import save_settings
        from gmail_auto.store import set_state

        now = int(time.time())
        set_state("scan_since_ts", now - 300)
        save_settings({"notify_important": True, "whatsapp_to": "+64 21 123 4567"})
        add_document("Pricing", "Standard consultation is $200/hr.", "note", summarize=False)

        mail = sample(
            id="notice-1",
            subject="Consultation invoice due Friday",
            from_email="billing@client.co.nz",
            body="Please confirm your $200/hr consultation invoice before Friday.",
            received_ts=now - 10,
        )
        sent_notices = []
        os.environ["OPENAI_API_KEY"] = "sk-test"
        try:
            with patch("gmail_auto.llm.complete", return_value="Client asks to confirm $200/hr consultation invoice by Friday.") as mocked_llm, \
                 patch.object(whatsapp, "ready", return_value=True), \
                 patch.object(whatsapp, "send_notice", side_effect=lambda item: sent_notices.append(item) or "wa-1"):
                run_once(gmail=FakeGmail([mail]), generate=lambda *_: Draft(action="skip", reason="no reply needed"))
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

        self.assertEqual(len(sent_notices), 1)
        self.assertIn("Client asks to confirm $200/hr consultation invoice by Friday.", sent_notices[0]["summary"])
        mocked_llm.assert_called_once()
        self.assertIn("$200/hr", mocked_llm.call_args[0][1])


class ConversationalAgentTests(TempHome):
    def setUp(self):
        super().setUp()
        from gmail_auto.knowledge import add_document
        from gmail_auto.profile_store import save_profile

        save_profile(
            {
                "name": "Robin",
                "role": "Founder",
                "introduction": "Runs consulting practice.",
                "tone": "Warm and concise",
                "cannot_promise": ["Free work"],
                "sample_phrases": ["Happy to help."],
            }
        )
        add_document("Rates", "Standard rate is $200/hr.", "note", summarize=False)

    def test_kb_injected_and_compose_requires_confirmation_before_sending(self):
        from gmail_auto import assistant, whatsapp

        sent_emails = []

        class _Client(FakeGmail):
            def send_notice(self, to, subject, body):
                sent_emails.append((to, subject, body))

        client = _Client([])
        compose_json = (
            '{"intent":"compose","to":"lee@example.com","subject":"Consulting rate",'
            '"body":"Hi Lee, our standard rate is $200/hr.","number":"","query":"","instruction":"","reply":""}'
        )
        with patch("gmail_auto.assistant.complete", return_value=compose_json) as mocked_llm:
            preview = assistant.handle(whatsapp.Incoming(sender="64211234567", text="帮我给 lee@example.com 发封邮件说明报价"), clients=[client])

        self.assertIn("$200/hr", mocked_llm.call_args[0][1])
        self.assertIn("Ready to send:", preview)
        self.assertIn("lee@example.com", preview)
        self.assertEqual(sent_emails, [])

        confirmed = assistant.handle(whatsapp.Incoming(sender="64211234567", text="YES"), clients=[client])
        self.assertIn("Sent email to lee@example.com", confirmed)
        self.assertEqual(sent_emails, [("lee@example.com", "Consulting rate", "Hi Lee, our standard rate is $200/hr.")])

    def test_natural_language_read_and_archive_with_confirmation(self):
        from gmail_auto import assistant, whatsapp

        mail = sample(id="m9", from_name="Alice", from_email="alice@example.com", subject="Contract update", body="Please review v2.")
        client = FakeGmail([mail])
        assistant.handle(whatsapp.Incoming(sender="64211234567", text="UNREAD"), clients=[client])

        read_json = '{"intent":"read","number":"1","query":"","to":"","subject":"","body":"","instruction":"","reply":""}'
        with patch("gmail_auto.assistant.complete", return_value=read_json):
            details = assistant.handle(whatsapp.Incoming(sender="64211234567", text="第一封邮件写了什么？"), clients=[client])
        self.assertIn("Contract update", details)
        self.assertIn("Please review v2.", details)

        archive_json = '{"intent":"archive","number":"1","query":"","to":"","subject":"","body":"","instruction":"","reply":""}'
        with patch("gmail_auto.assistant.complete", return_value=archive_json):
            ask = assistant.handle(whatsapp.Incoming(sender="64211234567", text="把第一封删掉"), clients=[client])
        self.assertIn("Reply YES to confirm", ask)
        self.assertEqual(client.archived, [])

        done = assistant.handle(whatsapp.Incoming(sender="64211234567", text="确认"), clients=[client])
        self.assertIn("Archived:", done)
        self.assertEqual(client.archived, ["m9"])

    def test_analyze_and_invite_intents_from_natural_language(self):
        from gmail_auto import assistant, whatsapp
        from gmail_auto.store import get_state

        mail = sample(
            id="spam1",
            from_name="Carhireexcess.com",
            from_email="info@carhireexcess.com",
            subject="特邀开户領取188彩金",
            body="企鹅Q：914797848 領取！",
        )
        client = FakeGmail([mail])
        assistant.handle(whatsapp.Incoming(sender="64211234567", text="UNREAD"), clients=[client])
        analyze_json = (
            '{"intent":"analyze","number":"1","numbers":["1"],"query":"","to":"","subject":"",'
            '"body":"","instruction":"","reply":"This is a phishing gambling ad. Ignore it."}'
        )
        with patch("gmail_auto.assistant.complete", return_value=analyze_json):
            result = assistant.handle(whatsapp.Incoming(sender="64211234567", text="这封是不是广告"), clients=[client])
        self.assertIn("phishing", result.lower())
        self.assertIn("特邀开户", result)

        invite_json = (
            '{"intent":"invite","title":"Call with Lee","start":"2026-11-17T15:00:00",'
            '"end":"2026-11-17T16:00:00","attendees":["lee@example.com"],'
            '"number":"","query":"","to":"lee@example.com","subject":"","body":"","instruction":"","reply":""}'
        )
        with patch("gmail_auto.assistant.complete", return_value=invite_json):
            preview = assistant.handle(
                whatsapp.Incoming(sender="64211234567", text="明天下午3点和 lee@example.com 开会"),
                clients=[client],
            )
        self.assertIn("Add to calendar", preview)
        self.assertIn("lee@example.com", preview)
        pending = get_state("wa_pending_action")
        self.assertEqual(pending["kind"], "create_event")
        self.assertIn("15:00", preview)
        self.assertIn("16:00", preview)

    def test_yes_after_calendar_preview_does_not_approve_waiting_notices(self):
        from gmail_auto.store import get_state, set_state

        for n in range(3):
            approvals.create(
                sample(id=f"n{n}", from_email="billing@x.com", subject=f"Notice {n}", body="Please see attached."),
                Draft(action="skip", reply="", reason="FYI", summary="A notice."),
                "en",
                role="notice",
                kind="invoice",
            )
        ids = [item["id"] for item in approvals.open_items()]
        self.assertGreaterEqual(len(ids), 3)
        set_state(
            "wa_pending_action",
            {
                "kind": "create_event",
                "provider": "gmail",
                "draft": {
                    "title": "Meeting",
                    "start": "2026-11-17T14:00:00",
                    "end": "2026-11-17T15:00:00",
                    "location": "",
                    "attendees": [],
                    "description": "",
                    "kind": "meeting",
                },
            },
        )
        stuffed = (
            '{"intent":"approve","numbers":'
            + str(ids).replace("'", '"')
            + ',"number":"","query":"","to":"","subject":"","body":"","instruction":"","reply":""}'
        )
        created = []

        def fake_create(provider, draft):
            created.append((provider, draft))
            return {"title": draft["title"], "when": "Tue 17 Nov 2026 14:00"}

        with patch("gmail_auto.assistant.complete", return_value=stuffed), patch(
            "gmail_auto.calendars.create_event", side_effect=fake_create
        ):
            result = assistant.handle(whatsapp.Incoming(sender="64211234567", text="Yes"), clients=[FakeGmail([])])
        self.assertIn("Added to calendar", result)
        self.assertNotIn("has no draft yet", result)
        self.assertEqual(len(created), 1)
        self.assertIsNone(get_state("wa_pending_action"))
        self.assertEqual(len(approvals.open_items()), len(ids))

        set_state(
            "wa_pending_action",
            {
                "kind": "create_event",
                "provider": "gmail",
                "draft": {
                    "title": "Meeting",
                    "start": "2026-11-17T14:00:00",
                    "end": "2026-11-17T15:00:00",
                    "location": "",
                    "attendees": [],
                    "description": "",
                    "kind": "meeting",
                },
            },
        )
        with patch("gmail_auto.calendars.create_event", side_effect=fake_create):
            again = assistant.handle(whatsapp.Incoming(sender="64211234567", text="好的加上"), clients=[FakeGmail([])])
        self.assertIn("Added to calendar", again)
        self.assertNotIn("has no draft yet", again)
