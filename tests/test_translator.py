import unittest
from unittest.mock import patch

from gmail_auto.models import Mail
from gmail_auto.translator import (
    detect_language,
    split_mail_body,
    extract_entities,
    verify_entities_preserved,
    translate_text,
    translate_inbound_mail,
    translate_outbound_reply,
)


class TestTranslator(unittest.TestCase):

    def test_detect_language(self):
        self.assertEqual(detect_language(""), "en")
        self.assertEqual(detect_language("Hello, how are you doing today?"), "en")
        self.assertEqual(detect_language("您好，请问这封邮件的价格是多少？"), "zh")
        self.assertEqual(detect_language("こんにちは、明日の会議について連絡します。"), "ja")
        self.assertEqual(detect_language("안녕하세요, 내일 회의 일정 확인 부탁드립니다."), "ko")
        self.assertEqual(detect_language("Здравствуйте, подтверждаем встречу завтра."), "ru")
        self.assertEqual(detect_language("مرحبا، أود أن أستفسر عن الخدمة"), "ar")
        self.assertEqual(detect_language("Bonjour, merci pour votre retour rapide."), "fr")
        self.assertEqual(detect_language("Guten Tag, bitte senden Sie mir das Angebot."), "de")
        self.assertEqual(detect_language("Hola, muchas gracias por su respuesta."), "es")

    def test_split_mail_body(self):
        text = "Thanks for your reply!\n\nOn Wed, Nov 20, 2026 at 10:00 AM Alice wrote:\n> Can we meet?"
        active, history = split_mail_body(text)
        self.assertEqual(active, "Thanks for your reply!")
        self.assertTrue(history.startswith("On Wed, Nov 20"))

        orig = "Please check this out.\n\n-----Original Message-----\nFrom: Bob\nSent: Yesterday"
        active2, history2 = split_mail_body(orig)
        self.assertEqual(active2, "Please check this out.")
        self.assertTrue(history2.startswith("-----Original Message-----"))

        pure = "Just a single message without history."
        active3, history3 = split_mail_body(pure)
        self.assertEqual(active3, "Just a single message without history.")
        self.assertEqual(history3, "")

    def test_extract_and_verify_entities(self):
        src = "Please send $500 to billing@company.com or visit https://pay.example.com"
        dst_valid = "请汇款 $500 到 billing@company.com 或访问 https://pay.example.com"
        ok, missing = verify_entities_preserved(src, dst_valid)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

        dst_dropped_currency = "请汇款 500 到 billing@company.com 或访问 https://pay.example.com"
        ok_currency, missing_currency = verify_entities_preserved(src, dst_dropped_currency)
        self.assertFalse(ok_currency)
        self.assertTrue(any("Currency missing" in item for item in missing_currency))

        dst_missing_amount = "请汇款到 billing@company.com 或访问 https://pay.example.com"
        ok2, missing2 = verify_entities_preserved(src, dst_missing_amount)
        self.assertFalse(ok2)
        self.assertTrue(any("500" in m for m in missing2))

        dst_missing_url = "请汇款 $500 到 billing@company.com"
        ok3, missing3 = verify_entities_preserved(src, dst_missing_url)
        self.assertFalse(ok3)
        self.assertTrue(any("URL missing" in m for m in missing3))

    @patch("gmail_auto.translator.model_ready", return_value=True)
    @patch("gmail_auto.translator.complete")
    def test_translate_text(self, mock_complete, mock_ready):
        mock_complete.return_value = "你好世界"
        res = translate_text("Hello world", "en", "zh")
        self.assertEqual(res, "你好世界")
        mock_complete.assert_called_once()

    def test_translate_text_same_language(self):
        res = translate_text("你好", "zh", "zh")
        self.assertEqual(res, "你好")

    @patch("gmail_auto.translator.model_ready", return_value=True)
    @patch("gmail_auto.translator.complete")
    def test_translate_inbound_mail(self, mock_complete, mock_ready):
        mock_complete.side_effect = lambda sys, user, **kw: "翻译后：" + user.split("[Text to Translate]:\n")[-1]

        mail = Mail(
            id="101",
            thread_id="t101",
            from_name="John Doe",
            from_email="john@example.com",
            reply_email="john@example.com",
            subject="Invoice inquiry",
            date="Today",
            body="Could you check invoice #1234?\n\nOn yesterday Bob wrote:\n> old mail",
            snippet="Could you check...",
        )

        res_mail = translate_inbound_mail(mail, user_lang="zh")
        self.assertEqual(res_mail.detected_lang, "en")
        self.assertTrue(res_mail.translated_subject.startswith("翻译后：Invoice inquiry"))
        self.assertIn("翻译后：Could you check invoice #1234?", res_mail.translated_body)
        self.assertIn("--- 原始邮件引用 ---", res_mail.translated_body)
        self.assertIn("On yesterday Bob wrote:", res_mail.translated_body)

    def test_translate_inbound_mail_already_l1(self):
        mail = Mail(
            id="102",
            thread_id="t102",
            from_name="张三",
            from_email="zhang@example.com",
            reply_email="zhang@example.com",
            subject="发票咨询",
            date="Today",
            body="请问发票开具了吗？",
            snippet="请问发票...",
        )
        res_mail = translate_inbound_mail(mail, user_lang="zh")
        self.assertEqual(res_mail.detected_lang, "zh")
        self.assertEqual(res_mail.translated_subject, "发票咨询")
        self.assertEqual(res_mail.translated_body, "请问发票开具了吗？")


if __name__ == "__main__":
    unittest.main()
