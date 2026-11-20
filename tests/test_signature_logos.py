import io
import os
import tempfile
import unittest

from gmail_auto import files
from gmail_auto.profile_store import save_profile
from gmail_auto.settings import load_settings, save_settings, signature_logo_bytes
from gmail_auto.web import create_app

_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
_JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


class SignatureLogoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("AUTOMAIL_HOME")
        os.environ["AUTOMAIL_HOME"] = self.tmp.name
        save_profile(
            {
                "name": "Robin",
                "role": "Director",
                "introduction": "InnoPro",
                "tone": "Clear",
                "cannot_promise": ["No unverified claims."],
                "sample_phrases": ["Thanks."],
            }
        )

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("AUTOMAIL_HOME", None)
        else:
            os.environ["AUTOMAIL_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_upload_preview_replace_and_remove_logo_per_mailbox(self):
        save_settings({"mail_signatures": {"outlook": "Robin E\nInnoPro"}})
        app = create_app()
        client = app.test_client()

        res = client.post(
            "/settings/signature/logo",
            data={"provider": "outlook", "logo": (io.BytesIO(_PNG_BYTES), "innopro.png")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("/settings/signature/logo/outlook", body)
        self.assertIn("innopro.png", body)

        settings = load_settings()
        self.assertEqual(settings["mail_signatures"]["outlook"], "Robin E\nInnoPro")
        self.assertNotIn("gmail", settings["signature_logos"])
        first_id = settings["signature_logos"]["outlook"]["file_id"]
        loaded = signature_logo_bytes(settings, provider="outlook")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0]["mime"], "image/png")
        self.assertEqual(loaded[1], _PNG_BYTES)

        preview = client.get("/settings/signature/logo/outlook")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "image/png")
        self.assertEqual(preview.data, _PNG_BYTES)
        self.assertEqual(client.get("/settings/signature/logo/gmail").status_code, 404)

        # Replace with JPEG and verify old file is cleaned up.
        res2 = client.post(
            "/settings/signature/logo",
            data={"provider": "outlook", "logo": (io.BytesIO(_JPG_BYTES), "innopro.jpg")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res2.status_code, 200)
        self.assertIsNone(files.get(first_id))
        second_id = load_settings()["signature_logos"]["outlook"]["file_id"]
        self.assertEqual(load_settings()["signature_logos"]["outlook"]["mime"], "image/jpeg")

        # Remove logo clears settings and deletes stored file.
        res3 = client.post(
            "/settings/signature/logo",
            data={"provider": "outlook", "action": "remove"},
            follow_redirects=True,
        )
        self.assertEqual(res3.status_code, 200)
        self.assertNotIn("outlook", load_settings()["signature_logos"])
        self.assertIsNone(files.get(second_id))

    def test_rejects_non_image_upload(self):
        app = create_app()
        client = app.test_client()
        res = client.post(
            "/settings/signature/logo",
            data={"provider": "gmail", "logo": (io.BytesIO(b"<svg>bad</svg>"), "bad.svg")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Upload a PNG, JPG, GIF, or WebP logo image.", res.get_data(as_text=True))
        self.assertEqual(load_settings()["signature_logos"], {})
