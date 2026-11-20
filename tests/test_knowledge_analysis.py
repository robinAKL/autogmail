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
            if "Fill the editable profile" in system:
                return json.dumps({
                    "name": "", "role": "", "introduction": "", "tone": "",
                    "cannot_promise": [], "sample_phrases": [],
                    "rules": [], "auto_reply_allow": [], "auto_reply_block": [],
                })
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
            received = browser.post("/knowledge/analyze/step").json
            self.assertEqual(received["emails"], 1)
            self.assertEqual(received["received"], 1)
            sent = browser.post("/knowledge/analyze/step").json
            self.assertEqual(sent["emails"], 3)
            self.assertEqual(sent["sent"], 2)
            self.assertEqual(browser.post("/knowledge/analyze/step").json["status"], "done")

        index = knowledge.load_index()
        learned = [item for item in index["model"]["items"] if item["origin"] == "analysis"]
        self.assertEqual([item["kind"] for item in learned].count("fact"), 2)
        self.assertEqual([item["kind"] for item in learned].count("storyline"), 1)
        self.assertEqual(index["rules"][0], "Use the contact's preferred name.")
        self.assertIn("Open workflow enquiries with a thank-you.", index["rules"])
        self.assertIn("Open workflow enquiries with a thank-you.", index["brief"]["text"])
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

    def test_invalid_model_output_retries_the_same_batch_without_losing_evidence(self):
        knowledge.add_document("Company", "We run three weekly training sessions.", "note", summarize=False)
        good = json.dumps({"items": [
            model_item("fact", "We run three weekly training sessions.", "1",
                       "We run three weekly training sessions.", temporal="stable"),
        ]})
        with patch("gmail_auto.knowledge_analysis.complete", side_effect=['{"items":[', good]) as model:
            knowledge_analysis.start([])
            result = knowledge_analysis.step(None)
        self.assertEqual(result["documents"], 1)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args_list[0].args[1], model.call_args_list[1].args[1])
        self.assertEqual(model.call_args_list[0].kwargs["response_format"]["type"], "json_schema")
        self.assertIn("compact JSON object", model.call_args_list[1].args[0])
        self.assertEqual(knowledge.list_items()[0]["source"], "Company")

    def test_same_source_facts_collapse_and_observation_only_facts_are_dropped(self):
        text = "The monthly price is $129 and the yearly price is $1299."
        refs = {"1": {
            "source_id": "deck", "source": "Sales deck", "type": "document",
            "observed_at": "2026-01-01", "text": text,
        }}
        unknown = knowledge_analysis._supported_item(
            model_item("fact", "Monthly price is $129.", "1", "The monthly price is $129", temporal="unknown"),
            refs,
        )
        self.assertIsNone(unknown)
        kept = [
            knowledge_analysis._supported_item(
                model_item("fact", "Monthly price is $129.", "1", "The monthly price is $129", temporal="stable"),
                refs,
            ),
            knowledge_analysis._supported_item(
                model_item("fact", "Yearly price is $1299.", "1", "the yearly price is $1299", temporal="stable"),
                refs,
            ),
        ]
        collapsed = knowledge_analysis._collapse_facts(kept)
        self.assertEqual(len(collapsed), 1)
        self.assertIn("Monthly price is $129", collapsed[0]["text"])
        self.assertIn("Yearly price is $1299", collapsed[0]["text"])

    def test_refreshing_during_a_batch_does_not_mark_the_job_failed(self):
        from gmail_auto.errors import ConfigError

        knowledge.add_document("Notes", "The office opens at nine.", "note", summarize=False)
        browser = create_app().test_client()
        browser.post("/knowledge/analyze")
        with patch(
            "gmail_auto.knowledge_analysis.workspace_operation",
            side_effect=ConfigError("This workspace operation is already in progress. Try again shortly."),
        ):
            response = browser.post("/knowledge/analyze/step")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "running")
        self.assertIn("previous batch", response.json["message"])

    def test_analysis_writes_the_profile_rules_and_auto_reply_keywords(self):
        from gmail_auto.profile_store import load_profile, save_profile
        from gmail_auto.settings import load_settings, save_settings

        save_profile({
            "name": "Mailbox owner",
            "role": "The person sending this reply",
            "introduction": "No personal or business details have been supplied.",
            "tone": "Clear, polite, and concise. Do not add a name or signature unless supplied.",
            "cannot_promise": ["Do not invent facts, commitments, contact details, or a signature."],
            "sample_phrases": ["Thank you for your message."],
        })
        knowledge.save_rules([
            "Always ask me before agreeing to a meeting or a price.",
            "Never reply to recruiters.",
            "Emails from school are important: always ask me first.",
        ])
        save_settings({"auto_reply_allow": ["quote"], "auto_reply_block": ["invoice"]})
        knowledge.merge_analysis([{
            "kind": "person", "text": "Robin E is Co-Founder at InnoPro.",
            "temporal": "stable", "source": "Signature",
        }])
        voice = json.dumps({
            "name": "Robin E",
            "role": "Co-Founder and Technology Director",
            "introduction": "Robin leads InnoPro and writes practical replies for customers.",
            "tone": "Warm, short, and direct.",
            "cannot_promise": ["Do not agree a price without asking Robin."],
            "sample_phrases": ["Thanks for your note."],
            "rules": ["Ask Robin before agreeing a meeting or a price."],
            "auto_reply_allow": ["inquiry"],
            "auto_reply_block": ["contract"],
        })
        with patch("gmail_auto.knowledge_analysis.complete", return_value=voice):
            knowledge_analysis._apply_learned_voice(None)
        profile = load_profile()
        self.assertEqual(profile["name"], "Robin E")
        self.assertEqual(profile["role"], "Co-Founder and Technology Director")
        self.assertIn("InnoPro", profile["introduction"])
        self.assertEqual(profile["tone"], "Warm, short, and direct.")
        self.assertIn("Do not agree a price without asking Robin.", profile["cannot_promise"])
        self.assertIn("Thanks for your note.", profile["sample_phrases"])
        self.assertEqual(knowledge.load_index()["rules"], ["Ask Robin before agreeing a meeting or a price."])
        self.assertEqual(load_settings()["auto_reply_allow"], ["quote", "inquiry"])
        self.assertEqual(load_settings()["auto_reply_block"], ["invoice", "contract"])

        save_profile({**profile, "name": "Robin Edited"})
        with patch("gmail_auto.knowledge_analysis.complete", return_value=voice):
            knowledge_analysis._apply_learned_voice({"name": "Robin E"})
        self.assertEqual(load_profile()["name"], "Robin Edited")

    def test_storyline_needs_more_than_one_reply(self):
        knowledge_analysis._flush_storylines({"threads": {
            "gmail:once": {
                "count": 2, "sent": 1, "subject": "Hello", "parties": ["a@example.com"], "sample": "Thanks",
            },
            "gmail:twice": {
                "count": 3, "sent": 2, "subject": "Project", "parties": ["c@example.com"], "sample": "Following up",
            },
        }})
        items = knowledge.list_items("storyline")
        self.assertEqual(len(items), 1)
        self.assertIn("Project", items[0]["text"])
        self.assertIn("2 written by you", items[0]["text"])
        self.assertNotIn("Latest:", items[0]["text"])
        self.assertEqual(items[0]["steps"][0]["summary"], "Following up")
        self.assertTrue(items[0]["steps"][0]["outbound"])

    def test_provider_without_schema_support_uses_prompt_constrained_json(self):
        error = ReplyError("The AI provider returned an error (400): response_format json_schema is not supported.")
        with patch("gmail_auto.knowledge_analysis.complete", side_effect=[error, '{"items":[]}']) as model:
            self.assertEqual(knowledge_analysis._request_items('{"1":{}}'), [])
        self.assertIsNotNone(model.call_args_list[0].kwargs["response_format"])
        self.assertIsNone(model.call_args_list[1].kwargs["response_format"])
