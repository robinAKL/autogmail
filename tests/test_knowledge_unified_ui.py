import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmail_auto import knowledge
from gmail_auto.web import create_app


class KnowledgeUnifiedUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "AUTOMAIL_HOME": str(self.root),
                "OPENAI_API_KEY": "sk-test-key",
            },
            clear=False,
        )
        self.env_patch.start()
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_unified_button_disabled_without_sources_or_key(self):
        html = self.client.get("/knowledge").get_data(as_text=True)
        self.assertIn('id="kb-analyze-form"', html)
        self.assertIn('action="/knowledge/analyze"', html)
        self.assertIn('id="kb-analyze-status"', html)
        self.assertIn('id="kb-analyze-btn" type="submit" disabled', html)
        self.assertIn("No connected mailboxes, documents, or rules yet.", html)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            no_key_html = self.client.get("/knowledge").get_data(as_text=True)
            self.assertIn('id="kb-analyze-btn" type="submit" disabled', no_key_html)
            self.assertIn("OPENAI_API_KEY is not set", no_key_html)

    def test_unified_button_enabled_with_document_and_standalone_learn_deprecated(self):
        knowledge.add_document("Pricing", "Standard workshop fee is $1200.", "note")
        html = self.client.get("/knowledge").get_data(as_text=True)
        self.assertIn('id="kb-analyze-btn" type="submit" >Analyse knowledge</button>', html)
        self.assertNotIn("/knowledge/learn", html)
        self.assertNotIn("Learn from past email", html)

        with mock.patch.object(knowledge, "learn_from_mailbox") as learn_mock:
            resp = self.client.post("/knowledge/learn", data={"provider": "gmail"}, follow_redirects=True)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Separate mailbox summarisation has been retired.", resp.get_data(as_text=True))
            learn_mock.assert_not_called()

            json_resp = self.client.post(
                "/knowledge/learn",
                data={"provider": "gmail"},
                headers={"Accept": "application/json"},
            )
            self.assertEqual(json_resp.status_code, 410)
            self.assertEqual(json_resp.get_json()["deprecated"], True)

    def test_analysis_results_render_sources_preferences_and_temporal_states(self):
        fake_index = knowledge.load_index()
        fake_index["model"]["items"] = [
            {
                "id": "fact-current",
                "kind": "fact",
                "text": "Office hours are 9am-5pm NZT.",
                "sources": ["Handbook.pdf", "Sent mail 2026-11-01"],
                "observed_at": "2026-11-01",
                "valid_from": "2026-01-01",
                "valid_until": "",
                "status": "active",
            },
            {
                "id": "fact-expired",
                "kind": "fact",
                "text": "2025 early-bird discount is 20% off.",
                "source": "Promo.docx",
                "observed_at": "2025-05-01",
                "valid_from": "2025-05-01",
                "valid_until": "2025-06-30",
                "status": "ended",
            },
            {
                "id": "fact-uncertain",
                "kind": "fact",
                "text": "Parking validation may be available on request.",
                "source": "Old note",
                "temporal_status": "uncertain",
                "status": "active",
            },
            {
                "id": "pref-1",
                "kind": "preference",
                "text": "Keep replies concise with a warm sign-off.",
                "recipient": "clients@example.com",
                "tone": "Warm & concise",
                "source": "Sent mail",
                "observed_at": "2026-11-10",
                "status": "active",
            },
        ]

        with mock.patch.object(knowledge, "load_index", return_value=fake_index):
            html = self.client.get("/knowledge").get_data(as_text=True)
        self.assertIn("Storylines", html)
        self.assertNotIn("Business knowledge", html)
        self.assertNotIn("Time-scoped facts", html)
        self.assertIn("Office hours are 9am-5pm NZT.", html)
        self.assertIn("Keep replies concise with a warm sign-off.", html)
        self.assertIn("Rules that always win", html)
        self.assertIn("Current knowledge only —", html)
        self.assertNotIn("â€", html)

    def test_rules_stay_in_the_editor_and_not_on_the_memory_map(self):
        fake_index = knowledge.load_index()
        fake_index["rules"] = ["Sign off external emails with Kind regards, Robin."]
        fake_index["model"]["items"] = [
            {
                "id": "rule-1",
                "kind": "rule",
                "text": "Sign off external emails with Kind regards, Robin.",
                "origin": "user",
                "status": "active",
            }
        ]
        with mock.patch.object(knowledge, "load_index", return_value=fake_index):
            html = self.client.get("/knowledge").get_data(as_text=True)
        self.assertIn("Sign off external emails with Kind regards, Robin.", html)
        self.assertNotIn("kind-rule", html)
        self.assertIn('name="rules"', html)

    def test_storylines_open_as_a_timeline_instead_of_a_paragraph(self):
        fake_index = knowledge.load_index()
        fake_index["model"]["items"] = [{
            "id": "story-1",
            "kind": "storyline",
            "text": (
                "With Yvette Lin <y.lin3@barfoot.co.nz> about Re: Fixed Term Lease Expiry - 5/9/2026. "
                "2 letters, 2 written by you. Latest: Dear Yvette, Thank you for the reminder about the tenancy."
            ),
            "source": "Re: Fixed Term Lease Expiry - 5/9/2026",
            "status": "active",
            "steps": [
                {"at": "2026-09-01 09:00", "who": "Yvette Lin", "outbound": False, "summary": "The fixed term ends on 5 September."},
                {"at": "2026-09-02 11:00", "who": "You", "outbound": True, "summary": "Thank you for the reminder about the tenancy."},
            ],
        }]
        with mock.patch.object(knowledge, "load_index", return_value=fake_index):
            closed = self.client.get("/knowledge").get_data(as_text=True)
            html = self.client.get("/knowledge?story=story-1").get_data(as_text=True)
        self.assertIn("Fixed Term Lease Expiry - 5/9/2026", closed)
        self.assertNotIn("story-line", closed)
        self.assertNotIn("more storylines", closed)
        self.assertIn("Waiting on them", html)
        self.assertIn("Yvette Lin", html)
        self.assertIn("Timeline", html)
        self.assertIn("<b>Status</b>", html)
        self.assertLess(html.index("2 Sep"), html.index("1 Sep"))
        self.assertNotIn("With Yvette Lin", html)
        self.assertNotIn("Latest:", html)

    def test_storylines_page_instead_of_growing_the_screen(self):
        fake_index = knowledge.load_index()
        fake_index["model"]["items"] = [
            {
                "id": f"s{number}",
                "kind": "storyline",
                "text": f"With Lee about Topic {number}. 2 letters, 2 written by you.",
                "status": "active",
                "steps": [{
                    "at": f"2026-11-{number + 1:02d} 09:00",
                    "who": "You",
                    "outbound": True,
                    "summary": f"Note {number}",
                }],
            }
            for number in range(9)
        ]
        with mock.patch.object(knowledge, "load_index", return_value=fake_index):
            first = self.client.get("/knowledge").get_data(as_text=True)
            second = self.client.get("/knowledge?story_page=2").get_data(as_text=True)
        self.assertIn('id="item-s8"', first)
        self.assertNotIn('id="item-s0"', first)
        self.assertIn("Older", first)
        self.assertIn("1 / 2", first)
        self.assertIn('id="item-s0"', second)
        self.assertNotIn('id="item-s8"', second)


if __name__ == "__main__":
    unittest.main()
