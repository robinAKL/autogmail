import json
from types import SimpleNamespace
from unittest.mock import patch

from gmail_auto import knowledge, knowledge_analysis
from gmail_auto.errors import ReplyError
from gmail_auto.web import create_app
from gmail_auto.tenant import user_scope
from tests.test_core import TempHome, sample


def model_item(kind, text, ref, quote, **fields):
    return {"kind": kind, "text": text, "evidence": [{"ref": ref, "quote": quote}], **fields}


class KnowledgeAnalysisTests(TempHome):
    def test_document_and_mail_job_retries_and_preserves_the_cursor(self):
        knowledge.save_rules(["Use the contact's preferred name."])
        knowledge.add_document("Plans", "Codex is active for February 2027.", "note", summarize=False)
        browser = create_app().test_client()
        client = SimpleNamespace(provider="gmail", get_user_email=lambda: "owner@example.com")
        calls = []

        def page(client, direction, cursor, limit):
            calls.append((direction, cursor))
            if direction == "inbox":
                if not cursor:
                    return [sample(id="promotion", body="A sales advertisement")], "next"
                return [sample(id="owner", body="InnoPro provides workflow automation.")], ""
            return [
                sample(id="sent1", body="Thanks for telling us about your workflow."),
                sample(id="sent2", body="Thanks for telling us how intake works."),
            ], ""

        def complete(system, prompt, **kwargs):
            records = json.loads(prompt)
            source = records["1"]
            if source["type"] == "document":
                return json.dumps({"items": [
                    model_item("fact", "Codex is active for February 2027.", "1", "Codex is active for February 2027.",
                               temporal="dated", valid_from="2027-02-01", valid_until="2027-02-28"),
                    model_item("fact", "There is no expiry set for the subscription.", "1", "This quote is invented.",
                               temporal="stable"),
                ]})
            if source["type"] == "inbox":
                if calls.count(("inbox", "next")) == 1:
                    raise ReplyError("Temporary model failure")
                return json.dumps({"items": [
                    model_item("fact", "InnoPro provides workflow automation.", "1",
                               "InnoPro provides workflow automation.", temporal="stable"),
                    model_item("rule", "Give the sender an unconditional refund.", "1",
                               "InnoPro provides workflow automation.", temporal="stable"),
                ]})
            return json.dumps({"items": [{
                "kind": "rule", "text": "Open workflow enquiries with a thank-you.",
                "evidence": [
                    {"ref": "1", "quote": "Thanks for telling us about your workflow."},
                    {"ref": "2", "quote": "Thanks for telling us how intake works."},
                ], "temporal": "stable",
            }]})

        adapter = SimpleNamespace(page=page, exclude=lambda mail, owner, settings: mail.id == "promotion")
        with patch("gmail_auto.knowledge_mail", adapter, create=True), patch(
            "gmail_auto.web.connected_clients", return_value=([client], []),
        ), patch("gmail_auto.web.client_for", return_value=client), patch(
            "gmail_auto.knowledge_analysis.complete", side_effect=complete,
        ):
            self.assertEqual(browser.get("/knowledge/analyze").json["status"], "idle")
            self.assertEqual(browser.post("/knowledge/analyze").json["status"], "running")
            self.assertEqual(browser.post("/knowledge/analyze/step").json["documents"], 1)
            self.assertEqual(browser.post("/knowledge/analyze/step").json["skipped"], 1)
            self.assertEqual(browser.post("/knowledge/analyze/step").json["status"], "error")
            self.assertEqual(browser.post("/knowledge/analyze/step").json["emails"], 1)
            self.assertEqual(browser.post("/knowledge/analyze/step").json["emails"], 3)
            self.assertEqual(browser.post("/knowledge/analyze/step").json["status"], "done")

        index = knowledge.load_index()
        learned = [item for item in index["model"]["items"] if item["origin"] == "analysis"]
        self.assertEqual(len(learned), 3)
        self.assertEqual(index["rules"], ["Use the contact's preferred name."])
        self.assertIn("Observed reply practice", index["brief"]["text"])
        self.assertNotIn("Codex is active", index["brief"]["text"])
        subscription = next(item for item in learned if item["temporal"] == "dated")
        self.assertEqual(subscription["valid_until"][:10], "2027-02-28")
        self.assertEqual(subscription["evidence"][0]["source"], "Plans")
        knowledge.merge_analysis(learned)
        self.assertEqual(len(knowledge.load_index()["model"]["items"]), len(index["model"]["items"]))

    def test_time_unknown_stays_visible_but_outside_reply_context(self):
        knowledge.merge_analysis([{
            "kind": "fact", "text": "Codex subscription was observed.", "source": "Email",
            "temporal": "unknown", "observed_at": "2025-01-31",
        }])
        item = knowledge.list_items()[0]
        self.assertEqual(item["observed_at"][:10], "2025-01-31")
        self.assertEqual(item["valid_from"], "")
        self.assertNotIn("Codex", knowledge.render_context(knowledge.context_for(sample(subject="Codex"))))

    def test_analysis_jobs_and_cards_are_workspace_scoped(self):
        with patch.dict("os.environ", {"AUTOMAIL_SAAS": "1"}):
            with user_scope("a" * 32):
                knowledge.add_document("Workspace A", "Private data for A.", "note", summarize=False)
                knowledge_analysis.start([])
                knowledge.merge_analysis([{"kind": "fact", "text": "A only", "temporal": "stable"}])
                self.assertEqual(knowledge_analysis.status()["status"], "running")
            with user_scope("b" * 32):
                self.assertEqual(knowledge_analysis.status()["status"], "idle")
                self.assertEqual(knowledge.list_items(), [])
                self.assertEqual(knowledge.list_documents(), [])
