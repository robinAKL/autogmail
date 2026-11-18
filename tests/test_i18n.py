from unittest.mock import patch

from gmail_auto import i18n
from gmail_auto.settings import load_settings
from gmail_auto.store import mark_processed
from tests.test_core import sample
from tests.test_web import Inbox, WebCase


class TranslatorTests(WebCase):
    def test_only_interface_text_is_translated(self):
        page = (
            '<html><body><nav><a title="Change how far replies can go">Inbox</a></nav>'
            '<p class="banner">Added 3 files to the knowledge base.</p>'
            "<span class=\"tag\">Skipped: System or no-reply address</span>"
            '<span class="row-subject" translate="no">Invoice</span>'
            "<textarea placeholder=\"What to change\">Inbox</textarea>"
            "<script>var label = \"Inbox\"; if (a < b) {}</script>"
            "<button data-confirm=\"Send this reply now?\">Don&#39;t send</button>"
            "<p>Tom &amp; Jerry</p><p>Something nobody listed</p></body></html>"
        )
        out = i18n.translate_page(page, "zh")
        self.assertIn('title="修改回复可以自动发到什么程度">收件箱</a>', out)
        self.assertIn("已向知识库添加 3 个文件。", out)
        self.assertIn("已跳过：系统或不回复地址", out)
        self.assertIn('translate="no">Invoice</span>', out)
        self.assertIn('placeholder="要改什么">Inbox</textarea>', out)
        self.assertIn('var label = "Inbox"; if (a < b) {}', out)
        self.assertIn('data-confirm="现在发送这封回复吗？">不发送</button>', out)
        self.assertIn("<p>Tom &amp; Jerry</p><p>Something nobody listed</p>", out)
        self.assertEqual(i18n.translate_page(page, "en"), page)


class InterfaceLanguageTests(WebCase):
    def test_switching_to_chinese_and_back(self):
        mark_processed({"id": "m1", "from_name": "Lee", "from_email": "lee@abc.example", "subject": "Inbox", "action": "sent", "processed_at": "2026-11-14 09:00"})
        with patch("gmail_auto.web.connected_clients", return_value=([], [])):
            _, english = self.page("/?show=all")
            self.assertIn(">Inbox<", english)
            self.assertIn('value="zh" title="中文">中文</button>', english)
            self.client.post("/language", data={"language": "zh", "next": "/?show=all"})
            self.assertEqual(load_settings()["ui_language"], "zh")
            _, chinese = self.page("/?show=all")
        self.assertIn('<html lang="zh-CN">', chinese)
        self.assertIn("收件箱", chinese)
        self.assertIn("已自动回复", chinese)
        # A letter whose subject happens to be "Inbox" keeps its own words.
        self.assertIn('class="row-subject" translate="no">Inbox</span>', chinese)
        self.assertIn('title="English">EN</button>', chinese)

        box = Inbox([sample(id="b1", subject="Settings", body="<p>Inbox</p>", html="<p>Inbox Settings</p>")])
        with patch("gmail_auto.web.connected_clients", return_value=([box], [])), patch("gmail_auto.web.client_for", return_value=box):
            letter = self.client.get("/mail/body?key=b1").get_data(as_text=True)
        self.assertIn("Inbox Settings", letter)

        for path in ("/documents", "/contacts", "/approvals", "/write", "/profile", "/knowledge", "/dashboard", "/invoices", "/connections"):
            response = self.client.get(path, follow_redirects=True)
            self.assertEqual(response.status_code, 200, path)

        self.client.post("/language", data={"language": "en"})
        _, back = self.page("/documents")
        self.assertIn("Files to send", back)
